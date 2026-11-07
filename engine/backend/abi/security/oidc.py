"""OIDC sign-in (SC-1, E-01.1): workspace providers, authorization code + PKCE, ID token checks, identity linking.

Decisions: work/validation/E-01.md. No code, token, state or nonce ever reaches a log or audit row (SC-4, SC-6).
"""

import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
from typing import Annotated

import httpx2
import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from abi import db, security
from abi.actions import ActionFailed, action, audit, require_confirmation
from abi.agent.llm import ProviderId
from abi.project import ID
from abi.security import Role, User, credentials
from abi.tools import builtin

COOKIE = "abi_oidc"
FLOW_TTL = "10 minutes"
MAX_BYTES = 1_000_000
COOLDOWN = 30.0  # PyJWKClient refetch cooldown on an unknown kid; tests set 0
ALGS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}  # never HS* / none
CLAIMS = ["iss", "aud", "exp", "iat", "sub", "nonce"]
# H-01.2-fix-4: starts per client IP per 10 minutes (each writes an oidc_flows row, swept after FLOW_TTL).
# ponytail: one office behind one NAT shares the count; raise it if a team's morning sign-ins hit 429
STARTS = (60, 600)


class Provider(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=100)
    issuer: str = Field(pattern=r"^https://[^\s?#]+$", max_length=500)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(pattern=r"^\$\{secret:[A-Za-z0-9_]+\}$", description="a ${secret:NAME} reference")


@action(Role.owner)
def set_oidc_provider(conn, user: User, args: Provider) -> dict:
    """Adds or changes a workspace OIDC sign-in provider (SC-1); its client secret is a ${secret:NAME} reference."""
    name = args.client_secret[len("${secret:"):-1]
    if not conn.execute("SELECT 1 FROM secrets WHERE name = %s AND tenant IS NULL", [name]).fetchone():  # SC-9: platform only
        raise ActionFailed(400, credentials.UNKNOWN)
    require_confirmation(f"People will sign in through {args.issuer}.", {"issuer": args.issuer, "client_id": args.client_id})
    conn.execute(
        "INSERT INTO oidc_providers VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET name = excluded.name,"
        " issuer = excluded.issuer, client_id = excluded.client_id, client_secret = excluded.client_secret",
        [args.id, args.name, args.issuer, args.client_id, args.client_secret],
    )
    return {}


@action(Role.owner, destructive=True)
def delete_oidc_provider(conn, user: User, args: ProviderId) -> dict:
    """Removes a sign-in provider; its linked identities stay but can no longer sign in."""
    if not conn.execute("DELETE FROM oidc_providers WHERE id = %s RETURNING id", [args.id]).fetchone():
        raise ActionFailed(404, "No such provider.")
    return {}


def _public() -> str:
    """ABI_PUBLIC_URL; OIDC is off (404) until it is set."""
    url = os.environ.get("ABI_PUBLIC_URL", "").rstrip("/")
    if not url:
        raise HTTPException(404, "OIDC sign-in is off: set ABI_PUBLIC_URL.")
    return url


def _redirect_uri(provider: str) -> str:
    return f"{_public()}/api/oidc/{provider}/callback"


def _client() -> httpx2.Client:
    return httpx2.Client(verify=builtin._tls(), trust_env=False, follow_redirects=False, timeout=10)


def _read(resp) -> bytes:
    body = b""
    for chunk in resp.iter_bytes():
        body += chunk
        if len(body) > MAX_BYTES:
            raise ValueError("response too large")
    return body


def _https(url) -> bool:
    u = isinstance(url, str) and urllib.parse.urlsplit(url)
    return bool(u and u.scheme == "https" and u.hostname)


@functools.cache  # ponytail: per-process, never refreshed; restart after an IdP moves endpoints, add a TTL if that bites
def _discover(issuer: str) -> dict:
    """The provider's discovery document: https only, exact issuer, https endpoints, PKCE S256 (E-01 decision 2)."""
    with _client() as c, c.stream("GET", issuer.rstrip("/") + "/.well-known/openid-configuration") as r:
        if r.status_code != 200:
            raise ValueError("discovery failed")
        doc = json.loads(_read(r))
    if doc.get("issuer") != issuer:
        raise ValueError("issuer mismatch")
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not _https(doc.get(k)):
            raise ValueError(f"{k} is not https")
    if doc.get("end_session_endpoint") is not None and not _https(doc["end_session_endpoint"]):
        raise ValueError("end_session_endpoint is not https")
    if "S256" not in doc.get("code_challenge_methods_supported", ["S256"]):  # Microsoft omits it, PKCE still works
        raise ValueError("no PKCE S256")
    return doc


class _JWKS(jwt.PyJWKClient):
    def fetch_data(self):
        """PyJWT's urllib fetch honours proxy env vars; ours doesn't (no proxy, no redirects, size cap)."""
        try:
            with _client() as c, c.stream("GET", self.uri) as r:
                if r.status_code != 200:
                    raise ValueError("jwks fetch failed")
                data = self._as_jwk_set_payload(json.loads(_read(r)))
        except (httpx2.HTTPError, ValueError) as e:
            raise jwt.PyJWKClientConnectionError("jwks fetch failed") from e
        if self.jwk_set_cache is not None:
            self.jwk_set_cache.put(data)
        self._last_successful_fetch = time.monotonic()
        return data


@functools.cache
def _jwks(uri: str) -> jwt.PyJWKClient:
    return _JWKS(uri, cache_keys=True, cooldown_duration=COOLDOWN)


def _exchange(conn, row, doc: dict, code: str, verifier: str) -> str:
    """Code -> ID token. The client secret goes only to the discovery token_endpoint host, through D-2 (SC-4)."""
    used: dict = {}
    secret = credentials.resolve(conn, row["client_secret"], used, None)
    credentials.allow(used, urllib.parse.urlsplit(doc["token_endpoint"]).hostname)
    q = urllib.parse.quote_plus
    basic = base64.b64encode(f"{q(row['client_id'])}:{q(secret)}".encode()).decode()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(row["id"]), "code_verifier": verifier}
    with _client() as c, c.stream("POST", doc["token_endpoint"], data=form, headers={"Authorization": f"Basic {basic}"}) as r:
        if r.status_code != 200:
            raise ValueError("token endpoint refused")
        token = json.loads(_read(r)).get("id_token")
    if not isinstance(token, str):
        raise ValueError("no id_token")  # noqa: TRY004 - a bad IdP reply, caught in FAILED
    return token


def _claims(row, doc: dict, token: str, nonce: str) -> dict:
    """Verified ID token claims: asymmetric alg the provider lists, JWKS key by kid, iss/aud/exp/nonce/azp."""
    alg = jwt.get_unverified_header(token).get("alg")
    if alg not in ALGS & set(doc.get("id_token_signing_alg_values_supported", ["RS256"])):
        raise ValueError("algorithm not allowed")
    key = _jwks(doc["jwks_uri"]).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token, key.key, algorithms=[alg], audience=row["client_id"], issuer=row["issuer"], leeway=60, options={"require": CLAIMS}
    )
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if ("azp" in claims or len(aud) > 1) and claims.get("azp") != row["client_id"]:
        raise ValueError("azp mismatch")
    if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise ValueError("nonce mismatch")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise ValueError("no sub")
    return claims


router = APIRouter(prefix="/api/oidc")
# TypeError: an IdP's odd JSON (a list where a string goes, e.g. a header `alg`) is a failed sign-in, not a 500
FAILED = (httpx2.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError, ActionFailed)


def _url(base: str, params: dict) -> str:
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode(params)


@router.get("/providers")
def providers() -> list[dict]:
    """Sign-in buttons for the login page (id and name only)."""
    _public()
    with db.connect() as conn:
        return conn.execute("SELECT id, name FROM oidc_providers ORDER BY name").fetchall()


@router.post("/{provider}/start")
def start(
    provider: str,
    request: Request,
    response: Response,
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> dict:
    """Starts a sign-in (no bearer) or links the caller's account (bearer); returns the IdP URL to open."""
    public = _public()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM oidc_providers WHERE id = %s", [provider]).fetchone()
        if not row:
            raise HTTPException(404, "No such provider.")
        if security.limited(conn, f"oidc-start:{request.client.host if request.client else ''}", *STARTS):
            raise HTTPException(429, "Too many sign-in attempts; try again in a few minutes.")
        link = None
        if cred:
            user = security.user_for_token(conn, cred.credentials)
            if not user:
                raise HTTPException(401, "Sign in again to link an account.")
            link = user.id
        try:
            doc = _discover(row["issuer"])
        except FAILED:
            raise HTTPException(502, "The sign-in provider is unreachable or misconfigured.") from None
        state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
        conn.execute(f"DELETE FROM oidc_flows WHERE created < now() - interval '{FLOW_TTL}'")
        conn.execute(
            "INSERT INTO oidc_flows (state_hash, browser_hash, provider, verifier, nonce, link_user)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            [security._token_hash(state), security._token_hash(browser), provider, verifier, nonce, link],
        )
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code", "client_id": row["client_id"], "redirect_uri": _redirect_uri(provider), "scope": "openid",
        "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
    }  # fmt: skip
    secure = public.startswith("https://")
    response.set_cookie(COOKIE, browser, max_age=600, path="/api/oidc", httponly=True, samesite="lax", secure=secure)
    return {"url": _url(doc["authorization_endpoint"], params)}


def _callback(conn, provider: str, p, browser: str | None) -> tuple[str, dict | None, int | None]:
    """(outcome, flow, user id to audit). The flow is spent first, so a state works once (replay -> expired)."""
    state = p.get("state")
    flow = state and conn.execute(
        "UPDATE oidc_flows SET used = true WHERE state_hash = %s AND NOT used"
        f" AND created > now() - interval '{FLOW_TTL}' RETURNING *",
        [security._token_hash(state)],
    ).fetchone()
    if not flow:
        return "expired", None, None
    if flow["provider"] != provider:
        return "mixup", flow, None
    if not browser or not hmac.compare_digest(security._token_hash(browser), bytes(flow["browser_hash"])):
        return "browser", flow, None
    if p.get("error") or not p.get("code"):
        return "denied", flow, None
    row = conn.execute("SELECT * FROM oidc_providers WHERE id = %s", [provider]).fetchone()
    if not row:
        return "failed", flow, None  # deleted while the person was at the IdP
    try:
        doc = _discover(row["issuer"])
        if ("iss" in p or doc.get("authorization_response_iss_parameter_supported")) and p.get("iss") != row["issuer"]:
            return "mixup", flow, None  # RFC 9207: the code came from another IdP; never send it on
        claims = _claims(row, doc, _exchange(conn, row, doc, p["code"], flow["verifier"]), flow["nonce"])
    except FAILED:
        return "failed", flow, None
    ident = conn.execute(
        "SELECT user_id FROM oidc_identities WHERE issuer = %s AND sub = %s", [row["issuer"], claims["sub"]]
    ).fetchone()
    if flow["link_user"]:  # linking only from the user's own session, never by email (E-01 decision)
        if ident and ident["user_id"] != flow["link_user"]:
            return "conflict", flow, flow["link_user"]
        conn.execute(
            "INSERT INTO oidc_identities VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            [row["issuer"], claims["sub"], flow["link_user"]],
        )
        return "linked", flow, flow["link_user"]
    if not ident:
        return "unknown", flow, None
    conn.execute("UPDATE oidc_flows SET user_id = %s WHERE state_hash = %s", [ident["user_id"], flow["state_hash"]])
    return "ok", flow, ident["user_id"]


@router.get("/{provider}/callback")
def callback(provider: str, request: Request, abi_oidc: Annotated[str | None, Cookie()] = None) -> RedirectResponse:
    """The IdP's redirect target. Outcomes are fixed codes; no code, token, state or nonce is logged (SC-4, SC-6)."""
    public = _public()
    with db.connect() as conn:
        outcome, flow, uid = _callback(conn, provider, request.query_params, abi_oidc)
        user = uid and security.user(conn, uid)
        status = 200 if outcome in ("ok", "linked") else 401
        audit(conn, user, "oidc", "oidc_signin", None, flow and {"provider": flow["provider"]}, status, outcome)
    return RedirectResponse(f"{public}/?signin={outcome}", 303)  # no-referrer: abi.app.Headers


@router.post("/session")
def session(response: Response, abi_oidc: Annotated[str | None, Cookie()] = None) -> dict:
    """Hands the browser that finished a sign-in its session token, once (never in a URL)."""
    with db.connect() as conn:
        row = abi_oidc and conn.execute(
            "DELETE FROM oidc_flows WHERE browser_hash = %s AND user_id IS NOT NULL"
            f" AND created > now() - interval '{FLOW_TTL}' RETURNING user_id, provider",
            [security._token_hash(abi_oidc)],
        ).fetchone()
        if not row:
            raise HTTPException(401, "No completed sign-in.")
        token = security.new_session(conn, row["user_id"], row["provider"])
        name = conn.execute("SELECT name FROM users WHERE id = %s", [row["user_id"]]).fetchone()["name"]
    response.delete_cookie(COOKIE, path="/api/oidc")
    return {"token": token, "name": name}  # the name keys per-user browser state, as after a password login


def end_session_url(conn, provider: str | None) -> str | None:
    """RP-initiated logout URL for a session that came from an IdP that offers it (SC-1)."""
    row = provider and conn.execute("SELECT issuer, client_id FROM oidc_providers WHERE id = %s", [provider]).fetchone()
    if not row or not os.environ.get("ABI_PUBLIC_URL"):
        return None
    try:
        end = _discover(row["issuer"]).get("end_session_endpoint")
    except FAILED:
        return None
    return end and _url(end, {"client_id": row["client_id"], "post_logout_redirect_uri": _public() + "/"})


class _NoQuery(logging.Filter):
    """uvicorn's access log would carry the callbacks' code and state (OIDC, ChatGPT's /callback, API source OAuth); keep only the path (SC-4)."""

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        if isinstance(a, tuple) and len(a) == 5 and str(a[2]).startswith(("/api/oidc", "/callback", "/api/sources/oauth")):
            record.args = (*a[:2], str(a[2]).split("?")[0], *a[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_NoQuery())
