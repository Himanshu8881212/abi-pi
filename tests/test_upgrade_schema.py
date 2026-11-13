"""1.2.6 C1: db.SCHEMA folds the ALTERs every shipped database already ran into its CREATE TABLEs. The proof: a database
made exactly as v1.0.0's db.py makes it, upgraded by this branch's init, has every column, constraint, index and
trigger a fresh database gets."""
import json

import pytest

from test_engine import PY, engine

UPGRADE = r"""
import ast, json, os, subprocess
os.environ["ABI_PG_POOL"] = "0"  # every connect() reads PGDATABASE as set below
import psycopg
from abi import db

REF, WAS = os.environ.get("ABI_UPGRADE_REF", "v1.0.0"), os.environ.get("ABI_UPGRADE_VERSION", "1.0.0")
old = subprocess.run(["git", "show", f"{REF}:engine/backend/abi/db.py"], capture_output=True, text=True, check=True).stdout
lit = {t.id: ast.literal_eval(n.value) for n in ast.parse(old).body if isinstance(n, ast.Assign)
       for t in n.targets if isinstance(t, ast.Name) and t.id in ("STATE", "SCHEMA", "SCHEMA_NUMBER")}
OLD, NEW = "abi_upgrade_v1_0", "abi_upgrade_fresh"
admin = psycopg.connect(autocommit=True)
for d in (OLD, NEW):
    admin.execute(f"DROP DATABASE IF EXISTS {d} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {d}")
with psycopg.connect(dbname=OLD) as c:  # v1.0.0's init: STATE, SCHEMA, its schema number
    c.execute(lit["STATE"])
    c.execute(lit["SCHEMA"])
    c.execute("INSERT INTO schema_state (app_version, app_schema) VALUES (%s, %s)", [WAS, lit["SCHEMA_NUMBER"]])
Q = {"columns": "SELECT table_name, column_name, data_type, is_nullable, column_default, is_identity"
                " FROM information_schema.columns WHERE table_schema = 'public'",
     "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE connamespace = 'public'::regnamespace",
     "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
     "triggers": "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal"}
# M10 round 4: one project's rows as a 1.2.5 install holds them, put in before this code's init and read back after it
SEED = [
    "INSERT INTO users (name, password_hash, workspace_owner) VALUES ('up_owner', 'x', true), ('up_viewer', 'x', false)",
    "INSERT INTO project_roles SELECT id, 'up_brew', 'viewer' FROM users WHERE name = 'up_viewer'",
    "INSERT INTO memory (project, text, created_by) VALUES ('up_brew', 'Revenue is net of discounts.', 'up_owner')",
    "INSERT INTO notifications (project, kind, message) VALUES ('up_brew', 'flow', 'Flow Monday: works again.')",
    "INSERT INTO sources (project, table_name, spec, fingerprint) VALUES ('up_brew', 'orders', '{\"location\": \"orders.parquet\"}', 'f1')",
    "INSERT INTO flow_runs (id, project, flow, status) VALUES ('up_brew/monday/2026-11-09T07:00Z', 'up_brew', 'monday', 'ok')",
    "INSERT INTO flow_steps (run_id, pos, step, status, input, output, started) VALUES ('up_brew/monday/2026-11-09T07:00Z', 0, 'q', 'ok',"
    " '{\"measures\": [\"revenue\"]}', '{\"rows\": [[1]]}', now())",
    "INSERT INTO flow_schedule VALUES ('up_brew', 'monday', now())",
    "INSERT INTO send_targets VALUES ('up_brew', 'mail:ops@example.com')",
    "INSERT INTO inbox (project, sender, text, data) VALUES ('up_brew', 'flows', 'Weekly summary', '{\"columns\": [\"revenue\"], \"rows\": [[1]]}')",
    "INSERT INTO flow_conditions (project, flow, holds, fired) VALUES ('up_brew', 'monday', true, now())",
]
KEPT = {"users": "name, password_hash, workspace_owner", "project_roles": "user_id, project, role", "memory": "project, text, created_by, created",
        "notifications": "project, kind, message, at", "sources": "project, table_name, spec, fingerprint",
        "flow_runs": "id, project, flow, status, error, created, finished", "flow_steps": "run_id, pos, step, status, input, output, started, finished",
        "flow_schedule": "project, flow, checked", "send_targets": "project, target", "inbox": "project, sender, text, data, at",
        "flow_conditions": "project, flow, holds, fired"}
def rows():
    with psycopg.connect(dbname=OLD) as c:
        return {t: sorted(json.dumps(r, default=str) for r in c.execute(f"SELECT {cols} FROM {t}").fetchall()) for t, cols in KEPT.items()}
seeded = os.environ.get("ABI_UPGRADE_ROWS") == "1"
if seeded:
    with psycopg.connect(dbname=OLD) as c:
        for q in SEED:
            c.execute(q)
    before = rows()
got, state = {}, {}
for d in (OLD, NEW):
    os.environ["PGDATABASE"] = d
    db.init(queue=True)  # the worker's start: SCHEMA and Procrastinate's schema
    with psycopg.connect(dbname=d) as c:
        got[d] = {k: {json.dumps(r, default=str) for r in c.execute(q).fetchall()} for k, q in Q.items()}
        state[d] = c.execute("SELECT app_schema FROM schema_state").fetchone()[0]
kept = {"rows": [rows() == before, sum(map(len, before.values()))]} if seeded else {}
for d in (OLD, NEW):
    admin.execute(f"DROP DATABASE {d} WITH (FORCE)")
print(json.dumps({k: sorted(got[NEW][k] - got[OLD][k]) for k in Q} | kept | {"fresh": {k: len(v) for k, v in got[NEW].items()},
                  "extra": {k: sorted(got[OLD][k] - got[NEW][k]) for k in Q},
                  "schema": [lit["SCHEMA_NUMBER"], state[OLD], state[NEW], db.SCHEMA_NUMBER]}))
"""


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_1_0_database_upgrades_to_this_schema():
    r = engine(UPGRADE, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    fresh, extra = out.pop("fresh"), out.pop("extra")
    assert out.pop("schema")[1:] == [7, 7, 7], "the upgraded database records this code's schema number"
    assert fresh["columns"] > 300 and fresh["constraints"] > 100, fresh  # the comparison read the whole schema
    assert out == {"columns": [], "constraints": [], "indexes": [], "triggers": []}, out  # nothing a fresh one has is missing
    # and nothing more than a fresh one but the column C1 dropped: a fold that lost a column fails here
    assert extra == {"columns": ['["sources", "spec", "jsonb", "YES", null, "NO"]'], "constraints": [], "indexes": [], "triggers": []}, extra


REF_125 = "d5496bd"  # the 1.2.5 code 1.2.6 is built on (1.2.5 has no tag of its own): an ancestor, so every clone has it


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_1_2_5_database_upgrades_to_this_schema(monkeypatch):
    """1.2.6 item 34: a database made exactly as 1.2.5's db.py makes it (schema 5), upgraded by this branch's init, has
    every column, constraint, index and trigger a fresh one gets, records schema 7, keeps nothing more than the
    column C1 dropped from the schema, and every row a project left in it reads the same afterwards."""
    monkeypatch.setenv("ABI_UPGRADE_REF", REF_125)
    monkeypatch.setenv("ABI_UPGRADE_VERSION", "1.2.5")
    monkeypatch.setenv("ABI_UPGRADE_ROWS", "1")  # with a project's rows in it: accounts, a role, memory, a source, a flow's run, schedule and state
    r = engine(UPGRADE, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    fresh, extra = out.pop("fresh"), out.pop("extra")
    assert out.pop("schema") == [5, 7, 7, 7], "1.2.5's schema number, then this code's on both databases"
    assert out.pop("rows") == [True, 12], "every row the 1.2.5 database held reads the same after the upgrade"
    assert fresh["columns"] > 300 and out == {"columns": [], "constraints": [], "indexes": [], "triggers": []}, out
    assert extra == {"columns": ['["sources", "spec", "jsonb", "YES", null, "NO"]'], "constraints": [], "indexes": [], "triggers": []}, extra


ROUND1 = r"""
import json, os
os.environ["ABI_PG_POOL"] = "0"
import psycopg
from abi import db
D = "abi_upgrade_plans"
admin = psycopg.connect(autocommit=True)
admin.execute(f"DROP DATABASE IF EXISTS {D} WITH (FORCE)")
admin.execute(f"CREATE DATABASE {D}")
os.environ["PGDATABASE"] = D
db.init()
with psycopg.connect(dbname=D) as c:  # the table as M12's first rounds made it, with a run in it
    c.execute("ALTER TABLE optimizer_runs DROP COLUMN started")
    c.execute("ALTER TABLE optimizer_runs DISABLE TRIGGER ALL")
    c.execute("INSERT INTO optimizer_runs (id, project, optimizer, user_id, restricted, spec, status) VALUES"
              " ('00000000-0000-0000-0000-000000000001', 'p', 'hours', 1, false, '{}', 'running')")
    c.execute("ALTER TABLE optimizer_runs ENABLE TRIGGER ALL")
db.init()
db.init()
with psycopg.connect(dbname=D) as c:
    cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'optimizer_runs'")]
    stale = c.execute("SELECT count(*) FROM optimizer_runs WHERE status = 'running' AND started < now() - interval '30 minutes'").fetchone()[0]
    rows = c.execute("SELECT optimizer, status, started FROM optimizer_runs").fetchall()
admin.execute(f"DROP DATABASE {D} WITH (FORCE)")
print(json.dumps({"started": "started" in cols, "stale": stale, "rows": rows}, default=str))
"""


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_plans_table_from_before_started_gets_the_column_and_keeps_its_runs():
    """M10 (M12's LOW): a database whose optimizer_runs was made before `started` existed gets the column at the next
    start, keeps its rows, and the query run_optimizer sweeps stale runs with reads it."""
    r = engine(ROUND1, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"started": True, "stale": 0, "rows": [["hours", "running", None]]}
