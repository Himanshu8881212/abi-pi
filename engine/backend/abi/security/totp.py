"""SC-10 TOTP multi-factor sign-in for local accounts (E-06.4; work/validation/E-06.md decisions 9-10, D-52).

RFC 6238 over RFC 4226 with stdlib only: SHA1, 6 digits, 30-s steps, steps t-1..t+1 accepted and never one at or before
the last accepted step (`users.totp_step`, replay). Secrets are 20 random bytes, Fernet-encrypted at rest; 10 recovery
codes per account are stored as sha256 and used once. Every MFA action is a person's at the REST API only (SC-4), never
an agent, MCP or flow tool. OIDC sign-ins never ask for a code (the IdP owns its factors)."""

import base64
import hmac
import re
import secrets
import struct
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from abi import actions, security
from abi.actions import ActionFailed, action, audit
from abi.project import git
from abi.security import Role, User, accounts, credentials, mail

STEP, DIGITS, CODES = 30, 6, 10


def code(key: bytes, step: int, digits: int = DIGITS) -> str:
    """The RFC 4226 HOTP of counter `step` with HMAC-SHA1 (RFC 6238 TOTP when step = unix time // 30)."""
    h = hmac.new(key, struct.pack(">Q", step), "sha1").digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits:0{digits}d}"


def match(key: bytes, given: str, last: int | None, now: float | None = None) -> int | None:
    """The step of t-1..t+1 whose code is `given`, only if newer than `last` (RFC 6238 §5.2: accepted once)."""
    if not re.fullmatch(r"[0-9]{6}", given):
        return None
    t = int((time.time() if now is None else now) // STEP)
    return next((s for s in range(t - 1, t + 2) if s > (last if last is not None else -1)
                 and hmac.compare_digest(code(key, s), given)), None)


def _clean(given: str) -> str:
    return re.sub(r"[\s-]", "", given).upper()


def _key(token: bytes) -> bytes:
    return credentials.decrypt(token)


def _shown(name: str, key: bytes) -> dict:
    """The key for an authenticator app: base32 and the otpauth URI (Key Uri Format)."""
    b32 = base64.b32encode(key).decode()
    return {"secret": b32, "uri": f"otpauth://totp/ABI:{quote(name, safe='')}?secret={b32}&issuer=ABI"}


def _verify(conn, row: dict, given: str) -> bool:
    """A TOTP of the account's secret (its step recorded) or one of its recovery codes (used up)."""
    g = _clean(given)
    if (s := match(_key(row["totp_secret"]), g, row["totp_step"])) is not None:
        conn.execute("UPDATE users SET totp_step = %s WHERE id = %s", [s, row["id"]])
        return True
    return conn.execute("DELETE FROM recovery_codes WHERE user_id = %s AND hash = %s", [row["id"], security._token_hash(g)]).rowcount == 1


def _enable(conn, uid: int, secret: bytes, step: int) -> list[str]:
    """Turns MFA on with an enrolled secret; returns 10 new recovery codes (shown once, stored hashed)."""
    conn.execute("UPDATE users SET totp_secret = %s, totp_pending = NULL, totp_step = %s WHERE id = %s", [secret, step, uid])
    conn.execute("DELETE FROM recovery_codes WHERE user_id = %s", [uid])
    codes = [base64.b32encode(secrets.token_bytes(7)).decode()[:10] for _ in range(CODES)]  # 50 bits each
    for c in codes:
        conn.execute("INSERT INTO recovery_codes VALUES (%s, %s)", [uid, security._token_hash(c)])
    mail.notice(conn, uid, "Two-step sign-in with an authenticator app was turned on for your account.")
    return codes


def _clear(conn, uid: int) -> None:
    conn.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL, totp_step = NULL WHERE id = %s", [uid])
    conn.execute("DELETE FROM recovery_codes WHERE user_id = %s", [uid])


ROW = ("SELECT u.id, u.name, u.password_hash, u.totp_secret, u.totp_pending, u.totp_step,"
       " COALESCE(t.mfa_required, false) AS required FROM users u LEFT JOIN tenants t ON t.id = u.tenant WHERE u.{} = %s")
SETUP = ("Your organisation requires two-step sign-in: add this key to an authenticator app, then sign in again with"
         " its code.")


def sign_in(conn, name: str, password: str, given: str | None) -> tuple[bool, int, dict]:
    """One password sign-in: (not a failed check, HTTP status, body). A wrong password or code is a failed check."""
    row = conn.execute(ROW.format("name"), [name]).fetchone()
    if not security.verify_password(password, row["password_hash"] if row else security._dummy_hash()) or not row:
        return False, 401, {"message": "Wrong name or password."}
    if row["totp_secret"]:
        if given is None:
            return True, 401, {"mfa": "code", "message": "Enter the code from your authenticator app, or a recovery code."}
        if not _verify(conn, row, given):
            return False, 401, {"mfa": "code", "message": "Wrong or already used code."}
    elif row["required"]:  # decision 10: enrol at sign-in with the key of the last setup answer
        if given is None:  # a new key every time (H-01.1-fix-5): a key shown once is void once anyone signs in again
            pending = credentials._fernet().encrypt(secrets.token_bytes(20))
            conn.execute("UPDATE users SET totp_pending = %s WHERE id = %s", [pending, row["id"]])
            return True, 403, {"mfa": "setup", "message": SETUP, **_shown(name, _key(pending))}
        if not (pending := row["totp_pending"]) or (s := match(_key(pending), _clean(given), None)) is None:
            return False, 403, {"mfa": "setup", "message": "Wrong code; check the key in your authenticator app."}
        codes = _enable(conn, row["id"], pending, s)
        audit(conn, User(row["id"], name, False), "rest", "confirm_mfa", None, None, 200)  # SC-6: a change at sign-in
        return True, 200, {"token": security.new_session(conn, row["id"]), "recovery_codes": codes}
    return True, 200, {"token": security.new_session(conn, row["id"])}


def _attempt(name: str, check) -> object:
    """security.attempt for an action: wrong passwords and codes count together per name and client IP (decision 7)."""
    try:
        return security.attempt(name, security.CLIENT.get(), check)
    except security.Throttled:
        raise ActionFailed(429, security.TOO_MANY) from None


def _rest() -> None:
    if actions.VIA.get() != "rest":  # SC-4: secrets and second factors are a person's, never an LLM's or a flow's
        raise ActionFailed(403, "Two-step sign-in is managed by people at the REST API only.")


@action(Role.viewer)
def mfa_status(conn, user: User, args: git.NoInput) -> dict:
    """Whether two-step sign-in is on for your account, and whether your tenant requires it."""
    row = conn.execute(ROW.format("id"), [user.id]).fetchone()
    return {"on": bool(row["totp_secret"]), "required": row["required"]}


class Password(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=1024)


@action(Role.viewer)
def start_mfa(conn, user: User, args: Password) -> dict:
    """Starts turning on two-step sign-in for your account, with your password: returns a new key (base32 and otpauth
    URI) for an authenticator app; confirm_mfa with its code turns it on."""
    _rest()
    pending = credentials._fernet().encrypt(secrets.token_bytes(20))

    def check(c):  # OWASP MFA: re-authenticate before changing factors, so a stolen session can't enrol its own
        row = c.execute("SELECT password_hash, totp_secret FROM users WHERE id = %s", [user.id]).fetchone()
        if not security.verify_password(args.password.get_secret_value(), row["password_hash"]):
            return False
        return "on" if row["totp_secret"] else c.execute(
            "UPDATE users SET totp_pending = %s WHERE id = %s", [pending, user.id]).rowcount

    if (r := _attempt(user.name, check)) == "on":
        raise ActionFailed(409, "Two-step sign-in is already on; turn it off first.")
    if not r:
        raise ActionFailed(403, "Your password is wrong.")
    return _shown(user.name, _key(pending))


class Code(BaseModel):
    code: SecretStr = Field(min_length=1, max_length=64)


@action(Role.viewer)
def confirm_mfa(conn, user: User, args: Code) -> dict:
    """Turns on two-step sign-in with a code of the key start_mfa gave; returns 10 single-use recovery codes, shown
    only now."""
    _rest()

    def check(c):
        row = c.execute("SELECT totp_pending FROM users WHERE id = %s AND totp_secret IS NULL", [user.id]).fetchone()
        if not (row and row["totp_pending"]):
            return "none"  # not a guess: not counted
        s = match(_key(row["totp_pending"]), _clean(args.code.get_secret_value()), None)
        return s is not None and _enable(c, user.id, row["totp_pending"], s)

    if (codes := _attempt(user.name, check)) == "none":
        raise ActionFailed(409, "Start with start_mfa: two-step sign-in is on, or no key was started.")
    if not codes:
        raise ActionFailed(400, "Wrong code; check the key in your authenticator app.")
    return {"recovery_codes": codes}


class Disable(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=1024)
    code: SecretStr = Field(min_length=1, max_length=64)


@action(Role.viewer)
def disable_mfa(conn, user: User, args: Disable) -> dict:
    """Turns off two-step sign-in for your account, with your password and a code (or a recovery code). Refused while
    your tenant requires it."""
    _rest()

    def check(c):
        row = c.execute(ROW.format("id"), [user.id]).fetchone()
        if not row["totp_secret"] or row["required"]:
            return "kept"  # not a guess: not counted
        if not (security.verify_password(args.password.get_secret_value(), row["password_hash"])
                and _verify(c, row, args.code.get_secret_value())):
            return False
        _clear(c, user.id)
        mail.notice(c, user.id, "Two-step sign-in was turned off for your account.")
        return "off"

    if (r := _attempt(user.name, check)) == "kept":
        raise ActionFailed(409, "Two-step sign-in is off already, or your tenant requires it.")
    if not r:
        raise ActionFailed(403, "Wrong password or code.")
    return {}


@action(Role.owner, destructive=True, tenant_admin=True)
def reset_mfa(conn, user: User, args: accounts.UserRef) -> dict:
    """Turns off an account's two-step sign-in (lost device and recovery codes) and signs it out everywhere; it enrols
    again at its next sign-in if its tenant requires it. Workspace owner, or a tenant admin in its tenant."""
    _rest()
    if not (target := accounts._find(conn, user, args.name)):
        raise ActionFailed(400, f"No user named '{args.name}'.")
    _clear(conn, target["id"])
    conn.execute("DELETE FROM sessions WHERE user_id = %s", [target["id"]])
    conn.execute("DELETE FROM personal_tokens WHERE user_id = %s", [target["id"]])  # M4 review: a lost device's too
    mail.notice(conn, target["id"], "An administrator turned off two-step sign-in for your account.")
    return {}


class Requirement(BaseModel):
    tenant: str = Field(pattern=git.ID)
    on: bool


@action(Role.owner, tenant_admin=True)
def set_mfa_required(conn, user: User, args: Requirement) -> dict:
    """Requires two-step sign-in for a tenant's local accounts (or stops requiring it). Turning it on signs out the
    password sessions of its accounts without it (D-52); they enrol at their next sign-in. Workspace owner, or the
    tenant's admin."""
    _rest()
    mine = security.tenant_of(conn, user)
    if (mine and mine != args.tenant) or not conn.execute(
            "UPDATE tenants SET mfa_required = %s WHERE id = %s", [args.on, args.tenant]).rowcount:
        raise ActionFailed(404, f"No tenant '{args.tenant}'.")
    if not args.on:
        return {}
    conn.execute("DELETE FROM personal_tokens WHERE user_id IN (SELECT id FROM users WHERE tenant = %s AND totp_secret IS NULL)",
                 [args.tenant])  # M4 review: tokens minted without two-step sign-in end with its sessions
    return {"signed_out": conn.execute(
        "DELETE FROM sessions WHERE oidc_provider IS NULL"
        " AND user_id IN (SELECT id FROM users WHERE tenant = %s AND totp_secret IS NULL)", [args.tenant]).rowcount}


class Login(accounts.Credentials):
    code: SecretStr | None = Field(None, max_length=64)  # a TOTP or a recovery code, when the account has MFA


router = APIRouter(prefix="/api")


@router.post("/login")
def login(c: Login, request: Request) -> dict:
    """SC-1/SC-10: the password, then (MFA on) a code: 401 {"mfa": "code"} asks for it, 403 {"mfa": "setup", ...}
    enrols an account whose tenant requires MFA. Throttled per name (unknown names too) and client IP; wrong codes
    count with wrong passwords, and the 429 is the same for every name."""
    got = []

    def check(conn):
        got[:] = sign_in(conn, c.name, c.password.get_secret_value(), c.code and c.code.get_secret_value())
        return got[0]

    try:
        security.attempt(c.name, request.client.host if request.client else "", check)
    except security.Throttled:
        actions.audit_login(c.name, "rest", 429)
        raise HTTPException(429, security.TOO_MANY) from None
    _, status, body = got
    actions.audit_login(c.name, "rest", status, body.get("mfa"))
    if status != 200:
        raise HTTPException(status, body if "mfa" in body else body["message"])
    return body
