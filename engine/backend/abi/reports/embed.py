"""Embedded reports, pages and visuals. PRD RP-9 (work/validation/E-08.md, E-08.1.md).

A project Owner creates an embed key bound to 1-10 origins; it is shown once and stored Fernet-encrypted. The host
app's backend signs short-lived HS256 tokens with it (header `kid`) naming the project, its tenant, one Viewer account
of the project and one report (optionally a page, or a visual of that page). The iframe loads
`/embed/<kid>#embed=<token>`: that page carries `Content-Security-Policy: frame-ancestors <origins>`, and the token goes
only in the `X-ABI-Embed` header of the two calls below. Every read is `visual_data` run as that Viewer (via="embed"),
so the Viewer's RLS rules, the semantic engine and SC-11 metering apply unchanged; nothing here writes, exports or
explains. Every call naming a known key is audited (`embed_view`, never the token; refusals up to REFUSED per key and minute)
and rate-limited per (key, viewer).
"""

import re
import secrets
import time
from typing import Annotated

import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from abi import actions, db, storage
from abi.actions import ActionFailed, action, audit, require_confirmation
from abi.project import ID
from abi.reports.share import SharedVisual, layout
from abi.security import Role, User, limited, role_in
from abi.security.credentials import _fernet, decrypt

RATE = 300  # calls per minute per (key, viewer), as share links: a view is one call per visual plus one
REFUSED = (10, 60)  # H-01.2-fix-4: refused calls audited per key per minute; the kid is public, so more go unaudited
DAY, LEEWAY = 24 * 3600, 30  # decision 2: exp at most 24 h ahead, 30 s clock skew
KID = re.compile(r"[0-9a-f]{16}")
# decision 3: https origins; http only for local development. No paths, wildcards or spaces (they go into a CSP header).
ORIGIN = r"^(https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?|http://(localhost|127\.0\.0\.1))(:[0-9]{1,5})?$"
# the key's creator is still an Owner of its project (D-35: the key dies with the role)
OWNER = ("(u.workspace_owner OR EXISTS (SELECT 1 FROM project_roles r"
         " WHERE r.user_id = k.creator AND r.project = k.project AND r.role = 'owner'))")


class NewKey(storage.ProjectRef):
    name: str = Field(min_length=1, max_length=100, description="what the key is for, e.g. the host site")
    origins: list[Annotated[str, Field(pattern=ORIGIN)]] = Field(
        min_length=1, max_length=10, description="origins whose pages may frame embeds, e.g. https://app.example.com")


@action(Role.owner)
def create_embed_key(conn, user: User, args: NewKey) -> dict:
    """Not for an agent: answers that embedding isn't available here. From ABI's own screens: makes a signing key for embedding this project's reports in other sites (the host's backend signs short-lived tokens with it naming one of the project's Viewers, whose view the embed shows), after a confirmation."""
    if actions.VIA.get() != "rest":  # before anything is asked. SC-4: the key is a credential; and in this install the embedded
        # page is served by the engine only, which the address people open ABI at does not reach (RELEASE.md, known limits)
        raise ActionFailed(403, "Embedding a report in another site isn't available in this install. A workspace owner can use Share on"
                                " the report instead: the people they pick open it after signing in.")
    origins = sorted(set(args.origins))
    require_confirmation(f"Pages on {', '.join(origins)} can show this project's reports as any of its Viewers, to"
                         " whoever the host site chooses, with tokens signed by this key, until you revoke it.")
    kid, key = secrets.token_hex(8), secrets.token_urlsafe(32)
    conn.execute("INSERT INTO embed_keys (kid, project, name, origins, creator, key) VALUES (%s, %s, %s, %s, %s, %s)",
                 [kid, args.project, args.name, origins, user.id, _fernet().encrypt(key.encode())])
    return {"kid": kid, "key": key, "url": f"/embed/{kid}"}


@action(Role.owner)
def list_embed_keys(conn, user: User, args: storage.ProjectRef) -> dict:
    """Embed keys of the project: ID, name, allowed origins, creator, created and revoked times and whether it still works. Never the key itself."""
    rows = conn.execute(f"SELECT k.kid, k.name, k.origins, u.name AS creator, k.created, k.revoked,"
                        f" k.revoked IS NULL AND {OWNER} AS active FROM embed_keys k JOIN users u ON u.id = k.creator"
                        " WHERE k.project = %s ORDER BY k.created DESC", [args.project]).fetchall()
    keys = [r | {"created": r["created"].isoformat(), "revoked": r["revoked"] and r["revoked"].isoformat()} for r in rows]
    # GATE-9-fix-8.3 (D-22): the server-made ids and times an agent may quote, as rows
    return {"keys": keys, "columns": ["kid", "created", "revoked"], "rows": [[k["kid"], k["created"], k["revoked"]] for k in keys]}


class KeyRef(storage.ProjectRef):
    kid: str = Field(pattern=KID.pattern)


@action(Role.owner)
def revoke_embed_key(conn, user: User, args: KeyRef) -> dict:
    """Stops an embed key at once: every token it signed stops working and its pages can no longer be framed."""
    if conn.execute("UPDATE embed_keys SET revoked = coalesce(revoked, now()) WHERE kid = %s AND project = %s RETURNING kid",
                    [args.kid, args.project]).fetchone() is None:
        raise ActionFailed(404, f"No embed key '{args.kid}' in project '{args.project}'.")
    return {}


router = APIRouter()
GONE = "This embed is invalid, expired or revoked."


def _key(conn, kid) -> dict | None:
    """Key `kid` with `live` (unrevoked, creator still Owner), or None when unknown. A key whose creator lost the Owner
    role is revoked for good (D-35), so a later promotion doesn't bring its tokens back."""
    if not isinstance(kid, str) or not KID.fullmatch(kid):
        return None
    k = conn.execute(f"SELECT k.*, k.revoked IS NULL AS live, {OWNER} AS owner"
                     " FROM embed_keys k JOIN users u ON u.id = k.creator WHERE k.kid = %s", [kid]).fetchone()
    if k and k["live"] and not k["owner"]:
        conn.execute("UPDATE embed_keys SET revoked = now() WHERE kid = %s", [kid])
        k["live"] = False
    return k


def frame_ancestors(kid: str) -> str:
    """CSP for the embed page of key `kid`: only its origins may frame it; nobody for an unknown or dead key."""
    with db.connect() as conn:
        k = _key(conn, kid)
    return "frame-ancestors " + (" ".join(k["origins"]) if k and k["live"] else "'none'")


class Claims(BaseModel):
    """The signed claims, exactly (decisions 4-6): no other claim is accepted."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    tenant: str | None  # required, may be null (a platform project)
    viewer: int  # account ID of a Viewer of the project
    report: str = Field(pattern=ID)
    page: str | None = Field(None, pattern=ID)
    visual: str | None = Field(None, pattern=ID)
    iat: float
    exp: float


def _verify(conn, k: dict, token: str) -> tuple[Claims | None, User | None]:
    """The token's claims and viewer when its signature (HS256 only) and claims hold, else what could be trusted."""
    try:  # pins the algorithm: `none` and RS/HS confusion fail here (RFC 8725 §3.1); exp and a future iat too
        c = Claims.model_validate(jwt.decode(token, decrypt(k["key"]).decode(), algorithms=["HS256"],
                                             options={"require": ["exp", "iat"]}, leeway=LEEWAY))
    except (jwt.PyJWTError, ValidationError):
        return None, None
    row = conn.execute("SELECT id, name, workspace_owner, tenant, (SELECT tenant FROM project_tenants WHERE project = %s)"
                       " AS project_tenant FROM users WHERE id = %s", [k["project"], c.viewer]).fetchone()
    viewer = row and User(row["id"], row["name"], row["workspace_owner"])
    ok = (row is not None and c.project == k["project"] and (c.visual is None or c.page is not None)
          and c.exp - c.iat <= DAY and c.exp <= time.time() + DAY + LEEWAY
          and c.tenant == row["tenant"] == row["project_tenant"]  # decision 5: one tenant throughout (null: platform)
          and role_in(conn, viewer, c.project) == Role.viewer)  # decision 4: exactly a Viewer, not via SH-1 sharing
    return (c if ok else None), viewer


def _open(token: str | None, visual: dict | None = None) -> tuple[Claims, User]:
    """The verified claims and viewer of `token`, or 404 (anything wrong, all alike) / 429 (rate limit). Audited when
    the token names a known key, whatever the outcome (refusals up to REFUSED); the refusal commits first."""
    try:
        kid = jwt.get_unverified_header(token or "").get("kid")
    except jwt.PyJWTError:
        kid = None
    with db.connect() as conn:
        k = _key(conn, kid)
        if k is None:
            raise HTTPException(404, GONE)
        c, viewer = _verify(conn, k, token) if k["live"] else (None, None)
        reached = c is not None and (visual is None or (c.page in (None, visual["page"]) and c.visual in (None, visual["visual"])))
        status = 404 if not reached else 429 if limited(conn, f"embed:{k['kid']}:{c.viewer}", RATE, 60) else 200
        what = c and {"report": c.report, **{n: getattr(c, n) for n in ("page", "visual") if getattr(c, n)}}
        if status == 200 or not limited(conn, f"embed-refused:{k['kid']}", *REFUSED):
            audit(conn, viewer, "embed", "embed_view", k["project"], {"key": k["kid"], **(what or {}), **(visual or {})}, status)
    if status != 200:
        raise HTTPException(status, GONE if status == 404 else "Too many views of this embed; try again in a minute.")
    return c, viewer


Token = Annotated[str | None, Header(alias="X-ABI-Embed")]


@router.post("/api/embed/report")
def embedded_report(x_abi_embed: Token = None) -> dict:
    """The token's report (or page, or visual) layout with the tenant branding: no queries, filters or measure IDs."""
    c, _ = _open(x_abi_embed)
    return layout(c.project, c.report, c.page, c.visual, GONE)


@router.post("/api/embed/visual_data")
def embedded_visual(body: SharedVisual, x_abi_embed: Token = None) -> dict:
    """`visual_data` of one visual the token reaches, as its Viewer (their RLS rules apply)."""
    c, viewer = _open(x_abi_embed, body.model_dump())
    try:
        d = actions.run("visual_data", {"project": c.project, "report": c.report, **body.model_dump()}, viewer, "embed")
    except ActionFailed as e:
        raise HTTPException(e.status, e.body)
    d.pop("edit", None)  # read-only: no write-back table or key names through an embed
    return d
