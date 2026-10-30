"""The own engine's housekeeping and loading (needs the engine: scripts/engine.sh start)."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "engine/backend/.venv/bin/python"


def engine(code: str, timeout=60) -> subprocess.CompletedProcess:
    env = subprocess.run(["sh", "-c", "scripts/engine.sh env"], cwd=ROOT, capture_output=True, text=True).stdout
    return subprocess.run(["sh", "-c", f"{env}\nexec {PY} -c \"$0\"", code], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_purge_of_deleted_lakes_is_not_blocked_by_a_stale_cached_connection(tmp_path):
    """A deleted project's lake stays cached (idle in transaction) in the app and the worker; the purge used to wait on
    that lock for ever, so no deleted project's data was ever freed."""
    r = engine(f"""
import psycopg, threading, time
from abi import db, storage
s = '_deleted_1_purgetest'
with db.connect() as c:
    c.execute(f'DROP SCHEMA IF EXISTS "{{s}}" CASCADE')
    c.execute(f'CREATE SCHEMA "{{s}}"')
    c.execute(f'CREATE TABLE "{{s}}".ducklake_metadata (key text, value text)')
    c.execute(f'INSERT INTO "{{s}}".ducklake_metadata VALUES (%s, %s)', ['data_path', '{tmp_path}/x/'])
    c.commit()
stale = psycopg.connect()  # a cached lake's catalog connection, left idle in transaction
stale.execute(f'SELECT * FROM "{{s}}".ducklake_metadata').fetchall()
with db.connect() as c:
    storage.purge_deleted(c)
    print('gone' if not c.execute('SELECT 1 FROM pg_namespace WHERE nspname = %s', [s]).fetchone() else 'kept')
""", timeout=60)
    assert r.stdout.strip() == "gone", r.stderr[-2000:]


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_two_digit_year_date_is_never_read_as_year_2(tmp_path):
    """YouTube's trending_date is yy.dd.mm (18.07.02 = 2 Jul 2018). With every day <= 12, `%d.%m.%Y` parsed it too
    (DuckDB takes "02" for %Y) and won by coming first: the calendar ran from year 2 to year 6."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import abi
    f = tmp_path / "videos.csv"  # one "n/a" keeps the column text, as the real file's multi-line rows do
    f.write_text("trending_date,views\n" + "".join(f"18.{d:02d}.{m:02d},{d * m}\n" for m in (1, 2, 3) for d in range(1, 13) for _ in range(4))
                 + "n/a,0\n")
    r = abi.load("demo", "pi_date_test", [str(f)])
    assert r["loaded"][0].get("date", {}).get("format") == "%y.%d.%m", r["loaded"]
