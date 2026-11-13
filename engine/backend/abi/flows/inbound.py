"""Inbound trust boundary shared by every inbound route (Slack, Teams, email, webhooks). PRD SC-7, SC-6, SC-4, AG-5;
DECISIONS D-32, work/validation/X-02.md decisions 1-2.

A route does `call = await receive(request, channel, project)` (64 KB cap while reading, 413 before any parser), then,
in this order: `call.verify(ok)` (constant-time HMAC: `hmac256` + `same`), `call.fresh(ts)` (±5 min), `call.once(key)`
(replay store), `call.resolve_sender(kind, id)` (mapping + 20/h per sender), `call.limit(...)` (e.g. 60/min per webhook
flow), then `call.audit(200)`. Every refusal is audited (`via="inbound"`) with channel, sender, status and the text's
length + sha256 only, never the text or a header value, and answers a fixed message (429: none). Failed unauthenticated
calls count per client IP (30/min); an IP over that gets 429 for its failures, but a correctly signed call is never
refused for its IP's count (H-01.6-fix-1: Slack, Teams and SaaS senders share egress IPs across customers).
"""

import base64
import contextvars
import email.errors
import email.policy
import hashlib
import hmac
import html
import imaplib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qs

import httpx2
from fastapi import APIRouter, HTTPException, Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from abi import actions, db, flows
from abi.actions import ActionFailed, action, audit, require_confirmation
from abi.project import ID, git, imports
from abi.project.git import NoInput
from abi.security import Role, User, credentials, limited, role_in
from abi.storage import PROJECT, ProjectRef
from abi.tools import builtin

BODY_CAP = 64 * 1024
TEXT_CAP = 4000  # Slack's own message limit; routes cut or refuse longer text
WINDOW = 300  # seconds a signed timestamp may differ from ours
IP_FAILS, SENDER_CALLS, FLOW_CALLS = (30, 60), (20, 3600), (60, 60)  # (calls, per seconds)
KINDS = {  # D-32: sender ID format per channel kind
    "slack": r"^[A-Z0-9]{1,30}:[A-Z0-9]{1,30}$",  # team_id:user_id
    "teams": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",  # aadObjectId
    "email": r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{1,190}$",
}
FORMATS = {"slack": "team_id:user_id, e.g. T024BE7LD:U0G9QF9C6", "teams": "the user's aadObjectId (a GUID)", "email": "the email address"}
_MESSAGES = {400: "The body must be a JSON object nested at most 20 deep.", 401: "Signature check failed.", 403: "Not a known sender.", 404: "Not found.", 409: "Already received.", 413: "Request too large."}


def hmac256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def same(expected: str, given: str | None) -> bool:
    """Constant-time comparison of an expected signature with the one a request carries."""
    return hmac.compare_digest(expected.encode(), (given or "").encode())


def normalize(kind: str, external_id: str) -> str:
    return external_id.strip().lower() if kind in ("email", "teams") else external_id.strip()


def purge() -> None:
    """Worker minute tick: replay keys and rate-limit windows older than a day go."""
    with db.connect() as conn:
        conn.execute("DELETE FROM inbound_seen WHERE at < now() - interval '1 day'")
        conn.execute("DELETE FROM inbound_hits WHERE win < now() - interval '1 day'")


def settings(project: str) -> dict:
    """project.json `settings.inbound`; {} for an unknown project or one whose project.json fails its schema."""
    if not re.fullmatch(PROJECT, project):
        return {}
    try:
        doc = json.loads((git.path(project) / "project.json").read_text())
    except (OSError, ValueError):
        return {}
    if best_match(imports._validator("project").iter_errors(doc)):
        return {}
    return doc.get("settings", {}).get("inbound", {})


def secret(project: str, channel: str, field: str) -> bytes | None:
    """A verification secret from project.json `settings.inbound.<channel>.<field>` (`${secret:NAME}`), or None when
    not configured (an unknown project too). Local use only (an HMAC key): it never goes into a request, so no D-2 host
    check applies."""
    ref = settings(project).get(channel, {}).get(field)
    if ref is None:
        return None
    with db.connect() as conn:
        try:
            return credentials.resolve(conn, ref, {}, project).encode()
        except ActionFailed:  # unknown secret: same as not configured
            return None


@dataclass
class Call:
    channel: str
    project: str | None
    ip: str
    body: bytes = b""
    sender: str | None = None
    user: User | None = None
    text: str | None = None  # set it once parsed; until then the audit hashes the body

    def audit(self, status: int, reason: str | None = None) -> None:
        t = (self.text.encode() if self.text is not None else self.body)
        logged = {"channel": self.channel, "sender": self.sender, "ip": self.ip,
                  "length": len(t), "sha256": hashlib.sha256(t).hexdigest()}
        with db.connect() as conn:
            audit(conn, self.user, "inbound", "inbound", self.project, logged, status, reason)

    def refuse(self, status: int, reason: str, failed: bool = False, message: str | None = None):
        """Audit and raise. `failed`: an unauthenticated failure, counted against the client IP (429 once over).
        An IP's 429s are audited once per window (audit_log is never pruned, so a flood must not grow it)."""
        quiet = False
        if failed:
            with db.connect() as conn:
                if limited(conn, f"ip:{self.ip}", *IP_FAILS):
                    status, reason, message = 429, "rate limit ip", None
                    quiet = limited(conn, f"audit-ip:{self.ip}", 1, IP_FAILS[1])
        if not quiet:
            self.audit(status, reason)
        raise HTTPException(status, message or _MESSAGES.get(status))

    def verify(self, ok: bool) -> None:
        if not ok:
            self.refuse(401, "bad signature", failed=True)

    def fresh(self, ts) -> None:
        try:
            ok = abs(time.time() - float(ts)) <= WINDOW
        except (TypeError, ValueError):
            ok = False
        if not ok:
            self.refuse(401, "stale timestamp", failed=True)

    def once(self, key: str) -> None:
        """Replay store: `key` (a signature or message ID) is accepted once."""
        k = hashlib.sha256(f"{self.channel}:{key}".encode()).hexdigest()
        with db.connect() as conn:
            new = conn.execute("INSERT INTO inbound_seen VALUES (%s) ON CONFLICT DO NOTHING", [k]).rowcount
        if not new:
            self.refuse(409, "replay", failed=True)

    def limit(self, key: str, calls: int, per: int) -> None:
        with db.connect() as conn:
            over = limited(conn, key, calls, per)
        if over:
            self.refuse(429, f"rate limit {key.split(':')[0]}")

    def resolve_sender(self, kind: str, external_id: str) -> User:
        """SC-7: the mapped User, or 403. Each sender (known or not) gets 20 calls per hour."""
        external_id = normalize(kind, external_id)[:300]
        self.sender = f"{kind}:{external_id}"
        self.limit(f"sender:{self.sender}", *SENDER_CALLS)
        with db.connect() as conn:
            row = conn.execute("SELECT u.id, u.name, u.workspace_owner FROM channel_senders s JOIN users u ON u.id = s.user_id"
                               " WHERE s.kind = %s AND s.external_id = %s", [kind, external_id]).fetchone()
        if row is None:
            self.refuse(403, "unknown sender")
        self.user = User(**row)
        return self.user


async def receive(request: Request, channel: str, project: str | None) -> Call:
    """Start an inbound call: read at most BODY_CAP bytes (413)."""
    call = Call(channel, project, request.client.host if request.client else "")
    size = request.headers.get("content-length", "")
    if size.isdigit() and int(size) > BODY_CAP:
        await run_in_threadpool(call.refuse, 413, "body too large", True)
    chunks, n = [], 0
    async for chunk in request.stream():
        n += len(chunk)
        if n > BODY_CAP:
            call.body = b"".join(chunks)  # audit hashes what was read, never more
            await run_in_threadpool(call.refuse, 413, "body too large", True)
        chunks.append(chunk)
    call.body = b"".join(chunks)
    return call


# SC-7 sender mapping: workspace-wide, like accounts, so only a workspace owner maps (a project Owner could otherwise
# make an outside ID speak as a user of other projects).

class SenderRef(BaseModel):
    kind: Literal["slack", "teams", "email"]
    external_id: str = Field(min_length=1, max_length=300, description="Sender ID: " + "; ".join(f"{k}: {v}" for k, v in FORMATS.items()))

    @model_validator(mode="after")
    def _format(self):
        self.external_id = normalize(self.kind, self.external_id)
        if not re.fullmatch(KINDS[self.kind], self.external_id):
            raise ValueError(f"external_id is not a {self.kind} sender ID; give {FORMATS[self.kind]}")
        return self


class SenderLink(SenderRef):
    user: str = Field(min_length=1, max_length=100)


@action(Role.owner)
def map_sender(conn, user: User, args: SenderLink) -> dict:
    """Links a Slack/Teams/email sender to a user account; inbound messages from it run with that user's permissions."""
    n = conn.execute("INSERT INTO channel_senders SELECT %s, %s, id FROM users WHERE name = %s"
                     " ON CONFLICT (kind, external_id) DO UPDATE SET user_id = excluded.user_id",
                     [args.kind, args.external_id, args.user]).rowcount
    if not n:
        raise ActionFailed(400, f"No user named '{args.user}'.")
    return {}


@action(Role.owner)
def unmap_sender(conn, user: User, args: SenderRef) -> dict:
    """Removes a sender link; that sender is then refused."""
    if not conn.execute("DELETE FROM channel_senders WHERE kind = %s AND external_id = %s",
                        [args.kind, args.external_id]).rowcount:
        raise ActionFailed(404, "No such sender link.")
    return {}


@action(Role.owner)
def list_senders(conn, user: User, args: NoInput) -> dict:
    """The linked senders and their users."""
    rows = conn.execute("SELECT s.kind, s.external_id, u.name AS user FROM channel_senders s JOIN users u ON u.id = s.user_id"
                        " ORDER BY s.kind, s.external_id").fetchall()
    return {"senders": rows}


# AG-5 (X-02.2): an inbound message is asked of the agent listening on that channel entry, as the mapped sender. Action
# tool calls (D-3) from such a run never execute; each waits in `inbound_approvals` for the sender's click in the app.

def _same_entry(c: dict, entry: dict) -> bool:
    if c.get("type") != entry.get("type"):
        return False
    if c["type"] == "email":
        return (c.get("address") or "").lower() == (entry.get("address") or "").lower() != ""
    return c.get("channel") is not None and c.get("channel") == entry.get("channel")


def listener(project: str, entry: dict) -> tuple[dict, dict]:
    """The one published agent whose `channels` hold `entry` ({type, address|channel}) and its matching channel entry.
    None or several: a plain refusal."""
    agents = [(a, c) for a in imports.objects(project, "agents") if a.get("status") == "published"
              for c in a.get("channels", []) if _same_entry(c, entry)]
    if len({a["id"] for a, _ in agents}) != 1:
        raise ActionFailed(404 if not agents else 409, "No agent answers at this address." if not agents else
                           "More than one agent answers at this address; ask an Owner to fix it.")
    return agents[0]


def ask(project: str, entry: dict, user: User, text: str) -> dict:
    """Run the agent listening on `entry` on `text`, as `user` (`via="inbound"`: grounding forced, action tools queued
    for approval)."""
    agent, _ = listener(project, entry)
    quiet = actions.INBOUND.set(True)  # D-32: the tool calls' audit rows hold strings as length + hash
    try:
        return actions.run("run_agent", {"project": project, "agent": agent["id"], "question": text}, user, "inbound")
    finally:
        actions.INBOUND.reset(quiet)


def listens(project: str, entry: dict) -> bool:
    """An agent (or, wrongly, several: `ask` then refuses plainly) answers at `entry`."""
    try:
        listener(project, entry)
    except ActionFailed as e:
        return e.status != 404
    return True


def message_flows(call: "Call", entries: list[dict], key: str) -> int:
    """FL-2 message trigger: start each enabled flow listening at one of `entries` ({type, address|channel}) once per
    message (`key`), as the sender (§11 #12), its text as {{ data.trigger.text }}. A sender with no role starts none.
    Called only from inbound routes and the mail poll, never inside a flow (D-23). Returns how many started."""
    with db.connect() as conn:
        if role_in(conn, call.user, call.project) is None:
            return 0
    run = "message-" + hashlib.sha256(f"{call.channel}:{key}".encode()).hexdigest()[:32]
    n = 0
    for f in flows._enabled(call.project):
        t = f["trigger"]
        at = {"type": t.get("channel"), "address": t.get("address"), "channel": t.get("channel_id")}
        if t["type"] == "message" and any(_same_entry(at, e) for e in entries):
            n += flows.start(call.project, f["id"], run, {"text": call.text}, by=call.user) is not None
    return n


def defer(project: str, agent: str, run_id: str, name: str, payload: dict, user: User) -> dict:
    """Queue an inbound run's action tool call for `user`'s approval in the app, with a private inbox notice."""
    with db.connect() as conn:
        conn.execute("INSERT INTO inbound_approvals (project, user_id, run_id, agent, action, input) VALUES (%s, %s, %s, %s, %s, %s)",
                     [project, user.id, run_id, agent, name, Jsonb(payload | {"project": project})])
        conn.execute("INSERT INTO inbox (project, sender, text, recipient) VALUES (%s, %s, %s, %s)",
                     [project, agent, (f"Agent '{agent}' asks to run {name} for your message from outside the app;"
                                       " approve or reject it in pending approvals."), user.id])
    return {"status": f"Not run: {name} is queued for {user.name}'s approval in the app. Don't retry it."}


class ApprovalRef(ProjectRef):
    id: int


def _open(conn, user: User, args: ApprovalRef) -> dict:
    """Only the mapped user decides, only by clicking in the app (`via="rest"`); never an agent, a flow or a message."""
    if actions.VIA.get() != "rest":
        raise ActionFailed(403, "Inbound actions are approved only by you, in the app.")
    row = conn.execute("SELECT id, agent, action, input, status FROM inbound_approvals WHERE id = %s AND project = %s"
                       " AND user_id = %s AND status IN ('pending', 'approved')", [args.id, args.project, user.id]).fetchone()
    if row is None:
        raise ActionFailed(404, f"No pending inbound action {args.id} of yours in project '{args.project}'.")
    return row


def _move(id: int, to: str, *was: str) -> bool:
    with db.connect() as conn:  # own transaction: committed before (and whatever happens in) the action's own call
        return bool(conn.execute("UPDATE inbound_approvals SET status = %s WHERE id = %s AND status = ANY(%s)",
                                 [to, id, list(was)]).rowcount)


@action(Role.viewer)
def inbound_approvals(conn, user: User, args: ProjectRef) -> dict:
    """Your pending inbound actions: action tool calls an agent made for your Slack/Teams/email message, waiting for your approval in the app."""
    rows = conn.execute(
        "SELECT id, at, run_id::text, agent, action, input, status FROM inbound_approvals WHERE project = %s AND user_id = %s"
        " AND status IN ('pending', 'approved') ORDER BY id", [args.project, user.id]).fetchall()
    # D-22: the id is the handle approve/reject take and run_id the agent run's own UUID (GATE-9-fix-8.6), so both are
    # quotable; queued inputs and the agent's ID (agent-written, D-114) never ground
    cols = ["id", "at", "run_id", "action", "status"]
    return {"approvals": rows, "columns": cols, "rows": [[r[c] for c in cols] for r in rows]}


@action(Role.viewer)
def approve_inbound_action(conn, user: User, args: ApprovalRef) -> dict:
    """Runs an action an agent queued for your inbound message, as you, after you confirm its preview in the app; the action's own confirmations (first send) still apply."""
    row = _open(conn, user, args)
    if row["status"] == "pending":  # 'approved': confirmed already, the action asked its own confirmation (FL-6)
        require_confirmation(f"Agent '{row['agent']}' asks to run {row['action']} for your message from outside the app.",
                             {"agent": row["agent"], "action": row["action"], "input": row["input"]})
    if not _move(row["id"], "running", "pending", "approved"):  # a double click runs it once
        raise ActionFailed(409, "This inbound action is already being handled.")
    token = actions._call.get()[3]  # the click's token; the action accepts only a token it signed itself
    actions.INBOUND.set(True)  # D-32: the queued input may hold the message text; this row and the action's hash it
    try:
        result = contextvars.copy_context().run(actions.run, row["action"], row["input"], user, "rest", token)
    except Exception:
        _move(row["id"], "approved", "running")  # its own confirmation is next, or retry, or reject
        raise
    _move(row["id"], "done", "running")
    return {"action": row["action"], "result": result}


@action(Role.viewer)
def reject_inbound_action(conn, user: User, args: ApprovalRef) -> dict:
    """Drops an action an agent queued for your inbound message; it never runs."""
    _open(conn, user, args)
    if not _move(args.id, "rejected", "pending", "approved"):
        raise ActionFailed(409, "This inbound action is already being handled.")
    return {}


# FL-2/SC-8 (X-02.3): a webhook-trigger flow runs once per signed call, as its owner (FL-6, D-32). The token is
# app-made, Fernet-encrypted in Postgres (never in Git), shown once, and replaced at once by a rotation.

@action(Role.editor)
def rotate_webhook_token(conn, user: User, args: flows.FlowRef) -> dict:
    """Makes a new signing token for a webhook-trigger flow and shows it once, to the person only (an agent never sees it); the old token stops working at once. Always asks the person to confirm."""
    f = imports.load(args.project, "flow", args.flow)
    if f["trigger"]["type"] != "webhook":
        raise ActionFailed(400, f"Flow '{args.flow}' has no webhook trigger.")
    if f["owner"] != str(user.id) and (role_in(conn, user, args.project) or 0) < Role.owner:  # FL-6: its calls run as the owner
        raise ActionFailed(403, f"Only the flow's owner or a project Owner rotates the webhook token of flow '{args.flow}'.")
    require_confirmation(f"Rotate the webhook token of flow '{f['name']}': calls signed with the current token are refused from now on.")
    if actions.VIA.get() != "rest":  # SC-4: the token goes to the person's screen only, never to an LLM or a flow trace
        raise ActionFailed(403, "A webhook token is shown only to you, in the app.")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO webhook_tokens VALUES (%s, %s, %s) ON CONFLICT (project, flow) DO UPDATE SET token = excluded.token",
                 [args.project, args.flow, credentials._fernet().encrypt(token.encode())])
    return {"token": token, "url": f"/api/hooks/{args.project}/{args.flow}"}  # signing recipe: schemas/v1/flow.json


_DUMMY = secrets.token_bytes(32)  # HMAC key for paths with no webhook flow (H-01.6-fix-4)


def _hook_key(project: str, flow: str) -> bytes | None:
    """The token of an enabled webhook-trigger flow that has one; anything else is None (refused alike)."""
    if not (re.fullmatch(PROJECT, project) and re.fullmatch(ID, flow)):
        return None
    try:
        f = imports.load(project, "flow", flow)
    except (ActionFailed, OSError, ValueError):
        return None
    if not f.get("enabled") or f.get("trigger", {}).get("type") != "webhook":
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT token FROM webhook_tokens WHERE project = %s AND flow = %s", [project, flow]).fetchone()
    return row and credentials.decrypt(row["token"])


def _depth(x) -> int:
    """Nesting depth in objects and arrays ({"a": 1} is 1)."""
    if not isinstance(x, (dict, list)):
        return 0
    return 1 + max(map(_depth, x.values() if isinstance(x, dict) else x), default=0)


def _hook(call: Call, project: str, flow: str, ts: str | None, sig: str | None) -> dict:
    call.sender = f"webhook:{flow}"[:300]
    key = _hook_key(project, flow)
    # H-01.6-fix-4: a path with no webhook flow fails like a bad signature (same status, text and HMAC work), so the
    # URL never tells an anonymous caller which project and flow IDs exist; only the audit detail says why
    signed = same("v1=" + hmac256(key or _DUMMY, f"{ts}.".encode() + call.body).hex(), sig)
    if key is None:
        call.refuse(401, "no webhook flow", failed=True)
    call.verify(signed)
    call.fresh(ts)
    call.once(sig)
    call.limit(f"flow:{project}/{flow}", *FLOW_CALLS)
    try:
        body = json.loads(call.body)
        ok = isinstance(body, dict) and _depth(body) <= 20
    except (ValueError, RecursionError):
        ok = False
    if not ok:
        call.refuse(400, "body not a JSON object of depth <= 20")
    # FL-4: the run key is the signature's hash, so one signed call is one run (the replay store refused it already)
    run = flows.start(project, flow, "webhook-" + hashlib.sha256(sig.encode()).hexdigest()[:32], {"body": body})
    call.audit(200)
    return {"run": run}


router = APIRouter(prefix="/api")


@router.post("/hooks/{project}/{flow}")
async def webhook(project: str, flow: str, request: Request) -> dict:
    """Inbound webhook (FL-2, SC-8): no session; the signature over timestamp + body is the authentication."""
    call = await receive(request, "webhook", project)
    return await run_in_threadpool(_hook, call, project, flow, request.headers.get("x-abi-timestamp"),
                                   request.headers.get("x-abi-signature"))


# AG-5 (X-02.4): Slack slash command. Acked at once (Slack waits 3 s); a worker job asks the agent listening on that
# channel as the mapped user and posts the answer to the command's `response_url`, which must be on hooks.slack.com.

SLACK_REPLY = ("https", "hooks.slack.com", None)  # (scheme, host, port) of every response_url; tests point it at a stub
UNKNOWN = "You're not a known user of this app; ask an Owner to link your {} account."
# a response_url is a credential (SC-4): httpx2's URL logging is off, set by tools/api.py (abi.tools' last import)


def _slack_url(url: str) -> str | None:
    """The host of a response_url we may POST to (SSRF guard, parsed as httpx connects), else None."""
    try:
        u = httpx2.URL(url)
    except httpx2.InvalidURL:
        return None
    return u.host if (u.scheme, u.host, u.port) == SLACK_REPLY and not u.userinfo else None


def _slack(call: Call, project: str, ts: str | None, sig: str | None) -> dict:
    key = secret(project, "slack", "signing_secret")  # unknown project or no secret: refused like a bad signature
    call.verify(key is not None and same("v0=" + hmac256(key, f"v0:{ts}:".encode() + call.body).hex(), sig))
    call.fresh(ts)
    call.once(sig)
    form = parse_qs(call.body.decode(errors="replace"), keep_blank_values=True)
    if any(len(v) > 1 for v in form.values()) or not all(form.get(k, [""])[0] for k in ("team_id", "user_id", "channel_id")):
        call.refuse(400, "not a Slack slash command", message="Not a Slack slash command.")  # signed but malformed: never a 500
    form = {k: v[0] for k, v in form.items()}
    call.text = form.get("text", "")[:TEXT_CAP]
    if not _slack_url(url := form.get("response_url", "")):
        call.refuse(400, "response_url not on hooks.slack.com", message="response_url must be on hooks.slack.com.")
    if (user := _sender(call, "slack", f"{form['team_id']}:{form['user_id']}")) is None:
        return {"response_type": "ephemeral", "text": UNKNOWN.format("Slack")}
    entry = {"type": "slack", "channel": form["channel_id"]}
    if message_flows(call, [entry], sig) and not listens(project, entry):
        call.audit(200)
        return {"response_type": "ephemeral", "text": "Started."}
    _enqueue(call, "answer_slack", user, form["channel_id"], url)
    return {"response_type": "ephemeral", "text": "Working on it…"}


def _sender(call: Call, kind: str, external_id: str) -> User | None:
    """The mapped user; None for an unknown sender due its plain reply (D-32: at most once an hour), else 403."""
    try:
        return call.resolve_sender(kind, external_id)
    except HTTPException as e:
        with db.connect() as conn:
            if e.status_code == 403 and not limited(conn, f"reply:{call.sender}", 1, 3600):
                return None
        raise


def _enqueue(call: Call, task: str, user: User, channel: str, reply_to: str | None, thread: dict | None = None) -> None:
    """Store the accepted message and defer worker job `task` with its ID only (Procrastinate logs job arguments)."""
    with db.connect() as conn:  # committed before the job can start
        id = conn.execute("INSERT INTO inbound_messages (project, user_id, channel, text, reply_to, thread) VALUES"
                          " (%s, %s, %s, %s, %s, %s) RETURNING id",
                          [call.project, user.id, channel, call.text, reply_to, thread and Jsonb(thread)]).fetchone()["id"]
    # not at top: the worker imports every action module, this one included
    from abi import worker

    worker.defer(getattr(worker, task), id=id)
    call.audit(200)


def _take(id: int) -> tuple[dict, User] | tuple[None, None]:
    """Claim an accepted message once: a retry or a second worker never answers twice."""
    with db.connect() as conn:
        m = conn.execute("DELETE FROM inbound_messages m USING users u WHERE m.id = %s AND u.id = m.user_id RETURNING"
                         " m.project, m.channel, m.text, m.reply_to, m.thread, u.id AS uid, u.name, u.workspace_owner", [id]).fetchone()
    return (m, User(m["uid"], m["name"], m["workspace_owner"])) if m else (None, None)


def _sent(user: User, name: str, project: str, logged: dict, text: str, status: int, reason: str | None) -> None:
    """SC-6: an external send, audited without its text (length + sha256) or a URL path."""
    t = text.encode()
    with db.connect() as conn:
        audit(conn, user, "inbound", name, project, logged | {"length": len(t), "sha256": hashlib.sha256(t).hexdigest()},
              status, reason)


@router.post("/inbound/slack/{project}")
async def slack(project: str, request: Request) -> dict:
    """Slack slash command (AG-5, SC-7): no session; Slack's v0 request signature is the authentication."""
    call = await receive(request, "slack", project)
    return await run_in_threadpool(_slack, call, project, request.headers.get("x-slack-request-timestamp"),
                                   request.headers.get("x-slack-signature"))


def _table(data: dict) -> str:
    rows = [r.values() if isinstance(r, dict) else r for r in data["rows"][:50]]
    more = f"\n… {len(data['rows']) - 50} more rows" if len(data["rows"]) > 50 else ""
    return "\n".join(" | ".join(map(str, r)) for r in [data["columns"], *rows]) + more


def answer_slack(id: int) -> None:
    """Worker job: answer one accepted slash command as its mapped user and post it to its response_url."""
    m, user = _take(id)
    if m is None:
        return
    try:
        r = ask(m["project"], {"type": "slack", "channel": m["channel"]}, user, m["text"])
        body = {"response_type": "in_channel", "text": r["answer"] + (f"\n```\n{_table(r['data'])}\n```" if r.get("data") else "")}
    except ActionFailed as e:  # no agent here, no role, LLM down: a plain reply only the sender sees
        body = {"response_type": "ephemeral", "text": e.body["message"]}
    status, reason, host = 200, None, _slack_url(m["reply_to"])
    if host is None:  # checked when accepted too; never POST anywhere else (SSRF)
        status, reason = 403, "response_url not on hooks.slack.com"
    else:
        try:  # httpx doesn't follow redirects by default: a 3xx can't send it elsewhere
            res = httpx2.post(m["reply_to"], json=body, timeout=10)
            if not res.is_success:
                status, reason = 502, f"HTTP {res.status_code}"
        except httpx2.HTTPError as e:
            status, reason = 502, type(e).__name__
    _sent(user, "slack_reply", m["project"], {"channel": "slack", "host": host}, body["text"], status, reason)


# AG-5 (X-02.5): Teams outgoing webhook (@mention in a public channel). Teams waits 5 s for the synchronous reply, too
# short for a grounded run, so the reply is "Working on it" and a worker job posts the answer to the project's outbound
# Teams delivery channel named by the agent's channel entry (`reply`, T-05.1 Workflows webhook). Known limit (D-32): a
# channel post, not the reply chain; a Bot Framework bot is the upgrade path.

_obj = lambda **props: {"type": "object", "properties": props}
TEAMS_ACTIVITY = Draft202012Validator(_obj(
    id={"type": "string", "minLength": 1, "maxLength": 300}, timestamp={"type": "string"}, text={"type": "string"},
    **{"from": _obj(aadObjectId={"type": "string"})}, channelData=_obj(channel=_obj(id={"type": "string"}))
) | {"required": ["id", "timestamp"]})


def _dict_rows(data: dict | None) -> dict | None:
    """deliver_message/deliver_email take dict rows (`builtin._data`); a semantic_query result has list rows."""
    return data and {"columns": data["columns"], "rows": [x if isinstance(x, dict) else dict(zip(data["columns"], x))
                                                          for x in data["rows"]]}


def _teams_text(text: str) -> str:
    """The question without the <at>webhook</at> mention and markup."""
    return html.unescape(re.sub(r"<[^>]*>", " ", re.sub(r"<at>.*?</at>", "", text, flags=re.DOTALL))).strip()


def _teams(call: Call, project: str, authorization: str | None) -> dict:
    key = secret(project, "teams", "hmac_token")  # unknown project or no token: refused like a bad signature
    try:
        key = key and base64.b64decode(key, validate=True)
    except ValueError:
        key = None
    call.verify(bool(key) and same("HMAC " + base64.b64encode(hmac256(key, call.body)).decode(), authorization))
    try:  # a signed but malformed activity: 400, never a 500
        act = json.loads(call.body)
        ok = TEAMS_ACTIVITY.is_valid(act)
        ts = ok and datetime.fromisoformat(act["timestamp"])
        ts = ok and (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).timestamp()  # Bot Framework timestamps are UTC
    except (ValueError, RecursionError):
        ok = False
    if not ok:
        call.refuse(400, "not a Teams message activity", message="Not a Teams message activity.")
    call.fresh(ts)
    call.once(act["id"])
    call.text = _teams_text(act.get("text", ""))[:TEXT_CAP]
    if (user := _sender(call, "teams", act.get("from", {}).get("aadObjectId", ""))) is None:
        return {"type": "message", "text": UNKNOWN.format("Teams")}
    entry = {"type": "teams", "channel": act.get("channelData", {}).get("channel", {}).get("id", "")}
    if message_flows(call, [entry], act["id"]) and not listens(project, entry):
        call.audit(200)
        return {"type": "message", "text": "Started."}
    _enqueue(call, "answer_teams", user, entry["channel"], None)
    return {"type": "message", "text": "Working on it…"}


@router.post("/inbound/teams/{project}")
async def teams(project: str, request: Request) -> dict:
    """Teams outgoing webhook (AG-5, SC-7): no session; the HMAC over the body is the authentication."""
    call = await receive(request, "teams", project)
    return await run_in_threadpool(_teams, call, project, request.headers.get("authorization"))


def answer_teams(id: int) -> None:
    """Worker job: answer one accepted @mention as its mapped user and post it to the agent's outbound Teams channel."""
    m, user = _take(id)
    if m is None:
        return
    entry, text, reply, status, reason = {"type": "teams", "channel": m["channel"]}, "", None, 200, None
    try:
        reply = listener(m["project"], entry)[1]["reply"]  # schemas/v1/agent.json requires it on a teams entry
        if builtin._delivery(m["project"]).get("channels", {}).get(reply, {}).get("type") != "teams":
            raise ActionFailed(400, f"Delivery channel '{reply}' is not a Teams channel of this project.")
        try:
            r = ask(m["project"], entry, user, m["text"])
            text, data = r["answer"], _dict_rows(r.get("data"))
        except ActionFailed as e:  # no role, LLM down: the plain reason, in the channel the question was asked in
            text, data = e.body["message"], None
        with db.connect() as conn:
            builtin.deliver_message(conn, m["project"], reply, text, data, None)
    except ActionFailed as e:  # no agent here, no such channel, secret not allowed for its host, not reached
        status, reason = e.status, e.body["message"]
    _sent(user, "teams_reply", m["project"], {"channel": "teams", "reply": reply}, text, status, reason)


# AG-5/FL-2 (X-02.6): email. The worker reads each project's IMAP mailbox every minute (TLS verified, the password only
# to that host, D-2). A message counts only if the top Authentication-Results header, written by the trusted receiving
# server, shows dmarc=pass for the domain of its one From address, and that address is a linked sender (D-32). The
# agent's answer is a reply in the message's thread; message-trigger flows on its To/Cc addresses run as the sender.

MAIL_CAP = 1024 * 1024
DATE_WINDOW = 12 * 3600  # seconds an email's Date may differ from ours; under purge()'s day, so a replay key outlives it
MESSAGE_ID = re.compile(r"<[^<>\s]{1,250}>")
# RFC 5322 §3.6 single-instance fields + the MIME ones (D-126): Python reads the first copy, DKIM verifies the last
SINGLE = ("Date", "From", "Sender", "Reply-To", "To", "Cc", "Bcc", "Message-ID", "In-Reply-To", "References", "Subject",
          "MIME-Version", "Content-Type", "Content-Transfer-Encoding", "Content-Disposition")
# RFC 6376 §3.2 tag-spec (D-127): FWS = WSP or a fold; VALCHAR without quotes and parens (no comments, no quotes)
WS = r"(?:[ \t]|\r?\n(?=[ \t]))"
TVAL = r"[!#-&*-:<-~]+"
TAG = re.compile(rf"{WS}*+([A-Za-z][A-Za-z0-9_]*){WS}*+={WS}*+((?:{TVAL}(?:{WS}++{TVAL})*+)?){WS}*+")  # possessive: linear, no ReDoS
log = logging.getLogger(__name__)


def poll_email() -> None:
    """Worker minute tick: every project with `settings.inbound.imap`."""
    for project in git.projects():
        if cfg := settings(project).get("imap"):
            try:
                _poll(project, cfg)
            except Exception as e:  # noqa: BLE001 - one mailbox down must not stop the others
                log.error("email inbound for project %s failed: %s", project, credentials.log_text(e))  # SC-4: no message


def _poll(project: str, cfg: dict) -> None:
    used = {}
    try:
        with db.connect() as conn:
            password = credentials.resolve(conn, cfg["password"], used, project)
            credentials.allow(used, cfg["host"].lower())  # D-2, before connecting
    except ActionFailed as e:
        Call("email", project, "imap").audit(e.status, e.body["message"])
        return
    with imaplib.IMAP4_SSL(cfg["host"], cfg.get("port", 993), ssl_context=builtin._tls(), timeout=30) as m:
        m.login(cfg["username"], password)
        m.select(f'"{cfg.get("mailbox", "INBOX")}"')  # the schema allows no quote or backslash in it
        for uid in m.uid("SEARCH", "UNSEEN")[1][0].split()[:50]:  # ponytail: 50 a minute per project; the rest wait a tick
            size = re.search(rb"RFC822\.SIZE (\d+)", m.uid("FETCH", uid, "(RFC822.SIZE)")[1][0] or b"")
            raw = m.uid("FETCH", uid, "(BODY.PEEK[])")[1][0][1] if size and int(size[1]) <= MAIL_CAP else None
            try:
                _email(project, cfg, raw)
            except HTTPException:
                pass  # refused and audited
            finally:  # ponytail: seen even if queuing its answer failed (no poison loop); that sender gets no reply then
                m.uid("STORE", uid, "+FLAGS", "(\\Seen)")


def _dmarc(results: list, authserv: str, sender: str) -> bool:
    """The top Authentication-Results (prepended by the receiving server, RFC 8601 §7.1; any below may be forged) is
    from `authserv` and has dmarc=pass with header.from = the From domain (RFC 7489 §11.2). Comments are dropped."""
    if not results:
        return False
    head, *infos = re.sub(r"\([^()]*\)", " ", str(results[0])).split(";")
    if (head.split() or [""])[0].lower() != authserv.lower():
        return False
    domain = re.escape(sender.rpartition("@")[2])
    return any(re.match(r"\s*dmarc\s*=\s*pass\b", i, re.IGNORECASE) and re.search(rf"\bheader\.from\s*=\s*\"?{domain}\"?(\s|$)", i, re.IGNORECASE)
               for i in infos)


def _signatures(msg) -> set[str]:
    """Replay keys of the DKIM signatures (D-126; a copy keeps the signature bytes): sha256 of each strictly decoded
    `b=` (D-127). A DKIM-Signature outside the RFC 6376 §3.2 tag-list grammar (no comments or quotes, a tag twice in
    any case, `b=`/`bh=` not strict base64 with FWS only) raises ValueError, so there is one reading of every
    accepted signature. Raw header text (no encoded-word decoding). Plus `header.b` of the trusted server's dkim=pass
    results (RFC 6008: its own reading of the signature it verified)."""
    out = set()
    for name, value in msg.raw_items():
        if name.strip().lower() == "dkim-signature":
            *specs, last = value.split(";")
            tags = {}
            for spec in [*specs, last] if last.strip(" \t\r\n") else specs:
                m = TAG.fullmatch(spec)
                if not m or m[1].lower() in tags:  # mailauth lowercases tag names: `B=` is `b=` there
                    raise ValueError("DKIM-Signature")
                tags[m[1].lower()] = re.sub(r"[ \t\r\n]", "", m[2])
            sig = base64.b64decode(tags.get("b", ""), validate=True)  # binascii.Error is a ValueError
            if not sig or not base64.b64decode(tags.get("bh", ""), validate=True):
                raise ValueError("DKIM-Signature")
            out.add("dkim:" + hashlib.sha256(sig).hexdigest())
    top = re.sub(r"\([^()]*\)", " ", str((msg.get_all("Authentication-Results") or [""])[0]))
    for clause in top.split(";"):
        if re.match(r"\s*dkim\s*=\s*pass\b", clause, re.IGNORECASE):
            out |= {f"dkim-b:{b}" for b in re.findall(r"\bheader\.b\s*=\s*\"?([^\s\";]+)", clause, re.IGNORECASE)}
    return out


def _email(project: str, cfg: dict, raw: bytes | None) -> None:
    call = Call("email", project, "imap")
    if raw is None:
        call.refuse(413, "message over 1 MB")
    try:  # hostile input: anything unreadable is a 400, never a crash
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        # RFC 5322 §3.6: one From, and no invalid syntax another parser (DMARC) may read as another address; Python
        # files a second, obsolete `From :` (§4.5) as an mbox line or a message defect, not a header. Obsolete names
        # (`John Q. Public`) and raw UTF-8 names (RFC 6532) stay readable. At most one of each SINGLE header: DKIM
        # verifies the LAST copy of a header (RFC 6376 §5.4.2) while we read the first, so an unsigned one on top lies
        bad = (email.errors.InvalidHeaderDefect, email.errors.MisplacedEnvelopeHeaderDefect, email.errors.MissingHeaderBodySeparatorDefect)
        if not msg["From"] or any(len(msg.get_all(h) or []) > 1 for h in SINGLE) or msg.get_unixfrom() or any(isinstance(d, bad) for d in [*msg.defects, *msg["From"].defects]):
            raise ValueError("From")
        [sender] = msg["From"].addresses
        sender, mid = sender.addr_spec.lower(), str(msg["Message-ID"] or "").strip()
        refs, subject = " ".join(str(msg["References"] or "").split())[:2000], " ".join(str(msg["Subject"] or "").split())[:200]
        to = [a.addr_spec.lower() for h in ("To", "Cc") if msg[h] for a in msg[h].addresses]
        part = msg.get_body(("plain",))
        call.text = (part.get_content() if part else "").strip()[:TEXT_CAP]
        auto = str(msg["Auto-Submitted"] or "no").split(";")[0].strip().lower()
        sent = getattr(msg["Date"], "datetime", None)
        sigs = _signatures(msg)
        ok = bool(MESSAGE_ID.fullmatch(mid))
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        call.refuse(400, "not a readable email with one From and a Message-ID", message="Not a readable email.")
    call.sender = f"email:{sender}"[:300]
    own = (builtin._delivery(project).get("smtp") or {}).get("from", "").lower()
    if auto != "no" or sender == own:  # D-23: the app's own mail (flows, replies) and auto-responders never trigger
        call.refuse(403, "sent by the app or an auto-responder")
    if not _dmarc(msg.get_all("Authentication-Results") or [], cfg["authserv_id"], sender):
        call.refuse(403, "no DMARC pass from the trusted mail server")
    # SC-7/D-32 replay: a signed copy re-sent later is refused by its Date (kept under the replay store's day), one
    # re-sent at once under a new (unsigned) Message-ID by its DKIM signature value (D-126), whatever else changed
    if sent is None or abs(time.time() - (sent if sent.tzinfo else sent.replace(tzinfo=UTC)).timestamp()) > DATE_WINDOW:
        call.refuse(403, "Date missing or more than 12 h from now")
    call.once(mid)
    for sig in sigs:
        call.once(sig)
    user = call.resolve_sender("email", sender)
    entries = [{"type": "email", "address": a} for a in dict.fromkeys(to)]
    started = message_flows(call, entries, mid)
    if (agent := next((e for e in entries if listens(project, e)), None)) is None:
        if not started:
            call.refuse(404, "no agent or flow at this address")
        call.audit(200)
        return
    _enqueue(call, "answer_email", user, agent["address"], sender, {"message_id": mid, "references": refs, "subject": subject})


def answer_email(id: int) -> None:
    """Worker job: answer one accepted email as its mapped user, as a reply in its thread with the result table and
    the agent's chart snapshot; a refusal (no role, LLM down) goes back as its plain reason."""
    m, user = _take(id)
    if m is None:
        return
    data = snapshot = None
    try:
        r = ask(m["project"], {"type": "email", "address": m["channel"]}, user, m["text"])
        text, data, snapshot = r["answer"], _dict_rows(r.get("data")), r.get("snapshot")
    except ActionFailed as e:
        text = e.body["message"]
    t, status, reason = m["thread"], 200, None
    subject = t["subject"] if t["subject"].lower().startswith("re:") else f"Re: {t['subject']}".strip()
    try:
        with db.connect() as conn:
            builtin.deliver_email(conn, m["project"], [m["reply_to"]], subject, text, data, snapshot,
                                  (t["message_id"], t["references"]))
    except ActionFailed as e:  # no SMTP settings, secret not allowed for its host, not reached
        status, reason = e.status, e.body["message"]
    _sent(user, "email_reply", m["project"], {"channel": "email"}, text, status, reason)
