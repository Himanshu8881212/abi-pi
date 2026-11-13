"""The own engine's housekeeping and loading (needs the engine: scripts/engine.sh start)."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "engine/backend/.venv/bin/python"


def engine(code: str, timeout=60, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = subprocess.run(["sh", "-c", "scripts/engine.sh env"], cwd=ROOT, capture_output=True, text=True).stdout
    return subprocess.run(["sh", "-c", f"{env}\nexec {PY} -c \"$0\"", code], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                          input=stdin)


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


@pytest.mark.skipif(not PY.exists(), reason="no engine venv")
def test_the_engines_grounding_takes_the_harness_rules_of_m2_round_2():
    """M2 round 2's false masks, fixed in both copies: a range's first figure takes its last one's percent sign ("each
    fell 44–61%" from % changes of -0.4395 and -0.6058; a plain 11 of a period label no longer backs "11–61%"), and a
    preposition or degree word opening a label's value is no name ("**Where it sits:** Almost all of it"), nor a "No",
    "Up" or "Down" opening a phrase ("**Segment:** No segment stands out"); the rest of the phrase, or one alone, is checked."""
    r = engine("import json\nfrom abi.agents import grounding as g\n"
               "x = {'columns': ['dimension', 'member', 'period_a', 'change_pct'], 'measures': {}, 'filters': [], 'rows':"
               " [['cafes.city', 'Hanover', '2025-11', -0.439543694918046], ['cafes.city', 'Bremen', '2025-11', -0.605793190498471]]}\n"
               "c = lambda a: (lambda o: o['numbers'] + o['names'])(g.check(a, [x]))\n"
               "print(json.dumps([c('Each fell 44–61%.'), c('Each fell 11–61%.'), c('**Where it sits:** Almost all, in Bremen: -61%.'),"
               " c('- **Cities:** Within Atlantis, -44%.'), c('- **Segment:** No segment stands out: -44%.'), c('- **Member:** No, -44%.'),"
               " c('- **Hanover:** Down 44%.'), c('| Down | -44% |')]))")
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [[], ["11"], [], ["Atlantis"], [], ["No"], [], ["Down"]], r.stderr[-500:]


@pytest.mark.skipif(not PY.exists(), reason="no engine venv")
def test_the_engines_grounding_takes_the_harness_rules_of_the_m2_review():
    """tests/test_ground.py's REVIEW cases (an expression's MEL and a name the model gave a column ground nothing,
    percentage points do, ordinary "why" words pass) give the same flags in ABI's grounding.py."""
    import test_ground
    r = engine("import json, sys\nfrom abi.agents import grounding as g\n"
               "print(json.dumps([(lambda o: o['numbers'] + o['names'])(g.check(a, rs, fields={'day'})) for a, rs, _ in json.load(sys.stdin)]))",
               stdin=json.dumps(test_ground.REVIEW))
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [want for _, _, want in test_ground.REVIEW], r.stderr[-500:]


@pytest.mark.skipif(not PY.exists(), reason="no engine venv")
def test_the_engines_grounding_takes_the_harness_count_rule_of_1_2():
    """1.2 (owner, 5 Nov 2026): a count grounds when it names that many backed items, in both copies: "3 visuals: A, B
    and C" with the visual titles in a result passes; a bare count, or one with fewer names, stays masked."""
    import ground
    rep = {"report": {"id": "sales", "pages": [{"id": "main", "visuals": [{"id": "kpi", "title": "Revenue KPI"},
           {"id": "by_region", "title": "Revenue by region"}, {"id": "by_month", "title": "Revenue by month"}]}]}}
    cases = [("I built a report with 3 visuals: Revenue KPI, Revenue by region and Revenue by month.", []),
             ("I built three visuals:\n\n1. Revenue KPI\n2. Revenue by region\n3. Revenue by month", []),
             ("I built a report with 3 visuals.", ["3"]),
             ("I built 3 visuals: Revenue KPI and Revenue by region.", ["3"])]
    r = engine("import json, sys\nfrom abi.agents import grounding as g\nrep, cases = json.load(sys.stdin)\n"
               "print(json.dumps([g.check(a, [rep])['numbers'] for a, _ in cases]))", stdin=json.dumps([rep, cases]))
    want = [w for _, w in cases]
    assert [ground.check(a, [rep])["numbers"] for a, _ in cases] == want
    assert json.loads(r.stdout.strip().splitlines()[-1]) == want, r.stderr[-500:]


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_lake_on_an_object_store_keeps_its_url_through_rename_delete_and_purge(tmp_path):
    """M4.4: pathlib turned "s3://b/lake/p/" into "s3:/b/lake/…": connect spilled into a local "s3:" folder, lake_exists
    looked there, move_lake wrote data_path "s3:/b/lake/new/" (the renamed lake no longer attached) and purge_deleted
    rmtree'd a URL, leaving every file. Offline: the lakes are empty, so DuckLake never reads the bucket."""
    r = engine(f"""
import os, json
os.chdir('{tmp_path}')
os.environ['ABI_DATA_PATH'] = 's3://abi-m44-test/lake'
from abi import db, storage
with db.connect() as c:
    for s in ('pi_s3_a', 'pi_s3_b', '_deleted_1_pi_s3_b'):
        c.execute(f'DROP SCHEMA IF EXISTS "{{s}}" CASCADE')
    c.commit()
con = storage.connect('pi_s3_a')
spill = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
con.close()
os.makedirs('s3:/abi-m44-test/lake/pi_s3_b')  # where pathlib looked
out, path = {{'spill_local': '://' not in spill and not os.path.exists('s3:/abi-m44-test/lake/.tmp')}}, lambda c, s: c.execute(
    f'SELECT value FROM "{{s}}".ducklake_metadata WHERE key = %s', ['data_path']).fetchone()['value']
with db.connect() as c:
    out['taken'] = storage.lake_exists(c, 'pi_s3_b')
    out['moves'] = storage.move_lake(c, 'pi_s3_a', 'pi_s3_b')
    c.commit()
    out['path'] = path(c, 'pi_s3_b')
storage.connect('pi_s3_b').close()  # attaches at the files' own place
calls = []
storage.drop_files = lambda s, f=storage.drop_files: (calls.append(s), f(s))
storage.shutil.rmtree = lambda p, f=storage.shutil.rmtree, **k: (calls.append(p), f(p, **k))  # other old lakes still go
with db.connect() as c:
    storage.move_lake(c, 'pi_s3_b', '_deleted_1_pi_s3_b')
    c.commit()
    storage.purge_deleted(c)
    out['gone'] = not c.execute("SELECT 1 FROM pg_namespace WHERE nspname = '_deleted_1_pi_s3_b'").fetchone()
out['calls'] = [x for x in calls if 'pi_s3' in x]
print(json.dumps(out))
""", timeout=120)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "spill_local": True, "taken": False, "moves": [], "path": "s3://abi-m44-test/lake/pi_s3_a/", "gone": True,
        "calls": ["_deleted_1_pi_s3_b"]}, r.stderr[-2000:]


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_local_lake_still_moves_its_folder_and_drop_files_deletes_every_file(tmp_path):
    """M4.4, today's behaviour on a folder: lake_exists sees a leftover folder, move_lake moves the folder and data_path to
    the sibling; drop_files (the object-store purge) deletes every file a lake's catalog lists, older snapshots' too."""
    r = engine(f"""
import os, json
os.environ['ABI_DATA_PATH'] = '{tmp_path}/lake'
from abi import db, storage
with db.connect() as c:
    for s in ('pi_loc_a', 'pi_loc_b'):
        c.execute(f'DROP SCHEMA IF EXISTS "{{s}}" CASCADE')
    c.commit()
con = storage.connect('pi_loc_a')
con.execute('CREATE TABLE lake.raw.t AS SELECT range i FROM range(5)')
con.execute('INSERT INTO lake.raw.t VALUES (9)')
con.close()
os.makedirs('{tmp_path}/lake/pi_loc_c')
files = lambda: sorted(os.path.relpath(f'{{d}}/{{f}}', '{tmp_path}') for d, _, fs in os.walk('{tmp_path}/lake') for f in fs if f.endswith('.parquet'))
out = {{'files': len(files())}}
with db.connect() as c:
    out['taken'] = [storage.lake_exists(c, p) for p in ('pi_loc_a', 'pi_loc_b', 'pi_loc_c')]
    out['moves'] = storage.move_lake(c, 'pi_loc_a', 'pi_loc_b')
    for a, b in out['moves']:
        os.rename(a, b)
    c.commit()
con = storage.connect('pi_loc_b')
out['rows'] = con.execute('SELECT count(*) FROM lake.raw.t').fetchone()[0]
con.close()
storage.drop_files('pi_loc_b')
out['left'] = files()
with db.connect() as c:
    c.execute('DROP SCHEMA pi_loc_b CASCADE')
    c.commit()
print(json.dumps(out))
""", timeout=120)
    lake = f"{tmp_path}/lake"
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "files": 2, "taken": [True, False, True], "moves": [[f"{lake}/pi_loc_a/", f"{lake}/pi_loc_b/"]], "rows": 6,
        "left": []}, r.stderr[-2000:]


@pytest.mark.skipif(not PY.exists() or engine("from abi import db; db.connect().close()").returncode, reason="engine's Postgres not running")
def test_a_lake_of_a_project_that_is_gone_is_forgotten():
    """M10: a process kept the lake (a DuckDB instance, its memory and its catalog connection) of every project it ever
    read, also once the project was deleted or renamed, so a worker that saw projects come and go only grew. Within a
    minute of the next read the lakes of projects that are gone are forgotten; those that exist stay."""
    r = engine("""
import json, psycopg
from abi import storage
a, b = "pi_lake_gone_a", "pi_lake_kept_b"
con = {p: storage.lake(p) for p in (a, b)}
with psycopg.connect(autocommit=True) as pg:
    pg.execute(f'ALTER SCHEMA "{a}" RENAME TO "_deleted_1_{a}"')  # as delete_project leaves it
storage.lake(b)
soon = sorted(p for p in (a, b) if p in storage._lakes)
storage._swept[0] = 0.0  # a minute later
kept = storage.lake(b) is con[b]
after = sorted(p for p in (a, b) if p in storage._lakes)
storage._close_lakes()
with psycopg.connect(autocommit=True) as pg:
    for s in (f"_deleted_1_{a}", b):
        pg.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
print(json.dumps([soon, after, kept]))
""", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == [["pi_lake_gone_a", "pi_lake_kept_b"], ["pi_lake_kept_b"], True]
