"""Local accounts (SC-1) and per-project roles (SC-2) as actions, plus first-run setup and login."""

import hashlib
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr

from abi import db, security
from abi.actions import ActionFailed, action, audit, current_user, require_confirmation
from abi.project import SCHEMA, git
from abi.project.bundle import dumps
from abi.project.imports import ROLES
from abi.security import Role, User, oidc


class Credentials(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=8, max_length=1024)


class NewUser(Credentials):
    workspace_owner: bool = False
    tenant: str | None = None  # the account's tenant (SC-9); a tenant admin's new accounts always get its own


class RoleRef(BaseModel):
    project: str = Field(min_length=1)
    user: str


class RoleAssignment(RoleRef):
    role: Literal["viewer", "editor", "owner"]


class UserRef(BaseModel):
    name: str


def _find(conn, user: User, name: str, lock: str = ""):
    """The account named `name` as the caller may see it: a tenant account sees its own tenant's only, so a name of
    another tenant gets the same answer as an unknown one (SC-9)."""
    t = security.tenant_of(conn, user)
    return conn.execute(f"SELECT id, password_hash FROM users WHERE name = %s AND (%s::text IS NULL OR tenant = %s) {lock}",
                        [name, t, t]).fetchone()


@action(Role.owner, tenant_admin=True)
def create_user(conn, user: User, args: NewUser) -> dict:
    """Creates a local account (in a tenant with `tenant`; a tenant admin's accounts join its own tenant)."""
    return {"id": new_account(conn, user, args.name, security.hash_password(args.password.get_secret_value()),
                              args.tenant, args.workspace_owner)}


def new_account(conn, user: User, name: str, password_hash: str, tenant: str | None, owner: bool = False,
                email: str | None = None) -> int:
    """Inserts an account as `user` may create it (SC-9): a tenant admin only in its own tenant, never an owner."""
    if not user.workspace_owner:  # a tenant admin (actions.run let it in)
        if owner or tenant not in (None, mine := security.tenant_of(conn, user)):
            raise ActionFailed(403, "A tenant admin creates accounts of its own tenant only, and no workspace owners.")
        tenant = mine
    try:
        return conn.execute(
            "INSERT INTO users (name, password_hash, workspace_owner, tenant, email) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            [name, password_hash, owner, tenant, email]).fetchone()["id"]
    except psycopg.errors.UniqueViolation:  # accepted leak (D-50): names and emails are unique per install
        raise ActionFailed(400, f"An account named '{name}' or with that email already exists.") from None
    except psycopg.errors.ForeignKeyViolation:
        raise ActionFailed(400, f"No tenant '{tenant}'.") from None


@action(Role.owner, definition=True, tenant_admin=True)
def set_role(conn, user: User, args: RoleAssignment) -> dict:
    """Gives a user a role in a project, replacing any role they had there."""
    no_user = ActionFailed(400, f"No user named '{args.user}'.")
    if not (target := _find(conn, user, args.user)):
        raise no_user
    try:
        conn.execute("INSERT INTO project_roles VALUES (%s, %s, %s) ON CONFLICT (user_id, project) DO UPDATE SET role = excluded.role",
                     [target["id"], args.project, args.role])
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")  # the SC-9 membership trigger, now
    except psycopg.errors.DatabaseError as e:
        if e.sqlstate == "AB901":  # another tenant's account: answered as an unknown name (no probing)
            raise no_user from None
        raise
    _write_roles(conn, args.project)
    return {}


@action(Role.owner, definition=True, destructive=True, tenant_admin=True)
def remove_role(conn, user: User, args: RoleRef) -> dict:
    """Removes a user's role in a project, so they no longer have access to it."""
    target = _find(conn, user, args.user)
    if not (target and conn.execute("DELETE FROM project_roles WHERE project = %s AND user_id = %s",
                                    [args.project, target["id"]]).rowcount):
        raise ActionFailed(400, f"'{args.user}' has no role in project '{args.project}'.")
    _write_roles(conn, args.project)
    return {}


def _write_roles(conn, project: str) -> None:
    """D-11: security/roles.json mirrors project_roles (Postgres stays the source of truth, D-1)."""
    rows = conn.execute("SELECT user_id, role FROM project_roles WHERE project = %s", [project])
    f = git.path(project) / ROLES
    f.parent.mkdir(exist_ok=True)
    f.write_text(dumps({"$schema": SCHEMA + "roles.json"} | {str(r["user_id"]): r["role"] for r in rows}))


@action(Role.owner, destructive=True, tenant_admin=True)
def delete_user(conn, user: User, args: UserRef) -> dict:
    """Deletes the account with its sessions and project roles."""
    if args.name == user.name:
        raise ActionFailed(400, "You can't delete your own account.")  # keeps at least one workspace owner
    if not (target := _find(conn, user, args.name)):
        raise ActionFailed(400, f"No user named '{args.name}'.")
    projects = [r["project"] for r in conn.execute("DELETE FROM project_roles WHERE user_id = %s RETURNING project", [target["id"]])]
    from abi.agent import chatgpt  # not at top: the agent package imports security

    chatgpt.revoke(conn, target["id"])  # D-53: the link's tokens are revoked at OpenAI, not only dropped
    conn.execute("DELETE FROM users WHERE id = %s", [target["id"]])
    for p in projects:  # D-11: one undoable definition change per project whose roles.json lists the user
        if (git.path(p) / ROLES).exists():
            with git.change(p, user, "delete_user"):
                _write_roles(conn, p)
    return {}


class NewPassword(BaseModel):
    password: SecretStr = Field(min_length=8, max_length=1024)
    current: SecretStr | None = None  # your current password, when changing your own
    name: str | None = None  # another account's (workspace owner, or its tenant admin); omitted: your own


@action(Role.viewer)
def set_password(conn, user: User, args: NewPassword) -> dict:
    """Changes a password: your own with your current one, or (workspace owner, tenant admin in its tenant) another
    account's. Signs that account out everywhere. Never an agent or flow tool: it takes a secret (SC-4)."""
    name = args.name or user.name
    if name != user.name and not (user.workspace_owner or security.admin_of(conn, user, None)):
        raise ActionFailed(403, "Only a workspace owner or a tenant admin can set another account's password.")
    new = args.password.get_secret_value()
    if name == user.name:
        cur = args.current and args.current.get_secret_value()
        try:  # checked before the row lock below: a parallel login's session insert waits on that lock (deadlock)
            ok = security.attempt(name, security.CLIENT.get(), lambda c: cur and security.verify_password(
                cur, c.execute("SELECT password_hash FROM users WHERE id = %s", [user.id]).fetchone()["password_hash"]))
        except security.Throttled:
            raise ActionFailed(429, security.TOO_MANY) from None
        if not ok:
            raise ActionFailed(403, "Your current password is wrong.")
    row = _find(conn, user, name, "FOR UPDATE")
    if row is None:
        raise ActionFailed(400, f"No user named '{name}'.")
    if name != user.name:  # the preview signs the password, so a token confirms only the one the owner typed
        require_confirmation(f"This sets '{name}''s password and signs them out everywhere.",
                             {"name": name, "password_sha256": hashlib.sha256(new.encode()).hexdigest()[:12]})
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", [security.hash_password(new), row["id"]])
    conn.execute("DELETE FROM sessions WHERE user_id = %s", [row["id"]])
    conn.execute("DELETE FROM account_tokens WHERE user_id = %s", [row["id"]])  # SC-10: older reset/invite links die
    return {}


class OwnerFlag(UserRef):
    on: bool


@action(Role.owner, destructive=True)
def set_workspace_owner(conn, user: User, args: OwnerFlag) -> dict:
    """Makes an account a workspace owner (Owner in every project) or takes that away; one owner always stays."""
    owners = {r["name"] for r in conn.execute("SELECT name FROM users WHERE workspace_owner FOR UPDATE")}
    if not args.on and owners <= {args.name}:
        raise ActionFailed(400, "The workspace needs at least one owner; make someone else owner first.")
    if not conn.execute("UPDATE users SET workspace_owner = %s WHERE name = %s", [args.on, args.name]).rowcount:
        raise ActionFailed(400, f"No user named '{args.name}'.")
    return {}


@action(Role.owner, tenant_admin=True)
def list_users(conn, user: User, args: git.NoInput) -> dict:
    """The accounts you manage: name, workspace_owner, tenant, tenant_admin and their role per project; a tenant admin
    gets its own tenant's accounts and also the IDs of its tenant's projects (to give roles in)."""
    t = security.tenant_of(conn, user)
    out = {"users": conn.execute(
        "SELECT name, workspace_owner, tenant, tenant_admin,"
        " COALESCE(jsonb_object_agg(project, role) FILTER (WHERE project IS NOT NULL), '{}') AS roles"
        " FROM users LEFT JOIN project_roles ON user_id = id WHERE %s::text IS NULL OR tenant = %s GROUP BY id ORDER BY name",
        [t, t]).fetchall()}
    if t:
        out["projects"] = [r["project"] for r in conn.execute("SELECT project FROM project_tenants WHERE tenant = %s ORDER BY 1", [t])]
    return out


class AdminFlag(UserRef):
    on: bool


@action(Role.owner, tenant_admin=True)
def set_tenant_admin(conn, user: User, args: AdminFlag) -> dict:
    """Makes an account of a tenant that tenant's admin (manages its accounts and roles, no data access), or takes it away."""
    if not (target := _find(conn, user, args.name)):
        raise ActionFailed(400, f"No user named '{args.name}'.")
    conn.execute("UPDATE users SET tenant_admin = %s WHERE id = %s", [args.on, target["id"]])
    return {}


router = APIRouter(prefix="/api")


@router.post("/setup")
def setup(c: Credentials) -> dict:
    """First run only: creates the first account as workspace owner."""
    with db.connect() as conn:
        conn.execute("LOCK TABLE users")
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise HTTPException(409, "Setup is already done; ask a workspace owner for an account.")
        uid = conn.execute(
            "INSERT INTO users (name, password_hash, workspace_owner) VALUES (%s, %s, true) RETURNING id",
            [c.name, security.hash_password(c.password.get_secret_value())],
        ).fetchone()["id"]
        audit(conn, User(uid, c.name, True), "setup", "create_user", None, {"name": c.name, "workspace_owner": True}, 200)
    return {"id": uid}


@router.get("/projects/{project}/me")
def my_role(project: str, user: Annotated[User, Depends(current_user)]) -> dict:
    """The caller's role in the project (SC-2), so the UI can show edit controls read-only to a Viewer (UI-3), and
    whether it is a view-only production environment (D-56)."""
    with db.connect() as conn:
        role = security.role_in(conn, user, project)
    return {"role": role.name if role else None, "production": bool(role) and git.production(project)}


@router.post("/logout")
def logout(
    cred: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())], user: Annotated[User, Depends(current_user)]
) -> dict:
    """Signs out: deletes the caller's session, so its token stops working (SC-1, audited SC-6).

    An OIDC session also gets the IdP's end-session URL, for the UI to open (single logout)."""
    with db.connect() as conn:
        provider = security.logout(conn, cred.credentials)
        audit(conn, user, "rest", "logout", None, None, 200)
        url = oidc.end_session_url(conn, provider)
    return {"end_session_url": url} if url else {}
