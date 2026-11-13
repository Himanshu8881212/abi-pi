"""1.2.7 M0 item 1 (needs the engine at :8780, no LLM): nothing changes for a 1.2.6 project. A project with what 1.2.6
has (an ontology with object types and links, none of them `rls: follow`) and no process, app or graph file plans
byte-identical SQL to 1.2.6 (tests/golden/plans_1_2_6.json, written from the 1.2.6 code on the test server by
ABI_GOLDEN_WRITE=1) for the queries of the RLS-shared suite and the 1.2.5 golden, the bench's question and every visual
of the starter report, as the owner and as two restricted Viewers; each restricted Viewer's filters and the text of
every view they read through are the same, to the byte; and the project as a bundle imports onto itself with no change
and exports the same files. Copy stamps (DuckLake table IDs) are masked, nothing else."""
import json
import os
import secrets
import time

import pytest
from conftest import drop
from test_engine import engine
from test_ontology_nobreak import EXTRA, ROUNDTRIP, _same, brew
from test_pages import sure
from test_rls_shared import QUERIES, WAS

import abi
from pi_driver import ROOT

P = "pi_golden_126"
GOLDEN = ROOT / "tests/golden/plans_1_2_6.json"
WRITE = os.environ.get("ABI_GOLDEN_WRITE") == "1"
NEW = ("processes/", "apps/", "graphs/")  # 1.2.7's file kinds: a 1.2.6 project has none and gets none
BENCH = [{"measures": ["revenue"], "dimensions": ["cafes.region"], "filters": [{"column": "calendar.year", "op": "=", "value": 2025}]}]
ONTO = {  # as 1.2.6 saves one; `visited` is walked, never followed by row-level security
    "objects": [
        {"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name"},
        {"id": "customer", "table": "customers", "name": "Customer", "plural": "Customers", "title": "customer_id"},
        {"id": "order", "table": "orders", "name": "Order", "plural": "Orders", "title": "order_id"}],
    "links": [
        {"id": "order_cafe", "from": "order", "to": "cafe", "name": "café", "inverse": "orders", "via": ["orders-cafe_id"]},
        {"id": "order_customer", "from": "order", "to": "customer", "name": "customer", "inverse": "orders", "via": ["orders-customer_id"]},
        {"id": "visited", "from": "customer", "to": "cafe", "name": "visited", "inverse": "visitors",
         "via": ["orders-customer_id", "orders-cafe_id"]}],
}

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
con = storage.lake(P)
cols = semantic._columns_of(con.cursor(), "model")
views = {}
for w, uid in a["users"].items():
    schema = semantic._rls(P, uid, con, semantic._snapshot(con), None)[0]  # the views every read of theirs goes through
    text = dict(con.cursor().execute("SELECT view_name, sql FROM duckdb_views() WHERE database_name = 'memory' AND schema_name = ?"
                                     " ORDER BY view_name", [schema]).fetchall())
    views[w] = {"conditions": rls.conditions(P, uid, read, cols), "explain": rls.explain(P, uid), "schema": schema, "views": text}
views["all"] = {"filtered": sorted(rls.filtered(P)), "keys": {t: rls.key_columns(P, t) for t in sorted(rls.filtered(P))},
                "follows": rls.follows(P)}
print(json.dumps({"queries": [q for _, q in qs], "plans": plans, "rls": views}, sort_keys=True, default=str))
"""


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    """P (Brewline, the starter report, more measures, the ontology above) plus two restricted Viewers: one sees the
    North region (a rule on cafes.region), one a single café."""
    brew(P, tmp_path_factory.mktemp("golden126"))
    stamp, made = time.strftime("%H%M%S"), {}
    try:
        for w in ("north", "one"):
            made[w] = f"pi_g126_{w}_{stamp}"
            abi.call("create_user", name=made[w], password=secrets.token_urlsafe(16))
            abi.call("set_role", project=P, user=made[w], role="viewer")
        ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}
        uid = {w: ids[n] for w, n in made.items()}
        cafes = [r[0] for r in abi.call("sql_query", project=P, sql="SELECT cafe_id FROM cafes ORDER BY 1")["rows"]]
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region",
             users={uid["north"]: ["North"], uid["one"]: ["East", "North", "South", "West"]})
        sure("set_rls_rule", project=P, id="cafe", table="cafes", column="cafe_id", users={uid["north"]: cafes, uid["one"]: cafes[1:2]})
        sure("save_ontology", project=P, ontology=ONTO)
        r = engine(CAPTURE, timeout=600, stdin=json.dumps({"project": P, "users": uid, "queries": QUERIES + EXTRA + WAS + BENCH}))
        assert r.returncode == 0, r.stderr[-3000:]
        got = json.loads(r.stdout.splitlines()[-1])
        # the Viewers' IDs differ from run to run and are in no text below but explain's own (never: it names values only)
        yield got
    finally:
        for n in made.values():
            sure("delete_user", name=n)
        drop([P])


@pytest.fixture(scope="module")
def want(golden):
    if WRITE:
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
    assert GOLDEN.exists(), "no golden plans: write them from the 1.2.6 code with ABI_GOLDEN_WRITE=1"
    return json.loads(GOLDEN.read_text())


def test_plan_sql_unchanged_without_new_files(golden, want):
    assert golden["queries"] == want["queries"]  # the same questions, so the same answers are due
    assert len(want["plans"]) >= 3 * 20 and sum(v.startswith("refused") for v in want["plans"].values()) >= 6
    assert any("orders__" in v for k, v in want["plans"].items() if k.startswith("owner ")), "the owner's plans read query copies"
    _same(golden["plans"], want["plans"])


def test_rls_views_unchanged_without_follow_links(golden, want):
    """The filters, what explain says, and the text of every view of each restricted Viewer, with the schema's name
    (the hash of all its views, which is also their cache key). The project has an ontology and no follow link."""
    assert want["rls"]["all"]["follows"] == [] and "North" in want["rls"]["north"]["explain"] and want["rls"]["all"]["filtered"]
    for w in ("north", "one"):
        assert {"cafes", "orders"} <= set(want["rls"][w]["views"]) and want["rls"][w]["schema"].startswith("rls_"), want["rls"][w]["schema"]
        assert all("rls_keys" not in v and "list_contains(" in v for t, v in want["rls"][w]["views"].items() if t in ("cafes", "orders"))
        _same(golden["rls"][w]["views"], want["rls"][w]["views"])
    _same({w: {k: v for k, v in x.items() if k != "views"} for w, x in golden["rls"].items()},
          {w: {k: v for k, v in x.items() if k != "views"} for w, x in want["rls"].items()})


def test_126_bundle_roundtrip_no_new_files(golden):
    """The 1.2.6 project as a bundle imports onto itself with no change (dry run and real) and exports the same files;
    it holds its ontology, and neither the import nor a build_model adds a process, app or graph file."""
    r = engine(ROUNDTRIP, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert out["dry"] == [] and out["done"] == [] and out["same"], out
    assert "model/ontology.json" in out["files"] and "model/model.json" in out["files"], out["files"]
    assert not [f for f in out["files"] + out["after_build"] if f.startswith(NEW)], out
