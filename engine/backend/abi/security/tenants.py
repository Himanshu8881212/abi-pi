"""Tenants (SC-9, D-50): client organisations grouping accounts and projects inside one workspace.

Workspace-owner actions only (no `project` field: workspace scope). A tenant lives in Postgres, not Git, so no import
moves anything between tenants. The membership rule itself is the Postgres trigger in abi.db; actions.run turns its
refusal into a 400."""

import base64
import hashlib
import re
import struct
import zlib
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg import sql
from pydantic import BaseModel, BeforeValidator, Field, PlainSerializer

from abi import db
from abi.actions import ActionFailed, action, current_user, require_confirmation
from abi.project import git
from abi.security import Role, User, role_in

MAX_LOGO = 256 * 1024
MAX_PIXELS = 2048 * 2048  # PNG decompression bomb cap: at most 32 MB of scanlines (D-17)
# what is left after the rebuild must not read as markup to a browser (polyglots, SC-9 logo)
_MARKUP = re.compile(rb"(?i)<(?:\?xml|!doctype|html|svg|script|body|iframe)")
_SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # JPEG frame headers (ITU-T T.81 B.1.1.3)
_JPEG_KEEP = _SOF | {0xC4, 0xCC, 0xDA, 0xDB, 0xDD}  # frame, Huffman/arithmetic/quantisation tables, restart interval, scans
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
_DEPTHS = {0: (1, {1, 2, 4, 8, 16}), 2: (3, {8, 16}), 3: (1, {1, 2, 4, 8}), 4: (2, {8, 16}), 6: (4, {8, 16})}


def _bad(why: str):
    raise ValueError(f"The logo must be a PNG or JPEG image of at most 256 KB: {why}.")


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(b: bytes) -> bytes:
    """Decodes the whole PNG down to its filtered scanlines (W3C PNG 3rd ed.): every chunk's CRC, one IHDR first, IEND
    last with nothing after it, the IDAT stream inflating to exactly the size IHDR implies, valid filter bytes. Returns
    the image rebuilt from IHDR, PLTE, tRNS and freshly compressed scanlines: text and metadata chunks are dropped."""
    keep = []
    i, kinds, idat, hdr = 8, [], [], b""
    while i + 12 <= len(b):
        n, kind = struct.unpack(">I4s", b[i:i + 8])
        if i + 12 + n > len(b) or zlib.crc32(kind + b[i + 8:i + 8 + n]) != struct.unpack(">I", b[i + 8 + n:i + 12 + n])[0]:
            _bad("a damaged PNG chunk")
        kinds.append(kind)
        hdr = b[i + 8:i + 8 + n] if kind == b"IHDR" else hdr
        keep += [b[i:i + 12 + n]] if kind in (b"PLTE", b"tRNS") else []
        idat += [b[i + 8:i + 8 + n]] if kind == b"IDAT" else []
        i += 12 + n
        if kind == b"IEND":
            break
    if not kinds or kinds[0] != b"IHDR" or kinds.count(b"IHDR") > 1 or len(hdr) != 13 or kinds[-1] != b"IEND" or i != len(b) or not idat:
        _bad("not a complete PNG (or data after its end)")
    w, h, depth, ctype, comp, filt, lace = struct.unpack(">IIBBBBB", hdr)
    channels, depths = _DEPTHS.get(ctype, (0, set()))
    if depth not in depths or comp or filt or lace > 1 or (ctype == 3 and b"PLTE" not in kinds):
        _bad("an invalid PNG header")
    if not 0 < w * h <= MAX_PIXELS:
        _bad("at most 2048 × 2048 pixels")
    rows = [(-(-(h - ys) // dy), (-(-(w - xs) // dx) * channels * depth + 7) // 8)
            for xs, ys, dx, dy in (_ADAM7 if lace else ((0, 0, 1, 1),)) if w > xs and h > ys]
    size = sum(n * (1 + width) for n, width in rows)
    d = zlib.decompressobj()
    try:
        raw = d.decompress(b"".join(idat), size + 1)
    except zlib.error:
        _bad("damaged PNG image data")
    if len(raw) != size or not d.eof or d.unused_data:
        _bad("PNG image data of the wrong size")
    pos = 0
    for n, width in rows:
        for _ in range(n):
            if raw[pos] > 4:
                _bad("an invalid PNG row filter")
            pos += 1 + width
    return b[:8] + _chunk(b"IHDR", hdr) + b"".join(keep) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def _jpeg(b: bytes) -> bytes:
    """Walks the whole JPEG (ITU-T T.81 Annex B): length-prefixed segments, a frame header with a size, scans whose
    entropy-coded data holds only stuffed 0xFF bytes and restart markers, EOI as the last two bytes. Returns it rebuilt
    from the segments that make the image (APPn metadata such as Exif/XMP/ICC and comments are dropped; an Adobe APP14
    colour-transform flag stays, it is 12 fixed bytes)."""
    # ponytail: the Huffman data itself is not decoded (needs an image library); malformed scans only render as garbage
    i, frame, scan, out = 2, False, False, [b"\xff\xd8"]
    while True:
        if i + 2 > len(b) or b[i] != 0xFF:
            _bad("a damaged JPEG")
        if b[i + 1] == 0xFF:  # fill byte
            i += 1
            continue
        m, i = b[i + 1], i + 2
        if m == 0xD9:
            if not scan or i != len(b):
                _bad("not a complete JPEG (or data after its end)")
            return b"".join(out) + b"\xff\xd9"
        n = int.from_bytes(b[i:i + 2]) if i + 2 <= len(b) else 0
        if n < 2 or i + n > len(b) or 0xD0 <= m <= 0xD7 or m in (0x01, 0xD8):
            _bad("a damaged JPEG segment")
        start, seg, i = i - 2, b[i + 2:i + n], i + n
        if m in _SOF:
            if frame or len(seg) < 6 or not all(struct.unpack(">HH", seg[1:5])):
                _bad("an invalid JPEG frame header")
            frame = True
        if m == 0xDA:
            if not frame:
                _bad("a JPEG scan before its frame header")
            scan = True
            while (j := b.find(b"\xff", i)) >= 0 and j + 1 < len(b) and (b[j + 1] == 0 or 0xD0 <= b[j + 1] <= 0xD7):
                i = j + 2  # stuffed 0xFF or a restart marker: still entropy-coded data
            if j < 0 or j + 1 >= len(b):
                _bad("a JPEG without its end")
            i = j
        if m in _JPEG_KEEP or (m == 0xEE and n == 14 and seg.startswith(b"Adobe")):
            out.append(b[start:i])


def check_logo(b: bytes) -> tuple[str, bytes]:
    """The logo's content type, from its bytes alone, and the image rebuilt without metadata: PNG or JPEG of at most
    256 KB, decoded as far as the stdlib can, nothing after its end, no markup left (polyglots). Anything else, SVG
    included, raises ValueError."""
    if len(b) > MAX_LOGO:
        _bad(f"this one has {len(b)} bytes")
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, b = "image/png", _png(b)
    elif b.startswith(b"\xff\xd8\xff"):
        kind, b = "image/jpeg", _jpeg(b)
    else:
        _bad("other formats, SVG included, are refused")
    if _MARKUP.search(b):
        _bad("it also contains markup (HTML, SVG or XML)")
    return kind, b


def text_color(color: str) -> str:
    """Black or white, whichever has the higher WCAG 2.1 contrast on `color` (#rrggbb); always ≥ 4.5:1 (1.4.3)."""
    c = [int(color[k:k + 2], 16) / 255 for k in (1, 3, 5)]
    r, g, b = (x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#000000" if (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) else "#ffffff"


def _logo_in(v):
    if not isinstance(v, str | bytes):
        raise ValueError("The logo must be a base64 string.")  # noqa: TRY004  pydantic turns only ValueError into a 422
    if isinstance(v, str) and len(v) > MAX_LOGO * 4 // 3 + 4:  # refuse before decoding a huge string
        _bad("too large")
    b = base64.b64decode(v, validate=True) if isinstance(v, str) else v
    return b and check_logo(b)[1]


# base64 in, the checked and rebuilt image inside; the audit log (SC-6) records its size and hash, not the image
Logo = Annotated[bytes, BeforeValidator(_logo_in),
                 PlainSerializer(lambda b: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}, when_used="json")]


Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")]  # shown in mail headers: one line


class TenantRef(BaseModel):
    id: str = Field(pattern=git.ID)


class Tenant(TenantRef):
    name: Name


@action(Role.owner)
def create_tenant(conn, user: User, args: Tenant) -> dict:
    """Creates a tenant (a client organisation): its accounts and projects are isolated from other tenants'."""
    if conn.execute("INSERT INTO tenants VALUES (%s, %s) ON CONFLICT DO NOTHING", [args.id, args.name]).rowcount == 0:
        raise ActionFailed(409, f"Tenant '{args.id}' already exists.")
    return {"id": args.id}


# a lowercased DNS name (RFC 4343: case-insensitive), matched exactly against the text after an email's last "@"
Domain = Annotated[str, BeforeValidator(lambda v: v.lower() if isinstance(v, str) else v),
                   Field(max_length=253, pattern=r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")]


class TenantBranding(TenantRef):
    name: Name | None = None
    logo: Logo | None = Field(None, description="base64 PNG or JPEG of at most 256 KB; empty removes it")
    color: str | None = Field(None, pattern="^(#[0-9a-fA-F]{6})?$", description="brand colour #rrggbb; empty removes it")
    time_zone: git.TimeZone | None = None  # SE-13: its projects' day boundaries unless project.json sets one
    currency: git.Currency | None = None  # SE-13: its projects' display currency unless project.json sets one
    signup_domains: list[Domain] | None = Field(None, max_length=100, description=(
        "SC-10 self-service sign-up: people with an email at exactly one of these domains may create an account in this"
        " tenant; replaces the list, [] closes sign-up. A domain belongs to one tenant only."))


@action(Role.owner)
def set_tenant(conn, user: User, args: TenantBranding) -> dict:
    """Sets a tenant's display name, branding (SC-9: logo and one brand colour), time zone and display currency
    (SE-13: its projects' default) and sign-up domains (SC-10); an omitted
    field stays as it is. Workspace owner only: a tenant admin could otherwise claim another organisation's domain."""
    sets = {"name": args.name} if args.name else {}
    if args.logo is not None:  # b"": removed; the type comes from the bytes, never from the caller
        sets |= {"logo": args.logo or None, "logo_type": args.logo and check_logo(args.logo)[0] or None}
    if args.color is not None:
        sets["color"] = args.color.lower() or None
    sets |= {k: v or None for k, v in (("time_zone", args.time_zone), ("currency", args.currency)) if v is not None}
    assign = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in [*sets, "id"])  # id = id: no-op
    if not conn.execute(sql.SQL("UPDATE tenants SET {} WHERE id = %s").format(assign), [*sets.values(), args.id, args.id]).rowcount:
        raise ActionFailed(404, f"No tenant '{args.id}'.")
    if args.signup_domains is not None:
        conn.execute("DELETE FROM tenant_domains WHERE tenant = %s", [args.id])
        for d in sorted(set(args.signup_domains)):
            if not conn.execute("INSERT INTO tenant_domains VALUES (%s, %s) ON CONFLICT DO NOTHING", [d, args.id]).rowcount:
                raise ActionFailed(400, f"The domain '{d}' belongs to another tenant; remove it there first.")
    if args.time_zone is not None:  # IN-5/SE-13: its projects' new local days may need calendar rows right away
        from abi.semantic import model  # not at top: semantic imports security

        projects = [r["project"] for r in conn.execute("SELECT project FROM project_tenants WHERE tenant = %s", [args.id])]
        conn.commit()  # extend_calendar reads the zone on its own connection
        for p in projects:
            model.extend_calendars(p, user.name)
    return {}


def branding(conn, *, project: str | None = None, user: User | None = None, tenant: str | None = None) -> dict | None:
    """The branding that applies (SC-9, E-05.md decision 9): the project's tenant, else the account's (or `tenant`'s
    own); None = platform."""
    which = ("(SELECT tenant FROM project_tenants WHERE project = %s)", project) if project else \
        ("(SELECT tenant FROM users WHERE id = %s)", user.id) if user else ("%s", tenant)
    row = conn.execute("SELECT name, color, logo, logo_type FROM tenants WHERE id = " + which[0], [which[1]]).fetchone()
    return row and row | {"text_color": row["color"] and text_color(row["color"])}


router = APIRouter(prefix="/api")


def _branding(user: User, project: str | None) -> dict | None:
    with db.connect() as conn:
        if project and role_in(conn, user, project) is None:  # the same answer as any project without a role
            raise HTTPException(403, f"You have no role in project '{project}'.")
        return branding(conn, project=project, user=user)


Me = Annotated[User, Depends(current_user)]
ProjectQuery = Annotated[str | None, Query(pattern=git.ID)]


@router.get("/branding")
def get_branding(user: Me, project: ProjectQuery = None) -> dict | None:
    """The signed-in account's tenant branding, or with `project` that project's (any role); null = the platform look."""
    b = _branding(user, project)
    return b and {"name": b["name"], "color": b["color"], "text_color": b["text_color"],
                  "logo": b["logo"] and "/api/branding/logo" + (f"?project={project}" if project else "") or None}


@router.get("/branding/logo")
def get_logo(user: Me, project: ProjectQuery = None) -> Response:
    """The logo under the same rule, as the type its bytes were checked to be; never sniffed into anything else."""
    b = _branding(user, project)
    if not (b and b["logo"]):
        raise HTTPException(404, "No logo.")
    return Response(bytes(b["logo"]), media_type=b["logo_type"], headers={
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'", "Cache-Control": "private, no-cache"})


@action(Role.owner, destructive=True)
def delete_tenant(conn, user: User, args: TenantRef) -> dict:
    """Deletes a tenant that has no accounts and no projects left."""
    row = conn.execute("SELECT (SELECT count(*) FROM users WHERE tenant = %(t)s) AS users,"
                       " (SELECT count(*) FROM project_tenants WHERE tenant = %(t)s) AS projects,"
                       " (SELECT count(*) FROM secrets WHERE tenant = %(t)s) AS secrets", {"t": args.id}).fetchone()
    if row["users"] or row["projects"] or row["secrets"]:
        raise ActionFailed(409, f"Tenant '{args.id}' still has {row['users']} account(s), {row['projects']} project(s) and"
                           f" {row['secrets']} secret(s); delete or move them first.")
    if not conn.execute("DELETE FROM tenants WHERE id = %s", [args.id]).rowcount:  # the foreign keys refuse a racing join
        raise ActionFailed(404, f"No tenant '{args.id}'.")
    return {}


@action(Role.owner)
def list_tenants(conn, user: User, args: git.NoInput) -> dict:
    """The tenants with their accounts and project IDs."""
    return {"tenants": conn.execute(
        "SELECT id, name, mfa_required, time_zone, currency, ARRAY(SELECT name FROM users WHERE tenant = t.id ORDER BY 1) AS users,"
        " ARRAY(SELECT project FROM project_tenants WHERE tenant = t.id ORDER BY 1) AS projects,"
        " ARRAY(SELECT domain FROM tenant_domains WHERE tenant = t.id ORDER BY 1) AS signup_domains FROM tenants t ORDER BY id").fetchall()}


class UserTenant(BaseModel):
    name: str
    tenant: str | None = None  # None: a platform account


@action(Role.owner)
def set_user_tenant(conn, user: User, args: UserTenant) -> dict:
    """Moves an account into a tenant, or (tenant omitted) to the platform; it stops being a tenant admin. Refused while
    it holds roles in projects outside its new tenant. Moving it into a tenant that requires two-step sign-in signs out
    its password sessions until it enrols."""
    try:
        n = conn.execute("UPDATE users SET tenant = %s, tenant_admin = false WHERE name = %s", [args.tenant, args.name]).rowcount
    except psycopg.errors.ForeignKeyViolation:
        raise ActionFailed(400, f"No tenant '{args.tenant}'.") from None
    if not n:
        raise ActionFailed(400, f"No user named '{args.name}'.")
    conn.execute(  # D-52: a tenant that requires MFA ends the password sessions of an account without it, moved in too
        "DELETE FROM sessions WHERE oidc_provider IS NULL AND user_id IN (SELECT u.id FROM users u JOIN tenants t"
        " ON t.id = u.tenant WHERE u.name = %s AND t.mfa_required AND u.totp_secret IS NULL)", [args.name])
    conn.execute("DELETE FROM personal_tokens WHERE user_id IN (SELECT u.id FROM users u JOIN tenants t ON t.id = u.tenant"
                 " WHERE u.name = %s AND t.mfa_required AND u.totp_secret IS NULL)", [args.name])  # M4 review: and its tokens
    return {}


class ProjectTenant(BaseModel):
    project: str
    tenant: str | None = None  # None: a platform project


@action(Role.owner, confirms=True)
def set_project_tenant(conn, user: User, args: ProjectTenant) -> dict:
    """Moves a project into a tenant, or (tenant omitted) to the platform. Refused while an account of another tenant
    holds a role there; asks for a confirmation listing the project's roles."""
    if not user.workspace_owner:  # a project Owner passes run()'s check, but tenancy is the workspace owner's
        raise ActionFailed(403, "Only a workspace owner can move a project between tenants.")
    git._existing(args.project)
    try:
        if args.tenant is None:
            conn.execute("DELETE FROM project_tenants WHERE project = %s", [args.project])
        else:
            conn.execute("INSERT INTO project_tenants VALUES (%s, %s) ON CONFLICT (project) DO UPDATE SET tenant = excluded.tenant",
                         [args.project, args.tenant])
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")  # refuse before asking to confirm a move that can't happen
    except psycopg.errors.ForeignKeyViolation:
        raise ActionFailed(400, f"No tenant '{args.tenant}'.") from None
    roles = conn.execute("SELECT u.name, r.role FROM project_roles r JOIN users u ON u.id = r.user_id WHERE r.project = %s"
                         " ORDER BY u.name", [args.project]).fetchall()
    require_confirmation(f"Moving project '{args.project}' to {f'tenant {args.tenant!r}' if args.tenant else 'the platform'}"
                         " changes who may be given roles in it.",
                         {"project": args.project, "tenant": args.tenant, "roles": [f"{r['name']}: {r['role']}" for r in roles]})
    return {}
