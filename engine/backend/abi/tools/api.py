"""External API tools (PRD §7.1 TL-5; DECISIONS D-35, D-2, D-27; SC-4).

`call` sends a tool's filled `request` and returns the rows at its `rows` JSON pointer. Guard: https only (schema), the
host resolved once and every address checked (loopback, link-local incl. 169.254.169.254, unspecified, multicast and
reserved always refused; other non-public addresses only for hosts in `ABI_API_PRIVATE_HOSTS`), the connection pinned
to the checked address (TLS still verifies the host name, so no DNS rebinding), redirects never followed, no ambient
proxy/netrc/env settings, 10 s and 1 MB caps. Secrets (`${secret:NAME}` header/query values) go only to their allowed
hosts and every resolved value is scrubbed from the result and from error texts.
"""

import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import quote, quote_plus, urlsplit

import httpx2

from abi import db
from abi.actions import ActionFailed
from abi.security import credentials

LIMIT, TIMEOUT = 1 << 20, 10
logging.getLogger("httpx2").setLevel(logging.WARNING)  # its INFO line logs the full URL, secret query values included


def refused(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> str | None:
    """Why an API tool may not connect to `ip` (resolved from `host`), or None."""
    ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:127.0.0.1 is 127.0.0.1
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved \
            or getattr(ip, "is_site_local", False):  # fec0::/10: deprecated, but `is_global` calls it global
        return "a loopback, link-local, multicast or reserved address"
    allowed = {h.strip().lower() for h in os.environ.get("ABI_API_PRIVATE_HOSTS", "").split(",")}
    if not ip.is_global and not (ip.is_private and host in allowed):
        return "a private address; the operator can allow this host in ABI_API_PRIVATE_HOSTS"
    return None


class TooBig(ValueError):
    """A response over the byte cap (API-SRC.2: never retried)."""


def checked(host: str, port: int) -> list:
    """`host`'s addresses, resolved once; OSError when it doesn't resolve, ValueError(why) when any address is `refused`.
    Also the Git remote policy (D-125)."""
    try:
        addrs = [ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    except UnicodeError as e:
        raise OSError(str(e)) from None
    if why := next(filter(None, (refused(ip, host) for ip in addrs)), None):  # any bad address: refuse the host
        raise ValueError(why)
    return addrs


def _scrub(x, used: dict):
    """`x` (parsed JSON) with every resolved secret value replaced, in keys, strings and numbers alike."""
    if isinstance(x, dict):
        return {_scrub(k, used): _scrub(v, used) for k, v in x.items()}
    if isinstance(x, list):
        return [_scrub(v, used) for v in x]
    if isinstance(x, (str, int, float)) and not isinstance(x, bool) and (s := credentials.scrub(str(x), used)) != str(x):
        return s
    return x


def _send(method: str, url: httpx2.URL, host: str, headers, params: dict, body, limit: int, timeout: float,
          box: dict) -> None:
    deadline = time.monotonic() + timeout
    try:
        with httpx2.Client(trust_env=False, verify=builtin._tls(), timeout=timeout, follow_redirects=False) as c, \
                c.stream(method, url, params=params, headers=headers, extensions={"sni_hostname": host},
                         **({"content": body} if isinstance(body, bytes) else {"json": body})) as r:  # bytes: a form (API-SRC.3)
            box["status"], box["headers"], data = r.status_code, r.headers, b""
            if r.is_success:  # raw bytes: never decompressed, so the cap holds
                for chunk in r.iter_raw():
                    data += chunk
                    if len(data) > limit:
                        raise TooBig(f"its response is over {limit >> 20} MB")
                    if time.monotonic() > deadline:
                        raise ValueError(f"it took over {timeout} s")
            box["body"] = data
    except httpx2.TimeoutException:
        box["error"] = f"it took over {timeout} s"
    except Exception as e:  # noqa: BLE001 - handed to the caller's thread
        box["error"] = e


def fetch(method: str, url: str, addr, headers: dict, params: dict, body=None, limit: int | None = None,
          timeout: float | None = None) -> tuple[int, httpx2.Headers, bytes]:
    """D-35: one request to `url`, connected to `addr` (from `checked`: never a second lookup, so no DNS rebinding) with
    TLS checked against the URL's host, no redirects, no ambient env/proxy, `limit` bytes and `timeout` seconds in all.
    Returns (status, headers, body; the body only on 2xx). Raises ValueError(why); `why` may quote the URL, so scrub it.
    The one HTTP path of API tools and API sources (API-SRC.1)."""
    limit, timeout = limit or LIMIT, timeout or TIMEOUT  # read at call time: tests lower them
    u = httpx2.URL(url)
    headers = httpx2.Headers(headers)  # case-insensitive: a caller's `accept-encoding` can't override ours
    headers.update({"Host": u.netloc.decode(), "Accept-Encoding": "identity"})
    # httpx2 replaces a URL's query with `params`: keep the URL's own (a next link's), `params` winning per key
    params = [(k, v) for k, v in u.params.multi_items() if k not in params] + list(params.items())
    box: dict = {}
    # ponytail: a thread for the total cap; a stalled read keeps it alive up to one more read timeout
    th = threading.Thread(target=_send, args=(method, u.copy_with(host=str(addr)), u.host, headers, params, body, limit,
                                              timeout, box), daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise ValueError(f"it took over {timeout} s")
    if (err := box.get("error")) is not None:
        raise ValueError(str(err)) from err if isinstance(err, BaseException) else None
    return box["status"], box.get("headers"), box.get("body", b"")


def hidden(used: dict) -> dict:
    """`used` plus every percent-encoded form of its values, for `credentials.scrub` (SC-4: an echoed URL carries a
    query secret percent-encoded, `/` kept or not, `+` for space, hex in either case)."""
    return used | {f"{k} {e}": (e, h) for k, (v, h) in used.items()
                   for q in (quote(v, safe=""), quote(v), quote_plus(v))
                   for e in (q, re.sub("%[0-9A-F]{2}", lambda m: m[0].lower(), q))}


def pointer(doc, path: str):
    """The value at JSON pointer `path` (RFC 6901) in `doc`; KeyError, IndexError, TypeError or ValueError when absent."""
    for p in path.split("/")[1:]:
        p = p.replace("~1", "/").replace("~0", "~")
        doc = doc[int(p)] if isinstance(doc, list) else doc[p]
    return doc


def call(project: str, tool: str, raw: dict, req: dict, rows: str) -> list[dict]:
    """Send `project`'s tool `tool`'s request (`raw` as saved, `req` with its inputs filled in) and return the rows at
    pointer `rows` in its JSON response. Secrets come from `raw` only, so an input value `${secret:…}` stays literal text."""
    parts = urlsplit(req["url"])  # schema: https://<literal host>[:port]/<path>, no user info, query or fragment
    try:
        host, port = parts.hostname, parts.port or 443
    except ValueError:
        raise ActionFailed(400, f"Tool '{tool}': the URL's port is invalid.") from None
    used: dict = {}
    with db.connect() as conn:
        def fill(part: str) -> dict:  # only a whole saved value `${secret:NAME}` is a secret (schema)
            return {k: credentials.resolve(conn, raw[part][k], used, project)
                    if isinstance(raw[part][k], str) and re.fullmatch(credentials.REF, raw[part][k]) else v
                    for k, v in req.get(part, {}).items()}

        headers, params = fill("headers"), fill("query")
        credentials.allow(conn, used, host, False)  # D-2 on the host TLS verifies; a 403 is audited by actions.run
    hide = hidden(used)
    try:  # resolve once: the request goes to this address, never to a second lookup (DNS rebinding)
        addrs = checked(host, port)
    except OSError:
        raise ActionFailed(502, f"Tool '{tool}': host '{host}' doesn't resolve.") from None
    except ValueError as e:
        raise ActionFailed(403, f"Tool '{tool}': host '{host}' resolves to {e}. API tools never call it.") from None
    try:
        status, _, body = fetch(req["method"], req["url"], addrs[0], headers, params, req.get("body"))
    except ValueError as e:
        raise ActionFailed(502, credentials.scrub(f"Tool '{tool}': API host '{host}' failed: {e}", hide)) from None
    if 300 <= status < 400:
        raise ActionFailed(502, f"Tool '{tool}': API host '{host}' answered a redirect (HTTP {status}); redirects are never followed.")
    if not 200 <= status < 300:  # the body isn't shown: it may echo the request
        raise ActionFailed(502, f"Tool '{tool}': API host '{host}' answered HTTP {status}.")
    try:
        doc = _scrub(pointer(json.loads(body), rows), hide)
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise ActionFailed(502, f"Tool '{tool}': the API's response isn't JSON with a value at `rows` ({rows!r}).") from None
    if not isinstance(doc, list) or not all(isinstance(r, dict) for r in doc):
        raise ActionFailed(502, f"Tool '{tool}': `rows` ({rows!r}) of the API's response isn't an array of objects.")
    return doc


# last: builtin imports abi.tools; its `_tls` is the transports' TLS context
from abi.tools import builtin
