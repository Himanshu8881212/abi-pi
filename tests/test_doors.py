"""The data doors (1.2 M1.1), on this machine's engine, no LLM: CSV, Excel, JSON and database tables come in through
the app's Sources page, abi.py and the agent's actions; a server path loads only what the caller may read; a database
login is the person's card in the app (two ABI secrets bound to its host), never in a log, Git, an audit row or the page."""
import base64
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import duckdb
import pytest
from conftest import drop

import abi
import app

P = "pi_doors_test"
DATA = Path(os.environ.get("ABI_PI_DATA") or abi.ROOT / "data")  # scripts/engine.sh's data folder
PGBIN = Path(os.environ.get("PGBIN") or Path(shutil.which("pg_ctl") or "/opt/homebrew/opt/postgresql@17/bin/pg_ctl").parent)


def projects() -> set:
    return {x["project"] for x in abi.call("list_projects")["projects"]}


def sure(action: str, **args) -> dict:
    """An action on what this test made (its accounts, rule, sources, secrets): ABI's confirmation is answered here."""
    try:
        return abi.call(action, **args)
    except abi.ActionError as e:
        if e.status != 409 or not (e.body or {}).get("confirm_token"):
            raise
        return abi.call(action, confirm=e.body["confirm_token"], **args)


def rows(project: str, table: str) -> int:
    q = abi.call("data_quality", project=project)
    t, n = q["columns"].index("table"), q["columns"].index("rows")
    return next(r[n] for r in q["rows"] if r[t] == table)


def source(project: str, table: str) -> dict:
    return json.loads((abi.ABI_PROJECTS / project / "sources" / f"{table}.json").read_text())


@pytest.fixture(scope="module")
def srv():
    """The app on this project, one person on this machine (the workspace owner), as the Sources page talks to it."""
    if P in projects():  # an interrupted run's
        drop([P])
    abi.new_project("demo", P)
    s = app.serve(P, 0, open_browser=False)
    port = s.server_address[1]
    key = re.search(r"KEY='([^']+)'", urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()).group(1)
    yield port, key
    s.shutdown()
    if P in projects():
        drop([P])


def post(srv, path: str, body: dict) -> dict:
    """A request as the page sends it: its key, its browser's hub (the app keeps one per browser) and project."""
    port, key = srv
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                 {"X-Abi-App": key, "Content-Type": "application/json", "X-Abi-Client": "doors", "X-Abi-Project": P})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=900).read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read() or b"{}")}


def upload(srv, table: str, f: Path, **extra) -> dict:
    """A file into a table through the Sources form, as the page sends it (one piece: these files are small)."""
    return post(srv, "/upload", {"table": table, "name": f.name, "data": base64.b64encode(f.read_bytes()).decode(),
                                 "id": secrets.token_hex(16), "last": True, **extra})


def test_a_server_path_loads_only_what_the_caller_may_read(tmp_path):
    """1.1 loaded any path in the engine's files folder for any Editor, so a restricted Viewer of project X who edits Y
    could load X's uploaded file into Y, past X's row-level security. Now a server path is an upload into a project
    where the caller reads every row (Editor or Owner), a path such a project already loads, or the workspace owner's."""
    x, y = "pi_doors_x", "pi_doors_y"
    ann, bob = (f"doors_{n}_{secrets.token_hex(3)}" for n in ("ann", "bob"))
    pw, tok = {n: secrets.token_urlsafe(16) for n in (ann, bob)}, {}
    drop([p for p in (x, y) if p in projects()])  # an interrupted run's
    try:
        for p in (x, y):
            abi.call("create_project", project=p, name=p)
        ids = {n: abi.call("create_user", name=n, password=pw[n])["id"] for n in (ann, bob)}
        for p, n, role in ((x, ann, "editor"), (x, bob, "viewer"), (y, bob, "editor")):
            abi.call("set_role", project=p, user=n, role=role)
        sure("set_rls_rule", project=x, id="north", table="orders", column="region", users={str(ids[bob]): ["North"]})
        tok = {n: abi.login(n, pw[n])[1]["token"] for n in (ann, bob)}
        (tmp_path / "orders.csv").write_text("order_id,region,amount\n1,North,10\n2,South,20\n3,South,30\n")
        with abi.as_user(tok[ann]):
            abi.upload(x, "orders", tmp_path / "orders.csv", abi.CSV_SAFE)
        where = source(x, "orders")["location"]
        assert re.fullmatch(r"uploads/[0-9a-f]{32}/orders\.csv", where), where
        with abi.as_user(tok[bob]):  # X's file, its folder, a glob over everyone's uploads, the uploads folder
            for loc in (where, where.rsplit("/", 1)[0], "uploads/*/orders.csv", "uploads"):
                with pytest.raises(abi.ActionError) as e:
                    abi.call("add_data", project=y, table="stolen", location=loc)
                assert e.value.status == 403, (loc, e.value.body)
            stolen = {"$schema": "https://schema.abi.invalid/v1/source.json", "id": "stolen", "location": where}
            with pytest.raises(abi.ActionError) as e:  # the import door too (pull, revert and Publish go through it)
                abi.call("import_project", project=y, bundle={"project.json": json.loads((abi.ABI_PROJECTS / y / "project.json").read_text()),
                                                              "sources/stolen.json": stolen})
            assert e.value.status == 403, e.value.body
            assert not (abi.ABI_PROJECTS / y / "sources/stolen.json").exists()
            abi.upload(y, "mine", tmp_path / "orders.csv", abi.CSV_SAFE)  # bob's own file into his project
            abi.call("add_data", project=y, table="mine", location=source(y, "mine")["location"], csv={"quote": '"'})  # again: y loads it
        with abi.as_user(tok[ann]):
            abi.call("refresh", project=x, table="orders")  # an existing source keeps loading
            abi.upload(x, "orders", tmp_path / "orders.csv", abi.CSV_SAFE)  # a new file for the table...
            assert source(x, "orders")["location"] != where
            abi.call("add_data", project=x, table="orders", location=where, csv=abi.CSV_SAFE)  # ...and back: its record says X
        abi.call("add_data", project=y, table="copied", location=where, csv=abi.CSV_SAFE)  # the workspace owner reads every path
        assert rows(y, "copied") == 3
    finally:
        for t in tok.values():
            abi.logout(t)
        drop([p for p in (x, y) if p in projects()])
        for n in (ann, bob):
            with contextlib.suppress(abi.ActionError):
                sure("delete_user", name=n)


def test_brewline_csv_with_quoted_line_breaks_loads_every_row(srv, tmp_path):
    """bench/brewline.py's CSV (product notes quoted over several lines, with "" inside) through abi.py's load (the
    agent's abi_load) and the Sources form: as many rows as DuckDB reads with quote and escape '"'. In the second file
    the quoted rows come after the first 32,000 lines, where DuckDB's own guess fails: the CSV-safe defaults read it."""
    subprocess.run([sys.executable, abi.ROOT / "bench/brewline.py", tmp_path, "--csv"], check=True, capture_output=True)  # --scale 0.01
    late, read = tmp_path / "brewline_late.csv", lambda f: f"read_csv('{f}', quote = '\"', escape = '\"')"  # noqa: E731
    with duckdb.connect() as db:
        db.execute(f"COPY (SELECT * FROM {read(tmp_path / 'brewline.csv')} ORDER BY product_note IS NOT NULL, order_id) TO '{late}' (HEADER)")
        want = db.execute(f"SELECT count(*) FROM {read(tmp_path / 'brewline.csv')}").fetchone()[0]
        assert db.execute(f"SELECT count(*) FROM {read(late)}").fetchone()[0] == want > 70_000
    r = abi.load("demo", P, [str(tmp_path / "brewline.csv")])
    assert [x["table"] for x in r["loaded"]] == ["brewline"] and not r["failed"], r
    assert rows(P, "brewline") == want
    r = upload(srv, "brewline_late", late)
    assert "result" in r, r
    assert rows(P, "brewline_late") == want


def test_the_second_sheet_of_a_workbook_and_ndjson_load(srv, tmp_path):
    """An .xlsx of three sheets (2, 5 and 3 rows, written with the engine's own xlsxwriter) loads its second sheet
    through the Sources form's sheet option; a sheet it lacks fails and loads nothing; an NDJSON file loads."""
    book = tmp_path / "book.xlsx"
    code = ("import sys, xlsxwriter\nwb = xlsxwriter.Workbook(sys.argv[1])\n"
            "for name, n in (('Notes', 2), ('Orders 2025', 5), ('Regions', 3)):\n"
            "    ws = wb.add_worksheet(name)\n    ws.write_row(0, 0, ['id', 'region', 'amount'])\n"
            "    for i in range(n):\n        ws.write_row(i + 1, 0, [i + 1, 'North' if i % 2 else 'South', (i + 1) * 2.5])\n"
            "wb.close()\n")
    subprocess.run([abi.ROOT / "engine/backend/.venv/bin/python", "-c", code, book], check=True)
    r = upload(srv, "book_orders", book, sheet="Orders 2025")
    assert "result" in r, r
    assert rows(P, "book_orders") == 5 and source(P, "book_orders")["xlsx"] == {"sheet": "Orders 2025"}
    assert "error" in upload(srv, "book_nope", book, sheet="Nope")
    assert not (abi.ABI_PROJECTS / P / "sources/book_nope.json").exists()
    events = tmp_path / "events.ndjson"
    events.write_text("".join(json.dumps({"id": i, "kind": "view" if i % 3 else "click", "at": f"2026-05-{i % 28 + 1:02d}"}) + "\n"
                              for i in range(40)))
    abi.upload(P, "events", events)
    assert rows(P, "events") == 40


@contextlib.contextmanager
def postgres(d: Path):
    """A throwaway Postgres 17 on a free TCP port of 127.0.0.1 with a password login (no socket), gone after."""
    pw = secrets.token_urlsafe(18)
    (d / "pw").write_text(pw)
    subprocess.run([PGBIN / "initdb", "-D", d / "pg", "-U", "shop", "--pwfile", d / "pw", "-A", "scram-sha-256", "-E", "UTF8",
                    "--no-locale"], check=True, capture_output=True)
    (d / "pw").unlink()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run([PGBIN / "pg_ctl", "-D", d / "pg", "-l", d / "pg.log", "-w", "start", "-o",
                    f"-c listen_addresses=127.0.0.1 -c port={port} -c unix_socket_directories=''"], check=True, capture_output=True)
    try:
        def psql(sql: str) -> None:
            subprocess.run([PGBIN / "psql", "-h", "127.0.0.1", "-p", str(port), "-U", "shop", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                            "-qc", sql], check=True, capture_output=True, env={**os.environ, "PGPASSWORD": pw})
        yield port, pw, psql
    finally:
        subprocess.run([PGBIN / "pg_ctl", "-D", d / "pg", "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(d / "pg", ignore_errors=True)


def test_a_postgres_table_loads_from_the_database_card(srv, tmp_path):
    """The Database card: the login goes to the app's admin endpoint only, which stores it as two ABI secrets bound to
    the host; the source names them. The card's load is the person's click; the agent's (the confirm bridge replays an
    MCP call with a confirm header) asks first for the new host. The password is in no log, Git history, audit row
    or page afterwards."""
    with postgres(tmp_path) as (port, pw, psql):
        psql("CREATE TABLE orders (id serial PRIMARY KEY, region text, amount numeric(10,2));"
             "INSERT INTO orders (region, amount) SELECT (ARRAY['North','South','East','West'])[1 + i % 4], i * 1.25"
             " FROM generate_series(1, 500) i; CREATE TABLE regions AS SELECT DISTINCT region FROM orders;")
        refs = post(srv, "/admin/dbkey", {"table": "pg_orders", "host": "127.0.0.1", "user": "shop", "password": pw})
        names = [re.fullmatch(r"\$\{secret:(\w+)\}", refs.get("result", {}).get(k, ""))[1] for k in ("user", "password")]
        try:
            loc = f"postgres://127.0.0.1:{port}/postgres"
            r = post(srv, "/api/add_data", {"project": P, "table": "pg_orders", "location": loc,
                                            "database": {"table": "public.orders", **refs["result"], "incremental": "id"}})
            assert "result" in r, r
            assert rows(P, "pg_orders") == 500
            assert source(P, "pg_orders")["database"] == {"table": "public.orders", **refs["result"], "incremental": "id"}
            agent = {"project": P, "table": "pg_regions", "location": loc, "database": {"table": "regions", **refs["result"]}}
            with pytest.raises(abi.ActionError) as e:  # what the confirm bridge sends first
                abi.call("add_data", confirm="ask", **agent)
            assert e.value.status == 409 and "source:127.0.0.1" in e.value.body["message"] and e.value.body.get("confirm_token")
            assert not (abi.ABI_PROJECTS / P / "sources/pg_regions.json").exists(), "nothing loads before the person's yes"
            abi.call("add_data", confirm=e.value.body["confirm_token"], **agent)  # the person said yes
            assert rows(P, "pg_regions") == 4
            abi.sync(P)
            page = urllib.request.urlopen(f"http://127.0.0.1:{srv[0]}/?p={P}").read()
            assert b"pg_orders" in page, "the page shows the source"
            audit = subprocess.run([PGBIN / "psql", "-h", DATA / "pg", "-U", "abi", "-d", "abi", "-Atc",
                                    "SELECT coalesce(input::text, '') || ' ' || coalesce(detail, '') FROM audit_log"
                                    " WHERE at > now() - interval '1 hour'"], capture_output=True, text=True, check=True).stdout
            assert "127.0.0.1" in audit, "the audit rows were read"
            history = subprocess.run(["git", "-C", abi.ABI_PROJECTS / P, "log", "-p", "--all"], capture_output=True, text=True, check=True).stdout
            assert "${secret:" in history, "the source is in Git, with references only"
            files = [f for d in (DATA / "logs", abi.ROOT / "logs", abi.ABI_PROJECTS / P, abi.folder(P)) for f in d.rglob("*") if f.is_file()]
            hits = [str(f) for f in files if pw.encode() in f.read_bytes()]
            assert not hits and pw not in audit and pw not in history and pw.encode() not in page, hits[:3]  # never the password itself
        finally:
            for t in ("pg_regions", "pg_orders"):
                if (abi.ABI_PROJECTS / P / "sources" / f"{t}.json").exists():
                    sure("delete_source", project=P, source=t)
            for n in names:
                sure("delete_secret", name=n)


def test_the_agent_loads_every_format_and_asks_before_a_new_host():
    """The extension's confirm bridge refuses no format any more, and it replays an MCP call that needed the person's
    confirmation with a confirm header, so ABI asks for a new host (or a first send) as it asked the agent; without
    one that REST call is the person's own, one step. The load-data skill sends logins to the app's Database card."""
    src = (abi.ROOT / ".pi/extensions/abi/index.ts").read_text()
    bridge = src[src.index("async function confirmBridge"):]
    bridge = bridge[:bridge.index("\n\t}\n")]
    assert "parquet" not in bridge.lower() and 'const first = await rest(tool, input, "ask");' in bridge
    skill = " ".join((abi.ROOT / ".pi/skills/load-data/SKILL.md").read_text().split())
    assert "only input" not in skill and "Add a database table" in skill and "Never ask for, repeat or pass a password" in skill
