"""SC-10 account basics by email (E-06.2, E-06.3; work/validation/E-06.md decisions 1-6): account email, the workspace's
account mail server, invites, self-service sign-up by tenant domain and password reset with single-use expiring links (token only in the URL fragment,
stored as sha256, used once with DELETE … RETURNING). All of it answers 404 until ABI_PUBLIC_URL and account mail are set."""

import logging
import os
import secrets
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr
from typing import Annotated

import psycopg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from psycopg.types.json import Jsonb
from pydantic import AfterValidator, BaseModel, Field, SecretStr

from abi import db, security
from abi.actions import ActionFailed, action, audit
from abi.project import imports
from abi.security import Role, User, accounts, credentials, tenants
from abi.tools import builtin

log = logging.getLogger(__name__)
OFF = "Account email is off: a workspace owner sets ABI_PUBLIC_URL and the account mail server (set_account_mail)."
Email = Annotated[str, Field(max_length=254, pattern=builtin.EMAIL), AfterValidator(str.lower)]
_SMTP = Draft202012Validator(imports._validator("project").schema["properties"]["settings"]["properties"]["delivery"]
                             ["properties"]["smtp"])  # the project smtp object's own rules (decision 2)
LIFE = {"invite": timedelta(days=7), "reset": timedelta(minutes=30), "signup": timedelta(hours=24)}
TEXT = {
    "invite": ("Your new account", ("You have been invited to an account named {name}. Open this link within 7 days"
                                    " to choose your password:\n\n{link}\n")),
    "reset": ("Reset your password", ("Someone asked to reset the password of your account {name}. Open this link"
                                      " within 30 minutes to choose a new one:\n\n{link}\n\nIf it wasn't you, ignore"
                                      " this email; your password stays.")),
    "signup": ("Create your account", ("Open this link within 24 hours to create your account {name} and choose its"
                                       " password:\n\n{link}\n\nIf you didn't ask for it, ignore this email.")),
    "exists": ("Your account", ("Someone asked to sign up with this email, but you already have an account named {name}."
                                " To choose a new password, open this link within 30 minutes:\n\n{link}\n\nIf it wasn't"
                                " you, ignore this email; your password stays.")),
}
USED = {"invite": "invite_confirm", "reset": "password_reset_confirm", "signup": "signup_confirm"}  # audit action names


def _on(conn) -> tuple[str, dict] | None:
    """(public URL, smtp object) when account email is on."""
    public = os.environ.get("ABI_PUBLIC_URL", "").rstrip("/")
    row = conn.execute("SELECT smtp FROM account_mail").fetchone()
    return (public, row["smtp"]) if public and row else None


def _send(conn, public: str, smtp: dict, account: User | None, email: str, kind: str, text: str | None = None,
          tenant: str | None = None) -> None:
    """A new `kind` token for `account` (sign-up: for `email`, into `tenant`), mailed with the tenant's branding (SC-9)
    as a link with it in the fragment."""
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM account_tokens WHERE expires < now()")
    conn.execute("INSERT INTO account_tokens (hash, kind, user_id, email, expires) VALUES (%s, %s, %s, %s, now() + %s)",
                 [security._token_hash(token), kind, account and account.id, None if account else email, LIFE[kind]])
    subject, body = TEXT[text or kind]
    _deliver(conn, smtp, account, email, subject, body.format(name=account.name if account else email,
                                                              link=f"{public}/#{kind}={token}"), tenant)


def notice(conn, uid: int, text: str) -> None:
    """Decision 9: tells an account that its sign-in factors changed, when it has an email and account mail is on.
    Best effort: a failed send is logged and never undoes the change."""
    row = conn.execute("SELECT id, name, workspace_owner, email FROM users WHERE id = %s", [uid]).fetchone()
    if not (row and row["email"] and (on := _on(conn))):
        return
    try:
        with conn.transaction():  # a failing secret/host check must not abort the caller's transaction
            _deliver(conn, on[1], User(row["id"], row["name"], row["workspace_owner"]), row["email"],
                     "Your sign-in settings changed", f"{text}\n\nIf it wasn't you, tell your administrator.")
    except ActionFailed:  # send_smtp's refusals and network errors
        log.warning("Sign-in change notice for account %s not sent", uid)


def _deliver(conn, smtp: dict, account: User | None, email: str, subject: str, text: str, tenant: str | None = None):
    brand = tenants.branding(conn, user=account) if account else tenants.branding(conn, tenant=tenant)
    msg = EmailMessage()
    msg["From"] = formataddr((brand["name"], smtp["from"])) if brand else smtp["from"]
    msg["To"], msg["Subject"], msg["Auto-Submitted"] = email, subject, "auto-generated"
    msg.set_content(text)
    if brand:
        builtin._branded(msg, brand, text)
    builtin.send_smtp(conn, smtp, None, msg, [email])


class AccountMail(BaseModel):
    smtp: dict | None  # a project.json settings.delivery.smtp object with a platform secret; null turns account email off


@action(Role.owner)
def set_account_mail(conn, user: User, args: AccountMail) -> dict:
    """Sets the workspace's mail server for invites and password resets (workspace owner); null turns them off."""
    if args.smtp is None:
        conn.execute("DELETE FROM account_mail")
        return {}
    if err := best_match(_SMTP.iter_errors(args.smtp)):  # path and rule only, never the value (SC-4)
        raise ActionFailed(400, f"smtp: {imports.json_path(err, 'project')} fails '{err.validator}' in schemas/v1/project.json.")
    builtin.smtp_password(conn, args.smtp, None)  # platform secrets only; 400 if unknown, D-2: 403 unless it may go there
    conn.execute("INSERT INTO account_mail (smtp) VALUES (%s) ON CONFLICT (one) DO UPDATE SET smtp = excluded.smtp",
                 [Jsonb(args.smtp)])
    return {}


class UserEmail(BaseModel):
    name: str
    email: Email | None


@action(Role.owner, tenant_admin=True)
def set_user_email(conn, user: User, args: UserEmail) -> dict:
    """Sets or (null) clears an account's email, where its invite and password reset links go. Workspace owner, or a
    tenant admin for its tenant's accounts. Links already mailed stop working."""
    if not (target := accounts._find(conn, user, args.name)):
        raise ActionFailed(400, f"No user named '{args.name}'.")
    try:
        conn.execute("UPDATE users SET email = %s WHERE id = %s", [args.email, target["id"]])
    except psycopg.errors.UniqueViolation:  # accepted leak (D-50): emails, like names, are unique per install
        raise ActionFailed(400, "Another account has this email.") from None
    conn.execute("DELETE FROM account_tokens WHERE user_id = %s", [target["id"]])
    return {}


class Invite(BaseModel):
    email: Email
    tenant: str | None = None  # the new account's tenant (SC-9); a tenant admin's invites always join its own


@action(Role.owner, tenant_admin=True)
def invite_user(conn, user: User, args: Invite) -> dict:
    """Invites a person: creates the account named by their email, with no password yet, and emails them a link
    (valid 7 days) to choose one. Workspace owner; a tenant admin invites into its own tenant. Give roles with set_role."""
    if not (on := _on(conn)):
        raise ActionFailed(404, OFF)
    uid = accounts.new_account(conn, user, args.email, security.NO_PASSWORD, args.tenant, email=args.email)
    _send(conn, *on, User(uid, args.email, False), args.email, "invite")  # a failed send rolls the account back
    return {"name": args.email}


router = APIRouter(prefix="/api")


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


class ResetRequest(BaseModel):
    email: Email


@router.post("/password-reset", status_code=202)
def password_reset(body: ResetRequest, request: Request, tasks: BackgroundTasks) -> dict:
    """SC-10: mails a 30-minute reset link to the account with this email. The same 202 for every address, known or
    not; the lookup and the mail run after the response, so its timing tells nothing either."""
    with db.connect() as conn:
        if not _on(conn):
            raise HTTPException(404, OFF)
    tasks.add_task(_mail, "reset", body.email, _ip(request))
    return {"message": "If an account has this email, a link to reset its password is on its way."}


@router.post("/signup", status_code=202)
def signup(body: ResetRequest, request: Request, tasks: BackgroundTasks) -> dict:
    """SC-10 self-service sign-up (decision 5): mails a 24-hour link to create an account when the email's domain is a
    tenant's sign-up domain, or a reset link when an account already has the email, else nothing. The same 202 for all;
    the lookup and the mail run after the response."""
    with db.connect() as conn:
        if not _on(conn):
            raise HTTPException(404, OFF)
    tasks.add_task(_mail, "signup", body.email, _ip(request))
    return {"message": "If this email may sign up here, a link is on its way."}


def _mail(what: str, email: str, ip: str) -> None:
    """A reset or sign-up request's work, after its 202."""
    try:
        with db.connect() as conn:  # decision 7: 3 per address and 20 per IP an hour, silently (still 202)
            over = [security.limited(conn, f"{what}:{email}", 3, 3600), security.limited(conn, f"{what}-ip:{ip}", 20, 3600)]
            conn.commit()  # counted even if the send below fails
            if any(over) or not (on := _on(conn)):
                return
            if row := conn.execute("SELECT id, name, workspace_owner FROM users WHERE email = %s", [email]).fetchone():
                _send(conn, *on, User(**row), email, "reset", None if what == "reset" else "exists")
                audit(conn, User(**row), "rest", "password_reset" if what == "reset" else "signup", None, None, 202)
            elif what == "signup" and (t := conn.execute("SELECT tenant FROM tenant_domains WHERE domain = %s",
                                                          [email.rsplit("@", 1)[1]]).fetchone()):
                _send(conn, *on, None, email, "signup", tenant=t["tenant"])
                audit(conn, None, "rest", "signup", None, {"tenant": t["tenant"]}, 202)
    except ActionFailed as e:  # messages are scrubbed of secrets and never hold the token
        log.warning("Account %s mail not sent: %s", what, e)
    except Exception as e:  # noqa: BLE001 - after the response nobody else sees it; logged without its message (SC-4)
        log.warning("Account %s mail not sent: %s", what, credentials.log_text(e))


class TokenPassword(BaseModel):
    token: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=8, max_length=1024)


@router.post("/password-reset/confirm")
def reset_confirm(body: TokenPassword, request: Request) -> dict:
    """Sets the password with a reset link's token (once, within 30 minutes) and signs the account out everywhere."""
    return _use("reset", body, _ip(request))


@router.post("/signup/confirm")
def signup_confirm(body: TokenPassword, request: Request) -> dict:
    """Creates the account of a sign-up link (once, within 24 hours) in its domain's tenant, with no roles. Sign in
    afterwards."""
    return _use("signup", body, _ip(request))


@router.post("/invite/confirm")
def invite_confirm(body: TokenPassword, request: Request) -> dict:
    """Sets the first password with an invite's token (once, within 7 days). Sign in afterwards."""
    return _use("invite", body, _ip(request))


def _use(kind: str, body: TokenPassword, ip: str) -> dict:
    key = f"login-ip:{ip}"  # failed tokens count with failed sign-ins per client IP (SC-10, E-06.1)
    with db.connect() as conn:
        if not _on(conn):
            raise HTTPException(404, OFF)
        if ip and security.limited(conn, key, *security.IP_FAILS, count=False):
            raise HTTPException(429, security.TOO_MANY)
        row = conn.execute("DELETE FROM account_tokens WHERE hash = %s AND kind = %s RETURNING user_id, email, expires > now() AS live",
                           [security._token_hash(body.token), kind]).fetchone()  # single use, also under races
        if not (row and row["live"]):
            if ip:
                security.limited(conn, key, *security.IP_FAILS)
            conn.commit()  # an expired token stays deleted, the failure counted
            raise HTTPException(400, "This link is invalid, used or expired; ask for a new one.")
        if kind == "signup":  # the domain's tenant now (a domain removed or moved since the mail no longer lands here)
            new = conn.execute(
                "INSERT INTO users (name, password_hash, tenant, email) SELECT %(e)s, %(no)s, tenant, %(e)s FROM tenant_domains"
                " WHERE domain = %(d)s ON CONFLICT DO NOTHING RETURNING id",
                {"e": row["email"], "no": security.NO_PASSWORD, "d": row["email"].rsplit("@", 1)[1]}).fetchone()
            if not new:  # a taken name or email, or sign-up closed for the domain
                conn.commit()
                raise HTTPException(400, "Sign-up is closed for this email, or it has an account already: sign in or"
                                    " reset its password.")
            row["user_id"] = new["id"]
        account = User(**conn.execute("UPDATE users SET password_hash = %s WHERE id = %s RETURNING id, name, workspace_owner",
                                      [security.hash_password(body.password.get_secret_value()), row["user_id"]]).fetchone())
        conn.execute("DELETE FROM account_tokens WHERE user_id = %s OR email = %s", [account.id, row["email"]])
        conn.execute("DELETE FROM sessions WHERE user_id = %s", [account.id])
        conn.execute("DELETE FROM personal_tokens WHERE user_id = %s", [account.id])  # M4 review: as set_password does
        audit(conn, account, "rest", USED[kind], None, None, 200)
    return {}
