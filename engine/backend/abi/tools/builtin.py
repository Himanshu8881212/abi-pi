"""Built-in tools (PRD §7.1 TL-2): semantic_query and propose_edit live with their areas; refresh, report_snapshot,
send_email and post_message are here. Every built-in is an action, so UI, agents and flows (T-03) share one path.

Transports (T-05): `deliver_email` (SMTP via aiosmtplib) and `deliver_message` (Slack, Teams Workflows, generic webhook
over HTTPS) read the project's `settings.delivery` in project.json; `render_snapshot` (T-05.2) renders the frontend
print view of a report page or visual in one headless Chromium per call (Playwright) to an artifact file.
Every `${secret:NAME}` they use is checked against its allowed hosts before any connection (D-2). Channel `inbox` is
the project's in-app inbox (AT-1, IB-1), stored in Postgres. AT-4: inside a task test run (`flows.TEST_RUN`) both send
actions send nothing out and write one private inbox message to the task owner naming the original targets.
1.2.6 M8: inside a per-object run (`flows.OBJECT`) send_email mails only the account addresses of project members who see
the run's object themselves, and post_message posts only to the inbox, to those members (the trigger's recipients, else
everyone who sees it); each gets their own rows of the attached query or condition, filtered to that object (`_only`,
`_seeing`). A channel, a tool's result, a report and the docs are refused there; so is every published API tool
(tools.run_tool: an agent step's tool call too), a status post (`notify`) and every step outside flows._per_ok.
FL-6: the first send to an email domain or channel not yet confirmed in the project needs a confirmation (D-1 style
409 + token, from any caller); confirmed targets are stored per project. Principle 7: when the agent sends, every
number in its text must be a value of the attached tool or query result (`data`), which goes with the message.
"""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import signal
import ssl
import subprocess
import threading
import time
import uuid
import zipfile
from contextvars import ContextVar
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

import aiosmtplib
import duckdb
import httpx2
import pyarrow as pa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from jsonschema.exceptions import best_match
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from abi import (
    actions,
    db,
    flows,
    ingest,
    reports,
    security,
    semantic,
    storage,
    tools,
    transform,
)
from abi.actions import ActionFailed, action, current_user, require_confirmation
from abi.agents import grounding
from abi.project import bundle, git, imports, sharing
from abi.security import Role, User, credentials, tenants, usage
from abi.semantic import Filter, SemanticQuery, model, ontology, rls
from abi.semantic.subscriptions import Check, evaluate
from abi.storage import ProjectRef

EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
INBOX = "inbox"
_tls = ssl.create_default_context  # tests swap in a context that trusts their self-signed certificate
ARTIFACT = r"^[0-9a-f]{32}\.(png|pdf|pptx|csv|xlsx|zip)$"  # <uuid>.<format> in <ABI_ARTIFACTS_PATH>/<project>/
mimetypes.add_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")  # not built in
mimetypes.add_type("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx")
FORMULA = ("=", "+", "-", "@", "\t", "\r")  # a CSV cell starting with one of these runs as a formula in spreadsheets
# D-24: agent runs set this to a function giving the rendered measures outside the agent's knowledge scope
SCOPE: ContextVar = ContextVar("snapshot_scope", default=None)


def _delivery(project: str) -> dict:
    """project.json `settings.delivery`, checked against schemas/v1/project.json; messages never echo a value (SC-4)."""
    doc = json.loads((git.path(project) / "project.json").read_text())
    if err := best_match(imports._validator("project").iter_errors(doc)):
        raise ActionFailed(400, f"project.json: {imports.json_path(err, 'project')} fails '{err.validator}' in schemas/v1/project.json.")
    return doc.get("settings", {}).get("delivery", {})


def _rows(data: dict) -> list[list]:
    return [list(r.values()) for r in data["rows"]]  # `_data` gives dict rows on both paths


def _table(data: dict | None) -> str:
    """The attached result as plain text, values as returned (principle 7)."""
    return "\n".join(" | ".join(map(str, r)) for r in [data["columns"], *_rows(data)]) if data else ""


def _branded(msg: EmailMessage, brand: dict, text: str) -> None:
    """SC-9 (E-05.md decision 9): an HTML alternative, the tenant's header (brand colour, its WCAG text colour, inline
    logo, name) above the unchanged text. The colour is a checked #rrggbb, so it is safe inside the style."""
    cid = make_msgid()
    logo = f'<img src="cid:{cid[1:-1]}" alt="" height="40" style="vertical-align:middle;margin-right:12px">' if brand["logo"] else ""
    msg.add_alternative(
        f'<!doctype html><html><body style="margin:0;font-family:sans-serif">'
        f'<div style="background:{brand["color"] or "#ffffff"};color:{brand["text_color"] or "#000000"};padding:12px 16px">'
        f'{logo}<strong>{escape(brand["name"])}</strong></div>'
        f'<pre style="font-family:inherit;white-space:pre-wrap;padding:0 16px">{escape(text)}</pre></body></html>', subtype="html")
    if brand["logo"]:
        msg.get_payload()[1].add_related(bytes(brand["logo"]), *brand["logo_type"].split("/"), cid=cid)


def deliver_email(conn, project: str, to: list[str], subject: str, body: str, data: dict | None, snapshot: str | None,
                  thread: tuple[str, str] | None = None, docs: dict[str, str] | None = None) -> None:
    """Send one email over the project's SMTP settings, the result rows as a text table and as data.csv. `thread`
    (Message-ID, References) of the mail it answers puts the reply in that thread (RFC 5322 §3.6.4). `docs`
    (Markdown files by path, DC-6) goes as docs.zip. ABI-PI: a project without its own mail server sends through the
    workspace's (set_account_mail, the app's Settings › Email), with platform secrets only."""
    smtp, scope = _delivery(project).get("smtp"), project
    if not smtp and (row := conn.execute("SELECT smtp FROM account_mail").fetchone()):
        smtp, scope = row["smtp"], None
    if not smtp:
        raise ActionFailed(400, f"Project '{project}' has no email settings: set the workspace's mail server (the app's"
                                " Settings › Email), or add settings.delivery.smtp to project.json.")
    msg = EmailMessage()
    brand = tenants.branding(conn, project=project)  # SC-9: a tenant project's mail carries its tenant's branding
    sender = formataddr((brand["name"], smtp["from"])) if brand else smtp["from"]  # the schema keeps names out of smtp.from
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(to), subject
    # RFC 3834: marks the app's own mail, so it never triggers an inbound run or flow (D-23) nor an auto-responder
    msg["Auto-Submitted"] = "auto-replied" if thread else "auto-generated"
    if thread:
        msg["In-Reply-To"], msg["References"] = thread[0], f"{thread[1]} {thread[0]}".strip()
    text = body + (f"\n\n{_table(data)}\n" if data else "")
    msg.set_content(text)
    if brand:
        _branded(msg, brand, text)
    if data:
        f = io.StringIO()
        csv.writer(f).writerows([data["columns"], *_rows(data)])
        msg.add_attachment(f.getvalue().encode(), maintype="text", subtype="csv", filename="data.csv")
    if snapshot:
        name = snapshot.rsplit("/", 1)[1]
        main, sub = mimetypes.guess_type(name)[0].split("/")
        msg.add_attachment(_artifact(project, name).read_bytes(), maintype=main, subtype=sub, filename=name)
    if docs:
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
            for path, text in docs.items():
                f.writestr(path, text)
        msg.add_attachment(z.getvalue(), maintype="application", subtype="zip", filename="docs.zip")
    send_smtp(conn, smtp, scope, msg, to)
    usage.record(None, "send", project)  # SC-11: one successful delivery (own connection: the send happened)


def smtp_password(conn, smtp: dict, project: str | None) -> tuple[str | None, dict]:
    """The smtp object's password with its secret filled in, and the secrets used; 403 unless they may go to its host
    (D-2), before anything connects (audited by actions.run, SC-6)."""
    used = {}
    password = smtp.get("password") and credentials.resolve(conn, smtp["password"], used, project)
    credentials.allow(used, smtp["host"].lower())
    return password, used


def send_smtp(conn, smtp: dict, project: str | None, msg: EmailMessage, to: list[str]) -> None:
    """Send `msg` over `smtp` (the project.json smtp object; project None = account mail, platform secrets only)."""
    password, used = smtp_password(conn, smtp, project)
    try:
        asyncio.run(aiosmtplib.send(
            msg, sender=smtp["from"], recipients=to, hostname=smtp["host"], port=smtp["port"],
            username=smtp.get("username"), password=password, use_tls=smtp["security"] == "tls",
            start_tls=smtp["security"] == "starttls", tls_context=_tls(), timeout=30))
    except (aiosmtplib.SMTPException, OSError) as e:  # 502: a flow retries it (FL-4)
        raise ActionFailed(502, credentials.scrub(f"Email not sent: {e}", used))


def deliver_message(conn, project: str, channel: str, text: str, data: dict | None, snapshot: str | None) -> None:
    """Post one message to a channel of the project's settings: Slack, Teams (Workflows webhook) or a generic webhook."""
    ch, url, used = _channel(conn, project, channel)
    table = _table(data)
    text += f"\nSnapshot: {link(snapshot)}" if snapshot else ""
    payload = {
        "slack": {"text": text + (f"\n```\n{table}\n```" if data else "")},
        "teams": {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": [{"type": "TextBlock", "text": text, "wrap": True}]
            + ([{"type": "TextBlock", "text": table, "wrap": True, "fontType": "Monospace"}] if data else [])}}]},
        "webhook": {"project": project, "channel": channel, "text": text, "data": data, "snapshot": link(snapshot)},
    }[ch["type"]]
    _post(channel, url, used, json.dumps(payload, default=str).encode())
    usage.record(None, "send", project)  # SC-11


def deliver_status(conn, project: str, channel: str, status: dict) -> None:
    """FL-7: post a flow run's status to a `webhook` channel, signed with the channel's `secret` when it has one:
    X-ABI-Signature: v1=<hex HMAC-SHA256(secret, timestamp + "." + body)> (the SC-8 inbound scheme)."""
    ch, url, used = _channel(conn, project, channel)
    if ch["type"] != "webhook":
        raise ActionFailed(400, f"Channel '{channel}' is not a webhook channel.")
    body, ts = json.dumps(status, default=str).encode(), str(int(time.time()))
    headers = {}
    if "secret" in ch:  # an HMAC key used here only, never sent: no D-2 host check (as the inbound secrets)
        key = credentials.resolve(conn, ch["secret"], {}, project).encode()
        headers = {"X-ABI-Timestamp": ts, "X-ABI-Signature": "v1=" + hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()}
    _post(channel, url, used, body, headers)
    usage.record(None, "send", project)  # SC-11


def _channel(conn, project: str, channel: str) -> tuple[dict, httpx2.URL, dict]:
    """A delivery channel, its URL with secrets filled in, checked against their allowed hosts (D-2), and the secrets used."""
    ch = _delivery(project).get("channels", {}).get(channel)
    if ch is None:
        raise ActionFailed(400, f"No channel '{channel}' in project '{project}': add it to settings.delivery.channels"
                                f" in project.json, or use '{INBOX}'.")
    return ch, *_channel_url(conn, project, channel, ch)


def _channel_url(conn, project: str, channel: str, ch: dict) -> tuple[httpx2.URL, dict]:
    used = {}
    try:
        url = httpx2.URL(credentials.resolve(conn, ch["url"], used, project))
    except httpx2.InvalidURL:
        raise ActionFailed(400, f"Channel '{channel}': its URL is invalid once its secrets are filled in.")
    credentials.allow(used, url.host)  # the host httpx connects to (D-2); 403 audited by actions.run
    return url, used


def _post(channel: str, url: httpx2.URL, used: dict, body: bytes, headers: dict | None = None) -> None:
    try:
        r = httpx2.post(url, content=body, headers={"Content-Type": "application/json", **(headers or {})}, timeout=10, verify=_tls())
    except httpx2.HTTPError as e:
        raise ActionFailed(502, credentials.scrub(f"Channel '{channel}' not reached: {e}", used))
    if not r.is_success:  # the body isn't shown: it may echo the request
        raise ActionFailed(502, f"Channel '{channel}' answered HTTP {r.status_code}.")


def artifacts_root() -> Path:
    return Path(os.environ.get("ABI_ARTIFACTS_PATH", "/data/artifacts"))


def _artifact(project: str, name: str) -> Path:
    return artifacts_root() / project / name


def _new_artifact(project: str, fmt: str, user: User) -> Path:
    """A fresh artifact path, recorded with its creator (SE-6: `artifact` serves it only to them under RLS rules)."""
    name = f"{uuid.uuid4().hex}.{fmt}"
    with db.connect() as conn:
        conn.execute("INSERT INTO artifacts (project, name, creator) VALUES (%s, %s, %s)", [project, name, user.id])
    path = _artifact(project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _file(name: str) -> dict:
    """GATE-9-fix-8.3 (D-22): an artifact's server-made file ID as a row, so an agent quoting its link or name grounds."""
    return {"columns": ["file"], "rows": [[Path(name).stem]]}


def link(snapshot: str | None) -> str | None:
    """The app path serving a snapshot artifact to project members (sent with messages and stored in the inbox)."""
    return snapshot and "/api/artifacts/" + snapshot.removeprefix("artifact:")


RENDERS = threading.BoundedSemaphore(2)  # M3.2: Chromium renders at once in a process (each takes hundreds of MB)
RENDER_TIMEOUT = float(os.environ.get("ABI_RENDER_TIMEOUT", "120"))  # s: a render still drawing then loses its Chromium
DRAWING: set[int] = set()  # the people with a render under way or waiting (one each)
_drawing = threading.Lock()


def render_snapshot(user: User, project: str, report: str, pages: list[str], visual: str | None, filters: list[dict],
                    fmt: str = "png") -> str:
    """Transport (T-05.2): render the frontend print view of report pages (in order, one sheet each, MULTI-EXPORT.1)
    or one visual with `filters` as PNG, PDF or PPTX (MULTI-EXPORT.2: a slide per page) in one headless Chromium, signed in as `user` (FL-6: a flow's owner)
    through a session that exists only for this render. The page reads data only through visual_data (principle 4). Returns `artifact:<project>/<uuid>.<ext>`.
    M3.2: two renders at a time; a third waits for a free one (its session is made after), and one past
    ABI_RENDER_TIMEOUT has its Chromium killed (504). M3 review: a person has one render under way or waiting, and a
    wait ends at ABI_RENDER_TIMEOUT (503, which a flow retries), so no one fills the engine's threads with waits."""
    with _drawing:
        if user.id in DRAWING:
            raise ActionFailed(503, "Your previous export is still being drawn; try again once it is done.")
        DRAWING.add(user.id)
    try:
        if not RENDERS.acquire(timeout=RENDER_TIMEOUT):
            raise ActionFailed(503, "Two exports are being drawn; try again shortly.")
        try:
            return _render(user, project, report, pages, visual, filters, fmt)
        finally:
            RENDERS.release()
    finally:
        with _drawing:
            DRAWING.discard(user.id)


def _deadline(mark: str, late: list) -> threading.Timer:
    """M3.2: at ABI_RENDER_TIMEOUT kill the render's Chromium: the process group Playwright starts it in, found by its
    `mark` argument, so the call under way fails at once (a killed Playwright driver would hang the next call)."""
    def kill():
        late.append(True)
        try:
            for pid in subprocess.run(["pgrep", "-f", "--", mark], capture_output=True, text=True).stdout.split():
                os.killpg(int(pid), signal.SIGKILL)
        except OSError:  # gone meanwhile (or no pgrep: Playwright's own timeouts only)
            pass
    timer = threading.Timer(RENDER_TIMEOUT, kill)
    timer.daemon = True
    timer.start()
    return timer


def _render(user: User, project: str, report: str, pages: list[str], visual: str | None, filters: list[dict],
            fmt: str) -> str:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright  # noqa: PLC0415 (1.2 M4.3: loaded only to render)
    notes = fmt == "pptx" and _notes(user, project, report, pages, visual, filters)  # before the session exists
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:  # committed now: Chromium's requests use other connections
        # sessions last 30 days from `created` (security.user_for_token), so this one lapses after 5 minutes even if
        # `finally` never runs (a killed process)
        conn.execute("INSERT INTO sessions (token_hash, user_id, created) VALUES (%s, %s,"
                     " now() - interval '30 days' + interval '5 minutes')", [security._token_hash(token), user.id])
    q = urlencode({"print": report, "project": project, "pages": ",".join(pages), **({"visual": visual} if visual else {}),
                   "filters": json.dumps(filters)})
    mark, late = f"--abi-render={uuid.uuid4().hex}", []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-dev-shm-usage", mark])  # containers have a 64 MB /dev/shm
            timer = _deadline(mark, late)
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                ctx.add_init_script(f"sessionStorage.setItem('abi.token', {json.dumps(token)})")  # never in a URL
                pg = ctx.new_page()
                pg.goto(os.environ.get("ABI_APP_URL", "http://127.0.0.1:8000") + "/?" + q)
                done = pg.wait_for_selector("[data-ready]", state="attached", timeout=60_000)  # every visual drawn
                if err := done.get_attribute("data-error"):
                    raise ActionFailed(502, f"Snapshot not rendered: {err}")
                if (outside := SCOPE.get()) and (out := outside(json.loads(done.get_attribute("data-measures")))):
                    raise ActionFailed(403, f"Outside your scope: {', '.join(out)}.")  # D-24 on what was drawn
                path = _new_artifact(project, fmt, user)
                if fmt == "pdf":
                    pg.emulate_media(media="screen")
                    # ABI-PI: the charts are drawn for the 1280 px viewport; a portrait Letter sheet (816 px) cut off
                    # their right side. Landscape A4 at 0.8 lays the page out 1309 px wide, so every chart fits.
                    pg.pdf(path=path, print_background=True, tagged=True, outline=True,  # outline: page <h1>s
                           format="A4", landscape=True, scale=0.8, margin={k: "10mm" for k in ("top", "right", "bottom", "left")})
                elif fmt == "pptx":
                    shots = []
                    for i in range(len(pages)):  # each page alone under the brand bar (SC-9), one picture apiece
                        pg.evaluate("i => document.querySelectorAll('section.print-page').forEach((s, j) =>"
                                    " { s.style.display = i === j ? '' : 'none'; })", i)
                        shots.append(pg.screenshot(full_page=True))
                    _pptx(path, [(title, png, text) for (title, text), png in zip(notes, shots)])
                else:
                    pg.screenshot(path=path, full_page=True)
            finally:
                timer.cancel()
                browser.close()
    except PlaywrightError as e:  # 502: a flow retries it (FL-4)
        if late:
            raise ActionFailed(504, f"Snapshot not rendered within {RENDER_TIMEOUT:g} s (ABI_RENDER_TIMEOUT).") from None
        raise ActionFailed(502, f"Snapshot not rendered: {e.message.splitlines()[0]}")
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", [security._token_hash(token)])
    return f"artifact:{project}/{path.name}"


def _notes(user: User, project: str, report: str, pages: list[str], visual: str | None,
           filters: list[dict]) -> list[tuple[str, str]]:
    """(page name, speaker notes) per page: each data visual's measures and active filters from `how_calculated`,
    as `user` (RP-5; tool output only, principle 7; the SQL left out)."""
    r = imports.load(project, "report", report)
    out = []
    with db.connect() as conn:
        for pid in pages:
            p = next(x for x in r["pages"] if x["id"] == pid)
            titles = {v["id"]: v.get("title") or v["id"] for v in p["visuals"]}
            how = reports.how_calculated(conn, user, reports.ExplainRef(
                project=project, report=report, page=pid, visual=visual, interaction=filters))
            out.append((p["name"], "\n\n".join(
                f"{titles[v['visual']]}\nMeasures: " + "; ".join(f"{m['name']} = {m['expression']}" for m in v["measures"])
                + "\nFilters: " + ("; ".join(f"{f['level']}: {f['column']} {f['op']} {json.dumps(f['value'])}"
                                             for f in v["filters"]) or "none")
                for v in how["visuals"])))
    return out


def _pptx(path: Path, slides: list[tuple[str, bytes, str]]) -> None:
    """A 16:9 deck: per slide its title, the page picture fitted below it, and its speaker notes."""
    from pptx import Presentation  # noqa: PLC0415 (1.2 M4.3: loaded only for a deck)
    from pptx.util import Emu, Inches  # noqa: PLC0415
    deck = Presentation()
    deck.slide_width, deck.slide_height = w, h = Emu(12_192_000), Emu(6_858_000)  # 13.33 x 7.5 in
    margin, top = Inches(0.4), Inches(1.3)
    for title, png, notes in slides:
        s = deck.slides.add_slide(deck.slide_layouts[5])  # "Title Only" of the default template
        t = s.shapes.title
        t.text, t.left, t.top, t.width, t.height = title, margin, Inches(0.2), w - 2 * margin, Inches(1)
        pic = s.shapes.add_picture(io.BytesIO(png), 0, top)
        k = min((w - 2 * margin) / pic.width, (h - top - margin) / pic.height)
        pic.width, pic.height = int(pic.width * k), int(pic.height * k)
        pic.left = (w - pic.width) // 2
        s.notes_slide.notes_text_frame.text = notes
    deck.save(path)


def html_pdf(html: str, path: Path) -> None:
    """A self-contained HTML document (DC-3 docs) as a tagged PDF whose outline follows its headings; scripts off."""
    from playwright.sync_api import Error as PlaywrightError, sync_playwright  # noqa: PLC0415 (1.2 M4.3: loaded only to render)
    try:
        with sync_playwright() as p:
            pg = p.chromium.launch(args=["--disable-dev-shm-usage"]).new_page(java_script_enabled=False)
            pg.set_content(html)  # leaving `sync_playwright` closes the browser
            pg.pdf(path=path, tagged=True, outline=True, print_background=True)
    except PlaywrightError as e:
        raise ActionFailed(502, f"PDF not rendered: {e.message.splitlines()[0]}")


def purge_artifacts() -> None:
    """D-12: snapshot files are kept as long as data snapshots (`ABI_SNAPSHOT_RETENTION_DAYS`)."""
    cutoff = time.time() - storage.retention_days() * 86400
    for f in artifacts_root().glob("*/*"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
    with db.connect() as conn:
        conn.execute("DELETE FROM artifacts WHERE created < to_timestamp(%s)", [cutoff])


router = APIRouter(prefix="/api")


@router.get("/artifacts/{project}/{name}")
def artifact(project: str, name: str, user: Annotated[User, Depends(current_user)]) -> FileResponse:
    """A rendered snapshot or export, for members of its project only (SC-2), and only for its creator while the
    project has row-level security rules (SE-6: it holds that user's rows) or the creator has no role there (SH-1: a
    shared report's export); anyone else gets 404, as for a missing file."""
    if not re.fullmatch(storage.PROJECT, project) or not re.fullmatch(ARTIFACT, name):
        raise HTTPException(404, "No such snapshot.")
    with db.connect() as conn:
        member = security.role_in(conn, user, project)
        mine = conn.execute("SELECT 1 FROM artifacts WHERE project = %s AND name = %s AND creator = %s",
                            [project, name, user.id]).fetchone()
        shared = member is None and mine and sharing.shared_in(conn, user, project)  # SH-1: still shared with them
    if not _artifact(project, name).is_file() or (member is None and not shared) or (rls.rules(project) and not mine):
        raise HTTPException(404, "No such snapshot.")
    return FileResponse(_artifact(project, name))


class ToolCall(BaseModel):
    tool: str = Field(pattern=tools.ID, description="a published tool's ID")
    inputs: dict = Field({}, description="the tool's inputs")


class Snapshot(BaseModel):
    report: str = Field(pattern=reports.ID)
    page: str | None = Field(None, pattern=reports.ID, description="one page (or give `pages`)")
    pages: list[Annotated[str, Field(pattern=reports.ID)]] | Literal["all"] | None = Field(
        None, min_length=1, description='several pages in this order, or "all", as ONE PDF (a sheet per page) or'
                                        " PPTX (a slide per page): format pdf or pptx; not with `page`")
    visual: str | None = Field(None, pattern=reports.ID, description="one visual of `page` instead of the whole page")
    filters: list[Filter] = Field([], description="ANDed after the report's own filters")
    format: Literal["png", "pdf", "pptx"] = Field("png", description="pptx: a 16:9 slide per page, speaker notes ="
                                                  " how each visual is calculated")

    @model_validator(mode="after")
    def _pages(self):
        if (self.page is None) == (self.pages is None):
            raise ValueError("Give either `page` or `pages`.")
        if self.visual and self.pages is not None:
            raise ValueError("`visual` goes with `page`, not `pages`.")
        if self.pages is not None and self.format == "png":
            raise ValueError("Several pages make one file: use format 'pdf' or 'pptx'.")
        return self


DATA = Field(None, description="a published tool call {tool, inputs}, a semantic query, or a condition {query, measure,"
             " op, value, ...} as check_condition takes it (its rows, e.g. a % change, are computed by ABI); its result goes with the"
             " message, and when you (the agent) send, every number in your text must be one of its values")


def _data(conn, user: User, project: str, data: ToolCall | SemanticQuery | Check | None) -> dict | None:
    """The attached result, run as the caller (FL-6: owner's permissions), numbers only from tools (principle 7)."""
    if isinstance(data, ToolCall):
        out = tools.run_tool(conn, user, tools.RunTool(project=project, tool=data.tool, inputs=data.inputs))
    elif data:
        q = data.model_dump(mode="json")
        out = evaluate(project, q, user.id) if isinstance(data, Check) else semantic.query(project, q, user.id)
        out["rows"] = [dict(zip(out["columns"], map(semantic.norm, r))) for r in out["rows"]]
    else:
        return None
    return {"columns": out["columns"], "rows": out["rows"]} | {k: out[k] for k in ("filters", "measures") if k in out}


def _grounded(texts: list[str], data: dict | None, project: str | None = None, user: User | None = None) -> None:
    """Principle 7 for agent sends: every number in `texts` is a value of the attached result's rows, as written,
    rounded or a ratio x 100 shown as a percent (AG-2 check). Request parameters (tool inputs, filter values) never
    count (D-22): an agent could otherwise put a figure it computed into a filter and quote it. ABI-PI: a date-table
    filter value that exists in the data does ("revenue in 2025"; grounding.filtered), as in the agent's answers."""
    if not actions.by_agent():
        return  # people write their own words; flow agent steps are grounded by AG-2 (T-02)
    for text in texts:
        found = grounding.filtered(project, user.id, [data]) if data and project and user and "filters" in data else []
        if bad := grounding.check(text, [data or {}, *(x for x in found if isinstance(x, dict))])["numbers"]:
            raise ActionFailed(422, f"The number {bad[0]} isn't in the attached result; numbers come from tools"
                                    " (principle 7): attach the tool call or semantic query that gives it as `data`.")


def _first_send(conn, project: str, targets: set[str], preview: dict | None = None) -> None:
    """FL-6: confirm each email domain, channel or API host ('api:<host>', H-01.5-fix-1) once per project; nothing is
    sent without it. `preview` is signed into the token (D-35: an API tool's whole request shape)."""
    known = {r["target"] for r in conn.execute(
        "SELECT target FROM send_targets WHERE project = %s AND target = ANY(%s)", [project, sorted(targets)])}
    if new := sorted(targets - known):
        require_confirmation(f"First send from project '{project}' to {', '.join(new)}.", preview)
        for t in new:
            conn.execute("INSERT INTO send_targets VALUES (%s, %s) ON CONFLICT DO NOTHING", [project, t])


def _only(project: str, ob: dict, args) -> SemanticQuery | Check | None:
    """1.2.6 M8: what a per-object run attaches, filtered to its object: a semantic query or a condition, with the
    object's key as filters. Refused: a published tool's result, a report, the docs (they can't be filtered to it)."""
    if isinstance(args.data, ToolCall) or args.report or getattr(args, "docs", False):
        raise ActionFailed(400, "An alert for one object attaches only a semantic query or a condition, which is filtered to that"
                                " object for each recipient: leave out the tool result, the report and the docs.")
    got = ontology.keyed(ontology.read(project), model._read(project, "model.json"), ob["type"])
    if got is None:
        raise ActionFailed(409, f"Object type '{ob['type']}' is gone, so this alert can't be filtered to its object: save the flow again.")
    if args.data is None:
        return None
    q = args.data.query if isinstance(args.data, Check) else args.data
    q = q.model_copy(update={"filters": [*q.filters, *(Filter(column=c, op="=", value=v) for c, v in zip(got[1], ob["key"]))]})
    return args.data.model_copy(update={"query": q}) if isinstance(args.data, Check) else q


def _seeing(conn, project: str, ob: dict, where: str, values: list, request: SemanticQuery | Check | None, filled: dict,
            restricted: bool = False) -> list[tuple]:
    """1.2.6 M8 (owner, 6 Nov 2026): the project members matching `where` who see a per-object run's object themselves
    (get_object as each; anyone else gets nothing and learns nothing), each with what is theirs: (the account; the texts:
    an unrestricted member's as the run filled them, a restricted Viewer's the authored text with {{ data.trigger.title }}
    and {{ data.trigger.object.* }} from the object as they see it and every other value masked, D-47; the attached
    request's rows read as them, or None; the object as they see it: type, key, title). `restricted`: restricted Viewers only."""
    raw, out = flows.RAW.get() or {}, []
    for u in conn.execute(f"SELECT id, name, workspace_owner, email FROM users WHERE {where} AND {security.IN_PROJECT} ORDER BY id",
                          [*values, project]).fetchall():
        me = User(u["id"], u["name"], u["workspace_owner"])
        if restricted and not rls.restricted(project, me.id):
            continue
        try:
            got = ontology.get_object(conn, me, ontology.Get(project=project, type=ob["type"], key=ob["key"]))
        except ActionFailed:
            continue
        try:
            own = _data(conn, me, project, request)
        except ActionFailed:
            own = None
        view = {"trigger": {"title": got["title"], "object": ontology.nested(got["columns"], got["rows"][0])}}

        def mine(m: re.Match, view=view) -> str:
            try:
                v = flows._value(m, view) if m[1] == "trigger" and m[2].split(".")[1:2] in (["title"], ["object"]) else None
            except ValueError:
                v = None
            return rls.MARK if v is None or isinstance(v, dict) else str(v)
        texts = filled if not rls.restricted(project, me.id) else {
            k: re.sub(r"[\r\n]+", " ", flows.DATA.sub(mine, raw[k])) if k == "subject" and isinstance(raw.get(k), str)  # one header line
            else flows.DATA.sub(mine, raw[k]) if isinstance(raw.get(k), str) else rls.MARK for k in filled}
        out.append((u, texts, own, {"type": ob["type"], "key": ob["key"], "title": ontology._json(got["title"])}))
    return out


class SendEmail(storage.ProjectRef):
    to: list[Annotated[str, Field(pattern=EMAIL, max_length=254)]] = Field(min_length=1, max_length=50)
    subject: str = Field(min_length=1, max_length=300, pattern=r"^[^\r\n]*$")
    body: str = Field(max_length=20_000)
    data: ToolCall | SemanticQuery | Check | None = DATA
    report: Snapshot | None = Field(None, description="attach a report page or visual snapshot")
    docs: bool = Field(False, description="attach the project's generated documentation (Markdown) as docs.zip")


@action(Role.editor)
def send_email(conn, user: User, args: SendEmail) -> dict:
    """Sends an email, optionally with a tool or query result, a report snapshot and the project's documentation attached. The first send to a domain not yet used in this project asks the user to confirm (FL-6). Every number in subject and body must come from the attached `data`. In a per-object alert's run it reaches only the addresses of project members who see that object, each with their own rows of `data`, filtered to it."""
    ob = flows.OBJECT.get()
    req = _only(args.project, ob, args) if ob else args.data
    data = _data(conn, user, args.project, req)
    _grounded([args.subject, args.body], data, args.project, user)
    if (test := flows.TEST_RUN.get()) is None:
        _first_send(conn, args.project, {"email:" + a.rsplit("@", 1)[1].lower() for a in args.to})  # before any render
    snap = args.report and report_snapshot(conn, user, ReportSnapshot(project=args.project, **args.report.model_dump()))["snapshot"]
    from abi import docs  # circular at import time: docs reads builtin's tool list
    # DC-6/DC-4: generated as the sender (a flow's owner), no secrets; passed only when asked, so transport stubs keep their shape.
    # SE-6: the recipients' rules are unknown (addresses, not users), so under rules the docs carry no data values
    extra = {"docs": docs.package(user, [args.project], False, values=not rls.rules(args.project))["files"]} if args.docs else {}
    with flows.sending():  # FL-4: a run resumed after a crash never sends this twice
        if ob and test is None:  # 1.2.6 M8: one mail each, to the account's own address; an address that names no such member gets none
            # ponytail: a mail server failing part-way fails the step, and its retry sends to the first ones again
            to = [a.lower() for a in args.to]
            got = _seeing(conn, args.project, ob, "email = ANY(%s::text[])", [to], req, {"subject": args.subject, "body": args.body})
            for u, t, own, _ in got:
                deliver_email(conn, args.project, [u["email"]], t["subject"], t["body"], own, None)
            sent = [u["email"] for u, *_ in got]
            return {"sent": True, "to": sent, "skipped": [a for a in to if a not in sent]}
        if test is None:
            deliver_email(conn, args.project, args.to, args.subject, args.body, data, snap, **extra)
        else:
            _inbox(conn, args.project, user, f"Test run: email to {', '.join(args.to)}, subject: {args.subject}\n\n{args.body}", data, snap, test)
    return {"sent": True, "to": args.to}


class PostMessage(storage.ProjectRef):
    channel: str = Field(min_length=1, max_length=200, description="a channel named in the project's delivery settings, or `inbox` for its in-app inbox")
    text: str = Field(min_length=1, max_length=20_000)
    data: ToolCall | SemanticQuery | Check | None = DATA
    report: Snapshot | None = Field(None, description="attach a report page or visual snapshot (sent as a link)")


@action(Role.editor)
def post_message(conn, user: User, args: PostMessage) -> dict:
    """Posts a message to a channel, optionally with a tool or query result. The first post to a channel not yet used in this project asks the user to confirm (FL-6). Every number in the text must come from the attached `data`. In a per-object alert's run only channel inbox works: the post reaches the project members who see that object (the trigger's recipients, if it names any), each with their own rows of `data`, filtered to it."""
    ob = flows.OBJECT.get()
    if ob and args.channel != INBOX:
        raise ActionFailed(400, f"An alert for one object reaches only project members who see it: channel '{args.channel}' has readers"
                                " that can't be checked. Post to channel inbox, or use send_email.")
    req = _only(args.project, ob, args) if ob else args.data
    data = _data(conn, user, args.project, req)
    _grounded([args.text], data, args.project, user)
    test = flows.TEST_RUN.get()
    if args.channel != INBOX and test is None:  # the inbox is internal: no FL-6 confirmation
        _first_send(conn, args.project, {"channel:" + args.channel})  # before any render
    snap = args.report and report_snapshot(conn, user, ReportSnapshot(project=args.project, **args.report.model_dump()))["snapshot"]
    with flows.sending():  # FL-4: a run resumed after a crash never sends this twice
        if test is not None:
            _inbox(conn, args.project, user, f"Test run: message to channel {args.channel}\n\n{args.text}", data, snap, test)
        elif ob:  # 1.2.6 M8: the object goes with the post, so its title opens the object's page
            none = {"columns": [], "rows": []}
            if "members" in ob:  # the people its recipients name
                got = _seeing(conn, args.project, ob, "id = ANY(%s::bigint[])", [ob["members"]], req, {"text": args.text})
            else:  # everyone who sees it: unrestricted members see every object (one post), restricted Viewers a copy each
                _inbox(conn, args.project, user, args.text, (data or none) | {"object": {k: ob[k] for k in ("type", "key", "title")}}, None)
                got = _seeing(conn, args.project, ob, "TRUE", [], req, {"text": args.text}, True) if rls.rules(args.project) else []
            for u, t, own, o in got:
                _inbox(conn, args.project, user, t["text"], (own or none) | {"object": o}, None, u["id"])
        elif args.channel == INBOX:
            _inbox(conn, args.project, user, args.text, data, snap)
            _restricted_copies(conn, args.project, user, _authored(args.text), args.data)
        else:
            deliver_message(conn, args.project, args.channel, args.text, data, snap)
    return {"sent": True, "channel": args.channel}


def _inbox(conn, project: str, sender: User, text: str, data: dict | None, snap: str | None, recipient: int | None = None) -> None:
    """An inbox message for every member, or only for `recipient` (AT-4: a task test run's owner)."""
    conn.execute("INSERT INTO inbox (project, sender, text, data, snapshot, recipient) VALUES (%s, %s, %s, %s, %s, %s)",
                 [project, sender.name, text, data and Jsonb(data), link(snap), recipient])


def _authored(text: str) -> str:
    """D-47: the text a restricted Viewer's inbox copy keeps. In a flow, a post_message step's static template text
    stays and each {{ data.* }} value becomes a marker; anything else a run writes (an agent step's answer) is all data."""
    if not flows.IN_FLOW.get():
        return text  # D-46: a person's own words
    t = flows.TEXT.get()
    return rls.MARK if t is None else flows.DATA.sub(rls.MARK, t)


def _restricted_copies(conn, project: str, sender: User, text: str, request: ToolCall | SemanticQuery | None) -> None:
    """SE-6: an inbox post's rows are the sender's (FL-6), so each restricted Viewer gets a private copy with the same
    request run as them (no rows if they may not run it) and no snapshot link (creator-only under rules, E-02.3)."""
    if not rls.rules(project):
        return
    # ponytail: one read per restricted Viewer at post time; Viewers added later don't get older posts
    for u in conn.execute("SELECT u.id, u.name, u.workspace_owner FROM users u JOIN project_roles r ON r.user_id = u.id"
                          " AND r.project = %s AND r.role = 'viewer' WHERE NOT u.workspace_owner ORDER BY u.id", [project]).fetchall():
        try:
            own = _data(conn, User(**u), project, request)
        except ActionFailed:
            own = None
        _inbox(conn, project, sender, text, own, None, u["id"])


@action(Role.viewer)
def inbox(conn, user: User, args: ProjectRef) -> dict:
    """The project's in-app inbox: messages posted to channel `inbox` and your own task test runs' messages (sender, time, text, attached result), newest first."""
    # ponytail: newest 200 only; page it when an inbox outgrows that
    # SE-6: a restricted Viewer reads only messages addressed to them (their copies of posts carry their own rows)
    msgs = conn.execute("SELECT id, at, sender, text, data, snapshot FROM inbox WHERE project = %s"
                        " AND (recipient = %s OR (recipient IS NULL AND NOT %s)) ORDER BY id DESC LIMIT 200",
                        [args.project, user.id, rls.restricted(args.project, user.id)]).fetchall()
    # D-22: each post's attached rows (tool-made) are quotable, one row per data row; post text never grounds a figure
    # GATE-9-fix-8.6: and its id; the sender (a flow or agent ID: agent-chosen, D-114) stays out
    rows = [[m["id"], m["at"], list(x.values()) if isinstance(x, dict) else x]
            for m in msgs for x in (m["data"] or {}).get("rows") or [None]]
    return {"messages": msgs, "columns": ["id", "at", "row"], "rows": rows}


@action(Role.viewer)
def delivery_channels(conn, user: User, args: ProjectRef) -> dict:
    """The channel names post_message accepts in this project (its delivery settings), besides the built-in `inbox`."""
    return {"channels": sorted(_delivery(args.project).get("channels", {}))}  # names only: URLs hold secret refs (SC-4)


@action(Role.editor)
def delivery_settings(conn, user: User, args: ProjectRef) -> dict:
    """The project's mail server and channels (project.json settings.delivery) as stored: secrets only as ${secret:NAME}."""
    d = _delivery(args.project)
    return {"smtp": d.get("smtp"), "channels": d.get("channels", {})}


class Delivery(ProjectRef):
    smtp: dict | None = Field(None, description=(
        "the mail server send_email uses: {host, port, from, security: starttls|tls|none, username, password:"
        " \"${secret:NAME}\"}; null removes it, omitted keeps it"))
    channels: dict[str, dict] | None = Field(None, description=(
        "every post_message channel by name: {type: slack|teams|webhook, url: \"https://<host>/...${secret:NAME}\","
        " secret (webhook only): \"${secret:NAME}\"}; replaces all channels, null removes them, omitted keeps them"))


def _targets(d: dict) -> dict[str, str]:
    """Where a delivery setting sends: the mail server and each channel URL (secret references, never values)."""
    smtp = d.get("smtp")
    return {**({"smtp": f"{smtp['host']}:{smtp['port']}"} if smtp else {}),
            **{f"channel {n}": c["url"] for n, c in d.get("channels", {}).items()}}


@action(Role.owner, definition=True, confirms=True)
def set_delivery(conn, user: User, args: Delivery) -> dict:
    """Sets the project's mail server (send_email) and channels (post_message) in project.json settings.delivery
    (Owner). Credentials only as ${secret:NAME}, each allowed to go to its host (D-2). A changed destination asks for a
    confirmation showing old and new."""
    f = git.path(args.project) / "project.json"
    doc = json.loads(f.read_text())
    old = doc.get("settings", {}).get("delivery", {})
    new = {k: v for k, v in (old | {k: getattr(args, k) for k in args.model_fields_set - {"project"}}).items() if v}
    doc["settings"] = {k: v for k, v in (doc.get("settings", {}) | {"delivery": new}).items() if k != "delivery" or v}
    imports.check(conn, args.project, bundle.bundle(f.parent) | {"project.json": doc})  # 422: path, never value
    if smtp := new.get("smtp"):
        smtp_password(conn, smtp, args.project)  # 403 unless allowed to the host (D-2)
    for name, ch in new.get("channels", {}).items():
        _channel_url(conn, args.project, name, ch)
        if "secret" in ch:  # an HMAC key, never sent (deliver_status): it only has to exist
            credentials.resolve(conn, ch["secret"], {}, args.project)
    a, b = _targets(old), _targets(new)
    if changes := [{"to": k, "old": a.get(k), "new": b.get(k)} for k in sorted(a | b) if a.get(k) != b.get(k)]:
        require_confirmation(f"Change where project '{args.project}' sends: " + "; ".join(
            f"{c['to']}: {c['old'] or 'none'} → {c['new'] or 'none'}" for c in changes) + ".", {"changes": changes})
    f.write_bytes(bundle.dumps(doc).encode())
    return {"smtp": new.get("smtp"), "channels": new.get("channels", {})}


class ReportSnapshot(Snapshot):
    project: str = Field(pattern=storage.PROJECT)


@action(Role.viewer)
def report_snapshot(conn, user: User, args: ReportSnapshot) -> dict:
    """Renders a report page (or one visual) with filters as a PNG image or a PDF, as you, and returns a reference to the file (`artifact:<project>/<file>`). `pages` (a list of page IDs, or "all") renders several pages in order into one PDF (one sheet per page) or PPTX (one slide per page, speaker notes = how each visual is calculated)."""
    r = imports.load(args.project, "report", args.report)
    pages = [p["id"] for p in r["pages"]] if args.pages == "all" else args.pages or [args.page]
    for p in pages:
        reports._visuals(r, p, args.visual)  # 404 if missing
    snap = render_snapshot(user, args.project, args.report, pages, args.visual,
                           [f.model_dump(mode="json") for f in args.filters], args.format)
    usage.record(conn, "export", args.project, user.id)  # SC-11
    return {"snapshot": snap, **_file(snap)}


class ExportVisual(reports.ReportRef):
    page: str = Field(pattern=reports.ID)
    visual: str = Field(pattern=reports.ID)
    format: Literal["csv", "xlsx"]
    filters: list[Filter] = Field([], description="slicer selections and cross-filters, as in visual_data")


def _cell(x):
    """OWASP CSV injection: a text cell a spreadsheet would read as a formula gets a leading quote."""
    return "'" + x if isinstance(x, str) and x.startswith(FORMULA) else x


@action(Role.viewer)
def export_visual(conn, user: User, args: ExportVisual) -> dict:
    """Exports one report visual's data (its visual_data rows, with the same filters) as a CSV or Excel file and returns the link to download it (`/api/artifacts/<project>/<file>`)."""
    d = reports.visual_data(conn, user, reports.VisualRef(**args.model_dump(exclude={"format", "filters"}), interaction=args.filters))
    if "rows" not in d:
        raise ActionFailed(400, f"Visual '{args.visual}' is text; it has no data to export.")
    head = [d["measures"].get(c) or c.split(".", 1)[1] for c in d["columns"]]
    path = _new_artifact(args.project, args.format, user)
    if args.format == "csv":
        with path.open("w", newline="", encoding="utf-8-sig") as f:  # the BOM makes Excel read UTF-8
            csv.writer(f).writerows([[_cell(x) for x in r] for r in [head, *d["rows"]]])
    else:  # text is written as inline strings in xlsx, never as formulas
        cols = list(zip(*d["rows"])) or [[] for _ in head]
        with duckdb.connect() as con:  # only the file writer: the rows came from the semantic engine
            con.execute("LOAD excel")
            con.register("rows", pa.Table.from_arrays([pa.array(c) for c in cols], names=head))
            con.execute(f"COPY rows TO '{storage._lit(str(path))}' (FORMAT xlsx, HEADER true)")
    usage.record(conn, "export", args.project, user.id)  # SC-11
    return {"link": link(f"artifact:{args.project}/{path.name}"), **_file(path.name)}


class TableRef(storage.ProjectRef):
    table: str = Field(pattern=ingest.TABLE)


@action(Role.editor)
def refresh(conn, user: User, args: TableRef) -> dict:
    """Reloads a table from its source now (a new snapshot), then the tables that read it. On failure the previous snapshot stays live and the notification feed says why. A big table (millions of rows) answers at once with a run ID and reloads in the background: load_runs says when it is done."""
    if args.table not in (steps := transform.tables(args.project)) and not (git.path(args.project) / "sources" / f"{args.table}.json").exists():
        raise ActionFailed(404, f"No table '{args.table}' with a source or steps in project '{args.project}'.")
    try:  # M3.2: a source that fails the probe fails below, as in 1.1 (502, the feed says why)
        first = steps[args.table][0] if args.table in steps else None
        rows = ingest._big_built(args.project, first.table) if first and first.table else ingest._big(
            conn, ingest._source(args.project, first.source if first else args.table, args.table))
    except ActionFailed:
        rows = None
    if rows:
        return ingest.queue(conn, user, "refresh", args, rows)
    if failed := ingest.refresh(conn, args.project, set(), user.name, reload={args.table}):
        # 502, the source failed: a flow retries it (FL-4), the file may be back
        raise ActionFailed(502, "Refresh failed: " + " ".join(f"'{t}': {e}" for t, e in failed.items()))
    return storage.made(storage.snapshot_id(storage.lake(args.project)), table=args.table)  # ST-3: the id to restore before


SENT = {"type": "object", "properties": {"sent": {"const": True}}}
BUILTIN_TOOLS = {  # TL-2: name -> output JSON Schema; inputs come from the action's model
    "semantic_query": {"type": "object", "properties": {"columns": {"type": "array", "items": {"type": "string"}},
                       "measures": {"type": "object"}, "rows": {"type": "array", "items": {"type": "array"}}}},
    "report_snapshot": {"type": "object", "properties": {"snapshot": {"type": "string"}}},
    "send_email": SENT,
    "post_message": SENT,
    "refresh": {"type": "object", "properties": {"table": {"type": "string"}, "snapshot": {"type": "integer"}}},
    "propose_edit": {"type": "object", "properties": {"batch": {"type": "string"}, "status": {"const": "proposed"},
                                                      "changes": {"type": "array"}}},
}
