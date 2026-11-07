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


def sql(q: str) -> list:
    return abi.call("sql_query", project=P, sql=q)["rows"]


def ask(token: str | None = None, **q) -> dict:
    with abi.as_user(token) if token else contextlib.nullcontext():
        return abi.call("semantic_query", project=P, **q)


def refused(status: int, token: str | None = None, **q) -> str:
    with pytest.raises(abi.ActionError) as e:
        ask(token, **q)
    assert e.value.status == status, e.value.body
    return e.value.body["message"]


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
