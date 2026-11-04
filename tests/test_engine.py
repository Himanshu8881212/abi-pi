"""The own engine's housekeeping and loading (needs the engine: scripts/engine.sh start)."""
import json
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
    from conftest import parquet
    r = abi.load("demo", "pi_date_test", parquet(f))
    assert r["loaded"][0].get("date", {}).get("format") == "%y.%d.%m", r["loaded"]


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_named_models_narrow_the_model_queries_and_reports(tmp_path):
    """Several models in one project (model/models.json): a model shows only its tables and measures, a query or a
    report in it can't reach outside it, a report can't name a model that isn't there, and the numbers are the same."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import abi
    (tmp_path / "orders.csv").write_text("order_id,region,amount\n1,North,10\n2,South,20\n3,North,5\n")
    (tmp_path / "visits.csv").write_text("visit_id,page,seconds\n1,home,30\n2,cart,12\n")
    from conftest import parquet
    abi.load("demo", "pi_model_test", parquet(tmp_path / "orders.csv", tmp_path / "visits.csv"))
    P = "pi_model_test"
    ms = {m["id"]: m["expression"] for m in abi.call("semantic_model", project=P, all=True)["measures"]}
    sales = next(i for i, e in ms.items() if "orders.amount" in e)
    web = next(i for i, e in ms.items() if "visits." in e)
    assert abi.call("save_model", project=P, model={"id": "sales", "name": "Sales", "tables": ["orders"]}) == {"id": "sales"}
    m = abi.call("semantic_model", project=P, model="sales", all=True)
    assert set(m["tables"]) == {"orders"} and {x["id"] for x in m["measures"]} >= {sales} and web not in {x["id"] for x in m["measures"]}
    assert m["models"] == [{"id": "sales", "name": "Sales", "tables": ["orders"]}]  # the page filters by its tables
    inside = abi.call("semantic_query", project=P, model="sales", measures=[sales], dimensions=["orders.region"])["rows"]
    assert inside == abi.call("semantic_query", project=P, measures=[sales], dimensions=["orders.region"])["rows"]
    with pytest.raises(abi.ActionError) as e:
        abi.call("semantic_query", project=P, model="sales", measures=[web])
    assert "not in model 'sales'" in str(e.value)
    rep = {"id": "web_in_sales", "name": "W", "model": "sales", "pages": [{"id": "p", "name": "P", "visuals": [
        {"id": "v", "type": "kpi", "title": "t", "query": {"measures": [sales], "dimensions": ["visits.page"]}, "layout": {"x": 0, "y": 0, "w": 3, "h": 3}}]}]}
    with pytest.raises(abi.ActionError):
        abi.call("save_report", project=P, report=rep)
    with pytest.raises(abi.ActionError):
        abi.call("save_report", project=P, report=rep | {"model": "nope"})
    with pytest.raises(abi.ActionError) as e:  # destructive: it waits for a person's yes, as every delete does
        abi.call("delete_model", project=P, id="sales")
    abi.call("delete_model", confirm=e.value.body["confirm_token"], project=P, id="sales")
    assert abi.call("semantic_model", project=P, all=True)["models"] == []


@pytest.mark.skipif(not PY.exists(), reason="no engine venv")
def test_a_custom_agent_may_say_the_period_its_query_was_filtered_on():
    """Release follow-up 31 Oct: "the average ticket in 2025 was €5.66" fell back to raw rows: 2025 was only the query's
    filter. A date-table filter value that exists comes back from filtered() as a row; an invented figure stays caught."""
    r = engine("import json\nfrom abi.agents import grounding as g\n"
               "q = {'columns': ['average_ticket'], 'rows': [[5.6579]], 'measures': {'average_ticket': 'x'}, 'filters': []}\n"
               "y, d = {'columns': ['calendar.year'], 'rows': [[2025]]}, {'columns': ['calendar.day'], 'rows': [['2025-12-01']]}\n"
               "print(json.dumps([g.check('The average ticket in 2025 was €5.66.', [q])['numbers'],"
               " g.check('The average ticket in 2025 was €5.66.', [q, y])['numbers'],"
               " g.check('In December 2025 it was €5.66.', [q, d])['numbers'],"
               " g.check('In 2025 it was €6.10.', [q, y])['numbers']]))")
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [["2025"], [], [], ["€6.10"]], r.stderr[-500:]


@pytest.mark.skipif(not PY.exists(), reason="no engine venv")
def test_the_engines_grounding_takes_the_harness_rules_of_m2_2():
    """scripts/ground.py's rules are ABI's grounding.py's: M2.2 changed both. A name with letters beyond ASCII is checked
    whole ("Bremen S" was), a percentage grounds from a ratio only (1.1: "52%" through a revenue of 51.90) and without a
    minus from a falling one ("fell 51.5%"), a possessive ends a name ("North's December"), and a hyphenated phrase by its
    capitalized parts, a name after ordinary words ("Weekly North"), the plural of a backed name, an accented word of an ID
    ("Café" for cafes.cafe_name), and "Total", "Several" or "Why" after a bold label ground."""
    r = engine("import json\nfrom abi.agents import grounding as g\n"
               "rows = {'columns': ['cafes.cafe_name', 'calendar.month', 'revenue', 'orders_rows'], 'measures': {}, 'filters': [],"
               " 'rows': [['Bremen Süd', 12, '76.00', 11], ['Bremen Mitte', 12, '51.90', 2]]}\n"
               "pct = {'columns': ['cafes.region', 'revenue_change_pct'], 'measures': {'revenue_change_pct': 'Revenue % change'},"
               " 'filters': [], 'rows': [['North', -0.51515]]}\n"
               "c = lambda a, *r: (lambda o: o['numbers'] + o['names'])(g.check(a, list(r)))\n"
               "print(json.dumps([c('In December, Bremen Süd took 76.00 and Bremen Sud 11.', rows), c('North fell 52%, by 2%.', rows),"
               " c('North fell 51.5%.', pct), c('The North-region cafés took 76.00.', rows, pct), c('Like other Decembers, 76.00.', rows),"
               " c('- **Volume:** Total orders were 11.', rows), c('Check North’s December orders: 76.00.', rows, pct),"
               " c('A North-by-café report: 76.00.', rows, pct), c('In December, North’s Atlantis took 76.00.', rows, pct),"
               " c('- **Timing:** Weekly North took 76.00; Weekly Atlantis 11.', rows, pct), c('- **Cafés:** Several fell, to 76.00.', rows),"
               " c('| Café | Revenue |\\n|---|---|\\n| Bremen Süd | 76.00 |', rows), c('**What I can’t tell:** Why North fell to 76.00.', rows, pct)]))")
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [["Bremen Sud"], ["52%", "2%"], [], [], [], [], [], [], ["Atlantis"],
                                                            ["Weekly Atlantis"], [], [], []], r.stderr[-500:]
