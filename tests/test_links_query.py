"""1.2.6 M5 (needs the engine at :8780, no LLM): semantic queries that follow ontology links, on Brewline. A query
without `links` plans byte-identical SQL to 1.2.5 even with an ontology (#18). A named link picks the path, a `default`
link settles a role-playing date, and a query the planner can't route names the links that would (#19). Customers
grouped by the region of the cafés they visited (many-to-many through orders) count once per region; totals, a
hidden join column and two routes to one table are refused; a restricted Viewer's values are the owner's for the
members they see (#20). The owner's sql_query is the reference for every figure."""
import contextlib
import json
import secrets
import time

import pytest
from conftest import drop
from test_engine import engine
from test_ontology_nobreak import CAPTURE, EXTRA, GOLDEN, brew
from test_pages import sure
from test_rls_shared import QUERIES

import abi

P = "pi_links_query"
ONTO = {
    "objects": [
        {"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name"},
        {"id": "customer", "table": "customers", "name": "Customer", "plural": "Customers", "title": "customer_id"},
        {"id": "order", "table": "orders", "name": "Order", "plural": "Orders", "title": "order_id"},
    ],
    "links": [
        {"id": "order_cafe", "from": "order", "to": "cafe", "name": "café", "inverse": "orders", "via": ["orders-cafe_id"]},
        {"id": "order_customer", "from": "order", "to": "customer", "name": "customer", "inverse": "orders", "via": ["orders-customer_id"]},
        {"id": "visited", "from": "customer", "to": "cafe", "name": "visited", "inverse": "visitors",
         "via": ["orders-customer_id", "orders-cafe_id"], "rls": "follow"},
    ],
}
BY_VISITED = {"measures": ["cust_count"], "dimensions": ["cafes.region"], "links": ["visited"]}
PLAN = r"""
import json, sys
from abi import semantic
a = json.loads(sys.stdin.read())
print(json.dumps(semantic._plan(a["project"], a["q"], a.get("uid"))[1]))
"""

SHARED = r"""
import json, sys
from abi import semantic
a = json.loads(sys.stdin.read())
made = {}
out = semantic.plan(a["project"], a["q"], a["uid"], [], True, made)[1]
print(json.dumps({"sql": out, "shared": sorted(made)}))
"""
REACH = r"""
import json, sys
from abi.reports import _query
a = json.loads(sys.stdin.read())
print(json.dumps([_query(a["project"], {"filters": []}, {"filters": []}, {"query": v}, [f])[2] for v, f in a["cases"]]))
"""
SCOPED = r"""
import json, sys
from abi.agents import studio
from abi.project import bundle, git
from abi.semantic import lineage
a = json.loads(sys.stdin.read())
files = bundle.bundle(git.path(a["project"]))
print(json.dumps(sorted(studio.scope({"scope": {"datasets": a["datasets"]}}, files, lineage.graph(files))[1])))
"""
SCOPE = r"""
import json, sys
from abi.agents import runs
from abi.project import bundle, git
from abi.semantic import lineage
a = json.loads(sys.stdin.read())
files = bundle.bundle(git.path(a["project"]))
print(json.dumps([runs._refused("semantic_query", q, ["semantic_query"], files, lineage.graph(files), {"cust_count", "cafes_count"},
                                {"customers", "cafes"}, []) for q in a["queries"]]))
"""

# a pure planner model (no data): orders -> customers -> cafes (a home café), orders -> cafes, orders.ordered_at -> calendar
R = lambda f, fc, t, tc: {"id": f"{f}-{fc}", "from": {"table": f, "columns": [fc]}, "to": {"table": t, "columns": [tc]}}  # noqa: E731
MODEL = {"tables": [{"table": t} for t in ("orders", "customers", "cafes", "calendar")],
         "entities": [{"id": t, "table": t, "key": [k]} for t, k in
                      (("orders", "order_id"), ("customers", "customer_id"), ("cafes", "cafe_id"), ("calendar", "day"))],
         "relationships": [R("orders", "customer_id", "customers", "customer_id"), R("orders", "cafe_id", "cafes", "cafe_id"),
                           R("customers", "home_cafe", "cafes", "cafe_id"), R("orders", "ordered_at", "calendar", "day")],
         "date_table": {"table": "calendar", "column": "day"}}
PURE_ONTO = {"objects": [{"id": i, "table": t} for i, t in (("order", "orders"), ("customer", "customers"), ("cafe", "cafes"), ("day", "calendar"))],
             "links": [{"id": i, "from": f, "to": t, "via": v} for i, f, t, v in (
                 ("visited", "customer", "cafe", ["orders-customer_id", "orders-cafe_id"]),
                 ("home", "order", "cafe", ["orders-customer_id", "customers-home_cafe"]),
                 ("active", "customer", "day", ["orders-customer_id", "orders-ordered_at"]),
                 ("active2", "customer", "day", ["orders-customer_id", "orders-ordered_at"]),
                 ("seen", "day", "customer", ["orders-ordered_at", "orders-customer_id"]))]}
PURE_MEASURES = {"revenue": "SUM(orders.revenue)", "custs": "COUNT(customers.customer_id)", "custs_ytd": "YTD([custs])",
                 "share": "DIVIDE([custs], IGNORE([custs], cafes.region))", "days": "COUNT(calendar.day)",
                 "campus_custs": "WHERE([custs], cafes.format = 'campus')"}
REGION = {"measures": ["custs"], "dimensions": ["cafes.region"], "links": ["visited"]}
YEAR = {"measures": ["custs"], "dimensions": ["calendar.year"]}  # customers reach no date but through a link
COPY = {"orders": ["orders__c", ["cafes.region"]]}
CASES = {  # name: (query and options, what the answer holds, what it must not)
    "time": ({"q": {"measures": ["custs_ytd"], "dimensions": ["calendar.year"], "links": ["active"]}}, "refused: ", "time intelligence (YTD)"),
    "live_fact": ({"q": REGION, "live": {"customers": {"customer_id": "INTEGER"}}}, "refused: ", "live ClickHouse table 'customers'"),
    "live_bridge": ({"q": REGION, "live": {"orders": {"customer_id": "INTEGER"}}}, "refused: ", "live ClickHouse table 'orders'"),
    "two_m2m": ({"q": {**REGION, "dimensions": ["cafes.region", "calendar.year"], "links": ["visited", "active"]}}, "refused: ",
                "one many-to-many link at a time"),
    "second_way": ({"q": {**REGION, "measures": ["revenue"]}}, "refused: ", "reaches table 'orders' a second way"),
    "totals": ({"q": {**REGION, "totals": [[]]}}, "refused: ", "totals across it are refused"),
    "far_filter": ({"q": {**REGION, "filters": [{"column": "cafes.format", "op": "in", "value": ["campus", "kiosk"]}]}}, "refused: ",
                   "filters on cafes.format without grouping by it"),
    "grouped_filter": ({"q": {**REGION, "filters": [{"column": "cafes.region", "op": "in", "value": ["North"]}]}}, "sql: ",
                       'SELECT DISTINCT "orders"."customer_id" AS "__k0", "cafes"."region" FROM "orders" JOIN "cafes"'),
    "copy_no_link": ({"q": {"measures": ["revenue"], "dimensions": ["cafes.region"]}, "copies": COPY}, "sql: ", '"orders__c"'),
    "copy_link": ({"q": {"measures": ["revenue"], "dimensions": ["cafes.region"], "links": ["home"]}, "copies": COPY}, "sql: ",
                  '"cafes"."cafe_id" = "customers"."home_cafe"'),
    "two_defaults": ({"q": YEAR, "defaults": ["active", "active2"]}, "refused: ", "active and active2"),
    "one_default": ({"q": YEAR, "defaults": ["active"]}, "sql: ", "SELECT DISTINCT"),
    "ignore": ({"q": {**REGION, "measures": ["share"]}}, "refused: ", "ignores cafes.region"),
    "date_first": ({"q": {"measures": ["days"], "dimensions": ["customers.segment"], "links": ["seen"]}}, "refused: ", "onto a date"),
    "where_far": ({"q": {**REGION, "measures": ["campus_custs"]}}, "refused: ", "filters on cafes.format without grouping by it"),
    "middle": ({"q": {**REGION, "filters": [{"column": "orders.channel", "op": "=", "value": "app"}]}}, "refused: ",
               "Table 'orders' is a step of link 'visited'"),
    "ignore_ungrouped": ({"q": {**REGION, "measures": ["share"], "dimensions": ["customers.segment"]}}, "sql: ", "SELECT"),
    "unreachable": ({"q": {**REGION, "measures": ["revenue", "custs"], "links": ["home"]}}, "refused: ", "Following link 'home'"),
}
PURE = r"""
import json, sys
from abi.semantic import planner
a, out = json.loads(sys.stdin.read()), {}
for name, c in a["cases"].items():
    o = json.loads(json.dumps(a["onto"]))
    for ln in o["links"]:
        ln["default"] = ln["id"] in c.get("defaults", [])
    copies = {f: (n, set(cols), {}) for f, (n, cols) in c.get("copies", {}).items()}
    live = {t: (f'"db"."{t}"', ty) for t, ty in c.get("live", {}).items()}
    try:
        out[name] = "sql: " + planner.plan(c["q"], a["measures"], a["model"], copies, live, ontology=o)
    except planner.PlanError as e:
        out[name] = f"refused: {e}"
print(json.dumps(out))
"""


def sql(q: str) -> list:
    return abi.call("sql_query", project=P, sql=q)["rows"]


def ask(token: str | None = None, **q) -> dict:
    with abi.as_user(token) if token else contextlib.nullcontext():
        return abi.call("semantic_query", project=P, **q)


def refused(status: int, token: str | None = None, **q) -> str:
    with pytest.raises(abi.ActionError) as e:
        ask(token, **q)
    assert e.value.status == status, e.value.body
    return e.value.body.get("message") or json.dumps(e.value.body)


def planned(q: dict) -> str:
    r = engine(PLAN, timeout=120, stdin=json.dumps({"project": P, "q": q}))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


@pytest.fixture(scope="module")
def lq(tmp_path_factory):
    """P: Brewline (as the golden plans' project, plus a measure over customers) with a North-region Viewer (a row
    rule on cafes.region) and the ontology above (visited follows the rule)."""
    brew(P, tmp_path_factory.mktemp("lq"))
    name, pw = f"pi_lq_viewer_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    try:
        abi.call("create_user", name=name, password=pw)
        abi.call("set_role", project=P, user=name, role="viewer")
        token = abi.login(name, pw)[1]["token"]
        uid = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}[name]
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={uid: ["North"]})
        abi.call("set_measure", project=P, id="cust_count", name="Customers", expression="COUNT(customers.customer_id)")
        sure("save_ontology", project=P, ontology=ONTO)
        yield {"token": token, "uid": uid}
    finally:
        sure("delete_user", name=name)
        drop([P])


@contextlib.contextmanager
def two_dates(default: str | None = None):
    """A role-playing date for one block: orders reach calendar by order_date and by ordered_at, a link each (`default`
    marks one), then the model and ontology as before."""
    abi.call("add_relationship", project=P, **{"from": "orders.ordered_at", "to": "calendar.day"})
    rels = {r["id"]: r for r in abi.call("semantic_model", project=P, all=True)["relationships"]}
    by = {r["from"]["columns"][0]: i for i, r in rels.items() if r["from"]["table"] == "orders" and r["to"]["table"] == "calendar"}
    assert set(by) == {"order_date", "ordered_at"}, rels
    o = json.loads(json.dumps(ONTO))
    o["objects"].append({"id": "day", "table": "calendar", "name": "Day", "plural": "Days", "title": "day"})
    o["links"] += [{"id": "order_day", "from": "order", "to": "day", "name": "ordered on", "via": [by["order_date"]]},
                   {"id": "ordered_day", "from": "order", "to": "day", "name": "ordered at", "via": [by["ordered_at"]]}]
    for ln in o["links"]:
        if ln["id"] == default:
            ln["default"] = True
    try:
        sure("save_ontology", project=P, ontology=o)
        yield
    finally:
        sure("save_ontology", project=P, ontology=ONTO)
        sure("remove_relationship", project=P, id=by["ordered_at"])


@pytest.fixture(scope="module")
def pure():
    r = engine(PURE, timeout=120, stdin=json.dumps({"cases": {k: c for k, (c, _, _) in CASES.items()}, "model": MODEL,
                                                    "onto": PURE_ONTO, "measures": PURE_MEASURES}))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


@pytest.mark.parametrize("case", sorted(CASES))
def test_planner_link_cases(pure, case):
    """planner.plan alone, on a small model: each many-to-many refusal, a far-side filter, a query copy beside a link."""
    _, start, holds = CASES[case]
    assert pure[case].startswith(start) and holds in pure[case], pure[case]
    if case == "copy_link":
        assert "orders__c" not in pure[case] and '"orders"."cafe_id"' not in pure[case], pure[case]  # the link's café


# ---------- #18: no links, no change ----------

def test_query_without_links_plans_identically(lq):
    """Every golden query and starter-report visual, planned as the owner in a project with an ontology (a follow
    link, a row rule), is the 1.2.5 SQL byte for byte, refusals' text too."""
    want = {k: v for k, v in json.loads(GOLDEN.read_text())["plans"].items() if k.startswith("owner ")}
    r = engine(CAPTURE, timeout=600, stdin=json.dumps({"project": P, "users": {}, "queries": QUERIES + EXTRA}))
    assert r.returncode == 0, r.stderr[-3000:]
    got = json.loads(r.stdout.splitlines()[-1])["plans"]
    assert len(want) >= 15 and any(v.startswith("refused") for v in want.values())
    diff = sorted(k for k in got.keys() | want.keys() if got.get(k) != want.get(k))
    assert not diff, f"{len(diff)} changed, first {diff[0]}:\n1.2.5: {want.get(diff[0])}\nnow:   {got.get(diff[0])}"


def test_restricted_plans_unchanged_without_links(lq):
    """The same for a restricted Viewer (a rule on cafes.region; visited follows it): every golden query and visual
    plans as the golden North Viewer's."""
    want = {k: v for k, v in json.loads(GOLDEN.read_text())["plans"].items() if k.startswith("north ")}
    r = engine(CAPTURE, timeout=600, stdin=json.dumps({"project": P, "users": {"north": lq["uid"]}, "queries": QUERIES + EXTRA}))
    assert r.returncode == 0, r.stderr[-3000:]
    got = {k: v for k, v in json.loads(r.stdout.splitlines()[-1])["plans"].items() if k.startswith("north ")}
    assert len(want) >= 15
    diff = sorted(k for k in got.keys() | want.keys() if got.get(k) != want.get(k))
    assert not diff, f"{len(diff)} changed, first {diff[0]}:\n1.2.5: {want.get(diff[0])}\nnow:   {got.get(diff[0])}"


# ---------- #19: the path rule ----------

def test_named_link_picks_path(lq):
    ref = sql("SELECT year(order_date), sum(revenue) FROM orders GROUP BY 1 ORDER BY 1")
    q = {"measures": ["revenue"], "dimensions": ["calendar.year"]}
    with two_dates():
        for link, col, other in (("order_day", "order_date", "ordered_at"), ("ordered_day", "ordered_at", "order_date")):
            s = planned({**q, "links": [link]})
            assert f'"orders"."{col}"' in s and f'"orders"."{other}"' not in s, (link, s)
            rows = ask(**q, links=[link])["rows"]
            assert [[y, round(float(v), 2)] for y, v in rows] == [[y, round(float(v), 2)] for y, v in ref], link  # the same day either way


def test_default_link_resolves_role_playing(lq):
    q = {"measures": ["revenue"], "dimensions": ["calendar.year"]}
    with two_dates(default="ordered_day"):
        s = planned(q)
        assert '"orders"."ordered_at"' in s and '"orders"."order_date"' not in s, s
        assert ask(**q)["rows"] == ask(**q, links=["ordered_day"])["rows"]
        assert '"orders"."order_date"' in planned({**q, "links": ["order_day"]})  # a named link still wins


def test_ambiguous_names_links(lq):
    with two_dates():
        msg = refused(400, measures=["revenue"], dimensions=["calendar.year"])
        assert "can't choose" in msg and "order_day" in msg and "ordered_day" in msg, msg
    msg = refused(400, measures=["cust_count"], dimensions=["cafes.region"])  # no path but through a link
    assert "no relationship" in msg and "links: [visited]" in msg, msg
    assert "No link nope" in refused(400, **{**BY_VISITED, "links": ["nope"]})


def test_two_routes_refused(lq):
    msg = refused(400, measures=["revenue"], dimensions=["cafes.region"], links=["order_cafe", "visited"])
    assert "ask two queries" in msg and "order_cafe" in msg and "visited" in msg, msg
    # revenue by visited region would walk from orders to customers and back to orders
    msg = refused(400, measures=["revenue"], dimensions=["cafes.region"], links=["visited"])
    assert "ask two queries" in msg and "'orders'" in msg, msg
    msg = refused(400, measures=["revenue", "cust_count"], dimensions=["cafes.region"], links=["order_cafe"])
    assert "no relationship" in msg, msg  # customers don't reach cafés by order_cafe: the link is the orders'


# ---------- #20: many-to-many through a bridge ----------

def test_customers_by_visited_region_counts_once(lq):
    got = {r[0]: r[1] for r in ask(**BY_VISITED)["rows"]}
    ref = dict(map(tuple, sql("SELECT c.region, count(DISTINCT o.customer_id) FROM orders o JOIN cafes c USING (cafe_id)"
                              " WHERE o.customer_id IS NOT NULL GROUP BY 1")))
    rows = dict(map(tuple, sql("SELECT c.region, count(*) FROM orders o JOIN cafes c USING (cafe_id) GROUP BY 1")))
    none = sql("SELECT count(*) FROM customers WHERE customer_id NOT IN (SELECT customer_id FROM orders"
               " WHERE customer_id IS NOT NULL)")[0][0]
    assert {k: v for k, v in got.items() if k is not None} == ref and got.get(None, 0) == none, (got, ref, none)
    assert all(ref[k] < rows[k] for k in ref)  # once per customer and region, not once per order
    [(everyone,)] = sql("SELECT count(*) FROM customers")
    assert sum(got.values()) > everyone  # a customer who visited two regions counts in both: members overlap
    s = planned(BY_VISITED)
    assert "SELECT DISTINCT" in s and '"lake"' not in s, s  # the pairs, read from the model tables (no query copy)
    north = ask(**{**BY_VISITED, "filters": [{"column": "cafes.region", "op": "=", "value": "North"}]})["rows"]
    assert north == [["North", ref["North"]]], north
    abi.call("save_dataset", project=P, id="visitors", name="Visitors", query=BY_VISITED)  # a saved query with links
    assert {r[0]: r[1] for r in abi.call("dataset_data", project=P, dataset="visitors")["rows"]} == got
    down = abi.call("lineage", project=P, object="link:visited")["objects"]
    assert "dataset:visitors" in down, down  # lineage: what reads the link


def test_m2m_totals_refused(lq):
    msg = refused(400, **BY_VISITED, totals=[[]])
    assert "totals" in msg and "visited" in msg, msg
    assert ask(**BY_VISITED)["rows"]  # the same query without totals plans


def test_m2m_hidden_key_refused(lq):
    """COL-SEC.2: the bridge's join columns are the query's too."""
    sure("set_column_rule", project=P, id="pi_hide", table="orders", columns=["customer_id"])
    try:
        msg = refused(403, lq["token"], **BY_VISITED)
        assert "Column-level security" in msg and "orders.customer_id" in msg, msg
        assert ask(**BY_VISITED)["rows"], "the owner still reads it"
    finally:
        sure("delete_rls_rule", project=P, id="pi_hide")


def test_m2m_restricted_equals_unrestricted_on_visible(lq):
    """The North Viewer's customers (visited follows the rule: those who visited a North café) by visited region: the
    members they see (North only) have the owner's values; the pairs read their own orders and cafés."""
    mine, owner = ask(lq["token"], **BY_VISITED)["rows"], {r[0]: r[1] for r in ask(**BY_VISITED)["rows"]}
    seen = {r[0]: r[1] for r in mine if r[0] is not None}
    assert seen == {"North": owner["North"]}, (mine, owner)
    [(visitors,)] = sql("SELECT count(DISTINCT o.customer_id) FROM orders o JOIN cafes c USING (cafe_id)"
                        " WHERE c.region = 'North'")
    assert seen["North"] == visitors and all(r[1] == 0 for r in mine if r[0] is None), mine


def test_m2m_far_filter_counts_once(lq):
    """A filter on the far table: on a grouped column the pairs hold, refused on any other (it would count a customer
    once per value)."""
    q = {**BY_VISITED, "filters": [{"column": "cafes.region", "op": "in", "value": ["North", "South"]}]}
    got = {r[0]: r[1] for r in ask(**q)["rows"]}
    ref = dict(map(tuple, sql("SELECT c.region, count(DISTINCT o.customer_id) FROM orders o JOIN cafes c USING (cafe_id)"
                              " WHERE o.customer_id IS NOT NULL AND c.region IN ('North', 'South') GROUP BY 1")))
    assert got == ref, (got, ref)
    assert '"orders"."customer_id" AS "__k0", "cafes"."region" FROM' in planned(q)
    fmt = sql("SELECT DISTINCT format FROM cafes ORDER BY 1")
    msg = refused(400, **{**BY_VISITED, "filters": [{"column": "cafes.format", "op": "in", "value": [r[0] for r in fmt]}]})
    assert "cafes.format without grouping by it" in msg, msg


def test_m2m_invalid_link_id_422(lq):
    assert refused(422, **{**BY_VISITED, "links": ["Not An Id"]}) == "Invalid input."  # the ID pattern, before planning


def test_m2m_restricted_without_follow(lq):
    """No rule on the fact (customers) nor on the bridge (orders), one on the target (cafes): the Viewer's North is the
    owner's, and customers with no North visit are in the empty member, as the owner's sql_query counts them."""
    o = json.loads(json.dumps(ONTO))
    o["links"][2].pop("rls")
    sure("save_ontology", project=P, ontology=o)
    try:
        mine = {r[0]: r[1] for r in ask(lq["token"], **BY_VISITED)["rows"]}
        owner = {r[0]: r[1] for r in ask(**BY_VISITED)["rows"]}
        [(rest,)] = sql("SELECT count(*) FROM customers WHERE customer_id NOT IN (SELECT o.customer_id FROM orders o JOIN"
                        " cafes c USING (cafe_id) WHERE c.region = 'North' AND o.customer_id IS NOT NULL)")
        assert mine == {"North": owner["North"], None: rest}, (mine, owner, rest)
    finally:
        sure("save_ontology", project=P, ontology=ONTO)


def test_m2m_restricted_reads_no_shared_partials(lq):
    run = lambda q: json.loads(engine(SHARED, timeout=120, stdin=json.dumps({"project": P, "q": q, "uid": lq["uid"]})).stdout.splitlines()[-1])  # noqa: E731
    m2m = run(BY_VISITED)
    assert m2m["shared"] == [] and "__lake" not in m2m["sql"] and "SELECT DISTINCT" in m2m["sql"], m2m
    plain = run({"measures": ["revenue"], "dimensions": ["cafes.city"]})
    assert plain["shared"], plain  # the same Viewer's read without a link does share partials


def test_scoped_agent_refused_across_link(lq):
    """D-24: an agent scoped to customers and cafés doesn't read orders through `visited`, named or default."""
    o = json.loads(json.dumps(ONTO))
    o["links"][2]["default"] = True
    plain = {"measures": ["cust_count"], "dimensions": ["customers.segment"]}
    r = engine(SCOPE, timeout=120, stdin=json.dumps({"project": P, "queries": [BY_VISITED, plain]}))
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == ["Outside your scope: orders.", None]
    sure("save_ontology", project=P, ontology=o)
    try:
        r = engine(SCOPE, timeout=120, stdin=json.dumps({"project": P, "queries": [{**BY_VISITED, "links": []}]}))
        assert json.loads(r.stdout.splitlines()[-1]) == ["Outside your scope: orders."], r.stdout
    finally:
        sure("save_ontology", project=P, ontology=ONTO)


def test_m2m_restricted_link_choices_cached_apart(lq):
    """Customers by year through two links (when they signed up, the years they ordered): different rows, each its own
    cache entry for the Viewer. Last: the signup relationship extends the calendar."""
    abi.call("add_relationship", project=P, **{"from": "customers.signup_date", "to": "calendar.day"})
    rels = abi.call("semantic_model", project=P, all=True)["relationships"]
    dated = next(r["id"] for r in rels if r["from"] == {"table": "orders", "columns": ["order_date"]})
    o = json.loads(json.dumps(ONTO))
    o["objects"].append({"id": "day", "table": "calendar", "name": "Day", "plural": "Days", "title": "day"})
    o["links"] += [{"id": "signed", "from": "customer", "to": "day", "name": "signed up", "via": ["customers-signup_date"]},
                   {"id": "active", "from": "customer", "to": "day", "name": "ordered", "via": ["orders-customer_id", dated]}]
    try:
        sure("save_ontology", project=P, ontology=o)
        q = {"measures": ["cust_count"], "dimensions": ["calendar.year"]}
        a, b = (ask(lq["token"], **q, links=[x])["rows"] for x in ("signed", "active"))
        assert a and b and a != b, (a, b)
        assert ask(lq["token"], **q, links=["signed"])["rows"] == a and ask(lq["token"], **q, links=["active"])["rows"] == b
    finally:
        sure("save_ontology", project=P, ontology=ONTO)
        sure("remove_relationship", project=P, id="customers-signup_date")


@contextlib.contextmanager
def visited_default():
    o = json.loads(json.dumps(ONTO))
    o["links"][2]["default"] = True
    sure("save_ontology", project=P, ontology=o)
    try:
        yield
    finally:
        sure("save_ontology", project=P, ontology=ONTO)


def test_agent_scope_expressions_and_datasets(lq):
    """D-24: an inline expression's fact counts as the query's (it reads orders through visited); a scoped dataset
    with a link brings what it reads across it."""
    q = {"measures": ["cafes_count"], "dimensions": ["cafes.region"], "links": ["visited"],
         "expressions": [{"name": "n", "expr": "COUNT(customers.customer_id)"}]}
    r = engine(SCOPE, timeout=120, stdin=json.dumps({"project": P, "queries": [q, {**q, "links": []}]}))
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == ["Outside your scope: orders.", None]
    r = engine(SCOPED, timeout=120, stdin=json.dumps({"project": P, "datasets": ["visitors"]}))
    assert json.loads(r.stdout.splitlines()[-1]) == ["cafes", "orders"], r.stdout  # its measure's fact comes with the measure


def test_slicer_across_m2m_doesnt_filter(lq):
    """A cross-filter on a table a visual reaches only through a many-to-many link (here a default one) leaves it
    unfiltered, unless it groups by that column."""
    region = {"column": "cafes.region", "op": "=", "value": "North"}
    fmt = {"column": "cafes.format", "op": "=", "value": "campus"}
    cases = [({"measures": ["cust_count"], "dimensions": ["customers.segment"]}, region),
             ({"measures": ["cust_count"], "dimensions": ["cafes.region"]}, region),
             ({"measures": ["cust_count"], "dimensions": ["cafes.region"]}, fmt),  # a far-side slicer
             ({"measures": ["revenue"], "dimensions": ["customers.segment"]}, region)]  # many-to-one: it filters
    with visited_default():
        r = engine(REACH, timeout=120, stdin=json.dumps({"project": P, "cases": cases}))
        assert r.returncode == 0, r.stderr[-2000:]
        assert json.loads(r.stdout.splitlines()[-1]) == [["cafes.region"], [], ["cafes.format"], []]
        assert ask(**cases[0][0], filters=[])["rows"]  # the visual itself still answers


def test_edit_total_follows_the_visuals_links(lq):
    [(day, total)] = sql("SELECT order_date, sum(revenue) FROM orders GROUP BY 1 ORDER BY 1 LIMIT 1")
    cell = {"measure": "revenue", "filters": [{"column": "calendar.day", "op": "=", "value": str(day)}], "value": 1, "old": -1}
    with two_dates():
        with pytest.raises(abi.ActionError) as e:
            abi.call("edit_total", project=P, **cell)
        assert e.value.status == 400 and "order_day" in e.value.body["message"], e.value.body
        with pytest.raises(abi.ActionError) as e:  # planned through the link: only the stale total refuses it
            abi.call("edit_total", project=P, **cell, links=["order_day"])
        assert e.value.status == 409 and "changed" in e.value.body["message"] and total, e.value.body


def test_explain_change_refuses_m2m_dimension(lq):
    """Members across a many-to-many link don't add up to the total, so explain_change doesn't break a change into them."""
    abi.call("add_relationship", project=P, **{"from": "customers.signup_date", "to": "calendar.day"})
    try:
        with visited_default():
            with pytest.raises(abi.ActionError) as e:
                abi.call("explain_change", project=P, measure="cust_count", period="year", dimensions=["cafes.region"])
            assert e.value.status == 400 and "many-to-many" in e.value.body["message"], e.value.body
            r = abi.call("explain_change", project=P, measure="cust_count", period="year")
            assert "cafes.region" not in r["dimensions"] + r["also_checked"], r
    finally:
        sure("remove_relationship", project=P, id="customers-signup_date")
