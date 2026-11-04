"""1.2 M3.2 on this machine's engine, no LLM: a load too big to wait for runs in the heavy worker. add_data, set_steps
and refresh past ABI_INLINE_ROWS answer at once with a run (status queued) once the checks that fail fast have passed;
the job lands as the inline call does (one Git change, one snapshot) and load_runs shows its caller, and only them, how
far it got. Publish loads only what its import changed (in the worker), build_model fits a small DuckDB memory limit,
and exports and videos have their time limits."""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import duckdb
import pytest
from conftest import drop
from test_doors import projects, sure
from test_engine import engine

import abi
import app
import video

P = "pi_jobs_test"
FILES = Path(os.environ.get("ABI_PI_DATA") or abi.ROOT / "data") / "files"  # scripts/engine.sh's files folder
DIR = "pi_jobs"  # a server path there: the workspace owner reads every one
N = int(os.environ.get("ABI_INLINE_ROWS", "4400000")) + 100_000  # just past what loads inline
EXACT = [[N, N // 1000 * 499_500 + sum(range(N % 1000)), 4]]  # orders, the sum of amount (0..999 over and over), regions


# delete_project keeps a project's lake 30 days (PC-7): this module's (about 170 MB a run) goes at once, as
# storage.purge_deleted does it, and so do the uploads its projects made
PURGE = r"""
import os, re, shutil
from pathlib import Path
from abi import db
mine = r'^_deleted_[0-9]+_pi_jobs_(test|publish|publish_pub)$'
with db.connect() as c:
    gone = [r['nspname'] for r in c.execute('SELECT nspname FROM pg_namespace WHERE nspname ~ %s', [mine])]
for s in gone:
    with db.connect() as c:
        c.execute('SELECT pg_terminate_backend(l.pid) FROM pg_locks l JOIN pg_class r ON r.oid = l.relation JOIN pg_namespace n'
                  ' ON n.oid = r.relnamespace WHERE n.nspname = %s AND l.pid <> pg_backend_pid()', [s])  # a stale cached lake's
        c.execute("SET LOCAL lock_timeout = '30s'")
        c.execute(f'DROP SCHEMA "{s}" CASCADE')
for d in Path(os.environ['ABI_DATA_PATH']).iterdir():
    if re.match(mine, d.name):
        shutil.rmtree(d, ignore_errors=True)
"""


@pytest.fixture(scope="module", autouse=True)
def _tidy():
    yield
    for d in (FILES / "uploads").glob("*"):
        for rec in (d / ".owner.json", d / ".upload.json"):
            if rec.exists() and json.loads(rec.read_text()).get("project", "").startswith("pi_jobs_"):
                shutil.rmtree(d, ignore_errors=True)
                break
    assert engine(PURGE, timeout=180).returncode == 0


def orders(path: Path, n: int) -> None:
    duckdb.sql(f"COPY (SELECT range AS order_id, ['North', 'South', 'East', 'West'][range % 4 + 1] AS region, range % 1000 AS amount"
               f" FROM range({n})) TO '{path}' (FORMAT parquet)")


def exact(p: str, table: str = "orders") -> list:
    return abi.call("sql_query", project=p, sql=f"SELECT count(*), sum(amount), count(DISTINCT region) FROM {table}")["rows"]


def finished(p: str, run: str) -> dict:
    """The run once it is done or failed, polled as the app and the agent poll it."""
    end = time.monotonic() + 300
    while (x := abi.call("load_runs", project=p, run=run)["runs"][0])["status"] in ("queued", "running"):
        assert time.monotonic() < end, x
        time.sleep(0.5)
    return x


@pytest.fixture(scope="module")
def big():
    """A new project and three files in the engine's files folder: one just past the inline limit, the same one with its
    middle overwritten (its footer still says how many rows), and a small one."""
    drop([p for p in (P,) if p in projects()])  # an interrupted run's
    (FILES / DIR).mkdir(parents=True, exist_ok=True)
    orders(FILES / DIR / "big.parquet", N)
    shutil.copy(FILES / DIR / "big.parquet", FILES / DIR / "corrupt.parquet")
    with (FILES / DIR / "corrupt.parquet").open("r+b") as f:
        f.seek((FILES / DIR / "corrupt.parquet").stat().st_size // 2)
        f.write(b"\xff" * (1 << 20))
    orders(FILES / DIR / "small.parquet", 1000)
    try:
        abi.call("create_project", project=P, name=P)
        yield
    finally:
        drop([p for p in (P,) if p in projects()])
        shutil.rmtree(FILES / DIR, ignore_errors=True)


def test_a_big_load_answers_at_once_and_lands_exact_once(big):
    """1.1 loaded inside the call (100M rows: 449 s in one engine request, past the agent's 600 s with what follows).
    Now the call answers with a run (it writes nothing: a run that fails leaves no file, below); the heavy worker writes
    the source file and loads it as the inline call does: one undoable change named add_data, one snapshot, the table
    exact; a small file still loads inline; set_steps and refresh of the big table are jobs too."""
    t0 = time.monotonic()
    r = abi.call("add_data", project=P, table="orders", location=f"{DIR}/big.parquet")
    assert r["status"] == "queued" and r["rows"] == [[r["run"], "orders", "queued"]] and time.monotonic() - t0 < 30, r
    x = finished(P, r["run"])
    assert x["status"] == "done" and x["rows"] == N and x["error"] is None and x["action"] == "add_data", x
    assert exact(P) == EXACT
    assert [s[0] for s in abi.call("list_snapshots", project=P)["rows"] if s[3] == "refresh orders"] == [x["snapshot"]]
    assert abi.call("changes", project=P)["proposed_message"] == "add_data: sources/orders.json"
    assert abi.call("add_data", project=P, table="small", location=f"{DIR}/small.parquet")["snapshot"] > x["snapshot"]
    steps = [{"type": "source", "source": "orders"}, {"type": "filter", "condition": "region = 'North'"}]
    s = abi.call("set_steps", project=P, table="north", steps=steps)
    assert s["status"] == "queued" and not (abi.ABI_PROJECTS / P / "tables/north.json").exists(), s
    assert finished(P, s["run"])["rows"] == N // 4 + (N % 4 > 0) and exact(P, "north")[0][2] == 1
    f = abi.call("refresh", project=P, table="orders")
    assert f["status"] == "queued" and finished(P, f["run"])["status"] == "done", f
    runs = abi.call("load_runs", project=P)["rows"]
    assert [(t, a, st) for _, t, a, st, *_ in runs] == [("orders", "refresh", "done"), ("north", "set_steps", "done"),
                                                       ("orders", "add_data", "done")], runs


def test_a_big_load_that_fails_leaves_git_clean_and_its_run_says_why(big):
    """What fails fast fails in the call, as in 1.1 (a wrong path or format: nothing queued); a file that breaks while it
    loads (its footer whole, its middle not) fails its run with ABI's error, the previous snapshot stays live and no
    definition is left behind. A big upload that fails leaves its session as an inline failure does."""
    for loc, why in ((f"{DIR}/missing.parquet", "failed"), (f"{DIR}/small.weird", "Can't tell the format")):
        with pytest.raises(abi.ActionError) as e:
            abi.call("add_data", project=P, table="nothing", location=loc)
        assert e.value.status == 400 and why in e.value.body["message"], e.value.body
    snap = abi.call("list_snapshots", project=P, limit=1)["rows"][0][0]
    r = abi.call("add_data", project=P, table="broken", location=f"{DIR}/corrupt.parquet")
    x = finished(P, r["run"])
    assert x["status"] == "failed" and re.match(rf"Loading '{DIR}/corrupt\.parquet' failed \(\w+\); the details are in the"
                                                r" server log\.$", x["error"]) and x["snapshot"] is None, x
    assert abi.call("list_snapshots", project=P, limit=1)["rows"][0][0] == snap
    assert not (abi.ABI_PROJECTS / P / "sources/broken.json").exists() and "broken" not in json.dumps(abi.call("changes", project=P))
    assert [y["table"] for y in abi.call("load_runs", project=P)["runs"]].count("nothing") == 0
    with pytest.raises(abi.ActionError) as e:  # through the upload door: the file's name goes, its session stays
        abi.upload(P, "broken", FILES / DIR / "corrupt.parquet")
    assert "failed" in e.value.body["message"], e.value.body
    sessions = [d for d in (FILES / "uploads").iterdir() if (d / ".upload.json").exists()
                and json.loads((d / ".upload.json").read_text())["project"] == P]
    assert len(sessions) == 1 and {f.name for f in sessions[0].iterdir()} == {".upload.json", ".part"}, sessions
    shutil.rmtree(sessions[0])
    up = abi.upload(P, "uploaded", FILES / DIR / "big.parquet")  # a big upload that loads: waited for, its record kept
    assert up["status"] == "done" and up["rows"] == [[up["snapshot"], N]] and exact(P, "uploaded") == EXACT, up
    folder = FILES / Path(json.loads((abi.ABI_PROJECTS / P / "sources/uploaded.json").read_text())["location"]).parent
    assert {f.name for f in folder.iterdir()} == {".owner.json", "big.parquet"}


def test_no_one_else_sees_a_run(big):
    """load_runs runs as the person: another Editor of the project gets no row of someone else's run, by its ID or in
    the list."""
    run = abi.call("load_runs", project=P)["runs"][0]["run"]
    name, pw = f"jobs_{secrets.token_hex(3)}", secrets.token_urlsafe(16)
    abi.call("create_user", name=name, password=pw)
    tok = None
    try:
        abi.call("set_role", project=P, user=name, role="editor")
        tok = abi.login(name, pw)[1]["token"]
        with abi.as_user(tok):
            with pytest.raises(abi.ActionError) as e:
                abi.call("load_runs", project=P, run=run)
            assert e.value.status == 404 and run in e.value.body["message"], e.value.body
            assert abi.call("load_runs", project=P) == {"runs": [], "columns": ["run", "table", "action", "status", "rows",
                                                                                "snapshot", "error", "created", "finished"],
                                                        "rows": []}
    finally:
        if tok:
            abi.logout(tok)
        sure("delete_user", name=name)


def test_publish_loads_only_what_its_import_changed(tmp_path):
    """1.1's Publish reloaded every source into the published copy, even for a measure (40 s at 7.4M orders). The import
    forgets the fingerprint of what it changed, so the worker loads that within its minute: a measure-only Publish calls
    no add_data or refresh and the copy serves the new measure at once; after new data the copy shows it."""
    p, pub = "pi_jobs_publish", "pi_jobs_publish_pub"  # (a project ID ending in _pub is a published copy to the app)
    drop([x for x in (p, pub) if x in projects()])
    orders(tmp_path / "orders.parquet", 1000)
    abi.load("demo", p, [str(tmp_path / "orders.parquet")])
    hubs = set(app.HUBS)  # the app's hubs are per process: the ones made here go after, so later tests get their own
    s = app.serve(p, 0, open_browser=False)
    port = s.server_address[1]
    key = re.search(r"KEY='([^']+)'", urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()).group(1)

    def page(path: str, body: dict) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                     {"X-Abi-App": key, "Content-Type": "application/json", "X-Abi-Client": "jobs", "X-Abi-Project": p})
        return json.loads(urllib.request.urlopen(req, timeout=300).read())

    def publish() -> float:
        t = time.time()
        r = page("/publish", {})
        if r.get("confirm"):
            r = page(f"/confirm/{r['confirm']['id']}", {})
        assert "result" in r, r
        return t

    def calls(since: float) -> list:
        out = engine(f"""
from abi import db
with db.connect() as c:
    print([r['action'] for r in c.execute("SELECT action FROM audit_log WHERE project = %s AND action IN ('add_data', 'refresh')"
                                           " AND at >= to_timestamp(%s)", ['{pub}', {since}])])""")
        return eval(out.stdout.strip())  # noqa: S307 - a list of action names printed just above

    def loaded(rows: int) -> bool:  # the worker's minute, then the copy's load
        end = time.monotonic() + 180
        while time.monotonic() < end:
            try:
                if abi.call("sql_query", project=pub, sql="SELECT count(*) FROM orders")["rows"] == [[rows]]:
                    return True
            except abi.ActionError:  # no table yet
                pass
            time.sleep(1)
        return False
    try:
        publish()
        assert loaded(1000), "the worker loads the first Publish's tables"
        abi.call("set_measure", project=p, id="amount_total", name="Amount total", expression="SUM(orders.amount)")
        abi.call("commit", project=p, message="a measure")  # the app commits by hand: Publish sends the last commit
        t = publish()
        assert calls(t) == [], "a measure-only Publish reloads nothing"
        got = abi.call("semantic_query", project=pub, measures=["amount_total"])["rows"]
        assert got == abi.call("semantic_query", project=p, measures=["amount_total"])["rows"] == [[499500]], got
        orders(tmp_path / "orders.parquet", 2000)
        abi.upload(p, "orders", tmp_path / "orders.parquet")  # new data: a new upload, so a new location
        abi.call("commit", project=p, message="new orders")
        t = publish()
        assert calls(t) == [] and loaded(2000), "the new data reaches the copy through the worker"
    finally:
        s.shutdown()
        for k in set(app.HUBS) - hubs:
            app.HUBS.pop(k, None)
        drop([x for x in (p, pub) if x in projects()])


def test_build_model_fits_a_small_memory_limit(tmp_path):
    """M0.6: build_model ran out of DuckDB's 1500MB at 100M orders (a 503 twice, so no model): its key check counted
    every key-able column's distinct values in one read, and DuckDB holds several DISTINCTs' memory at once (it spills
    one alone). The same ratio, 1.5M orders in 100MB, ran out the same way; now each exact count is a read of its own,
    only for a column HyperLogLog can't rule out, and the model is the one a roomy limit gives."""
    r = engine(f"""
import json, os
os.environ['ABI_DATA_PATH'], os.environ['ABI_PROJECTS_PATH'] = '{tmp_path}/lake', '{tmp_path}/projects'
from abi import db, storage
from abi.security import User
from abi.semantic import model
p = 'pi_jobs_mem'
with db.connect() as c:
    c.execute(f'DROP SCHEMA IF EXISTS {{p}} CASCADE')
os.makedirs(f'{tmp_path}/projects/{{p}}/.git')
con = storage.lake(p)
for name, sql in [
    ('cafes', "SELECT printf('CF%03d', range + 1) AS cafe_id, 'c' || range AS cafe_name FROM range(120)"),
    ('customers', "SELECT printf('C%06d', range + 1) AS customer_id, 'seg' || (range % 5) AS segment FROM range(60000)"),
    ('orders', "SELECT printf('O%09d', range) AS order_id, DATE '2023-01-01' + (range % 1095)::INT AS order_date,"
               " printf('CF%03d', range % 120 + 1) AS cafe_id, CASE WHEN range % 5 < 3 THEN printf('C%06d', hash(range) % 60000 + 1) END"
               " AS customer_id, (range % 3 + 1)::INT AS quantity, (['card','cash','mobile'])[range % 3 + 1] AS payment FROM range(1500000)")]:
    storage.refresh(con, name, sql, author='test')
con.execute("SET memory_limit = '100MB'")
try:
    with db.connect() as conn:
        out = model.build_model(conn, User(id=0, name='test', workspace_owner=True), storage.ProjectRef(project=p))
        conn.rollback()  # its readiness post
    m = json.loads(open(f'{tmp_path}/projects/{{p}}/model/model.json').read())
    print(json.dumps({{'missing': out['missing'], 'keys': {{e['table']: e['key'] for e in m['entities']}},
                      'kinds': {{t['table']: t['kind'] for t in m['tables']}}, 'date': m['date_table'],
                      'links': sorted(f"{{r['from']['table']}}.{{r['from']['columns'][0]}}>{{r['to']['table']}}" for r in m['relationships'])}}))
finally:
    storage._close_lakes()
    with db.connect() as c:
        c.execute(f'DROP SCHEMA IF EXISTS {{p}} CASCADE')
""", timeout=300)
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else r.stderr[-3000:]
    assert out == {"missing": [f"Table '{t}' has no description." for t in ("cafes", "calendar", "customers", "orders")],
                   "keys": {"cafes": ["cafe_id"], "calendar": ["day"], "customers": ["customer_id"], "orders": ["order_id"]},
                   "kinds": {"cafes": "dimension", "calendar": "dimension", "customers": "dimension", "orders": "fact"},
                   "date": {"column": "day", "table": "calendar"},
                   "links": ["orders.cafe_id>cafes", "orders.customer_id>customers", "orders.order_date>calendar"]}, out


def test_a_running_load_tells_its_rows_so_far(tmp_path):
    """A load job reads DuckDB's progress of its own write: rows so far are that share of the probe's estimate, written
    to its run while it loads (the person sees it move instead of one long wait)."""
    r = engine(f"""
import functools, json, os, threading, time, uuid
os.environ['ABI_DATA_PATH'] = '{tmp_path}'
from abi import db, ingest, storage
import duckdb
p, n, run = 'pi_jobs_progress', 6_000_000, str(uuid.uuid4())
duckdb.sql(f"COPY (SELECT range AS i, range % 7 AS k FROM range({{n}})) TO '{tmp_path}/t.parquet' (FORMAT parquet)")
ingest.PROGRESS_S = 0.2
with db.connect() as c:
    c.execute(f'DROP SCHEMA IF EXISTS {{p}} CASCADE')
    uid = c.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()['id']
    c.execute("INSERT INTO load_runs (id, project, table_name, action, via, user_id, status, estimate) VALUES"
              " (%s, %s, 't', 'add_data', 'rest', %s, 'running', %s)", [run, p, uid, n])
con, seen = storage.lake(p), []
box, done = {{'table': 't', 'cur': None}}, threading.Event()
threading.Thread(target=ingest._progress, args=(run, box, n, done), daemon=True).start()

def watch():
    while not done.is_set():
        with db.connect() as c:
            seen.append(c.execute('SELECT rows FROM load_runs WHERE id = %s', [run]).fetchone()['rows'])
        time.sleep(0.25)
w = threading.Thread(target=watch)
w.start()
try:
    storage.refresh(con, 't', "SELECT * FROM read_parquet('{tmp_path}/t.parquet')", author='test',
                    hook=functools.partial(ingest._watched, box, None))
finally:
    done.set()
    w.join()
    storage._close_lakes()
    with db.connect() as c:
        c.execute('DELETE FROM load_runs WHERE id = %s', [run])
        c.execute(f'DROP SCHEMA {{p}} CASCADE')
print(json.dumps(seen))
""", timeout=300)
    seen = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else r.stderr[-3000:]
    moved = [s for s in seen if s]
    assert moved and all(0 < s <= 6_000_000 for s in moved) and moved == sorted(moved) and min(moved) < 6_000_000, seen


def test_a_third_export_waits_and_a_late_one_loses_its_chromium():
    """1.1 started a Chromium for every PDF, PPTX or PNG at once, with no limit but the page's 60 s. Now two draw at a
    time and a third waits its turn (no error), and one still drawing at ABI_RENDER_TIMEOUT has its Chromium killed
    and answers 504."""
    r = engine("""
import json, os, socket, subprocess, threading, time
os.environ['ABI_RENDER_TIMEOUT'] = '3'
from abi import db
from abi.actions import ActionFailed
from abi.security import User
from abi.tools import builtin

spans, real = [], builtin._render
def stand_in(*args):  # a render of a second
    t = time.monotonic()
    time.sleep(1)
    spans.append((t, time.monotonic()))
    return 'artifact:p/x.png'
builtin._render = stand_in
ts = [threading.Thread(target=builtin.render_snapshot, args=(None, 'p', 'r', ['pg'], None, [])) for _ in range(3)]
for t in ts:
    t.start()
for t in ts:
    t.join()
builtin._render = real
srv = socket.socket()  # a page that never answers
srv.bind(('127.0.0.1', 0))
srv.listen(8)
os.environ['ABI_APP_URL'] = f'http://127.0.0.1:{srv.getsockname()[1]}'
marks, deadline = [], builtin._deadline
builtin._deadline = lambda mark, late: marks.append(mark) or deadline(mark, late)
with db.connect() as c:
    owner = User(**c.execute('SELECT id, name, workspace_owner FROM users ORDER BY id LIMIT 1').fetchone())
t0, error = time.monotonic(), None
try:
    builtin.render_snapshot(owner, 'pi_jobs_render', 'r', ['pg'], None, [])
except ActionFailed as e:
    error = [e.status, str(e)]
took = time.monotonic() - t0
left = marks and subprocess.run(['pgrep', '-f', '--', marks[0]], capture_output=True, text=True).stdout.split()
print(json.dumps({'spans': sorted(spans), 'error': error, 'took': took, 'left': left}))
""", timeout=120)
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else r.stderr[-3000:]
    a, b, c = out["spans"]
    assert b[0] < a[1] and c[0] >= min(a[1], b[1]) - 0.01, out  # two at once, the third once one was done
    if "Executable doesn't exist" in out["error"][1]:  # as test_app's Chromium tests (CI installs it, and fails on a skip)
        pytest.skip("Playwright's Chromium isn't installed here")
    assert out["error"][0] == 504 and "ABI_RENDER_TIMEOUT" in out["error"][1] and 3 <= out["took"] < 30, out
    assert out["left"] == [], "its Chromium is gone"


def test_a_video_past_its_time_is_stopped_with_all_it_started(tmp_path, monkeypatch):
    """1.1 waited for Remotion's render for ever. Past ABI_VIDEO_TIMEOUT it stops, and so does every process it
    started, also one that left its process group as Remotion's Chromium does."""
    pids = tmp_path / "pids"
    (tmp_path / "npx").write_text(
        f"#!{sys.executable}\nimport os, subprocess, sys, time\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{kid.pid}}')\ntime.sleep(60)\n")
    (tmp_path / "npx").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ABI_VIDEO_TIMEOUT", "3")
    t0 = time.monotonic()
    with pytest.raises(SystemExit, match="ABI_VIDEO_TIMEOUT"):
        video.render({"slides": []}, tmp_path / "deck.mp4")
    assert time.monotonic() - t0 < 20
    end = time.monotonic() + 5
    while (alive := [p for p in map(int, pids.read_text().split()) if _alive(p)]) and time.monotonic() < end:
        time.sleep(0.1)
    assert not alive, alive


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return "Z" not in os.popen(f"ps -o stat= -p {pid}").read()  # a zombie isn't running: its parent reaps it


# .pi/extensions/abi/index.ts loaded as Pi loads it (test_app's stand-in), its tool_result hook given a big add_data's
# queued answer, against a stand-in engine that answers load_runs: running, then done
WAIT = r"""
import { registerHooks } from "node:module";
registerHooks({ resolve: (s, c, next) => s.startsWith("@earendil-works/")
  ? { url: "data:text/javascript,export const Type = new Proxy({}, { get: () => () => ({}) });", shortCircuit: true } : next(s, c) });
globalThis.__dirname = process.argv[1];  // Pi loads extensions as CommonJS
const on = {}, pi = new Proxy({}, { get: (_, k) => k === "on" ? (n, f) => (on[n] = f) : () => [] });
await (await import(process.argv[2])).default(pi);
const statuses = [], ctx = { ui: { setStatus: (k, t) => statuses.push([k, t ?? null]) }, hasUI: false };
const queued = { result: { run: "r1", table: "orders", status: "queued", columns: ["run", "table", "status"], rows: [["r1", "orders", "queued"]] } };
const out = await on.tool_result({ toolName: "mcp__abi__add_data", toolCallId: "t1", input: { project: "p", table: "orders" },
  content: [{ type: "text", text: JSON.stringify(queued) }], isError: false }, ctx);
console.log(JSON.stringify({ out, statuses: statuses.filter(([k]) => k === "abi-load") }));
"""


def test_the_agent_waits_for_a_big_load_with_short_calls(tmp_path):
    """MCP gives up on a call after 600 s (.pi/mcp.json) and a 100M-row load takes longer. The engine answers at once
    with the run; the extension then asks load_runs every few seconds, shows the rows so far on the status line (the
    app's chat puts it on the step) and gives the agent the load's outcome as the tool's answer."""
    import http.server
    import threading
    asked = []

    class Engine(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            asked.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            run = {"run": "r1", "table": "orders", "action": "add_data", "snapshot": None, "error": None}
            run |= {"status": "running", "rows": 1234567} if len(asked) == 1 else {"status": "done", "rows": 5000000, "snapshot": 42}
            body = json.dumps({"runs": [run], "columns": [], "rows": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ext = abi.ROOT / ".pi/extensions/abi/index.ts"
    try:
        r = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", WAIT, str(ext.parent), str(ext)],
                           cwd=abi.ROOT, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "ABI_PI_LLM_BASE": "http://127.0.0.1:9/v1", "ABI_TOKEN": "t",
                                "ABI_URL": f"http://127.0.0.1:{srv.server_address[1]}", "ABI_PI_WORKSPACES": str(tmp_path)})
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.splitlines()[-1])
    assert asked == [("/api/actions/load_runs", {"project": "p", "run": "r1"})] * 2, asked
    answer = json.loads(got["out"]["content"][0]["text"])
    assert not got["out"]["isError"] and answer["result"]["status"] == "done" and answer["result"]["rows"] == [[42, 5000000]], got
    assert got["statuses"] == [["abi-load", "orders: loading, about 1,234,567 rows so far"], ["abi-load", None]], got


def test_the_chat_and_the_sources_page_show_a_big_loads_progress():
    """The app's chat puts the extension's load status on the running step (and no other status line); the Sources
    page's card says queued, then the rows so far, then the rows loaded, or ABI's error, polling load_runs."""
    import types
    out = []
    h = app.Hub.__new__(app.Hub)
    h.pi = types.SimpleNamespace(stdout=iter(json.dumps(x) + "\n" for x in [
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "mcp__abi__add_data"},
        {"type": "extension_ui_request", "id": "u1", "method": "setStatus", "statusKey": "abi-load", "statusText": "orders: queued"},
        {"type": "extension_ui_request", "id": "u2", "method": "setStatus", "statusKey": "abi", "statusText": "ABI p · 9 ABI tools"}]))
    h.busy, h.emit, h.chat_note, h.send = False, out.append, lambda *a: None, lambda *a: None
    h.read()
    assert [e for e in out if e["type"] != "step"] == [{"type": "progress", "text": "orders: queued"}], out
    page = app.JS[app.JS.index("async function loaded("):app.JS.index("\nfunction sources(")]
    js = ("const said = [], asked = [], say = (m, t) => said.push(t), wait = async () => {}, fmt = (v) => v.toLocaleString('en');"
          "let runs = [];const api = async (a, b) => (asked.push([a, b]), {runs: [runs.shift()]});\n" + page +
          "\n(async () => { const r = {run: 'r1', table: 'orders', status: 'queued'}, out = [await loaded({snapshot: 3}, null, 'Loaded.')];"
          "runs = [{table: 'orders', status: 'running', rows: 1234567}, {table: 'orders', status: 'done', rows: 4500000}];"
          "out.push(await loaded(r, null, 'Loaded orders.'));"
          "runs = [{table: 'orders', status: 'failed', error: 'Loading failed (IOException).'}];"
          "try { await loaded(r, null, 'x') } catch (e) { out.push(e.message) }"
          "console.log(JSON.stringify({out, said, asked})) })()")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    assert got["out"] == ["Loaded.", "Loaded orders. 4,500,000 rows.", "Loading failed (IOException)."], got
    assert got["said"] == ["orders: queued, it starts when the loads ahead of it are done…", "orders: loading, about 1,234,567 rows so far…",
                           "orders: queued, it starts when the loads ahead of it are done…"], got
    assert got["asked"] == [["load_runs", {"run": "r1"}]] * 3, got
