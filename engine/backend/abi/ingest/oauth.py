"""API source OAuth 2.0 (API-SRC.3; RFC 6749 §4.1, §4.4, §6; RFC 7636 PKCE S256; SC-4, D-2, D-35).

`oauth_client` gets access tokens with the client credentials grant; `oauth_code` is connected once by an Editor in the
browser (start -> provider consent -> callback, as the ChatGPT link and OIDC do) and keeps the refresh token
Fernet-encrypted in `api_tokens`, bound to the token endpoint's host. Access tokens live in process memory only. Token
endpoints go through API tools' address policy (`api.checked` + `api.fetch`). No token reaches audit_log, logs, Git,
an API response or an agent tool: the connect flow is two REST routes, never an action.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Annotated, Literal
from urllib.parse import quote_plus, urlencode

import httpx2
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from abi import db, security, storage
from abi.actions import ActionFailed, audit, current_user
from abi.project import git
from abi.security import Role, User, credentials, oidc, role_in
from abi.storage import TABLE
from abi.tools import api

SECRET = rf"^{credentials.REF}$"
URL = r"^https://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s#]*)?$"  # a literal host: no user info, no fragment
SCOPE = r"^[\x21\x23-\x5B\x5D-\x7E]+( [\x21\x23-\x5B\x5D-\x7E]+)*$"  # RFC 6749 §3.3
PATH, COOKIE, EARLY = "/api/sources/oauth", "abi_api_oauth", 60  # EARLY: seconds before expiry a token counts as gone
CONNECT = ("API source '{}': its OAuth connection is missing, or the provider refused it (revoked or expired); an Editor"
           " must connect the source again.")
# (project, source, auth definition, API host, connection id, sha256 of tenant + resolved client id/secret)
#   -> (access token, expiry on the monotonic clock); D-128: the key changes with what it protects
_TOKENS: dict[tuple, tuple[str, float]] = {}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OAuthClient(_Strict):
    kind: Literal["oauth_client"]
    token_url: str = Field(pattern=URL, max_length=500)
    client_id: str = Field(pattern=SECRET)
    client_secret: str = Field(pattern=SECRET)
    scope: str | None = Field(None, pattern=SCOPE, max_length=1000)


class OAuthCode(_Strict):
    kind: Literal["oauth_code"]
    authorize_url: str = Field(pattern=URL, max_length=500)
    token_url: str = Field(pattern=URL, max_length=500)
    client_id: str = Field(pattern=r"^[A-Za-z0-9._~:@+/=-]{1,200}$", description="public: it is in the browser's URL")
    client_secret: str | None = Field(None, pattern=SECRET, description="omitted: a public client (PKCE only)")
    scope: str | None = Field(None, pattern=SCOPE, max_length=1000)


def _creds(conn, project: str, a: OAuthClient | OAuthCode, api_host: str, used: dict) -> tuple[str, str | None]:
    """The client id and secret resolved for `project`'s tenant (SC-9), after the D-2 check against the token host and
    `api_host`, where the access token they buy goes."""
    cid = credentials.resolve(conn, a.client_id, used, project)
    sec = a.client_secret and credentials.resolve(conn, a.client_secret, used, project)
    credentials.allow(conn, used, httpx2.URL(a.token_url).host, False)
    credentials.allow(conn, used, api_host, False)
    return cid, sec


def _token(project: str, a: OAuthClient | OAuthCode, form: dict, hide: dict, refused: str, api_host: str) -> dict:
    """POST `form` to `a`'s token endpoint and return the token response. The client secrets go by HTTP Basic (§2.3.1)
    only after the D-2 check against the token host AND `api_host`, where the access token they buy goes; `hide`
    collects what errors and data are scrubbed of. A 400/401
    (§5.2: invalid_grant, invalid_client) raises `refused`; the provider's own text is never shown."""
    u, used = httpx2.URL(a.token_url), {}
    with db.connect() as conn:
        cid, sec = _creds(conn, project, a, api_host, used)
    hide.update(api.hidden(used))
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if sec:
        basic = base64.b64encode(f"{quote_plus(cid)}:{quote_plus(sec)}".encode()).decode()
        headers["Authorization"], hide["basic"] = f"Basic {basic}", (basic, None)
    else:
        form = form | {"client_id": cid}
    try:
        addr = api.checked(u.host, u.port or 443)[0]
    except OSError:
        raise ActionFailed(502, f"OAuth token endpoint host '{u.host}' doesn't resolve.") from None
    except ValueError as e:
        raise ActionFailed(403, f"OAuth token endpoint host '{u.host}' resolves to {e}. It is never called.") from None
    try:
        status, _, body = api.fetch("POST", a.token_url, addr, headers, {}, urlencode(form).encode())
    except ValueError as e:
        raise ActionFailed(502, credentials.scrub(f"OAuth token endpoint '{u.host}' failed: {e}", hide)) from None
    if status in (400, 401):
        raise ActionFailed(409, refused)
    if not 200 <= status < 300:
        raise ActionFailed(502, f"OAuth token endpoint '{u.host}' answered HTTP {status}.")
    try:
        t = json.loads(body)
    except ValueError:
        t = None
    if not isinstance(t, dict) or not isinstance(t.get("access_token"), str) or not t["access_token"]:
        raise ActionFailed(502, f"OAuth token endpoint '{u.host}' didn't answer with an access token.")
    hide["access token"] = (t["access_token"], None)
    return t


def _grant(project: str, sid: str, name: str, a: OAuthClient | OAuthCode, api_host: str) -> tuple[str, float]:
    hide: dict = {}
    if a.kind == "oauth_client":  # §4.4
        t = _token(project, a, {"grant_type": "client_credentials"} | ({"scope": a.scope} if a.scope else {}), hide,
                   f"API source '{name}': the OAuth token endpoint refused the client credentials.", api_host)
    else:  # §6, under the row lock: a rotated refresh token is used once, so refreshes take turns
        with db.connect() as conn:
            row = conn.execute("SELECT host, api_host, token, used OR connected > now() - interval"
                               f" '{oidc.FLOW_TTL}' AS live FROM api_tokens WHERE project = %s AND source = %s FOR UPDATE",
                               [project, sid]).fetchone()
            # D-2: the refresh token only goes back to the host that issued it, its access tokens only to the API host
            # it was connected for (another Editor can't point the source elsewhere and receive them). A connection made
            # before its source was saved is taken by the add_data that follows, while a connect flow could, or never.
            if not row or not row["live"] or (row["host"], row["api_host"]) != (httpx2.URL(a.token_url).host, api_host):
                raise ActionFailed(409, CONNECT.format(name))
            old = credentials.decrypt(row["token"]).decode()
            hide["refresh token"] = (old, None)
            t = _token(project, a, {"grant_type": "refresh_token", "refresh_token": old}, hide, CONNECT.format(name), api_host)
            new = t.get("refresh_token")  # §6: a rotated one replaces the old one
            new = credentials._fernet().encrypt(new.encode()) if isinstance(new, str) and new and new != old else None
            conn.execute("UPDATE api_tokens SET used = true, token = coalesce(%s, token) WHERE project = %s AND source = %s",
                         [new, project, sid])
    try:
        ttl = int(t.get("expires_in") or 3600)
    except (TypeError, ValueError):
        ttl = 3600
    return t["access_token"], time.monotonic() + ttl - EARLY


def bearer(project: str, sid: str, name: str, a: OAuthClient | OAuthCode, api_host: str):
    """`get(fresh)` -> an access token of source `sid` for `api_host`, from memory until it expires; `fresh` fetches a
    new one. A code connection's id is in the key, so a reconnect or a deleted connection never reuses a cached token.
    D-128: the secrets are resolved and D-2 checked on every load, and the key carries a digest of them plus the
    project's tenant, so a deleted or narrowed secret, a tenant move or a re-created project never hits the cache."""
    conn_id = None
    with db.connect() as conn:
        cid, sec = _creds(conn, project, a, api_host, {})
        tenant = conn.execute("SELECT tenant FROM project_tenants WHERE project = %s", [project]).fetchone()
        if a.kind == "oauth_code":
            row = conn.execute("SELECT id FROM api_tokens WHERE project = %s AND source = %s", [project, sid]).fetchone()
            conn_id = row and row["id"]
    digest = hashlib.sha256(json.dumps([tenant and tenant["tenant"], cid, sec]).encode()).hexdigest()
    key = (project, sid, a.model_dump_json(), api_host, conn_id, digest)

    def get(fresh: bool) -> str:
        t = _TOKENS.get(key)
        if fresh or not t or t[1] <= time.monotonic():
            t = _TOKENS[key] = _grant(project, sid, name, a, api_host)
        return t[0]

    return get


router = APIRouter(prefix=PATH)
Me = Annotated[User, Depends(current_user)]


def _public() -> str:
    url = os.environ.get("ABI_PUBLIC_URL", "").rstrip("/")
    if not url:
        raise HTTPException(404, "OAuth connections are off: set ABI_PUBLIC_URL.")
    return url


class Connect(storage.ProjectRef):
    source: str = Field(pattern=TABLE)
    location: str = Field(pattern=URL, max_length=2000, description="the source's https location: its access tokens go there only")
    auth: OAuthCode


@router.post("/start")
def start(args: Connect, user: Me, response: Response) -> dict:
    """Starts connecting an API source's `oauth_code` auth (Editor); returns the provider's URL to open. The source may
    not exist yet: add_data then finds the connection (it loads at once)."""
    public, why, a, api_host = _public(), None, args.auth, httpx2.URL(args.location).host
    state, verifier, browser = (secrets.token_urlsafe(32) for _ in range(3))
    with db.connect() as conn:
        if (role_in(conn, user, args.project) or 0) < Role.editor:
            why = ActionFailed(403, f"Connecting a source needs the editor role in project '{args.project}'.")
        else:
            try:  # D-2: refuse now rather than at the callback
                _creds(conn, args.project, a, api_host, {})
            except ActionFailed as e:
                why = e
        if why:  # SC-6; raised after the block, which would roll the audit row back
            audit(conn, user, "rest", "api_oauth_start", args.project, {"source": args.source}, why.status, str(why))
        else:
            conn.execute(f"DELETE FROM api_oauth_flows WHERE created < now() - interval '{oidc.FLOW_TTL}'")
            conn.execute("INSERT INTO api_oauth_flows (state_hash, browser_hash, user_id, project, source, api_host, auth,"
                         " verifier) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                         [security._token_hash(state), security._token_hash(browser), user.id, args.project, args.source,
                          api_host, a.model_dump_json(), verifier])
    if why:
        raise HTTPException(why.status, str(why))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": a.client_id, "redirect_uri": public + PATH + "/callback",
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256"} | ({"scope": a.scope} if a.scope else {})
    response.set_cookie(COOKIE, browser, max_age=600, path=PATH, httponly=True, samesite="lax", secure=public.startswith("https://"))
    return {"url": oidc._url(a.authorize_url, params)}


def _callback(conn, public: str, p, browser: str | None) -> tuple[str, dict | None]:
    """(outcome, flow). The flow is spent first, so a state works once (replay -> expired)."""
    state = p.get("state")
    flow = state and conn.execute(
        "UPDATE api_oauth_flows SET used = true WHERE state_hash = %s AND NOT used"
        f" AND created > now() - interval '{oidc.FLOW_TTL}' RETURNING *", [security._token_hash(state)]).fetchone()
    if not flow:
        return "expired", None
    if not browser or not hmac.compare_digest(security._token_hash(browser), bytes(flow["browser_hash"])):
        return "browser", flow
    if p.get("error") or not p.get("code"):
        return "denied", flow
    if (role_in(conn, security.user(conn, flow["user_id"]), flow["project"]) or 0) < Role.editor:
        return "role", flow  # lost the role while at the provider
    a = OAuthCode.model_validate(flow["auth"])
    try:  # §4.1.3: the same redirect URI, and the PKCE verifier
        t = _token(flow["project"], a, {"grant_type": "authorization_code", "code": p["code"], "code_verifier": flow["verifier"],
                                        "redirect_uri": public + PATH + "/callback"}, {}, "refused", flow["api_host"])
    except ActionFailed:
        return "failed", flow
    if not isinstance(t.get("refresh_token"), str) or not t["refresh_token"]:
        return "no_refresh", flow  # nothing a later load could use
    conn.execute("INSERT INTO api_tokens (project, source, host, api_host, token, used) VALUES (%s, %s, %s, %s, %s, %s)"
                 " ON CONFLICT (project, source) DO UPDATE SET host = excluded.host, api_host = excluded.api_host,"
                 " token = excluded.token, id = gen_random_uuid(), used = excluded.used, connected = now()",
                 [flow["project"], flow["source"], httpx2.URL(a.token_url).host, flow["api_host"],
                  credentials._fernet().encrypt(t["refresh_token"].encode()),
                  # `used`: bound to a saved source, so the token dies with its file at once (flows.drop_stale_tokens)
                  (git.path(flow["project"]) / "sources" / f"{flow['source']}.json").is_file()])
    return "connected", flow


@router.get("/callback")
def callback(request: Request, abi_api_oauth: Annotated[str | None, Cookie()] = None) -> RedirectResponse:
    """The provider's redirect target. Outcomes are fixed codes; no code, token or state is logged (SC-4, SC-6)."""
    public = _public()
    with db.connect() as conn:
        outcome, flow = _callback(conn, public, request.query_params, abi_api_oauth)
        audit(conn, flow and security.user(conn, flow["user_id"]), "rest", "api_oauth_connect", flow and flow["project"],
              flow and {"source": flow["source"]}, 200 if outcome == "connected" else 401, outcome)
    r = RedirectResponse(f"{public}/#source-oauth={outcome}", 303)  # no-referrer: abi.app.Headers
    r.delete_cookie(COOKIE, path=PATH)
    return r
