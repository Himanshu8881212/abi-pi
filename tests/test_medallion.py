"""1.2 M4.2 on this machine's engine, no LLM: silver feeds gold. A `source` step may name a table the project builds
(`{"type": "source", "table": ...}`), read from its model, so a bronze -> silver -> gold rebuild reads the orders source
once. Brewline (bench/brewline.py, its smallest scale) built both ways on the same files: the 1.1 way (silver and gold
each from the source, gold repeating silver's steps: a project from before, unchanged) and the 1.2 way (silver from
bronze's table, gold from silver's), each rebuilt as `refresh bronze_orders` does it, in the engine's environment with
the source reader counted. A table with no steps loaded from files is stored once (raw.<t> was a copy of model.<t>)."""
import json
import shutil
import subprocess
import sys

import duckdb
import pytest
from conftest import drop
from test_doors import projects
from test_engine import engine

import abi
from pi_driver import ROOT

sys.path.insert(0, str(ROOT / "bench"))
import perf  # noqa: E402

OLD, NEW = "pi_medallion_old", "pi_medallion_new"
DIR = "pi_medallion"  # a server path in the engine's files folder
LAYERS = {OLD: [("silver_orders", perf.SILVER), ("gold_revenue", perf.SILVER + perf.GOLD)],
          NEW: [("silver_orders", [{"type": "source", "table": "bronze_orders"}, *perf.SILVER[1:]]),
                ("gold_revenue", [{"type": "source", "table": "silver_orders"}, *perf.GOLD])]}

# refresh bronze_orders as the refresh action does it, counting each read of a file source (ingest._reader builds it)
REBUILD = r"""
import json, os
from pathlib import Path
from abi import db, ingest
lake = Path(os.environ['ABI_DATA_PATH']) / P
size = lambda: {s: sum(f.stat().st_size for f in (lake / s).rglob('*') if f.is_file()) for s in ('raw', 'model')}
reads, read = [], ingest._reader
ingest._reader = lambda fmt, loc, src: (reads.append(src.sid), read(fmt, loc, src))[1]
before = size()
with db.connect() as c:
    failed = ingest.refresh(c, P, set(), 'test', reload={'bronze_orders'})
print(json.dumps({'failed': failed, 'reads': reads, 'bytes': {k: v - before[k] for k, v in size().items()}}))
"""
LAYOUT = r"""
import json
from abi import storage
print(json.dumps(storage.lake(P).cursor().execute("SELECT schema_name || '.' || table_name, list(column_name || ' ' || data_type"
    " ORDER BY column_index) FROM duckdb_columns() WHERE database_name = 'lake' AND schema_name IN ('raw', 'model')"
    " GROUP BY 1 ORDER BY 1").fetchall()))
"""


def layout(p: str) -> dict:
    """The project's raw and model tables, as the lake has them: {schema.table: [column TYPE, ...]}."""
    r = engine(f"P = {p!r}\n{LAYOUT}")
    assert r.returncode == 0, r.stderr[-2000:]
    return dict(json.loads(r.stdout.splitlines()[-1]))


@pytest.fixture(scope="module")
def built():
    drop([p for p in LAYERS if p in projects()])  # an interrupted run's
    d = perf.FILES / DIR
    perf.brew(37_000, d)
    try:
        for p, layers in LAYERS.items():
            abi.call("create_project", project=p, name=p)
            for t in ("cafes", "products", "orders"):
                perf.add(p, f"bronze_{t}", f"{DIR}/{t}/*.parquet")
            for t, steps in layers:
                abi.finished(p, abi.call("set_steps", project=p, table=t, steps=steps))
        yield d
    finally:
        drop([p for p in LAYERS if p in projects()])
        shutil.rmtree(d, ignore_errors=True)


def git(p: str, *args: str) -> str:
    return subprocess.run(["git", "-C", str(abi.ABI_PROJECTS / p), *args], capture_output=True, text=True, check=True).stdout


def test_silver_feeds_gold_reading_the_source_once(built):
    """Gold's revenue by region equals a hand-written DuckDB query over the files to the cent, both ways; the 1.2
    rebuild reads the orders source once (1.1: three times) and writes at most 60% of 1.1's bytes in raw/ and model/;
    bronze, silver and gold are rebuilt in that order, once each. The 1.1 project's steps stay as they were."""
    want = {r: round(float(v), 2) for r, v in duckdb.sql(
        f"SELECT c.region, sum(o.revenue) FROM read_parquet('{built}/orders/*.parquet') o"
        f" JOIN read_parquet('{built}/cafes/*.parquet') c USING (cafe_id) WHERE o.quantity > 0 GROUP BY 1").fetchall()}
    assert len(want) == 4
    since = {p: perf.snapshot(p) for p in LAYERS}
    out = {}
    for p in LAYERS:
        r = engine(f"P = {p!r}\n{REBUILD}", timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]
        out[p] = json.loads(r.stdout.splitlines()[-1])
        assert out[p]["failed"] == {}, out[p]
        got = abi.call("sql_query", project=p, sql="SELECT region, sum(revenue) FROM gold_revenue GROUP BY 1")["rows"]
        assert {r: round(float(v), 2) for r, v in got} == want, (p, got)
        made = [x[3] for x in abi.call("list_snapshots", project=p, limit=100)["rows"] if x[0] > since[p]][::-1]
        made = [x for x in made if x.startswith("refresh ")]  # 1.1: silver and gold both read only bronze, in either order
        assert (made if p == NEW else made[:1] + sorted(made[1:], reverse=True)) == [
            "refresh bronze_orders", "refresh silver_orders", "refresh gold_revenue"], made
    assert out[OLD]["reads"] == ["bronze_orders"] * 3 and out[NEW]["reads"] == ["bronze_orders"], out
    old, new = (sum(out[p]["bytes"].values()) for p in (OLD, NEW))
    assert 0 < new <= 0.6 * old, out
    steps = json.loads((abi.ABI_PROJECTS / OLD / "tables/silver_orders.json").read_text())["steps"]
    assert steps[0] == perf.SILVER[0] and [x["type"] for x in steps] == [x["type"] for x in perf.SILVER], steps  # no "table" key
    raw = {p: sorted(t for t in layout(p) if t.startswith("raw.")) for p in LAYERS}  # silver and gold of 1.1 load their own
    assert raw == {OLD: ["raw.gold_revenue", "raw.silver_orders"], NEW: []}, raw


def test_a_table_with_no_steps_is_stored_once_and_still_typed(built):
    """Text dates in a CSV still become DATE (IN-5) in the table's one copy; preview_steps reads such a table, and a
    table its steps start from."""
    (built / "days.csv").write_text("day,n\n31.01.2024,1\n01.02.2024,2\n")
    perf.add(NEW, "days", f"{DIR}/days.csv")
    lake = layout(NEW)
    assert lake["model.days"] == ["day DATE", "n BIGINT"] and "raw.days" not in lake, lake
    assert abi.call("sql_query", project=NEW, sql="SELECT max(day) FROM days")["rows"] == [["2024-02-01"]]
    got = abi.call("preview_steps", project=NEW, steps=[{"type": "source", "source": "days"}, {"type": "filter", "condition": "n > 1"}])
    assert got["columns"] == [{"name": "day", "type": "DATE"}, {"name": "n", "type": "BIGINT"}] and got["rows"] == [["2024-02-01", "2"]], got
    got = abi.call("preview_steps", project=NEW, steps=[{"type": "source", "table": "gold_revenue"}])
    assert [c["name"] for c in got["columns"]] == ["region", "month", "revenue", "orders"] and len(got["rows"]) > 4, got


def test_a_cycle_is_refused_and_git_stays_clean(built):
    """Two tables (silver from gold, which reads silver), three (silver from platinum, from gold, from silver) and one
    reading itself are refused at set_steps with a plain error; no file changes and nothing is committed (the project's
    work so far stays uncommitted, as it was)."""
    abi.finished(NEW, abi.call("set_steps", project=NEW, table="platinum", steps=[{"type": "source", "table": "gold_revenue"}]))
    before = [git(NEW, *x) for x in (("rev-parse", "HEAD"), ("status", "--porcelain"), ("diff", "HEAD"))]
    cycle = "Tables would read each other in a cycle: "
    for t, frm, why in (("silver_orders", "gold_revenue", {cycle, "silver_orders reads gold_revenue", "gold_revenue reads silver_orders"}),
                        ("silver_orders", "platinum", {cycle, "silver_orders reads platinum", "platinum reads gold_revenue",
                                                       "gold_revenue reads silver_orders"}),
                        ("gold_revenue", "gold_revenue", {"a table can't read itself"}),
                        ("gold_revenue", "nowhere", {"the project builds no such table"})):
        with pytest.raises(abi.ActionError) as e:
            abi.call("set_steps", project=NEW, table=t, steps=[{"type": "source", "table": frm}])
        assert e.value.status == 400 and all(w in e.value.body["message"] for w in why), e.value.body
        assert [git(NEW, *x) for x in (("rev-parse", "HEAD"), ("status", "--porcelain"), ("diff", "HEAD"))] == before
    with pytest.raises(abi.ActionError) as e:  # a step names one of the two
        abi.call("set_steps", project=NEW, table="x", steps=[{"type": "source", "source": "bronze_orders", "table": "silver_orders"}])
    assert e.value.status in (400, 422), e.value.body


# IN-6 as the heavy worker runs it, twice: the first time silver's build fails (bronze's new file lands, gold is skipped)
FAIL_ONCE = r"""
import shutil
import duckdb
from abi import ingest, storage
real, hit = storage.refresh, []
def broken(con, table, *a, **k):
    if table == 'silver_orders':
        hit.append(table)
        raise duckdb.IOException('silver fails this once (test)')
    return real(con, table, *a, **k)
storage.refresh = broken
shutil.copy(SRC, DST)
ingest.refresh_changed(only=P)
storage.refresh = real
ingest.refresh_changed(only=P)
print(len(hit))
"""


def test_a_rebuild_that_failed_after_its_input_landed_runs_at_the_next_check(built):
    """M4 review: a built table's fingerprint holds its inputs' (bronze's files), so a silver build that failed (or was
    killed) after bronze's new snapshot runs at IN-6's next check, and gold after it: gold matches the files again."""
    first = sorted((built / "orders").glob("*.parquet"))[0]
    r = engine(f"P, SRC, DST = {NEW!r}, {str(first)!r}, {str(built / 'orders' / 'zz_again.parquet')!r}\n{FAIL_ONCE}", timeout=300)
    assert r.returncode == 0 and r.stdout.split()[-1] == "1", (r.stdout[-500:], r.stderr[-2000:])  # silver's build failed once
    want = {r: round(float(v), 2) for r, v in duckdb.sql(
        f"SELECT c.region, sum(o.revenue) FROM read_parquet('{built}/orders/*.parquet') o"
        f" JOIN read_parquet('{built}/cafes/*.parquet') c USING (cafe_id) WHERE o.quantity > 0 GROUP BY 1").fetchall()}
    got = {r: round(float(v), 2) for r, v in abi.call(
        "sql_query", project=NEW, sql="SELECT region, sum(revenue) FROM gold_revenue GROUP BY 1")["rows"]}
    assert got == want, (got, want)
