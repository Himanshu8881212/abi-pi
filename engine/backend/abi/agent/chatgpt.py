"""Sign in with ChatGPT (AG-6, D-53, E-12.3.1): a person links their ChatGPT account and their own runs outside flows
try its plan's models first (`llm.complete` fails over to the workspace providers).

OpenAI's documented flow for open-source, locally hosted apps (work/validation/E-12.3.md): dynamic client registration,
PKCE S256, loopback redirect `http://127.0.0.1:<port>/callback` only, Responses API with plan limitations. Tokens are
Fernet-encrypted per user and go only to the two fixed OpenAI hosts, never to action results, audit rows, logs,
prompts or Git (SC-4). Link state, browser cookie and ID token checks reuse the OIDC client (`oidc`).
"""

import base64
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.parse
import uuid
from pathlib import Path
from typing import Annotated

import httpx2
import jwt
import openai
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from openai import OpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, Field

from abi import actions, db, security
from abi.actions import ActionFailed, action, audit, current_user, require_confirmation
from abi.agent import llm
from abi.project.git import NoInput
from abi.security import Role, User, credentials, oidc

AUTH = "https://auth.openai.com"  # issuer and host of authorize/token/revoke/JWKS; tests point AUTH and API at a stub
API = "https://api.openai.com/v1"
RESOURCE = "https://api.openai.com/v1"  # the token audience, always the real one
PLAN = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {PLAN}"
DYNAMIC = "dynamic_agent_client"  # first sign-in; the callback brings the issued client_id (one per ChatGPT account)
COOKIE = "abi_chatgpt"
CLIENT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
LOOPBACK = re.compile(r"^http://127\.0\.0\.1:\d{1,5}$")  # OpenAI allows only this redirect for locally hosted apps
OFF = ("Sign in with ChatGPT needs a locally hosted app at http://127.0.0.1:<port> (ABI_PUBLIC_URL);"
       " remotely hosted apps need OpenAI's approval.")
EARLY = "60 seconds"  # refresh an access token this long before it expires


class Refused(ValueError):
    """The token endpoint said no; `code` is its OAuth error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.dead = code == "invalid_grant" or code.startswith("refresh_token_")  # sign in again (errors-and-recovery)


LIMIT = "subscription_sharing_usage_limit_exceeded"  # 429 on inference (errors-and-recovery)


class UsageLimit(ValueError):
    """The plan's usage limit is reached (LLM-2-fix-18): `llm.complete` tells the person instead of a provider error."""


FAILED = (*oidc.FAILED, openai.APIError)


def _loopback() -> str:
    url = os.environ.get("ABI_PUBLIC_URL", "").rstrip("/")
    if not LOOPBACK.match(url):
        raise HTTPException(404, OFF)
    return url


def _host_id() -> str:
    """ext_agent_host_id: stable per install, opaque (HMAC of the secret key file, never the key itself)."""
    credentials._fernet()  # creates the key file on first use
    key = Path(os.environ.get("ABI_SECRET_KEY_FILE", "/data/secret.key")).read_bytes()
    return f"urn:uuid:{uuid.UUID(bytes=hmac.digest(key, b'abi-chatgpt-host', 'sha256')[:16], version=4)}"


def _client() -> httpx2.Client:
    from abi.tools import builtin  # not at top: builtin imports the agent package

    # SC-4/D-2: no proxy from the environment, no redirect carries a token elsewhere
    return httpx2.Client(verify=builtin._tls(), trust_env=False, follow_redirects=False)


def _token(form: dict) -> dict:
    with _client() as c, c.stream("POST", AUTH + "/api/accounts/oauth/token", data=form, timeout=10) as r:
        body = json.loads(oidc._read(r) or b"{}")
    if r.status_code != 200:
        raise Refused(str(body.get("error", r.status_code)) if isinstance(body, dict) else str(r.status_code))
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str) or not isinstance(body.get("refresh_token"), str):
        raise ValueError("no tokens")  # noqa: TRY004 - a bad reply, caught in FAILED
    return body


def _store(conn, uid: int, t: dict) -> None:
    sealed = credentials._fernet().encrypt(json.dumps({"access": t["access_token"], "refresh": t["refresh_token"]}).encode())
    conn.execute("UPDATE chatgpt_links SET tokens = %s, expires = now() + make_interval(secs => %s) WHERE user_id = %s",
                 [sealed, int(t.get("expires_in") or 3600), uid])


def _tokens(row) -> dict:
    return json.loads(credentials.decrypt(row["tokens"]))


def _claims(client: str, token: str, nonce: str) -> dict:
    """ID token: RS256 by a JWKS key, iss, aud = the issued client ID, exp, nonce (as `oidc._claims`)."""
    key = oidc._jwks(AUTH + "/.well-known/jwks.json").get_signing_key_from_jwt(token)
    claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client, issuer=AUTH, leeway=60,
                        options={"require": oidc.CLAIMS})
    if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise ValueError("nonce mismatch")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise ValueError("no sub")
    return claims


router = APIRouter()
Me = Annotated[User, Depends(current_user)]


@router.post("/api/chatgpt/start")
def start(user: Me, response: Response) -> dict:
    """Starts linking the caller's ChatGPT account; returns the OpenAI URL to open (D-53)."""
    public = _loopback()
    state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
    with db.connect() as conn:
        row = conn.execute("SELECT client_id FROM chatgpt_links WHERE user_id = %s", [user.id]).fetchone()
        client = row["client_id"] if row else DYNAMIC
        conn.execute(f"DELETE FROM chatgpt_flows WHERE created < now() - interval '{oidc.FLOW_TTL}'")
        conn.execute("INSERT INTO chatgpt_flows (state_hash, browser_hash, user_id, client_id, verifier, nonce)"
                     " VALUES (%s, %s, %s, %s, %s, %s)",
                     [security._token_hash(state), security._token_hash(browser), user.id, client, verifier, nonce])
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": client, **({"agent_name_hint": "ABI"} if client == DYNAMIC else {}), "ext_agent_host_id": _host_id(),
        "response_type": "code", "redirect_uri": public + "/callback", "scope": SCOPE, "resource": RESOURCE,
        "state": state, "nonce": nonce, "code_challenge_method": "S256", "code_challenge": challenge,
    }  # fmt: skip
    response.set_cookie(COOKIE, browser, max_age=600, path="/callback", httponly=True, samesite="lax")
    return {"url": AUTH + "/api/accounts/authorize?" + urllib.parse.urlencode(params)}


def _callback(conn, public: str, p, browser: str | None) -> tuple[str, int | None]:
    """(outcome, user id). The flow is spent first, so a state works once (replay -> expired)."""
    state = p.get("state")
    flow = state and conn.execute(
        "UPDATE chatgpt_flows SET used = true WHERE state_hash = %s AND NOT used"
        f" AND created > now() - interval '{oidc.FLOW_TTL}' RETURNING *", [security._token_hash(state)]).fetchone()
    if not flow:
        return "expired", None
    uid = flow["user_id"]
    if not browser or not hmac.compare_digest(security._token_hash(browser), bytes(flow["browser_hash"])):
        return "browser", uid
    if p.get("error") or not p.get("code"):
        return "denied", uid
    client = p.get("client_id", "") if flow["client_id"] == DYNAMIC else flow["client_id"]
    if client == DYNAMIC or not CLIENT_ID.match(client) or p.get("client_id", client) != client:
        return "failed", uid
    try:
        t = _token({"grant_type": "authorization_code", "client_id": client, "code": p["code"],
                    "code_verifier": flow["verifier"], "redirect_uri": public + "/callback", "resource": RESOURCE})
        if PLAN not in str(t.get("scope", "")).split():
            return "scope", uid  # signed in, but no plan usage granted
        claims = _claims(client, t.get("id_token") or "", flow["nonce"])
    except FAILED:
        return "failed", uid
    conn.execute(  # a different ChatGPT account keeps no model choice
        "INSERT INTO chatgpt_links (user_id, client_id, sub, email) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE"
        " SET client_id = excluded.client_id, sub = excluded.sub, email = excluded.email,"
        " model = CASE WHEN chatgpt_links.sub = excluded.sub THEN chatgpt_links.model END,"
        " backup = CASE WHEN chatgpt_links.sub = excluded.sub THEN chatgpt_links.backup END,"
        " effort = CASE WHEN chatgpt_links.sub = excluded.sub THEN chatgpt_links.effort END",
        [uid, client, claims["sub"], claims.get("email") if isinstance(claims.get("email"), str) else None])
    _store(conn, uid, t)
    return "linked", uid


@router.get("/callback")
def callback(request: Request, abi_chatgpt: Annotated[str | None, Cookie()] = None) -> RedirectResponse:
    """OpenAI's loopback redirect target. Outcomes are fixed codes; no code, token, state or nonce is logged (SC-4, SC-6)."""
    public = _loopback()
    with db.connect() as conn:
        outcome, uid = _callback(conn, public, request.query_params, abi_chatgpt)
        user = uid and security.user(conn, uid)
        audit(conn, user, "chatgpt", "chatgpt_link", None, None, 200 if outcome == "linked" else 401, outcome)
    r = RedirectResponse(f"{public}/#chatgpt={outcome}", 303)  # no-referrer: abi.app.Headers
    r.delete_cookie(COOKIE, path="/callback")
    return r


def _access(uid: int) -> str | None:
    """The user's access token, refreshed when it expires within EARLY under the link's row lock (refreshes are
    serialized: the refresh token rotates on every use). A refused refresh clears the tokens (relink needed)."""
    with db.connect() as conn:
        row = conn.execute(f"SELECT *, expires > now() + interval '{EARLY}' AS fresh FROM chatgpt_links"
                           " WHERE user_id = %s AND tokens IS NOT NULL FOR UPDATE", [uid]).fetchone()
        if not row:
            return None
        old = _tokens(row)
        if row["fresh"]:
            return old["access"]
        try:
            t = _token({"grant_type": "refresh_token", "client_id": row["client_id"], "refresh_token": old["refresh"]})
        except Refused as e:
            if e.dead:
                conn.execute("UPDATE chatgpt_links SET tokens = NULL, expires = NULL WHERE user_id = %s", [uid])
                conn.commit()
            raise
        _store(conn, uid, t)
        return t["access_token"]


def _models(access: str) -> list[dict]:
    """The plan's models to offer: `visibility == "list"`, sent as `slug`, shown as `display_name`, with the reasoning
    efforts it accepts (`supported_reasoning_levels[].effort`, D-71; none listed: no effort control) and its default."""
    with _client() as c, c.stream("GET", API + "/models", headers={"Authorization": f"Bearer {access}"}, timeout=10) as r:
        if r.status_code != 200:
            raise ValueError("models refused")
        data = json.loads(oidc._read(r))
    out = []
    for m in data.get("models", []):
        if isinstance(m, dict) and m.get("visibility") == "list" and isinstance(m.get("slug"), str):
            levels = [lv["effort"] for lv in m.get("supported_reasoning_levels") or []
                      if isinstance(lv, dict) and lv.get("effort") in llm.LEVELS]  # only names the SDK can send
            default = m.get("default_reasoning_level")
            out.append({"slug": m["slug"], "name": str(m.get("display_name") or m["slug"]), "efforts": levels,
                        "default_effort": default if default in levels else None})
    return out


# ponytail: one /models read per access token (it rotates on refresh, at most hourly); fresh reads for the account screen
@functools.lru_cache(maxsize=16)
def _listed(access: str) -> dict[str, dict]:
    return {m["slug"]: m for m in _models(access)}


def offered(user: User) -> dict[str, dict]:
    """slug → model (with `efforts`) of the user's plan; raises when ChatGPT can't be reached."""
    return _listed(_access(user.id) or "")


def revoke(conn, uid: int) -> bool:
    """Deletes the user's link and revokes its refresh token at OpenAI; False when the revocation failed (the link is
    gone here anyway; the person can disconnect the app in their ChatGPT settings)."""
    row = conn.execute("DELETE FROM chatgpt_links WHERE user_id = %s RETURNING client_id, tokens", [uid]).fetchone()
    if not row or not row["tokens"]:
        return True
    form = {"token": _tokens(row)["refresh"], "token_type_hint": "refresh_token", "client_id": row["client_id"]}
    try:
        with _client() as c:
            return c.post(AUTH + "/api/accounts/oauth/revoke", data=form, timeout=10).status_code == 200
    except httpx2.HTTPError:
        return False


class Model(BaseModel):
    model: str | None = Field(max_length=200, description="a slug from chatgpt_account's models; null stops using ChatGPT")
    effort: str | None = Field(None, max_length=20, description="one of the model's `efforts` (D-71); null: the model's default")
    backup: str | None = Field(None, max_length=200, description="a second slug tried when `model` fails or answers empty"
                               " (AG-6 failover); omitted: unchanged, null: none")


def _rest() -> None:
    if actions.VIA.get() != "rest":  # SC-4: a person's own account link, never an LLM's or a flow's
        raise ActionFailed(403, "Your ChatGPT link is managed by you at the REST API only.")


# ABI-PI: no ChatGPT sign-in; models come from Settings (a proxy or an API provider)
def chatgpt_account(conn, user: User, args: NoInput) -> dict:
    """Your ChatGPT link (AG-6): whether this app can link one, the linked email, the model your own runs use and the
    plan's models. `relink`: sign in again (the tokens were refused)."""
    _rest()
    row = conn.execute("SELECT email, model, backup, effort, tokens IS NOT NULL AS ok FROM chatgpt_links WHERE user_id = %s",
                       [user.id]).fetchone()
    out = {"available": bool(LOOPBACK.match(os.environ.get("ABI_PUBLIC_URL", "").rstrip("/"))), "linked": bool(row),
           "email": row and row["email"], "model": row and row["model"], "backup": row and row["backup"],
           "effort": row and row["effort"],
           "relink": bool(row and not row["ok"]), "models": []}
    if row and row["ok"]:
        try:
            out["models"] = _models(_access(user.id) or "")
        except FAILED as e:
            if getattr(e, "dead", False):
                out["relink"] = True
            else:
                out["error"] = "ChatGPT is unreachable; try again later."
    return out


# ABI-PI: no ChatGPT sign-in; models come from Settings (a proxy or an API provider)
def set_chatgpt_model(conn, user: User, args: Model) -> dict:
    """Picks the ChatGPT plan model your own runs try first (Main Agent, your custom agent runs; never flows), or null
    to stop using ChatGPT; `backup`: the plan model tried next when it fails or answers empty; `effort`: its reasoning
    effort (null: the model's default)."""
    _rest()
    if not conn.execute("SELECT 1 FROM chatgpt_links WHERE user_id = %s", [user.id]).fetchone():
        raise ActionFailed(404, "Link your ChatGPT account first.")
    if args.effort and not args.model:
        raise ActionFailed(400, "Pick a model for the effort.")
    if picked := {args.model, args.backup} - {None}:
        try:
            listed = {m["slug"]: m for m in _models(_access(user.id) or "")}
        except FAILED:
            raise ActionFailed(409, "Reconnect your ChatGPT account first.") from None
        if bad := sorted(picked - listed.keys()):
            raise ActionFailed(400, f"'{bad[0]}' is not one of your ChatGPT plan's models.")
        if args.effort:
            llm.unlisted(args.effort, args.model, listed[args.model]["efforts"])
    conn.execute("UPDATE chatgpt_links SET model = %s, effort = %s, backup = CASE WHEN %s THEN %s ELSE backup END"
                 " WHERE user_id = %s", [args.model, args.effort, "backup" in args.model_fields_set, args.backup, user.id])
    return {}


# ABI-PI: no ChatGPT sign-in; models come from Settings (a proxy or an API provider)
def unlink_chatgpt(conn, user: User, args: NoInput) -> dict:
    """Disconnects your ChatGPT account: revokes its tokens at OpenAI and deletes them here (after a confirmation;
    nothing linked: nothing to do)."""
    _rest()
    if not (row := conn.execute("SELECT email FROM chatgpt_links WHERE user_id = %s", [user.id]).fetchone()):
        return {}
    require_confirmation("Disconnect your ChatGPT account? Your runs go back to the workspace models.", {"email": row["email"]})
    if revoke(conn, user.id):
        return {"revoked": True}
    return {"revoked": False, "message": "Disconnected here; OpenAI didn't confirm, so also remove the app in your ChatGPT settings."}


NS = "abi"  # the one namespace tool that groups every function tool (plan limitation)


def models(user: User, plan: str | None = None) -> tuple[list[str], str | None]:
    """(the user's own ChatGPT plan models to try in order: `plan` (this chat's pick) or the picked one, then the backup
    (D-80), their effort); ([], None) when they use none."""
    with db.connect() as conn:
        row = conn.execute("SELECT model, backup, effort FROM chatgpt_links WHERE user_id = %s AND tokens IS NOT NULL"
                           " AND (model IS NOT NULL OR %s::text IS NOT NULL)", [user.id, plan]).fetchone()
    return ([m for m in dict.fromkeys([plan or row["model"], row["backup"]]) if m], row["effort"]) if row else ([], None)


def complete(user: User, kw: dict, model: str, wanted: tuple = ()) -> ChatCompletion | None:
    """The user's own ChatGPT plan answer from `model` to chat-completions `kw` (messages, tools), or None when the
    link has no usable token. Raises on any failure (HTTP, refresh, `response.failed`); `llm.complete` then fails over.
    `wanted`: efforts in preference order; the first one `model` lists is sent as `reasoning.effort`, else none (D-71)."""
    if not (access := _access(user.id)):
        return None
    body = request(model, kw)
    effort = next((e for e in wanted if e and e in _listed(access).get(model, {}).get("efforts", [])), None)
    if effort:
        body["reasoning"] = {"effort": effort}
    items = []  # the plan endpoint sends items only here; its `response.completed` has `output=[]` (LLM-2 probe)
    try:
        with _client() as h, OpenAI(api_key=access, base_url=API, max_retries=0, http_client=h).responses.create(**body) as events:
            for ev in events:
                if ev.type == "response.output_item.done":
                    items.append(ev.item)
                if ev.type == "response.completed":  # success only here (models-and-inference)
                    resp = _completion(ev.response, ev.response.output or items)
                    resp.effort = effort
                    return resp
                if ev.type in ("response.failed", "response.incomplete", "error"):
                    raise (UsageLimit if getattr(ev, "code", None) == LIMIT else ValueError)(ev.type)  # `error` event
    except openai.APIError as e:  # the 429 before the stream, or an `error` event the SDK raises itself
        if e.code == LIMIT:
            raise UsageLimit(e.code) from None
        raise
    raise ValueError("stream ended without response.completed")


def request(model: str, kw: dict) -> dict:
    """Chat-completions messages/tools -> a Responses API body within the plan's limits: system text as `instructions`,
    the whole history in `input` (store false), function tools in one namespace, stream true, no forbidden fields."""
    system, items = [], []
    for m in kw["messages"]:
        if m["role"] == "system":
            system.append(m["content"])
        elif m["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
        else:
            if m.get("content"):
                items.append({"role": m["role"], "content": m["content"]})
            items += [{"type": "function_call", "call_id": c["id"], "namespace": NS, "name": c["function"]["name"],
                       "arguments": c["function"]["arguments"]} for c in m.get("tool_calls") or []]
    body = {"model": model, "instructions": "\n\n".join(system), "input": items, "store": False, "stream": True}
    if kw.get("tools"):  # strict off: action schemas aren't strict-mode schemas
        body["tools"] = [{"type": "namespace", "name": NS, "description": "The app's actions.",
                          "tools": [{"type": "function", "strict": False, **t["function"]} for t in kw["tools"]]}]
    return body


def _completion(r, output) -> ChatCompletion:
    """A completed Response with its `output` items as the ChatCompletion both agent loops read (text, tool calls by
    call_id, usage)."""
    text = "".join(c.text for o in output if o.type == "message" for c in o.content if c.type == "output_text")
    calls = [{"id": o.call_id, "type": "function", "function": {"name": o.name, "arguments": o.arguments}}
             for o in output if o.type == "function_call"]
    u = r.usage
    return ChatCompletion.model_validate({
        "id": r.id, "object": "chat.completion", "created": int(r.created_at), "model": r.model,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                     "message": {"role": "assistant", "content": text or None, "tool_calls": calls or None}}],
        "usage": u and {"prompt_tokens": u.input_tokens, "completion_tokens": u.output_tokens, "total_tokens": u.total_tokens,
                        "prompt_tokens_details": {"cached_tokens": getattr(getattr(u, "input_tokens_details", None),
                                                                           "cached_tokens", None)},  # D-117
                        "completion_tokens_details": {"reasoning_tokens": getattr(getattr(u, "output_tokens_details", None),
                                                                                  "reasoning_tokens", None)}},
    })
