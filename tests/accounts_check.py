"""1.1 #9/#10 end to end on this machine's own engine: account mail (a local SMTP sink), an invite and a reset link
through the app, an owner-set password, and OpenID Connect sign-in (a mock IdP over HTTPS): unlinked, linking, then
signing in with it. Throwaway accounts, project and settings; all removed at the end. Prints no secret.

    .venv/bin/python tests/accounts_check.py      (restarts this machine's engine twice; needs data/accounts.txt)"""
import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
T = Path(__file__).resolve().parent
S = Path(tempfile.mkdtemp())
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
PORT, PROJ, MAILBOX = 8793, "s1_20261103", S / "mailbox.txt"
cert, key, bundle = S / "idp.crt", S / "idp.key", S / "bundle.pem"
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                "-addext", "subjectAltName=DNS:localhost", "-keyout", key, "-out", cert], check=True, capture_output=True)
bundle.write_text(Path(subprocess.check_output([ROOT / "engine/backend/.venv/bin/python", "-c", "import certifi;print(certifi.where())"], text=True).strip()).read_text() + cert.read_text())
os.environ["SSL_CERT_FILE"] = str(bundle)  # this process (the app reads the issuer) and the engine trust the mock IdP
os.environ["ABI_PUBLIC_URL"] = f"http://127.0.0.1:{PORT}"
MAILBOX.unlink(missing_ok=True)
procs = [subprocess.Popen([sys.executable, T / "smtp_sink.py", MAILBOX]),
         subprocess.Popen([ROOT / "engine/backend/.venv/bin/python", T / "mock_idp.py", cert, key])]
subprocess.run([ROOT / "scripts/engine.sh", "stop"], capture_output=True)
subprocess.run([ROOT / "scripts/engine.sh", "start"], check=True, capture_output=True)

import abi  # noqa: E402
import app  # noqa: E402
from conftest import parquet  # noqa: E402

acc = (ROOT / "data/accounts.txt").read_text()
OWNER_PW = next(x.split()[-1] for x in acc.splitlines() if re.match(r"\s*himanshu\b", x))
results = []


def ok(what, cond):
    results.append((what, bool(cond)))
    print(("ok   " if cond else "FAIL ") + what, flush=True)


def req(method, path, body=None, cookie="", key="", form=False):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    hd = {"Cookie": cookie} if cookie else {}
    if body is not None:
        hd["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        body = urllib.parse.urlencode(body) if form else json.dumps(body)
    if key:
        hd["X-Abi-App"] = key
    c.request(method, path, body, hd)
    r = c.getresponse()
    data = r.read()
    try:
        data = json.loads(data)
    except ValueError:
        data = data.decode(errors="replace")
    return r.status, data, r.getheader("Set-Cookie") or "", r.getheader("Location") or ""


def sign(name, pw):
    st, _, ck, _ = req("POST", "/signin", {"name": name, "password": pw}, form=True)
    if st != 303:
        return None, None
    sid = ck.split(";")[0]
    page = req("GET", "/", cookie=sid)[1]
    return sid, (re.search(r"KEY='([^']+)'", page) or [None, None])[1]


def admin(sid, key, op, body):
    st, r, _, _ = req("POST", "/admin/" + op, body, sid, key)
    if isinstance(r, dict) and r.get("confirm"):  # the owner's own yes in ABI's dialog (these are this run's throwaways)
        st, r, _, _ = req("POST", f"/confirm/{r['confirm']['id']}", {}, sid, key)
    return r


def mails(n, wait=20):
    for _ in range(wait * 4):
        import email as em  # a mail app's view: quoted-printable decoded
        got = [em.message_from_string(x).get_payload(decode=True).decode() for x in MAILBOX.read_text().split("=====\n")[:-1]] if MAILBOX.exists() else []
        if len(got) >= n:
            return got
        time.sleep(0.25)
    return got


def oidc(start_cookie="", sub="alice", link=False):
    """start (as this browser) -> the IdP -> the app's callback: (outcome, the browser's abi_oidc cookie)."""
    st, r, ck, _ = req("POST", "/api/oidc/mock/start", {"link": True} if link else {}, start_cookie)
    jar = ck.split(";")[0]
    url = r["url"] + "&" + urllib.parse.urlencode({"sub": sub})
    class No(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(No).open(url)
        loc = ""
    except urllib.error.HTTPError as e:
        loc = e.headers["Location"]
    back = urllib.parse.urlsplit(loc)
    ok("the IdP sends the browser to this app's callback", loc.startswith(f"http://127.0.0.1:{PORT}/api/oidc/mock/callback"))
    st, _, _, loc2 = req("GET", back.path + "?" + back.query, cookie="; ".join(filter(None, [start_cookie, jar])))
    return (re.search(r"signin=(\w+)", loc2) or [None, "?"])[1], jar, loc2


s = None
name, email = "inv_" + secrets.token_hex(3), f"inv-{secrets.token_hex(3)}@example.test"
try:
    if not abi.folder(PROJ):
        d = ROOT / "scratch" / PROJ
        d.mkdir(parents=True, exist_ok=True)
        (d / "orders.csv").write_text("order_id,region,amount\n1,North,3\n2,South,4\n")
        abi.load("demo", PROJ, parquet(d / "orders.csv"))
    s = app.serve(PROJ, PORT, open_browser=False, signin=True)
    osid, okey = sign("himanshu", OWNER_PW)
    ok("the owner signs in", osid and okey)
    r = admin(osid, okey, "mail", {"host": "127.0.0.1", "port": 2525, "security": "none", "from": "abi@example.test"})
    ok("the owner sets the account mail server", "result" in r)
    r = admin(osid, okey, "invite", {"email": email})
    ok("an invite goes out", "result" in r)
    m = mails(1)
    link = re.search(r"(http://\S+)#invite=([\w-]+)", m[0] if m else "")
    ok("the invite's link opens this app", link and link[1] == f"http://127.0.0.1:{PORT}/")
    pw1, pw2, pw3 = (secrets.token_urlsafe(12) for _ in range(3))
    st, r, _, _ = req("POST", "/signin/token", {"kind": "invite", "token": link[2], "password": pw1})
    ok("the invited person chooses a password from the link", st == 200 and pw1 not in json.dumps(r))
    ok("the link works once", req("POST", "/signin/token", {"kind": "invite", "token": link[2], "password": pw2})[0] == 400)
    ok("they sign in with it", sign(email, pw1)[0])
    st, r, _, _ = req("POST", "/signin/forgot", {"email": email})
    ok("forgot your password answers the same for everyone", st == 200 and "link" in (r.get("message") or ""))
    m = mails(2)
    link = re.search(r"#reset=([\w-]+)", m[1] if len(m) > 1 else "")
    ok("a reset link arrives", link)
    req("POST", "/signin/token", {"kind": "reset", "token": link[1], "password": pw2})
    ok("the reset link sets the new password; the old one is gone", sign(email, pw2)[0] and not sign(email, pw1)[0])
    ok("no password or token in the mails' other text", pw1 not in MAILBOX.read_text() and pw2 not in MAILBOX.read_text())
    r = admin(osid, okey, "setpass", {"user": email, "password": pw3})
    ok("the owner sets a new password for them", "result" in r and sign(email, pw3)[0])

    r = admin(osid, okey, "sso/save", {"id": "mock", "name": "Mock IdP", "issuer": "https://localhost:9443", "client_id": "abi-test",
                                       "secret": "test-client-secret-" + secrets.token_hex(4)})
    ok("the owner adds an SSO provider", "result" in r)
    got = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/signin/sso").read())
    ok("the sign-in page lists it (id and name only)", got == [{"id": "mock", "name": "Mock IdP"}])
    out, _, loc = oidc(sub="sub-" + name)
    ok("an unlinked company account doesn't sign anyone in", out == "unknown" and loc.startswith("/?signin="))
    psid, _ = sign(email, pw3)
    out, _, _ = oidc(psid, "sub-" + name, link=True)
    ok("the person links it from their own session", out == "linked")
    out, jar, _ = oidc(sub="sub-" + name)
    ok("signing in with it completes at the IdP", out == "ok")
    st, _, ck, _ = req("POST", "/api/oidc/session", {}, jar)
    sid = ck.split(";")[0] if "abi_u=" in ck else ""
    page = req("GET", "/", cookie=sid)[1] if sid else ""
    ok("and opens the app as that person", st == 200 and sid and "Sign in with your ABI account" not in page)
    ok("the finished sign-in can't be used twice", req("POST", "/api/oidc/session", {}, jar)[0] == 401)
    ok("someone else's company account stays out", oidc(sub="sub-stranger")[0] == "unknown")
finally:
    try:
        osid, okey = sign("himanshu", OWNER_PW)
        if osid:
            admin(osid, okey, "sso/remove", {"id": "mock"})
            admin(osid, okey, "mail", {"off": True})
        with abi.as_user(abi.login("himanshu", OWNER_PW)[1]["token"]):
            for n in (email,):
                try:
                    abi.call("delete_user", name=n)
                except abi.ActionError as e:
                    if e.status == 409 and (e.body or {}).get("confirm_token"):
                        abi.call("delete_user", confirm=e.body["confirm_token"], name=n)
            try:
                abi.call("delete_project", project=PROJ)
            except abi.ActionError as e:
                if e.status == 409:
                    abi.call("delete_project", confirm=e.body["confirm_token"], project=PROJ)
    finally:
        if s:
            s.shutdown()
        for p in procs:
            p.terminate()
        for v in ("SSL_CERT_FILE", "ABI_PUBLIC_URL"):
            os.environ.pop(v, None)
        subprocess.run([ROOT / "scripts/engine.sh", "stop"], capture_output=True)
        subprocess.run([ROOT / "scripts/engine.sh", "start"], capture_output=True)
print(f"{sum(o for _, o in results)} of {len(results)} ok")
