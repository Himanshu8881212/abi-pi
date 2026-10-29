"""Accounts, roles, sessions. PRD §9 SC-1 (local accounts), SC-2 (roles per project)."""

import functools
import hashlib
import hmac
import os
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import psycopg

from abi import db


class Role(IntEnum):
    viewer = 1
    editor = 2
    owner = 3


@dataclass(frozen=True)
class User:
    id: int
    name: str
    workspace_owner: bool


_N, _R, _P = 2**17, 8, 1  # OWASP password storage cheat sheet, scrypt


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=2**28)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${_scrypt(password, salt, _N, _R, _P).hex()}"


NO_PASSWORD = "!"  # SC-10: an invited account until it sets one; never verifies


def verify_password(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = (_dummy_hash() if stored == NO_PASSWORD else stored).split("$")  # same time as a wrong one
    ok = hmac.compare_digest(_scrypt(password, bytes.fromhex(salt), int(n), int(r), int(p)), bytes.fromhex(digest))
    return ok and stored != NO_PASSWORD


@functools.cache  # on first use, not at import: one N=2**17 scrypt costs every importing process 0.2 s and 128 MB RSS
def _dummy_hash() -> str:
    return hash_password("")  # unknown names cost the same time as wrong passwords


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def limited(conn, key: str, calls: int, per: int, count: bool = True) -> bool:
    """Fixed-window counter in Postgres (multi-process safe): True when `key` is over `calls` per `per` seconds.
    With count=False it only reads the window."""
    window = "date_bin(make_interval(secs => %s), now(), timestamptz 'epoch')"
    if count:
        n = conn.execute(f"INSERT INTO inbound_hits VALUES (%s, {window}, 1) ON CONFLICT (key, win)"
                         " DO UPDATE SET n = inbound_hits.n + 1 RETURNING n", [key, per]).fetchone()["n"]
    else:
        row = conn.execute(f"SELECT n FROM inbound_hits WHERE key = %s AND win = {window}", [key, per]).fetchone()
        n = row["n"] + 1 if row else 1
    return n > calls


FAILS, IP_FAILS = (10, 900), (100, 900)  # SC-10: failed password checks per account name / per client IP per 15 min
CLIENT: ContextVar[str] = ContextVar("abi_client_ip", default="")  # the REST caller's IP, for actions that check a password
TOO_MANY = "Too many failed sign-ins; try again in 15 minutes."


class Throttled(Exception):
    pass


def attempt(name: str, ip: str, check: Callable[[psycopg.Connection], Any]) -> Any:
    """SC-10 throttle around one password check for account `name` (known or not) from client `ip` ("" = none):
    raises Throttled, without running `check`, when the name or IP is over its limit; else returns `check(conn)` and
    counts a falsy result as a failure. Login, set_password and the SQL endpoint share it, so one count per name.
    One check per name at a time (advisory lock): parallel guesses can't all pass the read.
    ponytail: the per-IP count isn't locked, so parallel guesses at many names can pass 100 by the concurrency;
    lock the IP too if that matters."""
    keys = [(f"login:{name}", FAILS)] + ([(f"login-ip:{ip}", IP_FAILS)] if ip else [])
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"login:{name}"])
        if any(limited(conn, k, *lim, count=False) for k, lim in keys):
            raise Throttled
        if not (ok := check(conn)):
            for k, lim in keys:
                limited(conn, k, *lim)
        return ok


def new_session(conn: psycopg.Connection, user_id: int, oidc_provider: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, oidc_provider) VALUES (%s, %s, %s)",
        [_token_hash(token), user_id, oidc_provider],
    )
    return token


def logout(conn: psycopg.Connection, token: str) -> str | None:
    """Deletes the session; returns the OIDC provider it was signed in with (SC-1 single logout), if any."""
    row = conn.execute("DELETE FROM sessions WHERE token_hash = %s RETURNING oidc_provider", [_token_hash(token)]).fetchone()
    return row and row["oidc_provider"]


def user_for_token(conn: psycopg.Connection, token: str) -> User | None:
    # ponytail: fixed 30-day session lifetime, no refresh (sign-out revokes: SC-LOGOUT); add sliding refresh if users complain about re-login.
    # tools.builtin.render_snapshot backdates its sessions against these 30 days; change both together.
    row = conn.execute(
        "SELECT u.id, u.name, u.workspace_owner FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = %s AND s.created > now() - interval '30 days'",
        [_token_hash(token)],
    ).fetchone()
    return User(**row) if row else None


def user(conn: psycopg.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT id, name, workspace_owner FROM users WHERE id = %s", [user_id]).fetchone()
    return User(**row) if row else None


def role_in(conn: psycopg.Connection, user: User, project: str | None) -> Role | None:
    """A workspace owner is Owner everywhere; any account is Viewer at workspace scope."""
    if user.workspace_owner:
        return Role.owner
    if project is None:
        return Role.viewer
    row = conn.execute(
        "SELECT role FROM project_roles WHERE user_id = %s AND project = %s", [user.id, project]
    ).fetchone()
    return Role[row["role"]] if row else None


# SQL conditions over `users`, each %s the project (SC-9). ALLOWED: the accounts that may hold a role there (the trigger's
# rule), so the IDs a project file may name; another tenant's ID reads as unknown. MEMBERS: the accounts whose names the
# project shows: its tenant's accounts, workspace owners (Owners everywhere, E-05.md decision 8) and its role holders;
# platform staff without a role there stay unnamed in a tenant project.
ALLOWED = "(tenant IS NULL OR tenant = (SELECT tenant FROM project_tenants WHERE project = %s))"
MEMBERS = ("(tenant IS NOT DISTINCT FROM (SELECT tenant FROM project_tenants WHERE project = %s) OR workspace_owner"
           " OR id IN (SELECT user_id FROM project_roles WHERE project = %s))")


def tenant_of(conn: psycopg.Connection, user: User) -> str | None:
    """The account's tenant (SC-9); None = a platform account."""
    row = conn.execute("SELECT tenant FROM users WHERE id = %s", [user.id]).fetchone()
    return row and row["tenant"]


def admin_of(conn: psycopg.Connection, user: User, project: str | None) -> bool:
    """A tenant admin, at workspace scope or in a project of its own tenant (SC-9). Grants no project role."""
    return conn.execute(
        "SELECT 1 FROM users WHERE id = %s AND tenant_admin"
        " AND (%s::text IS NULL OR tenant = (SELECT tenant FROM project_tenants WHERE project = %s))",
        [user.id, project, project]).fetchone() is not None
