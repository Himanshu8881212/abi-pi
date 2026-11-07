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

old = subprocess.run(["git", "show", "v1.0.0:engine/backend/abi/db.py"], capture_output=True, text=True, check=True).stdout
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
    c.execute("INSERT INTO schema_state (app_version, app_schema) VALUES ('1.0.0', %s)", [lit["SCHEMA_NUMBER"]])
Q = {"columns": "SELECT table_name, column_name, data_type, is_nullable, column_default, is_identity"
                " FROM information_schema.columns WHERE table_schema = 'public'",
     "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE connamespace = 'public'::regnamespace",
     "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
     "triggers": "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal"}
got = {}
for d in (OLD, NEW):
    os.environ["PGDATABASE"] = d
    db.init(queue=True)  # the worker's start: SCHEMA and Procrastinate's schema
    with psycopg.connect(dbname=d) as c:
        got[d] = {k: {json.dumps(r, default=str) for r in c.execute(q).fetchall()} for k, q in Q.items()}
for d in (OLD, NEW):
    admin.execute(f"DROP DATABASE {d} WITH (FORCE)")
print(json.dumps({k: sorted(got[NEW][k] - got[OLD][k]) for k in Q} | {"fresh": {k: len(v) for k, v in got[NEW].items()}}))
"""


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_1_0_database_upgrades_to_this_schema():
    r = engine(UPGRADE, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    fresh = out.pop("fresh")
    assert fresh["columns"] > 300 and fresh["constraints"] > 100, fresh  # the comparison read the whole schema
    assert out == {"columns": [], "constraints": [], "indexes": [], "triggers": []}, out  # nothing a fresh one has is missing
