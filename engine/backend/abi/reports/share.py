"""View-only share links. PRD RP-6, DECISIONS D-4/D-35 (X-05.6).

A link is possession-based: whoever holds `/#share=<token>` sees one report (or one page) read-only, as its creator.
The token lives only in the URL fragment (never sent to a server or in a Referer) and in the `X-ABI-Share` header of
the share view's calls; Postgres keeps its SHA-256. The routes below reach nothing but the stripped report layout and
`visual_data` (run through `actions.run` as the creator, via="share", without interaction filters), and only while
the link is unexpired, unrevoked and its creator still holds Editor in the project. D-82: never in a project with
row-level security rules (a link shows the creator's unfiltered rows), so creating is refused there, the first rule
revokes every link (`rls.first_rule`), and a link whose project has rules answers 403. Every call on a known link is
audited (`share_view`, link id, never the token) and rate-limited per link in Postgres (D-32).
"""

import base64
import datetime
import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from abi import actions, db, storage
from abi.actions import ActionFailed, action, audit, require_confirmation
from abi.project import ID, imports
from abi.reports import ReportRef
from abi.security import Role, User, _token_hash, limited, role_in, tenants
from abi.semantic import rls

RATE = 300  # calls per minute per link: a page view is one call per visual plus one
RULED = ("Share links are off in projects with row-level security rules: a link shows the report as its creator sees it,"
         " every row included. Share the report with people instead, or give the other site an embed key that runs as a"
         " named Viewer.")


class NewLink(ReportRef):
    page: str | None = Field(None, pattern=ID, description="only this page; default: the whole report")
    days: int = Field(7, ge=1, le=30, description="days until the link stops working")


@action(Role.editor)
def create_share_link(conn, user: User, args: NewLink) -> dict:
    """Makes a view-only link to a report (or one page): anyone holding it sees it read-only, as you, until it expires (1-30 days, default 7) or is revoked. Always asks the person to confirm; the link is then shown once, to the person only: an agent never sees it."""
    if rls.rules(args.project):  # D-82
        raise ActionFailed(422, RULED)
    r = imports.load(args.project, "report", args.report)
    page = next((p for p in r["pages"] if p["id"] == args.page), None)
    if args.page and not page:
        raise ActionFailed(404, f"No page '{args.page}' in report '{args.report}'.")
    expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=args.days)
    what = f"report '{r['name']}'" + (f", page '{page['name']}'," if page else "")
    require_confirmation(f"Anyone with this link can see {what} as you until {expires:%Y-%m-%d %H:%M} UTC.")
    if actions.VIA.get() != "rest":  # SC-4: the link is a credential; it goes to the person's screen, never to an LLM or a trace
        raise ActionFailed(403, "A share link is shown only to you, in the app.")
    token = secrets.token_urlsafe(32)
    row = conn.execute("INSERT INTO share_links (project, report, page, creator, token_hash, expires)"
                       " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                       [args.project, args.report, args.page, user.id, _token_hash(token), expires]).fetchone()
    return {"id": row["id"], "url": f"/#share={token}", "expires": expires.isoformat()}


@action(Role.editor)
def list_share_links(conn, user: User, args: storage.ProjectRef) -> dict:
    """Share links of the project: your own (Owners: everyone's), each with its report, page, creator, created and expiry times and whether it still works. Never the link itself."""
    rows = conn.execute(
        "SELECT l.id, l.report, l.page, u.name AS creator, l.created, l.expires, l.revoked,"
        " l.revoked IS NULL AND l.expires > now() AS active FROM share_links l JOIN users u ON u.id = l.creator"
        " WHERE l.project = %s AND (%s OR l.creator = %s) ORDER BY l.id DESC",
        [args.project, role_in(conn, user, args.project) == Role.owner, user.id]).fetchall()
    links = [{k: v.isoformat() if isinstance(v, datetime.datetime) else v for k, v in r.items()} for r in rows]
    # GATE-9-fix-8.3 (D-22): the server-made ids and times an agent may quote, as rows
    return {"links": links, "columns": ["link", "created", "expires", "revoked"],
            "rows": [[x["id"], x["created"], x["expires"], x["revoked"]] for x in links]}


class LinkRef(storage.ProjectRef):
    id: int


@action(Role.editor)
def revoke_share_link(conn, user: User, args: LinkRef) -> dict:
    """Stops a share link at once (your own; Owners: any link of the project)."""
    row = conn.execute("SELECT creator FROM share_links WHERE id = %s AND project = %s", [args.id, args.project]).fetchone()
    if row is None:
        raise ActionFailed(404, f"No share link {args.id} in project '{args.project}'.")
    if row["creator"] != user.id and role_in(conn, user, args.project) != Role.owner:
        raise ActionFailed(403, "Only the link's creator or a project Owner can revoke it.")
    conn.execute("UPDATE share_links SET revoked = coalesce(revoked, now()) WHERE id = %s", [args.id])
    return {}


router = APIRouter(prefix="/api/share")
GONE = "This link is invalid, expired or revoked."


def _open(token: str | None, visual: dict | None = None) -> tuple[dict, User]:
    """The link of `token` and its creator, or 404 (unknown, expired, revoked, creator below Editor, a visual of
    another page) / 403 (the project has row-level security rules, D-82) / 429 (rate limit). Audited on a known link,
    whatever the outcome; the refusal commits first."""
    with db.connect() as conn:
        link = conn.execute("SELECT l.*, u.name, u.workspace_owner, l.revoked IS NULL AND l.expires > now() AS live"
                            " FROM share_links l JOIN users u ON u.id = l.creator WHERE l.token_hash = %s",
                            [_token_hash(token or "")]).fetchone()
        if link is None:
            raise HTTPException(404, GONE)
        creator = User(link["creator"], link["name"], link["workspace_owner"])
        role = role_in(conn, creator, link["project"])
        if role is None or role < Role.editor:  # D-35: the link dies with the creator's role, and stays dead
            conn.execute("UPDATE share_links SET revoked = coalesce(revoked, now()) WHERE id = %s", [link["id"]])
            link["live"] = False
        other_page = visual is not None and link["page"] not in (None, visual["page"])
        status = (404 if not link["live"] or other_page else 403 if rls.rules(link["project"])  # D-82
                  else 429 if limited(conn, f"share:{link['id']}", RATE, 60) else 200)
        audit(conn, creator, "share", "share_view", link["project"],
              {"link": link["id"], "report": link["report"], **(visual or {})}, status)
    if status != 200:
        raise HTTPException(status, GONE if status == 404 else RULED if status == 403 else "Too many views of this link; try again in a minute.")
    return link, creator


Token = Annotated[str | None, Header(alias="X-ABI-Share")]
LAYOUT = ("id", "type", "title", "text", "layout")


@router.post("/report")
def shared_report(x_abi_share: Token = None) -> dict:
    """The shared report's (or page's) layout only: no queries, filters or measure IDs."""
    link, _ = _open(x_abi_share)
    return layout(link["project"], link["report"], link["page"], None, GONE)


def layout(project: str, report: str, page: str | None, visual: str | None, gone: str) -> dict:
    """A report's layout cut to one page (and one visual), with the project's tenant branding inline; shared with
    RP-9 embeds. 404 `gone` when the report no longer exists."""
    try:
        r = imports.load(project, "report", report)
    except ActionFailed:
        raise HTTPException(404, gone)
    with db.connect() as conn:  # SC-9: the project's tenant branding; no session here, so the logo comes inline
        b = tenants.branding(conn, project=project)
    return {"id": r["id"], "name": r["name"], "pages": [
        {"id": p["id"], "name": p["name"],
         "visuals": [{k: v[k] for k in LAYOUT if k in v} for v in p["visuals"]
                     if visual in (None, v["id"]) and v.get("type") != "form"]}  # 1.2.6 M7: a form card is the app's, never a share's or an export's
        for p in r["pages"] if page in (None, p["id"])],
        "branding": b and {"name": b["name"], "color": b["color"], "text_color": b["text_color"], "logo": b["logo"]
                           and f"data:{b['logo_type']};base64,{base64.b64encode(b['logo']).decode()}"}}


class SharedVisual(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no project, report or interaction filters through a link
    page: str = Field(pattern=ID)
    visual: str = Field(pattern=ID)


@router.post("/visual_data")
def shared_visual(body: SharedVisual, x_abi_share: Token = None) -> dict:
    """`visual_data` of one visual of the shared report, as the creator (their current role is the ceiling)."""
    link, creator = _open(x_abi_share, body.model_dump())
    try:
        d = actions.run("visual_data", {"project": link["project"], "report": link["report"], **body.model_dump()},
                        creator, "share")
    except ActionFailed as e:
        raise HTTPException(e.status, e.body)
    d.pop("edit", None)  # read-only: no write-back table or key names through a link
    return d
