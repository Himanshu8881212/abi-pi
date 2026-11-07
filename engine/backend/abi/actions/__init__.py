"""Typed action API: the one path for UI, Main Agent, REST, SDK. PRD §5 #1-3, PC-14, SC-2, SC-6.

An action is `fn(conn, user, args: <pydantic model>) -> dict`, registered with `@action(role)`.
`run()` is the only way to call one: it validates input, checks the caller's role, demands a
confirmation token for destructive actions, runs the action in one transaction and audit-logs
every call, whatever the outcome. An input field named `project` scopes the role check to that
project; without it the check is at workspace scope. `definition=True` marks actions that edit the
project's files: each lands as one uncommitted, undoable change in its Git repo (abi.project.git).
"""

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated

import duckdb
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from abi import db
from abi.security import CLIENT, Role, User, admin_of, role_in, user_for_token


@dataclass(frozen=True)
class Action:
    fn: Callable
    input: type[BaseModel]
    role: Role
    destructive: bool  # listed as destructive: a confirmation always follows
    definition: bool
    confirms: bool  # asks its own confirmation inside, with a signed preview (so run() doesn't ask a plain one)
    tenant_admin: bool  # a tenant admin may run it in its own tenant (SC-9); the action scopes its targets itself


ACTIONS: dict[str, Action] = {}
VIA: ContextVar[str] = ContextVar("via", default="rest")  # the running action's caller; Git signs agent changes as the agent (UI-4)
OUTSIDE: ContextVar[bool] = ContextVar("abi_outside", default=False)  # M4.5: a personal token's outside client


def by_agent() -> bool:
    """An LLM is the caller: the Main Agent or an outside one over MCP (D-33), so the agent rules apply."""
    return VIA.get() in ("agent", "mcp")


def action(role: Role, destructive: bool = False, definition: bool = False, confirms: bool = False,
           tenant_admin: bool = False):
    def register(fn):
        ACTIONS[fn.__name__] = Action(fn, fn.__annotations__["args"], role, destructive or confirms, definition, confirms,
                                      tenant_admin)
        return fn

    return register


class ActionFailed(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.body = {"message": message, **extra}


# ponytail: per-process key, so a token only confirms on the process that issued it and stays
# replayable until it expires; move the key and a used-token table to Postgres for multi-process.
_KEY = secrets.token_bytes(32)
_CONFIRM_TTL = 300


def _sign(user: User, name: str, args: str, exp: str) -> str:
    return hmac.new(_KEY, f"{user.id}|{name}|{args}|{exp}".encode(), hashlib.sha256).hexdigest()


def _confirmed(token: str | None, user: User, name: str, args: str) -> bool:
    exp, _, sig = (token or "").partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(user, name, args, exp))


_call: ContextVar[tuple] = ContextVar("abi_call")  # (user, name, canonical args, confirm token) of the running action
_memo: ContextVar[dict | None] = ContextVar("abi_memo", default=None)
_said: ContextVar[dict | None] = ContextVar("abi_said", default=None)  # the preview the running action's person confirmed


def confirmed() -> dict | None:
    """The preview the running action already confirmed ({} without one; None: none confirmed). Its one token is used:
    a later ask needs the same preview, or it can never pass."""
    return _said.get()


def memo(key: tuple, fn: Callable):
    """M3.6: fn() once per read action (a Viewer's action that changes nothing, as `run` marks it): a role, a rule
    file or a locale read again inside one visual_data call gives the same answer. Never across actions, so a
    demotion or a rule change applies at the next one; outside a read action, fn() every time. One at a time per
    action, so threads of one action (a page's visuals) read each once."""
    m = _memo.get()
    if m is None:
        return fn()
    with m.setdefault(None, threading.RLock()):  # an fn may ask memo again
        if key not in m:
            m[key] = fn()
        return m[key]


def require_confirmation(why: str, preview: dict | None = None) -> None:
    """MA-3 mid-call: raise 409 with a confirm token unless the running action's caller sent a valid one.
    A `preview` goes into the 409 body and is signed too, so its token confirms exactly what it shows."""
    user, name, canon, token = _call.get()
    if preview is not None:
        canon += json.dumps(preview, sort_keys=True)
    if _confirmed(token, user, name, canon):
        _said.set(preview or {})
    else:
        exp = str(int(time.time()) + _CONFIRM_TTL)
        raise ActionFailed(409, f"{why} Confirm to proceed.", confirm_token=f"{exp}.{_sign(user, name, canon, exp)}",
                           **({} if preview is None else {"preview": preview}))


def audit(conn, user: User | None, via: str, name: str, project, input, status: int, detail=None) -> None:
    conn.execute(
        "INSERT INTO audit_log (user_id, user_name, via, action, project, input, status, detail)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        [user and user.id, user and user.name, f"{via}:token" if OUTSIDE.get() else via, name, project,
         None if input is None else Jsonb(input), status, detail],
    )


def audit_login(name: str, via: str, status: int, detail: str | None = None) -> None:
    """SC-6: one row per password sign-in (REST or SQL endpoint), right or wrong or throttled: the typed name, never the
    password or code. Its own connection, so the row stays whatever the sign-in does next."""
    with db.connect() as conn:
        row = conn.execute("SELECT id, name, workspace_owner FROM users WHERE name = %s", [name]).fetchone()
        audit(conn, row and User(**row), via, "login", None, {"name": name}, status, detail)


INBOUND: ContextVar[bool] = ContextVar("inbound", default=False)  # inside an inbound agent run or message-triggered flow


def inbound() -> bool:
    """The running code serves an inbound message (D-32): an inbound call, or anything nested in one or approved
    from one (an LLM, a flow step or the queued action may carry the message text)."""
    return VIA.get() == "inbound" or INBOUND.get()


def needs_click() -> bool:
    """§5 #3 for outside contacts (H-01.5-fix-1/-8): anything but a person's own token-less REST call. The Chat's Confirm
    and inbound approvals arrive as "rest" too, with a token or INBOUND, and must match what their 409 showed."""
    return VIA.get() != "rest" or inbound() or bool(_call.get((None,) * 4)[3])


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


_IDS = ("project", "agent", "flow", "tool", "report", "table", "id")  # top-level ID keys an auditor reads in clear
_ID = r"[a-z][a-z0-9_-]{0,62}"  # abi.project.ID, which holds git.ID (not imported: both import this module)


def hashed(x, top: bool = True):
    """D-32: every string at any depth as its length and sha256, except ID-shaped values of top-level ID keys."""
    if isinstance(x, str):
        return {"length": len(x), "sha256": sha(x)}
    if isinstance(x, dict):
        return {k: v if top and k in _IDS and (isinstance(v, int) or isinstance(v, str) and re.fullmatch(_ID, v))
                else hashed(v, False) for k, v in x.items()}
    return [hashed(v, False) for v in x] if isinstance(x, list) else x


_LITERAL = {False: re.compile(r"'(?:[^']|'')*'"), True: re.compile(r"'(?:[^'\\]|\\.|'')*'")}  # DuckDB, ClickHouse


def redacted(text: str, clickhouse: bool = False) -> str:
    """D-32: SQL `text` with each single-quoted literal replaced by '<sha256:…>' of its value
    ('' unescaped, so the hash matches the text it came from).
    ponytail: a regex, not a parser, so an apostrophe inside a "quoted identifier" shifts the matching and can leave a
    later literal in clear; rewrite exp.Literal nodes with sqlglot if such names show up."""
    return _LITERAL[clickhouse].sub(lambda m: "'<sha256:" + sha(m[0][1:-1].replace("''", "'")) + ">'", text)


SUMMARIZED = {  # audited as a digest, not in clear
    "save_chat": ("items",),  # CHAT-PERSIST: a chat transcript as {count, sha256}: delete_chat leaves no copy
    "import_project": ("bundle",),  # IMPORT-ECHO: each file as {length, sha256}: a credential pasted in any file (SC-4)
    "set_delivery": ("smtp", "channels"),  # DEMO-1-fix-1: each field/channel as a digest: a password pasted (SC-4)
    "set_account_mail": ("smtp",),
    "set_remote": ("secret",),  # DEMO-1-fix-3: a bare secret name, maybe a typed password
}


def _refs(x):
    """SC-4: every ${secret:NAME} at any depth with NAME as its sha256, since a NAME may be a password typed there."""
    if isinstance(x, str):
        return re.sub(r"\$\{secret:([^}]*)\}", lambda m: "${secret:<sha256:" + sha(m[1]) + ">}", x)
    if isinstance(x, dict):
        return {k: _refs(v) for k, v in x.items()}
    return [_refs(v) for v in x] if isinstance(x, list) else x


def _audited(name: str, logged: dict | None) -> dict | None:
    if logged is None:
        return None
    logged = _refs(logged)
    for k in SUMMARIZED.get(name, ()):
        if isinstance(v := logged.get(k), str):
            logged = logged | {k: {"length": len(v), "sha256": sha(v)}}
        elif isinstance(v, list):
            logged = logged | {k: {"count": len(v), "sha256": sha(json.dumps(v, sort_keys=True))}}
        elif isinstance(v, dict):
            logged = logged | {k: {p: {"length": len(t := json.dumps(c, sort_keys=True)), "sha256": sha(t)} for p, c in v.items()}}
    return hashed(logged) if inbound() else logged


_REFUSED = {  # SC-9: the membership trigger (db.py, SQLSTATE AB901) and the users CHECKs
    "AB901": "A tenant account can hold roles only in its own tenant's projects; remove its other roles first.",
    "users_owner_platform": "A workspace owner can't belong to a tenant.",
    "users_admin_tenant": "Only an account of a tenant can be its tenant admin.",
    "ml_runs_pinned": "Another run of this ML model was pinned at the same moment; check ml_models and try again.",  # pin_ml_run race
}


# SH-1 (D-51): the read actions an account with no role in the project may call on one object shared with it, by the
# payload key naming that object. Nothing else of the project is reachable through a share.
SHARED = {"list_reports": "report", "visual_data": "report", "how_calculated": "report", "export_visual": "report",
          "dataset_data": "dataset", "run_agent": "agent", "agent_runs": "agent"}
# E-03.2: inside a shared agent's run (set per tool call by agents.runs._call, after its scope check) the recipient's
# read tools run as Viewer of that project; nothing that writes or sends. Never set outside a run.
SHARE: ContextVar[tuple | None] = ContextVar("share", default=None)  # (project, user id)
SHARE_TOOLS = ("semantic_query", "run_tool")


def by_share(project, user: User) -> bool:
    """The running code is a tool call of an agent shared with `user` in `project` (E-03.2)."""
    return SHARE.get() == (project, user.id)


# D-56/D-57/D-60: what may run in a production environment (git.production). Default deny: an action not listed is
# refused there, whoever calls it, unless a flow dev defined runs it (flows.IN_FLOW, D-23).
# PRODUCTION_READS (reads, and asking) are all an agent may call there (D-56: it answers only): the Main Agent, custom
# agents, MCP, and a flow's agent step, which may also send (FLOW_SENDS). D-60 access admin needs the Owner role there.
PRODUCTION_READS = frozenset({
    "agent_runs", "changes", "check_condition", "data_quality", "dataset_data", "delivery_channels", "delivery_settings",
    "diff", "docs",
    "edit_history", "edit_proposals", "explain_change", "export_visual", "flow_runs", "generate_docs", "get_sharing", "history",
    "how_calculated", "inbound_approvals", "inbox", "lineage", "list_agents", "list_embed_keys", "list_flows",
    "list_reports", "list_secrets", "list_snapshots", "list_share_links", "list_subscriptions", "list_tools", "load_runs", "ml_history", "ml_models", "ml_trials",
    "ontology", "export_ontology", "object_history",  # 1.2.6: definitions only; an object's history
    "project_locale", "readiness", "remote_branches", "remote_settings", "report_snapshot", "rls_rules", "run_agent",
    "run_tool", "semantic_model", "semantic_query", "sql_query", "visual_data",
})
FLOW_SENDS = frozenset({"send_email", "post_message"})
PRODUCTION_ACCESS = frozenset({"set_sharing", "create_share_link", "revoke_share_link", "create_embed_key",  # D-60
                               "revoke_embed_key", "set_rls_rule", "delete_rls_rule"})
PRODUCTION_OK = PRODUCTION_READS | PRODUCTION_ACCESS | {
    "subscribe", "unsubscribe",  # a person's own alerts
    "save_chat", "list_chats",  # a person's own chats (CHAT-PERSIST)
    "create_project",  # a new project is never production; on an existing one it answers 409
    "approve_flow_run", "reject_flow_run", "approve_inbound_action", "reject_inbound_action",  # runtime decisions
    "pull",  # the promote: Owner and a confirmation with the diff in production (remote.pull)
    "set_role", "remove_role", "set_project_tenant", "set_environment", "set_remote",  # access/environment admin
    "edit_cells", "edit_total", "propose_edit", "approve_edit", "reject_edit", "undo_edits", "sync_edits",  # D-57
    "run_object_action",  # 1.2.6 M4: a write-back like edit_cells (D-57)
}


# H-01.3-fix-1/-2 (§5 #3, D-1, D-42; OWASP LLM06): who may reach the workspace's data, and where the project repository
# is pushed, change only on a person's click. From an agent, MCP, a flow or an inbound run these ask a confirmation that
# only the UI can give (the token goes to the Confirm button, never back to the caller); a person's own REST call is one step.
PERSON_CLICK = frozenset({"set_role", "set_tenant_admin", "set_user_tenant", "map_sender", "invite_user", "set_tenant",
                          "set_remote"})  # set_project_tenant confirms itself (confirms=True)


def production_refusal(name: str, project, role: Role | None = None) -> str | None:
    """D-56: why `name` may not run on `project` now (a production environment) for a caller with `role`; None if it
    may. A flow dev defined runs its own steps (via="flow"); an agent's tool calls, in a flow too, never change design."""
    from abi import flows  # not at top: both import this module
    from abi.project import git

    if not project or name in PRODUCTION_READS:
        return None
    in_flow = flows.IN_FLOW.get()
    allowed = in_flow and name in FLOW_SENDS if by_agent() else in_flow or name in PRODUCTION_OK
    if allowed and name not in PRODUCTION_ACCESS or not git.production(project):
        return None
    if not allowed:
        return (f"Project '{project}' is a production environment and is view-only: make this change in dev and"
                " promote it (pull).")
    if (role or 0) < Role.owner:
        return f"In production environment '{project}', '{name}' needs the Owner role (D-60)."
    return None


def invalid(e: ValidationError) -> list[dict]:
    """pydantic's errors with no input value (SC-4). `include_input=False` still leaves two built-in messages quoting the
    input: a discriminated union's tag and a UUID's bad character; those are rebuilt from ctx (AGENT-CONTEXT-fix-6.1)."""
    out = json.loads(e.json(include_url=False, include_input=False))
    for x in out:
        if x["type"] == "union_tag_invalid":
            x["ctx"].pop("tag", None)
            x["msg"] = (f"Input tag found using {x['ctx']['discriminator']} does not match any of the expected tags:"
                        f" {x['ctx']['expected_tags']}")
        elif x["type"] == "uuid_parsing":
            x.pop("ctx", None)
            x["msg"] = "Input should be a valid UUID"
    return out


def run(name: str, payload: dict, user: User, via: str, confirm: str | None = None):
    """Run action `name` as `user`. `via` is the caller: "rest" (UI, scripts), "agent", "mcp", "flow" or "inbound"."""
    args = logged = None
    VIA.set(via)
    held = INBOUND.get()  # an action may set INBOUND for itself and its audit row (approve_inbound_action)
    act = ACTIONS.get(name)
    token = _memo.set({} if act and act.role == Role.viewer and not (act.definition or act.destructive) else None)
    try:
        if act is None:
            raise ActionFailed(404, f"Unknown action '{name}'.")
        try:
            args = act.input.model_validate(payload)
        except ValidationError as e:
            raise ActionFailed(422, "Invalid input.", errors=invalid(e))
        logged = args.model_dump(mode="json")  # SecretStr fields dump as '**********'
        project = logged.get("project")
        with db.connect() as conn:
            have = role_in(conn, user, project)
            if act.tenant_admin and (have is None or have < act.role) and admin_of(conn, user, project):
                have = act.role  # SC-9: manages its tenant's accounts and roles; role_in (data access) is unchanged
            if have is None and (kind := SHARED.get(name)):
                from abi.project import sharing  # noqa: I001  not at top: abi.project imports this module
                if sharing.reaches(conn, user, project, kind, logged.get(kind)):
                    have = Role.viewer  # SH-1: one shared object's reads, view-only; RLS default-denies a non-member (D-42)
            if have is None and name in SHARE_TOOLS and by_share(project, user):
                have = Role.viewer  # E-03.2: a shared agent's read tool, already checked against its scope
            if have is None or have < act.role:
                where = f"project '{project}'" if project else "this workspace"
                you = f"your role there is {have.name}" if have else "you have no role there"
                raise ActionFailed(403, f"'{name}' needs the {act.role.name} role in {where}; {you}.")
            if why := production_refusal(name, project, have):
                raise ActionFailed(403, why)
            _call.set((user, name, json.dumps(logged, sort_keys=True), confirm))
            _said.set(None)
            if act.destructive and not act.confirms:
                require_confirmation(f"Destructive: {' '.join((act.fn.__doc__ or '').split())}")  # no action ID in the text
            if via != "rest" and name in PERSON_CLICK:  # every arg shown, uncut: the click approves all of them (a logo is a digest)
                require_confirmation(f"Changes access or where the project goes: {' '.join((act.fn.__doc__ or '').split())}"
                                     f" Asked: {json.dumps(_audited(name, logged), ensure_ascii=False)}.")  # SUMMARIZED fields hashed: the text is audited
            from abi.project import git  # not at top: git imports this module

            with git.change(project, user, name) if act.definition else nullcontext():
                try:
                    result = act.fn(conn, user, args)
                    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")  # the deferred SC-9 membership check, now
                except errors.DatabaseError as e:  # every writer gets the same 400, and git.change rolls its files back
                    if why := _REFUSED.get(e.sqlstate or "") or _REFUSED.get(e.diag.constraint_name or ""):
                        raise ActionFailed(400, why) from None
                    raise
                except duckdb.OutOfMemoryException:  # LOAD-MEMORY: the operator's cap (ABI_DUCKDB_MEMORY_LIMIT), not a bug
                    raise ActionFailed(503, f"'{name}' ran out of memory: this data needs more than the server's DuckDB"
                                            " memory limit allows. Try again later, or raise ABI_DUCKDB_MEMORY_LIMIT.") from None
                if act.definition:
                    from abi import flows  # not at top: flows imports this module

                    flows.drop_stale_tokens(conn, project)
            audit(conn, user, via, name, project, _audited(name, logged), 200)
            return result
    except Exception as e:
        status = e.status if isinstance(e, ActionFailed) else 500
        detail = f"<sha256:{sha(str(e))}>" if inbound() else str(e)  # D-32: a message may quote the input; the status stays
        with db.connect() as conn:  # own transaction: the action's changes were rolled back
            audit(conn, user, via, name, (logged or {}).get("project"), _audited(name, logged), status, detail)
        raise
    finally:
        INBOUND.set(held)
        _memo.reset(token)


router = APIRouter(prefix="/api")


def current_user(cred: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())]) -> User:
    with db.connect() as conn:
        user = user_for_token(conn, cred.credentials)
    if user is None:
        raise HTTPException(401, "Invalid or expired session.", headers={"WWW-Authenticate": "Bearer"})
    return user


@router.get("/actions")
def list_actions(user: Annotated[User, Depends(current_user)]) -> list[dict]:
    """PC-14: the action catalog the SDK, UI and agent share."""
    return [{"name": n, "description": (a.fn.__doc__ or "").strip(), "input": a.input.model_json_schema(),
             "role": a.role.name, "destructive": a.destructive} for n, a in ACTIONS.items()]


@router.post("/actions/{name}")
def call_action(
    name: str,
    payload: Annotated[dict, Body()],
    user: Annotated[User, Depends(current_user)],
    request: Request,
    x_abi_confirm: Annotated[str | None, Header()] = None,
):
    CLIENT.set(request.client.host if request.client else "")  # SC-10: set_password counts failures per IP
    try:
        return run(name, payload, user, "rest", x_abi_confirm)
    except ActionFailed as e:
        raise HTTPException(e.status, e.body)
