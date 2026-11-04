"""1.2 M3.1 on this machine's engine, no LLM: projects work in parallel. A lock per project (its lake writes and attach,
its Git repo, its in-memory views) lets two projects load at once while one project's writes still go one at a time;
heavy jobs (loads, query copies, ML) have their own worker queue, so a long one never holds up a tick or an answer."""
import json
import os
import shutil
import threading
import time
from pathlib import Path

import duckdb
import pytest
from conftest import drop
from test_doors import projects, sure
from test_engine import engine

import abi

A, B, A2 = "pi_lock_a", "pi_lock_b", "pi_lock_a2"
FILES = Path(os.environ.get("ABI_PI_DATA") or abi.ROOT / "data") / "files"  # scripts/engine.sh's files folder
LOC, N = "pi_lock/orders.parquet", 1_500_000  # a server path there: the workspace owner reads every one
EXACT = [[N, N // 1000 * 499_500, 4]]  # orders, the sum of amount (0..999 over and over), regions
# delete_project keeps a project's lake 30 days (PC-7): this test's (about 100 MB) goes at once, as purge_deleted does it
PURGE = r"""
import shutil
from abi import db
with db.connect() as c:
    gone = [r["nspname"] for r in c.execute(r"SELECT nspname FROM pg_namespace WHERE nspname ~ '^_deleted_[0-9]+_pi_lock_(a|b|a2)$'")]
for s in gone:
    with db.connect() as c:
        path = c.execute(f'SELECT value FROM "{s}".ducklake_metadata WHERE key = %s', ["data_path"]).fetchone()["value"]
        c.execute("SELECT pg_terminate_backend(l.pid) FROM pg_locks l JOIN pg_class r ON r.oid = l.relation JOIN pg_namespace n"
                  " ON n.oid = r.relnamespace WHERE n.nspname = %s AND l.pid <> pg_backend_pid()", [s])  # a stale cached lake's
        c.execute("SET LOCAL lock_timeout = '30s'")
        c.execute(f'DROP SCHEMA "{s}" CASCADE')
    shutil.rmtree(path, ignore_errors=True)
"""


def load(p: str) -> None:
    abi.call("add_data", project=p, table="orders", location=LOC)


def exact(p: str) -> list:
    return abi.call("sql_query", project=p, sql="SELECT count(*), sum(amount), count(DISTINCT region) FROM orders")["rows"]


def at_once(*steps, timeout: float = 300) -> float:
    """Each step in its own thread; the seconds until all are done. One still running after `timeout` is stuck (a
    deadlock); an error fails the test."""
    errors = []

    def run(step):
        try:
            step()
        except Exception as e:  # noqa: BLE001 - reported below, with the others
            errors.append(e)
    ts, t0 = [threading.Thread(target=run, args=(s,), daemon=True) for s in steps], time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join(max(0.0, t0 + timeout - time.monotonic()))
    assert not any(t.is_alive() for t in ts), f"still running after {timeout} s: a deadlock"
    assert not errors, errors
    return time.monotonic() - t0


@pytest.fixture(scope="module")
def two():
    """Projects A and B, each with the same 1.5M orders loaded from one file."""
    drop([p for p in (A, B, A2) if p in projects()])  # an interrupted run's
    (FILES / LOC).parent.mkdir(parents=True, exist_ok=True)
    duckdb.sql(f"COPY (SELECT range AS order_id, ['North', 'South', 'East', 'West'][range % 4 + 1] AS region, range % 1000 AS amount"
               f" FROM range({N})) TO '{FILES / LOC}' (FORMAT parquet)")
    try:
        for p in (A, B):
            abi.call("create_project", project=p, name=p)
        at_once(lambda: load(A), lambda: load(B))
        yield
    finally:
        drop([p for p in (A, B, A2) if p in projects()])
        shutil.rmtree((FILES / LOC).parent, ignore_errors=True)
        assert engine(PURGE, timeout=180).returncode == 0


def test_two_projects_load_at_once_and_both_are_exact(two):
    """1.1 had one lake write lock and one repo lock per process for every project: a load waited for any other
    project's, so two loads at once took as long as one after the other (here about 0.58 of it, 1.1 about 1; the
    better of two rounds, so one hiccup of a busy machine doesn't decide it)."""
    rounds = []
    for _ in range(2):
        one_by_one = at_once(lambda: load(A)) + at_once(lambda: load(B))
        rounds.append((at_once(lambda: load(A), lambda: load(B)) / one_by_one, one_by_one))
    assert min(rounds)[0] < 0.8, f"together / one after the other (s): {rounds}"
    assert exact(A) == exact(B) == EXACT


def test_a_slow_step_in_one_project_stalls_no_other_project(tmp_path):
    """While project X's lake attach, repo action, write or rebuild of its edit views is held up 4 s, project Y's
    takes well under that (1.1: each waited for X's under one lock per process; the review's "a slowed attach in A
    doesn't delay B")."""
    r = engine(f"""
import json, os, threading, time
os.environ['ABI_DATA_PATH'], os.environ['ABI_PROJECTS_PATH'] = '{tmp_path}/lake', '{tmp_path}/projects'
from abi import db, semantic, storage, writeback
from abi.project import git
x, y = 'pi_lock_x', 'pi_lock_y'
with db.connect() as c:
    for p in (x, y):
        c.execute(f'DROP SCHEMA IF EXISTS {{p}} CASCADE')
        os.makedirs(f'{tmp_path}/projects/{{p}}/.git')  # git.locked's repo: its flock file
cony = storage.lake(y)
storage.refresh(cony, 't', 'SELECT 1 AS i', author='test')
slow, connect, views = threading.local(), storage.connect, writeback.views
storage.connect = lambda p, *a, **k: (time.sleep(4) if p == x else None) or connect(p, *a, **k)
writeback.views = lambda cur: (time.sleep(4) if getattr(slow, 'on', False) else None) or views(cur)

def waited(hold_x, step_y):
    t = threading.Thread(target=hold_x)
    t.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    step_y()
    out = round(time.monotonic() - t0, 2)
    t.join()
    return out

def rebuild(con):
    slow.on = True
    semantic._edited(con, 10**9)  # a snapshot newer than any: rebuilt

def repo(p, s):
    with git.locked(p):
        time.sleep(s)

def recheck():
    storage._checked[y] = 0  # matched against the catalog now: a Postgres round trip under Y's lock
    storage.lake(y)

out = {{'attach': waited(lambda: storage.lake(x), recheck)}}
conx = storage.lake(x)
out['repo'] = waited(lambda: repo(x, 4), lambda: repo(y, 0))
out['write'] = waited(lambda: storage.refresh(conx, 't', 'SELECT 1 AS i', author='test', hook=lambda cur: time.sleep(4)),
                      lambda: storage.refresh(cony, 't', 'SELECT 2 AS i', author='test'))
out['views'] = waited(lambda: rebuild(conx), lambda: semantic._edited(cony, 10**9))
storage._close_lakes()
with db.connect() as c:
    for p in (x, y):
        c.execute(f'DROP SCHEMA {{p}} CASCADE')
print(json.dumps(out))
""", timeout=180)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert all(s < 2 for s in out.values()) and set(out) == {"attach", "repo", "write", "views"}, (out, r.stderr[-2000:])


def test_writes_to_one_project_still_go_one_at_a_time(tmp_path):
    """A project keeps its own write queue: 8 threads appending 3 rows each to one table lose no row (each append
    rebuilds model.t from raw.t), and each write is one snapshot of its own (ST-2, ST-3)."""
    r = engine(f"""
import json, os, threading
os.environ['ABI_DATA_PATH'] = '{tmp_path}'
from abi import db, storage
with db.connect() as c:
    c.execute('DROP SCHEMA IF EXISTS pi_lock_w CASCADE')
con, errors = storage.lake('pi_lock_w'), []
first = storage.refresh(con, 't', 'SELECT 0 AS i', author='test')

def write(k):
    try:
        for j in range(3):
            storage.refresh(con, 't', '', author='test', append=lambda cur, v=10 * k + j: f'SELECT {{v}} AS i')
    except Exception as e:
        errors.append(repr(e))
ts = [threading.Thread(target=write, args=(k,)) for k in range(1, 9)]
for t in ts:
    t.start()
for t in ts:
    t.join()
cur = con.cursor()
out = [errors, list(cur.execute('SELECT count(*), sum(i) FROM lake.model.t').fetchone()),
       cur.execute('SELECT count(*) FROM lake.snapshots() WHERE snapshot_id > ?', [first]).fetchone()[0]]
storage._close_lakes()
with db.connect() as c:
    c.execute('DROP SCHEMA pi_lock_w CASCADE')
print(json.dumps(out))
""", timeout=180)
    want = [[], [25, sum(10 * k + j for k in range(1, 9) for j in range(3))], 24]
    assert json.loads(r.stdout.strip().splitlines()[-1]) == want, r.stderr[-2000:]


def test_a_sleeping_heavy_job_never_holds_up_a_default_one():
    """The running workers: a training job (heavy queue) that waits on a held row stays running while a minute tick's
    job (default queue) runs and finishes within seconds. 1.1's one worker ran one job at a time: the tick waited for
    the training. A job a 1.1 volume queued (every one on "default") still runs. A flow run waits on the heavy queue
    only when its flow loads, trains, scores or renders."""
    r = engine("""
import json, time, uuid
import psycopg
from abi import db, flows, worker
from abi.actions import ActionFailed
run = str(uuid.uuid4())
with db.connect() as c:
    c.execute("INSERT INTO ml_runs (id, project, model, status, spec) VALUES (%s, 'pi_queue_test', 'nap', 'queued', '{}')", [run])
hold = psycopg.connect()  # the training's first step (ml.job: UPDATE ml_runs ... WHERE status = 'queued') waits on this row
hold.execute("SELECT 1 FROM ml_runs WHERE id = %s FOR UPDATE", [run])

def status(job):
    with db.connect() as c:
        return c.execute("SELECT status::text AS s FROM procrastinate_jobs WHERE id = %s", [job]).fetchone()["s"]

def until(job, done, wait):
    end = time.monotonic() + wait
    while status(job) not in done and time.monotonic() < end:
        time.sleep(0.2)
    return status(job)

out = {"queues": [worker.train_model.queue, worker.build_copies.queue, worker.refresh_changed.queue, worker.flow_tick.queue,
                  worker.answer_slack.queue]}
with worker.app.open():
    heavy = worker.train_model.defer(run_id=run)
    out["heavy"] = until(heavy, ("doing",), 120)
    t0 = time.monotonic()
    tick = worker.purge_inbound.configure(queueing_lock=f"pi-queue-test-{run}").defer(timestamp=0)
    out["tick"] = until(tick, ("succeeded", "failed"), 60)
    out["tick_s"], out["heavy_meanwhile"] = time.monotonic() - t0, status(heavy)
    old = worker.train_model.configure(queue="default").defer(run_id=str(uuid.uuid4()))  # queued as 1.1 queued every job
    out["old"] = until(old, ("succeeded", "failed"), 60)  # no such run: it ends at once, in the default worker
hold.execute("UPDATE ml_runs SET status = 'failed' WHERE id = %s", [run])  # not queued any more: the job ends at once
hold.commit()
hold.close()
out["heavy_after"] = until(heavy, ("succeeded", "failed"), 60)
with db.connect() as c:
    c.execute("DELETE FROM ml_runs WHERE id = %s", [run])
steps = {"load": [{"type": "refresh"}], "train": [{"type": "condition"}, {"type": "train"}], "agent": [{"type": "agent"}],
         "alert": [{"type": "condition"}, {"type": "action", "action": "post_message"}],
         "copy": [{"type": "action", "action": "add_data"}]}

def load(p, f):
    if f not in steps:
        raise ActionFailed(404, "no such flow")
    return {"steps": steps[f]}
flows._load = load
out["flows"] = {f: flows._queue(f"p/{f}/k") for f in [*steps, "gone"]}
print(json.dumps(out))
""", timeout=300)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["queues"] == ["heavy", "heavy", "heavy", "default", "default"], out
    assert out["heavy"] == "doing" and out["tick"] == "succeeded" and out["heavy_meanwhile"] == "doing", (out, r.stderr[-2000:])
    assert out["tick_s"] < 15 and out["heavy_after"] == "succeeded" and out["old"] == "succeeded", out
    assert out["flows"] == {"load": "heavy", "train": "heavy", "agent": "default", "alert": "default", "copy": "heavy", "gone": "default"}


def test_imports_and_renames_between_two_projects_while_both_load_never_deadlock(two):
    """Steps on two projects take their locks in name order. While A and B reload, A's files are imported into B and
    B's into A (Publish's import into a copy, both ways at once: the shape that deadlocks with locks taken in call
    order); then A is renamed (its lake moved under both IDs' locks) while B reloads. Everything finishes, and the data
    is exact under A's new ID and in B."""
    files = {p: {str(f.relative_to(abi.ABI_PROJECTS / p)): json.loads(f.read_text())
                 for f in (abi.ABI_PROJECTS / p).rglob("*.json") if ".git" not in f.parts} for p in (A, B)}
    at_once(lambda: load(A), lambda: load(B), lambda: sure("import_project", project=B, bundle=files[A]),
            lambda: sure("import_project", project=A, bundle=files[B]))
    at_once(lambda: sure("rename_project", project=A, new=A2), lambda: load(B))
    assert A not in projects() and exact(A2) == exact(B) == EXACT
