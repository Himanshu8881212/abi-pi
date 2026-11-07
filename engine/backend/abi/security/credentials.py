"""Workspace secret store. PRD SC-4 (encrypted at rest, never shown after entry, never sent to an LLM), §6.6 `${secret:NAME}`.

Values are Fernet-encrypted with a key file on the data volume, so a Postgres dump holds no plaintext.
There is no action that reads a secret back; only `resolve` decrypts, at load time, in-process.
Each secret may only be sent to the hosts its owner listed (DECISIONS D-2); callers check with `allow` first.
Secrets belong to a tenant or to the platform (SC-9, E-05.md decision 6): `${secret:NAME}` resolves only to a secret of
the using project's tenant, so another tenant's name reads exactly like an unknown one.
"""

import functools
import os
import re
import traceback
from pathlib import Path
from typing import Annotated

import psycopg
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import AfterValidator, BaseModel, Field, SecretStr

from abi import db, storage
from abi.actions import ActionFailed, action
from abi.project import git
from abi.security import Role, User

REF = r"\$\{secret:([A-Za-z0-9_]+)\}"
# SC-4: an unknown name may be a password typed where the name goes, so no refusal ever echoes it (DEMO-1-fix-3)
UNKNOWN = "A secret named here doesn't exist: give the name of a secret (set_secret), never the password or token itself."
Host = Annotated[str, Field(pattern=r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"), AfterValidator(str.lower)]  # no wildcards


# Every stored Fernet ciphertext as (table, column), re-encrypted by rotate_secret_key; sealed chat contexts aren't stored
# and only stop replaying (H-01.7-fix-2): while one of these exists, a missing key file is an error, never a new key.
CIPHERTEXTS = (("secrets", "value"), ("webhook_tokens", "token"), ("embed_keys", "key"), ("chatgpt_links", "tokens"),
               ("api_tokens", "token"), ("users", "totp_secret"), ("users", "totp_pending"))
STORED = "SELECT " + " OR ".join(f"EXISTS (SELECT FROM {t} WHERE {c} IS NOT NULL)" for t, c in CIPHERTEXTS) + " AS stored"


def _key_file() -> Path:
    return Path(os.environ.get("ABI_SECRET_KEY_FILE", "/data/secret.key"))


def _lost(why: str) -> ActionFailed:
    return ActionFailed(500, f"The secret key file {_key_file()} {why}, so stored secrets, webhook tokens, two-step"
                             " sign-in, embed keys and ChatGPT links can't be opened. The operator must restore the key"
                             " file that encrypted them (ABI_SECRET_KEY_FILE).")


@functools.cache
def _fernet() -> MultiFernet:
    """The key file holds one key per line, newest first (H-01.7-fix-4): encrypt with the first, decrypt with any."""
    # ponytail: key file next to the lake data; losing the volume loses the secrets (re-enter them). KMS if ever needed.
    path = _key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with db.connect() as conn:
            if conn.execute(STORED).fetchone()["stored"]:
                raise _lost("is missing")  # a new key would orphan every stored ciphertext
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(Fernet.generate_key())
    except FileExistsError:
        pass  # app and worker share it
    if path.stat().st_mode & 0o077:
        raise ActionFailed(500, f"The secret key file {path} is readable by others; make it owner-only (chmod 600).")
    return MultiFernet([Fernet(k) for k in path.read_bytes().split()])


def decrypt(value: bytes) -> bytes:
    """A stored ciphertext's plaintext; a value the key file can't open is a clear error, not a bare 500."""
    try:
        return _fernet().decrypt(bytes(value))
    except InvalidToken:
        raise _lost("does not match the stored values") from None


@action(Role.owner)
def rotate_secret_key(conn, user: User, args: git.NoInput) -> dict:
    """Re-encrypts every stored secret, webhook token, two-step sign-in secret, embed key, ChatGPT link and API token
    with the key file's first key (SC-4, H-01.7-fix-4). After a leaked key: put a new key on the first line above the
    old one, restart app and worker, run this, then drop the old line and restart again. Never an agent tool (agent._secret)."""
    f, n = _fernet(), 0
    for table, col in CIPHERTEXTS:
        for r in conn.execute(f"SELECT ctid::text AS id, {col} AS v FROM {table} WHERE {col} IS NOT NULL FOR UPDATE").fetchall():
            try:
                new = f.rotate(bytes(r["v"]))
            except InvalidToken:
                raise _lost("does not match the stored values") from None
            n += conn.execute(f"UPDATE {table} SET {col} = %s WHERE ctid = %s::tid", [new, r["id"]]).rowcount
    return {"rotated": n}


class NewSecret(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{1,100}$")
    value: SecretStr = Field(min_length=1)
    allowed_hosts: list[Host] = Field(min_length=1, description="Exact host names this secret may be sent to (D-2).")
    tenant: str | None = Field(None, description="The tenant whose projects may use it; omitted: platform projects (SC-9).")


# SC-9: the tenant of project %s (NULL: a platform project, or no project at all)
TENANT = "tenant IS NOT DISTINCT FROM (SELECT tenant FROM project_tenants WHERE project = %s)"


@action(Role.owner)
def set_secret(conn, user: User, args: NewSecret) -> dict:
    """Stores a credential of the platform or of one tenant, encrypted and bound to its allowed hosts; it is never shown again."""
    try:
        conn.execute(
            "INSERT INTO secrets (name, value, allowed_hosts, tenant) VALUES (%s, %s, %s, %s) ON CONFLICT ON CONSTRAINT"
            " secrets_tenant_name DO UPDATE SET value = excluded.value, allowed_hosts = excluded.allowed_hosts",
            [args.name, _fernet().encrypt(args.value.get_secret_value().encode()), args.allowed_hosts, args.tenant],
        )
    except psycopg.errors.ForeignKeyViolation:
        raise ActionFailed(400, f"No tenant '{args.tenant}'.") from None
    return {}


@action(Role.editor)
def list_secrets(conn, user: User, args: storage.ProjectRef) -> dict:
    """Names of the secrets this project may use (its tenant's), to reference as ${secret:NAME} (D-2: an Editor
    right); never values or hosts."""
    return {"secrets": [r["name"] for r in conn.execute(f"SELECT name FROM secrets WHERE {TENANT} ORDER BY name", [args.project])]}


class SecretRef(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{1,100}$")
    tenant: str | None = None  # omitted: the platform secret


@action(Role.owner, destructive=True)
def delete_secret(conn, user: User, args: SecretRef) -> dict:
    """Deletes a credential of the platform or of one `tenant`. Refused while a project file of that tenant (for a
    platform secret: of a platform project, a sign-in or an LLM provider) references it. Never an agent tool (agent._secret, SC-4/D-3)."""
    ref = re.compile(r"\$\{secret:" + args.name + r"\}")
    root = git._root()  # every folder, also rename_needed ones (git._folders)
    owner = {r["project"]: r["tenant"] for r in conn.execute("SELECT project, tenant FROM project_tenants")}
    uses = [f"{p}/{f.relative_to(root / p).as_posix()}" for p in sorted(git._folders()) if owner.get(p) == args.tenant
            for f in sorted((root / p).rglob("*"))
            if ".git" not in f.relative_to(root / p).parts and f.is_file() and ref.search(f.read_text(errors="ignore"))]
    if args.tenant is None:  # workspace-level uses resolve platform secrets only
        uses += [f"sign-in provider {r['id']}" for r in conn.execute(
            "SELECT id FROM oidc_providers WHERE client_secret = %s ORDER BY id", ["${secret:" + args.name + "}"])]
        uses += [f"LLM provider {r['id']}" for r in conn.execute(
            "SELECT id FROM llm_providers WHERE api_key = %s ORDER BY id", ["${secret:" + args.name + "}"])]
    if uses:
        raise ActionFailed(409, f"Secret '{args.name}' is still used by {', '.join(uses)}; remove those references first.", uses=uses)
    if not conn.execute("DELETE FROM secrets WHERE name = %s AND tenant IS NOT DISTINCT FROM %s", [args.name, args.tenant]).rowcount:
        raise ActionFailed(404, f"No secret '{args.name}'.")
    return {}


def resolve(conn, text: str, used: dict[str, tuple[str, list | None]], project: str | None) -> str:
    """Replace every ${secret:NAME} in `text` with the secret of `project`'s tenant (None: a workspace-level use,
    platform secrets only; SC-9); `used` collects name → (value, allowed_hosts) for `allow` and `scrub`."""

    def value(m):
        row = conn.execute(f"SELECT value, allowed_hosts FROM secrets WHERE name = %s AND {TENANT}", [m[1], project]).fetchone()
        if row is None:
            raise ActionFailed(400, UNKNOWN)
        used[m[1]] = (decrypt(row["value"]).decode(), row["allowed_hosts"])
        return used[m[1]][0]

    return re.sub(REF, value, text)


def allow(used: dict, host: str | None) -> None:
    """Raise 403 unless every used secret may go to `host` (None = no network host); the caller audits it (SC-6).
    A secret with no list (NULL, stored before F-09) goes nowhere until the workspace owner sets one."""
    for name, (_, hosts) in used.items():
        if host not in (hosts or []):
            where = f"host '{scrub(host, used)}'" if host else "this location (server path or unclear URL host)"
            raise ActionFailed(403, f"Secret '{name}' may not be sent to {where}; the workspace owner sets its allowed_hosts.")


def scrub(text: str, used: dict) -> str:
    for v, _ in used.values():
        text = text.replace(v, "***")
    return text


def log_text(e: BaseException, used: dict | None = None) -> str:
    """Server-log text for a background failure (SC-4): the type and stack frames, and the message only scrubbed
    with the secrets the call resolved (`used`); with those unknown, no message, since a driver error can quote one."""
    msg = f": {e}" if used is not None else ""
    return scrub(f"{type(e).__name__}{msg}\n{''.join(traceback.format_tb(e.__traceback__))}", used or {})
