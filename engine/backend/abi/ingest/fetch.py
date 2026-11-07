"""API sources (IN-1, IN-2, IN-5; D-2, D-35, SC-4; API-SRC.1): a source whose https location is a REST/JSON or CSV
API, fetched page by page inside the load through API tools' one HTTP path (`abi.tools.api.fetch`: address policy,
pinned address, no redirects, no ambient env) and written to files `ingest._load` reads in one snapshot."""

import base64
import csv
import email.utils
import io
import json
import re
import ssl
import time
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx2
from pydantic import AfterValidator, Field, model_validator

from abi.actions import ActionFailed
from abi.security import credentials
from abi.storage import TABLE
from abi.tools import api
from abi.transform import _type

from . import oauth
from .oauth import _Strict

SECRET_ONLY = rf"^{credentials.REF}$"
POINTER = r"^(/[^/]*)*$"  # RFC 6901; "" is the whole document
PAGE_MAX, TIMEOUT = 32 << 20, 60  # one page's bytes and seconds; D-17: pages go to disk, never all in memory
RETRIES, WAIT_MAX = 5, 300  # 429/5xx retries, and the longest total wait for them (seconds)
_sleep = time.sleep  # tests replace it


class Bearer(_Strict):
    kind: Literal["bearer"]
    token: str = Field(pattern=SECRET_ONLY)


class HeaderAuth(_Strict):
    kind: Literal["header"]
    name: str = Field(pattern=r"^[A-Za-z0-9-]{1,100}$")
    value: str = Field(pattern=SECRET_ONLY)


class Basic(_Strict):
    kind: Literal["basic"]
    username: str = Field(pattern=SECRET_ONLY)
    password: str = Field(pattern=SECRET_ONLY)


class Pagination(_Strict):
    kind: Literal["page", "offset", "cursor", "next_link"]
    param: str | None = Field(None, pattern=r"^[A-Za-z0-9_.\[\]-]{1,100}$", description="query parameter (page, offset, cursor)")
    start: int | None = Field(None, ge=0, description="first page number (default 1) or offset (default 0)")
    pointer: str | None = Field(None, pattern=POINTER, description="cursor: the next cursor in the response; next_link:"
                                                                   " the next URL in the response (default: the Link header)")
    max_pages: int = Field(100, ge=1, le=100_000)

    @model_validator(mode="after")
    def _needs(self):
        if self.kind != "next_link" and not self.param:
            raise ValueError(f"{self.kind} pagination needs `param`")
        if self.kind == "cursor" and self.pointer is None:
            raise ValueError("cursor pagination needs `pointer`")
        return self


class Column(_Strict):
    pointer: str | None = Field(None, pattern=POINTER, description="JSON pointer in a record (default /<name>)")
    type: Annotated[str, AfterValidator(_type)]


def _whole_secret(v: str) -> str:
    if "${" in v and not re.fullmatch(credentials.REF, v):
        raise ValueError("a query value is either a whole ${secret:NAME} or has no '${' at all")
    return v


class Incremental(_Strict):
    field: str = Field(pattern=TABLE, description="a loaded column (numeric, date/time or text)")
    param: str = Field(pattern=r"^[A-Za-z0-9_.\[\]-]{1,100}$", description="query parameter that gets its largest stored value")


class Api(_Strict):
    format: Literal["json", "csv"] = "json"
    records: str = Field("", pattern=POINTER, description="JSON pointer to the array of records in a response")
    # a secret only as a whole value, so no free text around it is ever a secret's neighbour in a URL
    query: dict[Annotated[str, Field(pattern=r"^[A-Za-z0-9_.\[\]$-]{1,100}$")],
                Annotated[str, AfterValidator(_whole_secret)] | int] = {}
    auth: Bearer | HeaderAuth | Basic | oauth.OAuthClient | oauth.OAuthCode | None = Field(None, discriminator="kind")
    pagination: Pagination | None = None
    columns: dict[Annotated[str, Field(pattern=TABLE)], Column] = {}
    rate: int | None = Field(None, ge=1, le=6000, description="requests per minute at most")
    incremental: Incremental | None = None
    every_hours: int | None = Field(None, ge=1, le=24, description="load every this many hours (default: daily)")

    @model_validator(mode="after")
    def _csv(self):
        if self.format == "csv" and (self.records or any(c.pointer for c in self.columns.values())
                                     or self.pagination and (self.pagination.kind == "cursor" or self.pagination.pointer)):
            raise ValueError("a CSV API has no JSON records, column pointers or cursors (next_link: the Link header only)")
        return self


def request(conn, src, used: dict) -> tuple[dict, dict, dict]:
    """(headers, query, hide) with the secrets resolved into `used`; `hide` is what errors and data are scrubbed of."""
    def r(text):
        return credentials.resolve(conn, text, used, src.project)

    headers, a, extra = {k: r(v) for k, v in src.headers.items()}, src.api.auth, {}
    if a and a.kind == "bearer":
        headers["Authorization"] = f"Bearer {r(a.token)}"
    elif a and a.kind == "header":
        headers[a.name] = r(a.value)
    elif a and a.kind == "basic":
        headers["Authorization"] = "Basic " + (b := base64.b64encode(f"{r(a.username)}:{r(a.password)}".encode()).decode())
        extra = {"basic": (b, None)}
    query = {k: r(v) if isinstance(v, str) else v for k, v in src.api.query.items()}
    return headers, query, api.hidden(used) | extra


def _wait(headers) -> float | None:
    """Retry-After (RFC 9110 §10.2.3) in seconds: delay-seconds or an HTTP-date."""
    v = (headers or {}).get("retry-after", "").strip()
    if v.isdigit():
        return int(v)
    try:
        return max(0.0, (email.utils.parsedate_to_datetime(v) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def _permanent(e: BaseException | None) -> bool:
    """A page over the cap or a TLS/certificate failure: retrying can't succeed."""
    while e is not None:
        if isinstance(e, (api.TooBig, ssl.SSLError)):
            return True
        e = e.__cause__ or e.__context__
    return False


def _get(name: str, url: str, addr, headers: dict, query: dict, hide: dict, token=None) -> tuple[httpx2.Headers, bytes]:
    """One page, retried on 429/5xx and failed connections (Retry-After honoured, `WAIT_MAX` in all). `token(fresh)`:
    an OAuth access token (API-SRC.3), fetched again once when the API answers 401."""
    waited, renew, renewed = 0.0, False, False
    for n in range(RETRIES + 1):
        hdrs = None
        if token:
            headers = headers | {"Authorization": f"Bearer {(t := token(renew))}"}
            renew = False
            hide.update(api.hidden({"access token": (t, None)}))  # SC-4: scrubbed from errors and data
        try:
            status, hdrs, body = api.fetch("GET", url, addr, headers, query, limit=PAGE_MAX, timeout=TIMEOUT)
        except ValueError as e:
            why = credentials.scrub(str(e), hide)
            if _permanent(e):
                raise ActionFailed(502, f"API source '{name}': the API call failed: {why}.") from None  # retrying can't help
        else:
            if 200 <= status < 300:
                return hdrs, body
            if status == 401 and token and not renewed:
                renew = renewed = True
                continue
            if 300 <= status < 400:
                raise ActionFailed(502, f"API source '{name}': the API answered a redirect (HTTP {status}); redirects"
                                        " are never followed.")
            if status != 429 and status < 500:  # the body isn't shown: it may echo the request
                raise ActionFailed(502, f"API source '{name}': the API answered HTTP {status}.")
            why = f"HTTP {status}"
        wait = _wait(hdrs)
        wait = 2 ** n if wait is None else wait
        if n == RETRIES or waited + wait > WAIT_MAX:
            raise ActionFailed(502, f"API source '{name}': the API failed {n + 1} time(s), last: {why}.")
        _sleep(wait)
        waited += wait


def _rows(body: bytes, a: Api, hide: dict, name: str) -> list[dict]:
    try:
        doc = api.pointer(json.loads(body), a.records)
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise ActionFailed(502, f"API source '{name}': a response isn't JSON with a value at `records` ({a.records!r}).") from None
    if not isinstance(doc, list) or not all(isinstance(r, dict) for r in doc):
        raise ActionFailed(502, f"API source '{name}': `records` ({a.records!r}) of a response isn't an array of objects.")
    out = []
    for r in api._scrub(doc, hide):  # SC-4: an API echoing a secret doesn't put it in the lake
        for c, col in a.columns.items():
            try:
                r[c] = api.pointer(r, col.pointer or f"/{c.replace('~', '~0').replace('/', '~1')}")
            except (KeyError, IndexError, TypeError, ValueError):
                r[c] = None
        out.append(r)
    return out


def pages(src, loc: str, headers: dict, query: dict, hide: dict, folder: str) -> tuple[list[str], bool]:
    """Fetch every page of `src`'s API at `loc` into files in `folder` (NDJSON, or one CSV per page). Returns the files
    and whether the load stopped at `max_pages` with pages left (API-SRC.2: the table is then partial)."""
    a, p, name = src.api, src.api.pagination, src.table
    url = httpx2.URL(loc)
    host, port = url.host, url.port or 443
    token = oauth.bearer(src.project, src.sid, name, a.auth, host) if a.auth and a.auth.kind.startswith("oauth") else None
    try:  # resolve once; every page goes to this address
        addr = api.checked(host, port)[0]
    except OSError:
        raise ActionFailed(502, f"API source '{name}': host '{host}' doesn't resolve.") from None
    except ValueError as e:
        raise ActionFailed(403, f"API source '{name}': host '{host}' resolves to {e}. API sources never call it.") from None
    value = (p.start if p.start is not None else int(p.kind == "page")) if p and p.kind in ("page", "offset") else None
    files, out, last, truncated = [], None, 0.0, False
    if a.format == "json":
        files.append(f"{folder}/records.ndjson")
        out = open(files[0], "w")  # noqa: SIM115 - closed in finally
    try:
        for n in range(p.max_pages if p else 1):
            q = query | ({p.param: value} if p and p.kind != "next_link" and value is not None else {})
            if a.rate:
                _sleep(max(0.0, last + 60 / a.rate - time.monotonic()))
                last = time.monotonic()
            hdrs, body = _get(name, str(url), addr, headers, q, hide, token)
            if a.format == "json":
                rows = _rows(body, a, hide, name)
                out.writelines(json.dumps(r) + "\n" for r in rows)
                count = len(rows)
            else:
                text = credentials.scrub(body.decode("utf-8", "replace"), hide)
                count = sum(1 for line in csv.reader(io.StringIO(text)) if line) - (src.csv is None or src.csv.header is not False)
                if count > 0 or not files:
                    files.append(f"{folder}/p{n:06d}.csv")
                    with open(files[-1], "w") as f:
                        f.write(text)
            if not p or count <= 0 and p.kind in ("page", "offset"):
                break
            if p.kind in ("page", "offset"):
                value += 1 if p.kind == "page" else count
                continue
            try:
                nxt = api.pointer(json.loads(body), p.pointer) if p.pointer is not None else httpx2.Response(200, headers=hdrs).links.get("next", {}).get("url")
            except (ValueError, KeyError, IndexError, TypeError):
                nxt = None
            if nxt in (None, ""):
                break
            if p.kind == "cursor":
                value = nxt if isinstance(nxt, (int, str)) else json.dumps(nxt)
                continue
            link = url.join(str(nxt))  # D-2, D-35: a next link stays on the location's host, port and scheme
            if (link.scheme, link.host, link.port or 443) != ("https", host, port):
                raise ActionFailed(403, f"API source '{name}': a next link leaves host '{host}' (https only); it isn't followed.")
            url = link
        else:  # every page fetched and another one named: stopped at max_pages
            truncated = p is not None
    finally:
        if out:
            out.close()
    return files, truncated
