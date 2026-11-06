"""1.2.6 M3 (needs the engine at :8780, no LLM): row-level security that follows links, on Brewline. Walking a link
reads only the person's own rows (#13). A link with `rls: follow` (#14) shows a restricted Viewer only the objects of
its `from` type linked to objects of its `to` type they may see: customers who visited a café of their region. The keys
are read by a system read and inlined as constants (the views stay join-free), the filter reaches tables pointing at
customers (a predictions table) but never goes back along the link's own relationships (anonymous orders stay), and
explain, the filtered tables and their key columns say so. Adding, changing or removing such a link needs the Owner
and a confirmation naming who sees fewer rows. The owner's sql_query is the reference for every set of rows."""
import contextlib
import copy
import json
import secrets
import time

import pytest
from conftest import drop
from test_engine import engine
from test_ontology_nobreak import brew
from test_pages import sure

import abi

P = "pi_rls_links"
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
VISITED = ("SELECT DISTINCT o.customer_id FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = 'North'"
           " AND o.customer_id IS NOT NULL ORDER BY 1")

# ML-5 as score_model writes it (a fact ml_<id> keyed by the dataset's customers.customer_id, related to customers),
# without training: the predictions are the segment itself
SCORE = r"""
import json, sys
from abi import db, security, storage
from abi.project import git
from abi.semantic import model
P = json.loads(sys.stdin.read())["project"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
write = lambda cur: cur.execute("CREATE OR REPLACE TABLE lake.model.ml_churn AS SELECT customer_id AS customers_customer_id,"
                                " segment AS customers_segment, segment AS prediction FROM lake.model.customers")
with git.change(P, owner, "score ML model churn"):
    storage._commit(storage.lake(P), write, owner.name, "score ML model churn")
    mj = model.load(P)
    mj["tables"]["ml_churn"] = {"kind": "fact", "key": None, "description": "Predictions of ML model 'Churn' (classification), written by score_model."}
    mj["relationships"].append(model._rel("ml_churn.customers_customer_id", "customers.customer_id"))
    model.save(P, mj)
print("{}")
"""

RLS = r"""
import json, sys
from abi import semantic, storage
from abi.semantic import rls
a = json.loads(sys.stdin.read())
P, uid = a["project"], a["uid"]
read = lambda sql, reads: semantic.execute(P, sql, {"test": "rls links"}, reads=reads)
cols = semantic._columns_of(storage.lake(P).cursor(), "model")
conds = rls.conditions(P, uid, read, cols)
print(json.dumps({"explain": rls.explain(P, uid), "filtered": sorted(rls.filtered(P)), "conds": conds,
                  "keys": {t: rls.key_columns(P, t) for t in ("cafes", "customers", "orders", "ml_churn")},
                  "denied": rls.unfiltered_outputs(P)}))
"""


def sql(q: str) -> list:
    return abi.call("sql_query", project=P, sql=q)["rows"]


@pytest.fixture(scope="module")
def links(tmp_path_factory):
    """P: Brewline with a North-region Viewer (a row rule on cafes.region), an Editor, the dataset and predictions of a
    churn model on customers, and the ontology above (visited follows the rule on cafes)."""
    brew(P, tmp_path_factory.mktemp("links"))
    stamp, made, tokens = time.strftime("%H%M%S"), {}, {}
    try:
        for w, role in (("viewer", "viewer"), ("editor", "editor")):
            made[w], pw = f"pi_links_{w}_{stamp}", secrets.token_urlsafe(16)
            abi.call("create_user", name=made[w], password=pw)
            abi.call("set_role", project=P, user=made[w], role=role)
            tokens[w] = abi.login(made[w], pw)[1]["token"]
        uid = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}[made["viewer"]]
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={uid: ["North"]})
        abi.call("save_dataset", project=P, id="custs", name="Customers", query={
            "measures": ["revenue"], "dimensions": ["customers.customer_id", "customers.segment"]})
        abi.call("save_ml_model", project=P, ml={"id": "churn", "name": "Churn", "task": "classification", "dataset": "custs",
                                                 "target": "customers.segment"})
        r = engine(SCORE, timeout=300, stdin=json.dumps({"project": P}))
        assert r.returncode == 0, r.stderr[-2000:]
        sure("save_ontology", project=P, ontology=ONTO)
        yield {"uid": uid, "names": made, "tokens": tokens}
    finally:
        for n in made.values():
            sure("delete_user", name=n)
        drop([P])


def mine(fx, action: str, **args):
    with abi.as_user(fx["tokens"]["viewer"]):
        return abi.call(action, project=P, **args)


def every(links, **args) -> list:
    rows, offset = [], 0
    while True:
        r = mine(links, "list_objects", **args, limit=1000, offset=offset)
        rows += r["rows"]
        if not r["more"]:
            return rows
        offset += 1000


def state(links) -> dict:
    r = engine(RLS, timeout=300, stdin=json.dumps({"project": P, "uid": links["uid"]}))
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.splitlines()[-1])


@contextlib.contextmanager
def saved(o: dict):
    """The ontology `o` saved for one block (the Owner confirms), then the fixture's again."""
    sure("save_ontology", project=P, ontology=o)
    try:
        yield
    finally:
        sure("save_ontology", project=P, ontology=ONTO)


# ---------- #13 traversal is under row-level security already ----------

def test_walk_never_shows_hidden_neighbor(links):
    """A customer who visited cafés of several regions: walking `visited` the North Viewer sees only the North ones."""
    [(c,)] = sql("SELECT customer_id FROM orders o JOIN cafes USING (cafe_id) WHERE customer_id IS NOT NULL GROUP BY 1"
                 " HAVING count(DISTINCT region) > 1 AND max(CASE WHEN region = 'North' THEN 1 ELSE 0 END) = 1 ORDER BY 1 LIMIT 1")
    owner = abi.call("get_object", project=P, type="customer", key=[c], links=["visited"], limit=1000)["links"][0]["rows"]
    north = {x[0] for x in sql("SELECT cafe_id FROM cafes WHERE region = 'North'")}
    theirs = mine(links, "get_object", type="customer", key=[c], links=["visited"], limit=1000)["links"][0]["rows"]
    assert {x[0] for x in owner} - north, "the owner walks to other regions' cafés"
    assert theirs and [x for x in owner if x[0] in north] == theirs, "the Viewer only to North ones"
    south = [{"link": "visited", "property": "region", "op": "=", "value": "South"}]
    assert abi.call("list_objects", project=P, type="customer", filters=south, limit=1)["rows"] and not every(links, type="customer", filters=south)


# ---------- #14 rls: follow ----------

def test_customers_follow_visible_cafes(links):
    want = [x[0] for x in sql(VISITED)]
    every_one = sql("SELECT count(*) FROM customers")[0][0]
    assert 1000 < len(want) < every_one
    assert [x[0] for x in every(links, type="customer", order=[{"property": "customer_id"}])] == want
    assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[len(want)]]
    out = sql(f"SELECT min(customer_id) FROM customers WHERE customer_id NOT IN ({VISITED})")[0][0]
    with pytest.raises(abi.ActionError) as e:
        mine(links, "get_object", type="customer", key=[out])
    assert e.value.status == 404, "a customer who never visited a North café is hidden"
    with abi.as_user(links["tokens"]["editor"]):
        assert abi.call("sql_query", project=P, sql="SELECT count(*) FROM customers")["rows"] == [[every_one]], "an Editor sees all"
    with saved({**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}):
        assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[every_one]], "without follow: every customer"


def test_anonymous_orders_stay_visible(links):
    """The follow filter of customers never travels back along orders-customer_id: every order of a North café, those
    without a customer too, is the Viewer's, as before the link."""
    q = "SELECT count(*), count(*) FILTER (WHERE customer_id IS NULL), sum(revenue) FROM orders"
    want = sql(f"{q} WHERE cafe_id IN (SELECT cafe_id FROM cafes WHERE region = 'North')")
    assert want[0][1] > 0, "Brewline has anonymous orders"
    assert mine(links, "sql_query", sql=q)["rows"] == want
    revenue = abi.call("semantic_query", project=P, measures=["revenue"], filters=[{"column": "cafes.region", "op": "=", "value": "North"}])
    assert mine(links, "semantic_query", measures=["revenue"])["rows"] == revenue["rows"]


def test_ml_table_follows_object(links):
    """Predictions on customers (ml_churn), trained on orders the cafés rule filters: denied to restricted Viewers until
    customers follow that rule; then the predictions of their own customers only."""
    want = [x[0] for x in sql(VISITED)]
    got = mine(links, "sql_query", sql="SELECT customers_customer_id FROM ml_churn ORDER BY 1")["rows"]
    assert [x[0] for x in got] == want
    s = state(links)
    assert "ml_churn" in s["filtered"] and "ml_churn" not in s["denied"] and s["keys"]["ml_churn"] == ["customers_customer_id"]
    with saved({**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}):
        assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_churn")["rows"] == [[0]], "fail closed without the link"
        assert "ml_churn" in state(links)["denied"]
        assert any("ml_churn" in g for g in abi.call("readiness", project=P)["missing"])


def test_explain_names_follow_rule(links):
    s = state(links)
    lines = s["explain"].splitlines()
    assert "-- customers: only rows linked by 'visited' (orders-customer_id, orders-cafe_id) to a row of cafes you may see" in lines, lines
    assert "-- orders: only rows whose cafe_id is a row of cafes you may see" in lines
    assert not any(x.startswith("-- orders:") and "customer" in x for x in lines), "never back along the link's own via"
    assert {"cafes", "customers", "orders", "ml_churn"} <= set(s["filtered"])
    assert s["keys"] == {"cafes": ["region"], "customers": ["customer_id"], "orders": ["cafe_id"], "ml_churn": ["customers_customer_id"]}
    c = s["conds"]["customers"]
    n = len(sql(VISITED))
    assert c.startswith("list_contains([") and c.count("CAST(") == n and "SELECT" not in c.upper(), "constants, never a join"
    assert "customer_id" not in s["conds"]["orders"]
    print(f"\nfollow keys inlined for the North Viewer: {n} customers, {len(c):,} characters of view text")


def test_fixpoint_with_cycle(links):
    """Customers follow cafés and cafés follow customers (two follow links, a cycle), with a rule on three customers:
    the greatest fixpoint gives the cafés of the North those three visited, and the three; it ends."""
    three = [x[0] for x in sql(VISITED)[:3]]
    lst = ", ".join(f"'{c}'" for c in three)
    cafes = [x[0] for x in sql(f"SELECT DISTINCT cafe_id FROM orders JOIN cafes USING (cafe_id) WHERE region = 'North'"
                               f" AND customer_id IN ({lst}) ORDER BY 1")]
    o = copy.deepcopy(ONTO)
    o["links"].append({"id": "visitors", "from": "cafe", "to": "customer", "name": "visitors", "inverse": "visited cafés",
                       "via": ["orders-cafe_id", "orders-customer_id"], "rls": "follow"})
    sure("set_rls_rule", project=P, id="few", table="customers", column="customer_id", users={links["uid"]: three})
    try:
        with saved(o):
            start = time.monotonic()
            got = mine(links, "sql_query", sql="SELECT cafe_id FROM cafes ORDER BY 1")["rows"]
            assert [x[0] for x in got] == cafes and len(cafes) < len(sql("SELECT cafe_id FROM cafes WHERE region = 'North'"))
            assert [x[0] for x in mine(links, "sql_query", sql="SELECT customer_id FROM customers ORDER BY 1")["rows"]] == three
            assert time.monotonic() - start < 60
            lines = state(links)["explain"].splitlines()
            assert "-- cafes: only rows linked by 'visitors' (orders-cafe_id, orders-customer_id) to a row of customers you may see" in lines
    finally:
        sure("delete_rls_rule", project=P, id="few")


def test_follow_needs_owner(links):
    """Owner answer 1: an Editor can't add, change or remove a follow link (403, nothing saved) but changes the rest;
    the Owner's change asks a confirmation naming the Viewers who will see fewer rows."""
    ed, viewer = links["tokens"]["editor"], links["names"]["viewer"]
    plain = copy.deepcopy(ONTO)
    plain["links"][2].pop("rls")
    changed = copy.deepcopy(ONTO)
    changed["links"][1]["rls"] = "follow"  # an existing link now followed
    added = copy.deepcopy(ONTO)
    added["links"].append({"id": "bought_at", "from": "customer", "to": "cafe", "name": "bought at", "via": ["orders-customer_id", "orders-cafe_id"],
                           "rls": "follow"})
    for o in (plain, changed, added):
        with abi.as_user(ed), pytest.raises(abi.ActionError) as e:
            abi.call("save_ontology", project=P, ontology=o)
        assert e.value.status == 403 and "Only a project Owner" in e.value.body["message"], e.value.body
    with abi.as_user(ed), pytest.raises(abi.ActionError) as e:
        abi.call("delete_ontology_item", project=P, kind="link", id="visited")
    assert e.value.status == 403, e.value.body
    assert abi.call("ontology", project=P)["links"][2].get("rls") == "follow", "nothing saved"
    desc = copy.deepcopy(ONTO)
    desc["objects"][0]["description"] = "One shop."
    with abi.as_user(ed), pytest.raises(abi.ActionError) as e:
        abi.call("save_ontology", project=P, ontology=desc)
    assert e.value.status == 409 and "follow" not in e.value.body["preview"], "the rest is an Editor's, with the usual yes"
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_ontology", project=P, ontology=changed)
    body = e.value.body
    assert e.value.status == 409 and body.get("confirm_token"), body
    assert "fewer rows" in body["message"] and viewer in body["message"] and "Orders only when linked by 'order_customer' to Customers" in body["message"], body
    assert body["preview"]["follow"]["new"]["order_customer"] == ["order", "customer", ["orders-customer_id"]]
    assert body["preview"]["changes"] == ["Changes link 'order_customer' (order customer customer, via orders-customer_id): rls."]
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_ontology", project=P, ontology=plain)
    assert "more rows" in e.value.body["message"] and viewer in e.value.body["message"]
