"""1.2 M4.5 data out, read-only, for outside clients (needs the engine at :8780, no LLM): a person mints a personal token
in the app; with it an MCP client (the app's /mcp) and a SQL client (the engine's Postgres-wire endpoint) read as that
person only (row-level security), nothing else; revoked or expired it signs in nowhere; it is in no log."""
import contextlib
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from conftest import drop, parquet
from test_https import compose
from test_pages import OWNER, PY, ROWS, engine, sure

import abi
import app
from pi_driver import ROOT

P = "pi_data_out"
DATA = Path(os.environ.get("ABI_PI_DATA", ROOT / "data"))

# the SQL client: psycopg (a real Postgres client) in the engine's environment; the token comes in on stdin, and a
# "wait" statement waits for the next line there
CLIENT = r"""
import json, sys, psycopg
a = json.loads(sys.stdin.readline())
out = []
try:
    with psycopg.connect(a["dsn"], password=a["password"], autocommit=True, connect_timeout=10) as c:
        out.append(["ssl", c.pgconn.ssl_in_use])
        for q in a["sql"]:
            if q == "wait":
                print("waiting", flush=True)
                sys.stdin.readline()
                continue
            try:
                out.append(["rows", [list(r) for r in c.execute(q).fetchall()]])
            except psycopg.Error as e:
                out.append(["error", e.sqlstate, str(e).strip()])
                if c.closed or e.sqlstate == "28000":
                    break
except psycopg.OperationalError as e:
    out.append(["refused", str(e).strip()[-300:]])
print(json.dumps(out, default=str))
"""

# the SQL endpoint on a port of its own, in the engine's code (the shared engine has no ABI_SQL_PORT)
SERVER = r"""
import asyncio, logging, sys
import abi.app  # noqa: F401 - every action, sql_query among them
from abi.semantic import sql
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
async def main():
    await sql.start()
    print("listening" if sql.SERVER else "closed", flush=True)
    if sql.SERVER:
        await asyncio.Event().wait()
asyncio.run(main())
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def sql_server(**env):
    """The SQL endpoint with these settings: (port, first lines it printed, whether it listens)."""
    port = free_port()
    e = subprocess.run(["sh", "-c", "scripts/engine.sh env"], cwd=ROOT, capture_output=True, text=True).stdout
    p = subprocess.Popen(["sh", "-c", f'{e}\nexec {PY} -c "$0"', SERVER], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env={**{k: v for k, v in os.environ.items() if not k.startswith("ABI_SQL")}, "ABI_SQL_PORT": str(port), **env})
    lines = []
    try:
        while (line := p.stdout.readline()) and line.strip() not in ("listening", "closed"):
            lines.append(line)
        yield port, lines, line.strip() == "listening"
    finally:
        p.kill()
        p.wait(10)


def psql(port: int, user: str, password: str, *sql: str, ssl: str = "disable") -> list:
    dsn = f"host=127.0.0.1 port={port} dbname={P} user={user} sslmode={ssl}"
    r = subprocess.run([PY, "-c", CLIENT], input=json.dumps({"dsn": dsn, "password": password, "sql": list(sql)}) + "\n",
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def mcp(port: int, token: str, method: str, params: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
                                 {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                  "MCP-Protocol-Version": "2025-06-18", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def tool(port: int, token: str, name: str, args: dict) -> tuple[bool, dict]:
    code, r = mcp(port, token, "tools/call", {"name": name, "arguments": args})
    assert code == 200, (code, r)
    return r["result"]["isError"], json.loads(r["result"]["content"][0]["text"])


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    """P (orders in three regions), a Viewer who sees North only, signed in to an app started here with signin, and
    the personal token they minted there. Gone at the end."""
    if not OWNER.exists():
        pytest.skip("no accounts file")
    d = tmp_path_factory.mktemp("out")
    (d / "orders.csv").write_text("order_id,region,channel,amount\n" + "".join(f"{i},{r},{c},{a}\n" for i, r, c, a in ROWS))
    if P in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([P])
    abi.load("demo", P, parquet(d / "orders.csv"))
    m = next(x["id"] for x in abi.call("semantic_model", project=P, all=True)["measures"] if x["expression"].upper() == "SUM(ORDERS.AMOUNT)")
    name, pw = f"pi_out_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    abi.call("create_user", name=name, password=pw)
    s = None
    try:
        abi.call("set_role", project=P, user=name, role="viewer")
        uid = next(u["id"] for u in abi.call("rls_rules", project=P)["accounts"] if u["name"] == name)
        sure("set_rls_rule", project=P, id="north", table="orders", column="region", users={uid: ["North"]})
        s = app.serve(P, 0, open_browser=False, signin=True)
        port = s.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request("POST", "/signin", urllib.parse.urlencode({"name": name, "password": pw}), {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        sid = (r.getheader("Set-Cookie") or "").split(";")[0]
        page = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Cookie": sid})).read().decode()
        key = re.search(r"KEY='([^']+)'", page).group(1)

        def tokens(**body):  # the page's Personal tokens dialog
            req = urllib.request.Request(f"http://127.0.0.1:{port}/tokens", json.dumps(body).encode(),
                                         {"Cookie": sid, "X-Abi-App": key, "Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())
        made = tokens(op="mint", name="psql and Claude")["result"]
        yield {"port": port, "name": name, "pw": pw, "uid": uid, "m": m, "tokens": tokens, "token": made["token"], "made": made,
               "seen": [made["token"]]}
    finally:
        if s:
            s.shutdown()
        sure("delete_user", name=name)
        drop([P])


def test_a_person_mints_a_read_only_token_in_the_app_and_lists_it(out):
    made = out["made"]
    assert made["token"].startswith("abi_pt_") and len(made["token"]) > 40 and made["name"] == "psql and Claude"
    days = (time.mktime(time.strptime(made["expires"][:19], "%Y-%m-%dT%H:%M:%S")) - time.time()) / 86400
    assert 89.9 < days < 90.1, made["expires"]  # 90 days unless the person chooses
    mine = out["tokens"](op="list")["result"]["tokens"]
    assert [(t["id"], t["name"], t["person"]) for t in mine] == [(made["id"], made["name"], out["name"])]
    assert all("token" not in t and "hash" not in json.dumps(t) for t in mine), "never the token again"
    assert {"created", "last_used", "expires"} <= set(mine[0])
    owner = sure("personal_tokens")["tokens"]  # a workspace owner sees everyone's
    assert made["id"] in [t["id"] for t in owner]
    with pytest.raises(abi.ActionError) as e:  # the token is not a session: no REST call, no action takes it
        with abi.as_user(made["token"]):
            abi.call("list_projects")
    assert e.value.status == 401
    _, r, _ = abi._open("POST", "/api/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                        {"Accept": "application/json, text/event-stream"})  # an agent (ABI's MCP, a session) has no such tool
    assert not {t["name"] for t in r["result"]["tools"]} & {"create_personal_token", "personal_tokens", "revoke_personal_token"}
    _, r, _ = abi._open("POST", "/api/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "create_personal_token", "arguments": {"name": "x"}}}, {"Accept": "application/json, text/event-stream"})
    assert r["result"]["isError"] and "abi_pt_" not in json.dumps(r)


def test_mcp_with_a_personal_token_reads_only_their_rows_and_refuses_the_rest(out):
    port, tok = out["port"], out["token"]
    code, r = mcp(port, tok, "tools/list")
    names = {t["name"] for t in r["result"]["tools"]}
    assert code == 200 and {"semantic_query", "visual_data", "semantic_model", "list_projects"} <= names, names
    assert not names & {"set_measure", "sql_query", "delete_project", "run_agent", "list_secrets", "create_personal_token", "rls_rules"}
    err, res = tool(port, tok, "semantic_query", {"project": P, "measures": [out["m"]], "dimensions": ["orders.region"]})
    north = sum(a for _, r_, _, a in ROWS if r_ == "North")
    assert not err and res["result"]["rows"] == [["North", north]], res
    for name, args in (("set_measure", {"project": P, "id": "x", "name": "x", "expression": "SUM(orders.amount)"}),
                       ("delete_project", {"project": P}), ("approve_edit", {"project": P, "id": 1}), ("sql_query", {"project": P, "sql": "SELECT 1"})):
        err, res = tool(port, tok, name, args)
        assert err and "only reads" in res["error"], (name, res)
    assert sure("semantic_model", project=P, all=True)["measures"] and "x" not in {
        x["id"] for x in sure("semantic_model", project=P, all=True)["measures"]}, "nothing changed"
    code, _ = mcp(port, abi.login(out["name"], out["pw"])[1]["token"], "tools/list")
    assert code == 401, "/mcp takes personal tokens only, never a session"
    code, _ = mcp(port, "abi_pt_" + secrets.token_urlsafe(32), "tools/list")
    assert code == 401
    audit = json.loads(engine(f"""
import json
from abi import db
with db.connect() as c:
    print(json.dumps([[r['via'], r['action'], r['status']] for r in c.execute(
        "SELECT via, action, status FROM audit_log WHERE user_id = %s AND at > now() - interval '5 minutes' ORDER BY id", [{out['uid']}])]))
""").stdout)
    assert ["mcp:token", "semantic_query", 200] in audit and ["mcp:token", "set_measure", 403] in audit, audit
    assert not [a for a in audit if a[0] == "mcp"], "every call through /mcp is marked as an outside client"


def test_sql_with_a_personal_token_is_rls_capped_and_revocable(out):
    tok, name = out["token"], out["name"]
    with sql_server(ABI_SQL_BIND="127.0.0.1") as (port, lines, up):
        assert up, lines
        r = psql(port, name, tok, "SELECT region, count(*) FROM orders GROUP BY 1 ORDER BY 1",
                 "SELECT count(*) FROM orders a, orders b, orders c, orders d", "SELECT a.order_id FROM orders a, orders b, orders c, orders d LIMIT 150000",
                 "DELETE FROM orders")
        assert r[1] == ["rows", [["North", 20]]], r  # their rows only
        assert r[2] == ["rows", [[160000]]], r
        assert r[3][:2] == ["error", "54000"] and "More than 100,000 rows" in r[3][2], r  # the cap, with the endpoint's notice
        assert r[4][0] == "error", r  # one read-only SELECT
        assert psql(port, name, "abi_pt_" + secrets.token_urlsafe(32), "SELECT 1")[0][0] == "refused"
        # a second token: expired, it signs in nowhere
        old = out["tokens"](op="mint", name="old", days=1)["result"]
        out["seen"].append(old["token"])
        engine(f"from abi import db\nwith db.connect() as c: c.execute(\"UPDATE personal_tokens SET expires = now() - interval '1 second' WHERE id = {old['id']}\")")
        assert psql(port, name, old["token"], "SELECT 1")[0][0] == "refused"
        assert mcp(out["port"], old["token"], "tools/list")[0] == 401
        # revoked in the app: refused at once, an open connection at its next statement
        held = subprocess.Popen([PY, "-c", CLIENT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        held.stdin.write(json.dumps({"dsn": f"host=127.0.0.1 port={port} dbname={P} user={name} sslmode=disable", "password": tok,
                                     "sql": ["SELECT 1", "wait", "SELECT 2"]}) + "\n")
        held.stdin.flush()
        assert held.stdout.readline().strip() == "waiting"  # signed in, SELECT 1 ran
        out["tokens"](op="revoke", id=out["made"]["id"])
        r = json.loads(held.communicate("go\n", timeout=60)[0])
        assert r[1] == ["rows", [[1]]] and r[2][:2] == ["error", "28000"] and "revoked" in r[2][2], r
        assert psql(port, name, tok, "SELECT 1")[0][0] == "refused"
        assert mcp(out["port"], tok, "tools/list")[0] == 401
        assert [t["name"] for t in out["tokens"](op="list")["result"]["tokens"]] == ["old"], "the revoked one is gone, the expired one listed"


def test_without_tls_the_sql_port_starts_only_on_this_machine(tmp_path):
    with sql_server() as (_, lines, up):
        assert not up and any("needs TLS" in x for x in lines), lines
    with sql_server(ABI_SQL_BIND="0.0.0.0") as (_, lines, up):
        assert not up
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-keyout", tmp_path / "k.pem", "-out", tmp_path / "c.pem"], check=True, capture_output=True)
    with sql_server(ABI_SQL_TLS_CERT=str(tmp_path / "c.pem"), ABI_SQL_TLS_KEY=str(tmp_path / "k.pem")) as (port, lines, up):
        assert up, lines
        r = psql(port, "nobody", "x", "SELECT 1", ssl="require")
        assert r[0][0] == "refused" and "password authentication failed" in r[0][1], r  # TLS first, then the password check


def test_compose_publishes_the_sql_port_only_when_it_is_set(tmp_path):
    ports = lambda s: sorted((p.get("host_ip") or "", p["published"], p["target"]) for p in s["abi"]["ports"])  # noqa: E731
    s = compose(tmp_path, "")
    assert ports(s) == [("", "8790", 8790)] and s["abi"]["environment"]["ABI_SQL_PORT"] == ""
    s = compose(tmp_path, "ABI_SQL_PORT=5433\n")
    assert ports(s) == [("", "5433", 5433), ("", "8790", 8790)] and s["abi"]["environment"]["ABI_SQL_TLS_CERT"] == ""
    s = compose(tmp_path, "COMPOSE_PROFILES=https\nABI_DOMAIN=abi.corp\nABI_SQL_PORT=5433\n")  # with HTTPS: this machine, as the app's port
    assert ports(s) == [("127.0.0.1", "5433", 5433), ("127.0.0.1", "8790", 8790)] and s["abi"]["environment"]["ABI_SQL_BIND"] == "127.0.0.1"
    s = compose(tmp_path, "ABI_TLS_CERT=/etc/ssl/abi.crt\nABI_TLS_KEY=/etc/ssl/abi.key\nABI_SQL_PORT=5433\nABI_SQL_BIND=0.0.0.0\n"
                          "ABI_SQL_TLS_CERT=${ABI_TLS_CERT}\nABI_SQL_TLS_KEY=${ABI_TLS_KEY}\n")  # the README's reuse of your own files
    mounts = {v["target"]: v["source"] for v in s["abi"]["volumes"]}
    assert mounts["/certs/sql.crt"] == "/etc/ssl/abi.crt" and mounts["/certs/sql.key"] == "/etc/ssl/abi.key"
    assert s["abi"]["environment"]["ABI_SQL_TLS_CERT"] == "/certs/sql.crt" and ("0.0.0.0", "5433", 5433) in ports(s)


def test_a_token_is_in_no_log(out):
    """Last: no token this module made in any log file, and no personal token in any audit or query row."""
    logs = [f for f in (DATA / "logs").rglob("*") if f.is_file()]
    assert logs and not [f for f in logs for t in out["seen"] if t.encode() in f.read_bytes()]
    found = engine("""
from abi import db
with db.connect() as c:
    print(c.execute("SELECT (SELECT count(*) FROM audit_log WHERE concat(input::text, detail) LIKE '%%abi_pt_%%')"
                    " + (SELECT count(*) FROM query_log WHERE concat(sql, query::text, error) LIKE '%%abi_pt_%%') AS n").fetchone()["n"])
""")
    assert found.stdout.strip() == "0", found.stderr[-1000:]
