"""1.2.6 M0 (needs the engine at :8780, no LLM): nothing changes by default. A project without model/ontology.json plans
byte-identical SQL to 1.2.5 (tests/golden/plans_1_2_5.json, written from the 1.2.5 code by ABI_GOLDEN_WRITE=1) for every
query of the RLS-shared suite, more joins, totals, time and refusals, and every visual of the starter report, as the
owner and as restricted Viewers; the restricted Viewers' row filters are the same; a project bundle imports and exports
unchanged, and nothing writes an ontology on its own. Copy stamps (DuckLake table IDs) are masked, nothing else."""
import json
import os
import secrets
import sys
import time

import duckdb
import pytest
from conftest import drop
from test_engine import engine
from test_pages import sure
from test_rls_shared import MEASURES, QUERIES

import abi
from pi_driver import ROOT

sys.path.insert(0, str(ROOT / "bench"))
import brewline  # noqa: E402

P = "pi_golden"
GOLDEN = ROOT / "tests/golden/plans_1_2_5.json"
WRITE = os.environ.get("ABI_GOLDEN_WRITE") == "1"
MORE = {"cafes_count": "COUNT(cafes.cafe_id)"}  # a measure over a dimension table
EXTRA = [
    {"measures": ["revenue"], "dimensions": ["cafes.region", "calendar.year"], "totals": [["cafes.region"], []]},
    {"measures": ["orders_rows"], "dimensions": ["customers.segment"],
     "filters": [{"column": "cafes.region", "op": "in", "value": ["North", "South"]}]},
    {"measures": ["revenue"], "dimensions": ["calendar.week_year", "calendar.week"], "limit": 5},
    {"measures": ["cafes_count"], "dimensions": ["cafes.region"]},
    {"measures": ["cafes_count"], "dimensions": ["products.category"]},  # refused: no path from cafes
    {"measures": ["revenue", "cafes_count"], "dimensions": ["cafes.city"]},  # two facts, one dimension
    {"measures": ["revenue"], "dimensions": ["nope.x"]},  # refused: no such table
]

CAPTURE = r"""
import json, re, sys, time
from abi import db, semantic, storage
from abi.project import imports
from abi.reports import _query
from abi.semantic import rls
a = json.loads(sys.stdin.read())
P = a["project"]
for i in range(5):  # the worker builds them too; built here so the owner's plans read the copies every time
    try:
        semantic.build_copies(P)
        break
    except Exception:
        time.sleep(2)
rep = imports.load(P, "report", "overview")
qs = [(f"q{i}", q) for i, q in enumerate(a["queries"])] + [
    (f"v:{pg['id']}/{v['id']}", _query(P, rep, pg, v, [])[0]) for pg in rep["pages"] for v in pg["visuals"] if "query" in v]
with db.connect() as c:
    owner = c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"]
stamp = lambda s: re.sub(r"__[0-9a-f]{16}", "__<stamp>", s)
plans = {}
for w, uid in {"owner": owner, **a["users"]}.items():
    for name, q in qs:
        q = dict(q)
        ex = q.pop("expressions", [])
        try:
            plans[f"{w} {name}"] = stamp(semantic._plan(P, q, uid, ex)[1])
        except Exception as e:
            plans[f"{w} {name}"] = f"refused: {e}"
read = lambda sql, reads: semantic.execute(P, sql, {"golden": "rls"}, reads=reads)
cols = semantic._columns_of(storage.lake(P).cursor(), "model")
views = {w: {"conditions": rls.conditions(P, uid, read, cols), "explain": rls.explain(P, uid)} for w, uid in a["users"].items()}
views["all"] = {"filtered": sorted(rls.filtered(P)), "keys": {t: rls.key_columns(P, t) for t in sorted(rls.filtered(P))}}
print(json.dumps({"queries": [q for _, q in qs], "plans": plans, "rls": views}, sort_keys=True, default=str))
"""

ROUNDTRIP = r"""
import json, sys
import abi.app  # noqa: F401  registers every action, as the engine does
from abi import actions, db, security
from abi.project import bundle, git
P = json.loads(sys.stdin.read())["project"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
before = bundle.bundle(git.path(P))
dry = actions.run("import_project", {"project": P, "bundle": before, "dry_run": True}, owner, "rest")
done = actions.run("import_project", {"project": P, "bundle": before}, owner, "rest")
after = bundle.bundle(git.path(P))
rebuilt = actions.run("build_model", {"project": P}, owner, "rest")
print(json.dumps({"dry": dry["changes"], "done": done["changes"], "same": before == after, "files": sorted(after),
                  "after_build": sorted(bundle.bundle(git.path(P)))}))
"""


def brew(p: str, d) -> None:
    """Brewline at its smallest scale (37,000 orders) as project p, the starter model and Overview report, more measures."""
    brewline.generate(d, 0.005)
    duckdb.sql(f"COPY (SELECT * FROM read_parquet('{d}/orders/*.parquet')) TO '{d}/orders.parquet' (FORMAT parquet)")
    if p in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([p])
    abi.load("demo", p, [str(d / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")] + [str(d / "orders.parquet")])
    for i, x in {**MEASURES, **MORE}.items():
        abi.call("set_measure", project=p, id=i, name=i, expression=x)


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    """P plus two restricted Viewers: one sees the North region (a rule on cafes.region), one a single café."""
    brew(P, tmp_path_factory.mktemp("golden"))
    stamp, made = time.strftime("%H%M%S"), {}
    try:
        for w in ("north", "one"):
            made[w] = f"pi_gold_{w}_{stamp}"
            abi.call("create_user", name=made[w], password=secrets.token_urlsafe(16))
            abi.call("set_role", project=P, user=made[w], role="viewer")
        ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}
        uid = {w: ids[n] for w, n in made.items()}
        cafes = [r[0] for r in abi.call("sql_query", project=P, sql="SELECT cafe_id FROM cafes ORDER BY 1")["rows"]]
        regions = ["East", "North", "South", "West"]
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={uid["north"]: ["North"], uid["one"]: regions})
        sure("set_rls_rule", project=P, id="cafe", table="cafes", column="cafe_id", users={uid["north"]: cafes, uid["one"]: cafes[1:2]})
        r = engine(CAPTURE, timeout=600, stdin=json.dumps({"project": P, "users": uid, "queries": QUERIES + EXTRA}))
        assert r.returncode == 0, r.stderr[-3000:]
        yield json.loads(r.stdout.splitlines()[-1])
    finally:
        for n in made.values():
            sure("delete_user", name=n)
        drop([P])


@pytest.fixture(scope="module")
def want(golden):
    if WRITE:
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
    assert GOLDEN.exists(), "no golden plans: write them from the 1.2.5 code with ABI_GOLDEN_WRITE=1"
    return json.loads(GOLDEN.read_text())


def _same(got: dict, want: dict) -> None:
    diff = sorted(k for k in got.keys() | want.keys() if got.get(k) != want.get(k))
    assert not diff, f"{len(diff)} changed, first {diff[0]}:\n1.2.5: {want.get(diff[0])}\nnow:   {got.get(diff[0])}"


def test_plan_sql_unchanged_without_ontology(golden, want):
    assert golden["queries"] == want["queries"]  # the same questions, so the same answers are due
    assert len(want["plans"]) >= 3 * 15 and sum(v.startswith("refused") for v in want["plans"].values()) >= 6
    _same(golden["plans"], want["plans"])


def test_rls_views_unchanged_without_follow_links(golden, want):
    assert "North" in want["rls"]["north"]["explain"] and want["rls"]["all"]["filtered"]
    _same(golden["rls"], want["rls"])


def test_125_bundle_roundtrip_no_new_files(golden):
    """The project as a bundle imports onto itself with no change (dry run and real), and exports the same files."""
    r = engine(ROUNDTRIP, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert out["dry"] == [] and out["done"] == [] and out["same"], out
    assert "model/model.json" in out["files"] and not any("ontology" in f for f in out["files"]), out["files"]


def test_build_model_writes_no_ontology(golden):
    """build_model (run by the load and again here) proposes nothing into the project's files on its own."""
    r = engine(ROUNDTRIP, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-3000:]
    assert not any("ontology" in f for f in json.loads(r.stdout.splitlines()[-1])["after_build"])
