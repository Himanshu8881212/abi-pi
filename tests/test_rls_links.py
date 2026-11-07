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

import duckdb
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
                  "keys": {t: rls.key_columns(P, t) for t in a.get("tables", ("cafes", "customers", "orders", "ml_churn"))},
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


def state(links, **more) -> dict:
    r = engine(RLS, timeout=300, stdin=json.dumps({"project": P, "uid": links["uid"], **more}))
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
    """Predictions on customers (ml_churn), trained on orders the cafés rule filters: denied to restricted Viewers with
    the follow link and without it (owner, round 5: a follow link never makes a table built from filtered rows
    visible; M9 revisits predictions as properties)."""
    got = mine(links, "sql_query", sql="SELECT customers_customer_id FROM ml_churn ORDER BY 1")["rows"]
    assert got == [], "denied with the link"
    s = state(links)
    assert "ml_churn" in s["filtered"] and "ml_churn" in s["denied"] and s["keys"]["ml_churn"] == ["customers_customer_id"]
    assert any("ml_churn" in g for g in abi.call("readiness", project=P)["missing"])
    with saved({**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}):
        assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_churn")["rows"] == [[0]], "fail closed without the link"
        assert "ml_churn" in state(links)["denied"]
        assert any("ml_churn" in g for g in abi.call("readiness", project=P)["missing"])
        assert set(state(links)["denied"]) <= set(s["denied"]), "a follow link only ever adds denials: none without it that it lacks"


def test_explain_names_follow_rule(links):
    s = state(links)
    lines = s["explain"].splitlines()
    assert "-- customers: only rows linked by 'visited' (orders-customer_id, orders-cafe_id) to a row of cafes you may see" \
        " (cafes is restricted for you)" in lines, lines
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
            assert "-- cafes: only rows linked by 'visitors' (orders-cafe_id, orders-customer_id) to a row of customers you may see" \
                " (customers is restricted for you)" in lines
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
    [new] = body["preview"]["follow"]["new"]["order_customer"]
    assert (new["from"], new["to"], new["cols"], [r["id"] for r, _ in new["hops"]]) == ("orders", "customers", ["customer_id"], ["orders-customer_id"])
    assert body["preview"]["changes"] == ["Changes link 'order_customer' (order customer customer, via orders-customer_id): rls."]
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_ontology", project=P, ontology=plain)
    assert "more rows" in e.value.body["message"] and viewer in e.value.body["message"]


BUNDLE = r"""
import json, sys
from abi.project import bundle, git
print(json.dumps(bundle.bundle(git.path(json.loads(sys.stdin.read())["project"]))))
"""

WRITE = r"""
import json, sys
from abi.project import git
a = json.loads(sys.stdin.read())
p = git.path(a["project"]) / "model/ontology.json"
old = p.read_text()
p.write_text(a["text"] if a["text"] is not None else old)
print(json.dumps({"old": old}))
"""


def files() -> dict:
    r = engine(BUNDLE, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


def editor_import(links, b: dict) -> abi.ActionError | dict:
    with abi.as_user(links["tokens"]["editor"]):
        try:
            return abi.call("import_project", project=P, bundle=b)
        except abi.ActionError as e:
            return e


def test_editor_import_cant_change_what_a_follow_link_means(links):
    """Security H1: every route changing a follow link's meaning, in the ontology or in model.json (a relationship's
    columns under the same ID), needs the Owner: an Editor's import is refused whole. A changed entity key changes
    nothing a follow filter reads (it tests the relationship's own columns)."""
    b, n = files(), mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"]
    o = copy.deepcopy(b["model/ontology.json"])
    o["links"][2].pop("rls")
    m = copy.deepcopy(b["model/model.json"])
    next(r for r in m["relationships"] if r["id"] == "orders-customer_id")["from"]["columns"] = ["order_id"]
    for x in ({**b, "model/ontology.json": o}, {**b, "model/model.json": m}):
        e = editor_import(links, x)
        assert isinstance(e, abi.ActionError) and e.status == 403 and "Only a project Owner" in e.body["message"], e
    assert files() == b, "nothing changed"
    k = copy.deepcopy(b["model/model.json"])
    next(x for x in k["entities"] if x["table"] == "customers")["key"] = ["segment"]
    q = "SELECT count(*), count(*) FILTER (WHERE customer_id IS NULL) FROM orders"
    orders = mine(links, "sql_query", sql=q)["rows"]
    e = editor_import(links, {**b, "model/model.json": k})
    assert not isinstance(e, abi.ActionError), e  # the key isn't what a follow link means: an Editor's change
    try:  # and it changes nothing a follow filter does
        assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == n
        assert mine(links, "sql_query", sql=q)["rows"] == orders and orders[0][1] > 0, "orders, anonymous ones too, unchanged"
        s = state(links)
        assert s["keys"]["customers"] == ["customer_id"] and s["keys"]["orders"] == ["cafe_id"] and "ml_churn" in s["denied"]
    finally:
        assert not isinstance(editor_import(links, b), abi.ActionError)
    assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == n


def test_broken_follow_link_fails_closed(links):
    """A follow link that no longer holds (a relationship gone from its via, its from type gone), written behind ABI's
    back: restricted Viewers see none of the rows it may mean, and ml_churn stays denied."""
    o = copy.deepcopy(files()["model/ontology.json"])
    gone, ghost = copy.deepcopy(o), copy.deepcopy(o)
    gone["links"][2]["via"] = ["orders-customer_id", "nope"]
    ghost["links"][2]["from"] = "ghost"
    count = lambda t: mine(links, "sql_query", sql=f"SELECT count(*) FROM {t}")["rows"][0][0]  # noqa: E731
    r = engine(WRITE, timeout=120, stdin=json.dumps({"project": P, "text": json.dumps(gone)}))
    assert r.returncode == 0, r.stderr[-2000:]
    old = json.loads(r.stdout.splitlines()[-1])["old"]
    try:
        assert count("customers") == 0 and count("ml_churn") == 0 and count("orders") > 0
        s = state(links)
        assert "ml_churn" in s["denied"] and "no longer holds" in s["explain"]
        r = engine(WRITE, timeout=120, stdin=json.dumps({"project": P, "text": json.dumps(ghost)}))
        assert r.returncode == 0, r.stderr[-2000:]
        assert count("customers") == count("orders") == count("cafes") == 0, "every table the link may mean"
    finally:
        r = engine(WRITE, timeout=120, stdin=json.dumps({"project": P, "text": old}))
        assert r.returncode == 0, r.stderr[-2000:]
    assert count("customers") == len(sql(VISITED))


LAKE = r"""
import json, sys
from abi import db, security, storage
from abi.project import git
from abi.semantic import model
a = json.loads(sys.stdin.read())
P, t = a["project"], a["table"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
rows = " UNION ALL ".join(f"SELECT '{k}' AS customer_id, '{n}' AS note" for k, n in a["rows"])
with git.change(P, owner, "a lake table"):
    storage._commit(storage.lake(P), lambda cur: cur.execute(f"CREATE OR REPLACE TABLE lake.model.{t} AS {rows}"), owner.name, "a lake table")
    mj = model.load(P)
    mj["tables"][t] = {"kind": "dimension", "key": ["customer_id"], "description": "Profiles."}
    mj["relationships"] += [model._rel(f"{t}.customer_id", "customers.customer_id"), model._rel("customers.customer_id", f"{t}.customer_id")]
    model.save(P, mj)
print("{}")
"""


@pytest.mark.parametrize("kind", ["input", "lake"])
def test_cycle_keeps_anonymous_orders(links, kind):
    """Correctness: a filter made from the follow filter is the link's too, so it never crosses orders-customer_id
    either, while a part made from a rule always does, each with its own tag. A cycle customers <-> profiles (one row
    per visitor, one-to-one both ways; an input table or a lake table) carries the follow filter round to customers
    again: anonymous orders still stay visible. With a rule on profiles, customers whose profile is hidden stay hidden
    and orders keep exactly what they have without the link (never more)."""
    three = [x[0] for x in sql(VISITED)[:3]]
    t = "profiles" if kind == "input" else "profiles_l"
    q = "SELECT count(*), count(*) FILTER (WHERE customer_id IS NULL) FROM orders"
    want = sql(f"{q} WHERE cafe_id IN (SELECT cafe_id FROM cafes WHERE region = 'North')")
    notes = [(c, "y" if i == 2 else "x") for i, c in enumerate(three)]
    if kind == "input":
        sure("create_input_table", project=P, table=t, key=["customer_id"],
             columns=[{"name": "customer_id", "type": "VARCHAR"}, {"name": "note", "type": "VARCHAR"}])
        abi.call("edit_cells", project=P, table=t, add=[{"customer_id": c, "note": n} for c, n in notes])
        abi.call("add_relationship", project=P, **{"from": f"{t}.customer_id", "to": "customers.customer_id"})
        abi.call("add_relationship", project=P, **{"from": "customers.customer_id", "to": f"{t}.customer_id"})
    else:
        r = engine(LAKE, timeout=300, stdin=json.dumps({"project": P, "table": t, "rows": notes}))
        assert r.returncode == 0, r.stderr[-2000:]
    try:
        got = [x[0] for x in mine(links, "sql_query", sql="SELECT customer_id FROM customers ORDER BY 1")["rows"]]
        assert got == three, "customers: visitors of the North with a profile"
        assert mine(links, "sql_query", sql=q)["rows"] == want and want[0][1] > 0, "every North order, anonymous ones too"
        assert state(links)["keys"]["orders"] == ["cafe_id"]
        sure("set_rls_rule", project=P, id="notes", table=t, column="note", users={links["uid"]: ["x"]})
        try:
            got = [x[0] for x in mine(links, "sql_query", sql="SELECT customer_id FROM customers ORDER BY 1")["rows"]]
            assert got == three[:2], "a customer whose profile is hidden stays hidden"
            q2 = "SELECT customer_id, count(*) FROM orders GROUP BY 1 ORDER BY 1 NULLS FIRST"
            with_link = mine(links, "sql_query", sql=q2)["rows"]
            with saved({**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}):
                assert mine(links, "sql_query", sql=q2)["rows"] == with_link, "orders: the same rows as without the link"
            assert [x[0] for x in with_link] == three[:2], "orders of the visible profiles only (no anonymous ones, as without the link)"
        finally:
            sure("delete_rls_rule", project=P, id="notes")
    finally:
        for r in ("customers-customer_id", f"{t}-customer_id"):
            sure("remove_relationship", project=P, id=r)


def test_follow_target_with_no_rows_denies(links):
    """A follow link whose `to` table shows the person no rows by default (every column hidden), though no rule
    filters it: its `from` objects are none of theirs either (fail closed), not all of them."""
    o = copy.deepcopy(ONTO)
    o["objects"].append({"id": "product", "table": "products", "name": "Product", "plural": "Products", "title": "product_name"})
    o["links"].append({"id": "bought", "from": "customer", "to": "product", "name": "bought", "via": ["orders-customer_id", "orders-product_id"],
                       "rls": "follow"})
    cols = abi.call("sql_query", project=P, sql="SELECT * FROM products LIMIT 0")["columns"]
    with saved(o):
        assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[len(sql(VISITED))]], "products restrict no one"
        sure("set_column_rule", project=P, id="noproducts", table="products", columns=cols)
        try:
            assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[0]]
            assert mine(links, "sql_query", sql="SELECT count(*) FROM orders")["rows"][0][0] > 0, "only the follower is denied"
        finally:
            sure("delete_rls_rule", project=P, id="noproducts")


def test_editor_add_relationship_on_a_via_refused(links):
    """Security: add_relationship re-adds a relationship under its ID (orders-customer_id onto another keyed table):
    what visited walks would change, so an Editor gets 403 with nothing changed; the Owner a confirmation."""
    sure("create_input_table", project=P, table="people", key=["customer_id"], columns=[{"name": "customer_id", "type": "VARCHAR"}])
    before = files()["model/model.json"]
    with abi.as_user(links["tokens"]["editor"]), pytest.raises(abi.ActionError) as e:
        abi.call("add_relationship", project=P, **{"from": "orders.customer_id", "to": "people.customer_id"})
    assert e.value.status == 403 and "Only a project Owner" in e.value.body["message"], e.value.body
    assert files()["model/model.json"] == before, "nothing changed"
    with pytest.raises(abi.ActionError) as e:
        abi.call("add_relationship", project=P, **{"from": "orders.customer_id", "to": "people.customer_id"})
    assert e.value.status == 409 and "different rows" in e.value.body["message"] and links["names"]["viewer"] in e.value.body["message"]
    assert files()["model/model.json"] == before
    with abi.as_user(links["tokens"]["editor"]):  # a relationship no follow link walks stays the Editor's
        abi.call("add_relationship", project=P, **{"from": "people.customer_id", "to": "customers.customer_id"})
    sure("remove_relationship", project=P, id="people-customer_id")


def test_undo_and_revert_of_a_follow_change_need_the_owner(links):
    """Undo and revert across a follow change go through the import's gate: an Editor gets 403 before any
    confirmation, the Owner a confirmation naming who sees fewer rows (the undo guard: git.py routes it there)."""
    ed, viewer = links["tokens"]["editor"], links["names"]["viewer"]
    plain = {**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}
    follow = lambda: abi.call("ontology", project=P)["links"][2].get("rls")  # noqa: E731
    sure("set_auto_commit", project=P, on=False)
    try:
        sure("save_ontology", project=P, ontology=plain)
        for action, args in (("undo", {}),):
            with abi.as_user(ed), pytest.raises(abi.ActionError) as e:
                abi.call(action, project=P, **args)
            assert e.value.status == 403 and viewer not in json.dumps(e.value.body), e.value.body
        with pytest.raises(abi.ActionError) as e:
            abi.call("undo", project=P)
        assert e.value.status == 409 and "fewer rows" in e.value.body["message"] and viewer in e.value.body["message"], e.value.body
        abi.call("undo", project=P, confirm=e.value.body["confirm_token"])
        assert follow() == "follow"
        try:
            abi.call("commit", project=P, message="follow kept")
        except abi.ActionError as x:
            assert x.status == 409, x
        head = abi.call("history", project=P)["commits"][0]["id"]
        sure("save_ontology", project=P, ontology=plain)
        abi.call("commit", project=P, message="follow removed")
        with abi.as_user(ed), pytest.raises(abi.ActionError) as e:
            abi.call("revert", project=P, to=head)
        assert e.value.status == 403 and viewer not in json.dumps(e.value.body), e.value.body
        assert follow() is None
        with pytest.raises(abi.ActionError) as e:
            abi.call("revert", project=P, to=head)
        assert e.value.status == 409 and "fewer rows" in e.value.body["message"], e.value.body
        abi.call("revert", project=P, to=head, confirm=e.value.body["confirm_token"])
        assert follow() == "follow"
    finally:
        sure("save_ontology", project=P, ontology=ONTO)
        sure("set_auto_commit", project=P, on=True)


def test_follow_crosses_its_via_toward_f_only(links):
    """The crossing rule is direction-aware: a follow filter that reaches T's side again (cafés owned by a customer,
    cafes -> cafe_owner -> customers, outside the via) crosses orders-cafe_id the T-side way, so the orders of a café
    whose owner is hidden stay hidden, while it never crosses orders-customer_id back from F: anonymous orders stay."""
    north = [x[0] for x in sql("SELECT cafe_id FROM cafes WHERE region = 'North' ORDER BY 1")]
    h = north[0]
    out = sql(f"SELECT min(customer_id) FROM customers WHERE customer_id NOT IN ({VISITED})")[0][0]
    owner = sql(f"SELECT min(o.customer_id) FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = 'North' AND o.cafe_id <> '{h}'")[0][0]
    sure("create_input_table", project=P, table="cafe_owner", key=["cafe_id"],
         columns=[{"name": "cafe_id", "type": "VARCHAR"}, {"name": "owner", "type": "VARCHAR"}])
    abi.call("edit_cells", project=P, table="cafe_owner", add=[{"cafe_id": c, "owner": out if c == h else owner} for c in north])
    abi.call("add_relationship", project=P, **{"from": "cafe_owner.owner", "to": "customers.customer_id"})
    abi.call("add_relationship", project=P, **{"from": "cafes.cafe_id", "to": "cafe_owner.cafe_id"})
    try:
        q = "SELECT count(*), count(*) FILTER (WHERE customer_id IS NULL) FROM orders"
        want = sql(f"{q} WHERE cafe_id IN (SELECT cafe_id FROM cafes WHERE region = 'North') AND cafe_id <> '{h}'")
        assert [x[0] for x in mine(links, "sql_query", sql="SELECT cafe_id FROM cafes ORDER BY 1")["rows"]] == north[1:]
        assert mine(links, "sql_query", sql=f"SELECT count(*) FROM orders WHERE cafe_id = '{h}'")["rows"] == [[0]], "the hidden café's orders"
        assert mine(links, "sql_query", sql=q)["rows"] == want and want[0][1] > 0, "the rest, anonymous ones too"
    finally:
        for r in ("cafes-cafe_id", "cafe_owner-owner"):
            sure("remove_relationship", project=P, id=r)


def test_hidden_table_on_the_way_denies(links):
    """No follow decision is made from rows the person can't see: orders (between customers and cafés) with every
    column hidden gives no customers, not all of them."""
    cols = abi.call("sql_query", project=P, sql="SELECT * FROM orders LIMIT 0")["columns"]
    sure("set_column_rule", project=P, id="noorders", table="orders", columns=cols)
    try:
        assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[0]]
        assert "reads a table that shows you no rows" in state(links)["explain"]
    finally:
        sure("delete_rls_rule", project=P, id="noorders")
    assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[len(sql(VISITED))]]


def test_derived_table_stays_denied(links):
    """A per-customer table built from orders (revenue over every café, hidden ones too) related to customers stays
    denied to the Viewer with and without the follow link: only ML predictions (ML-5) follow it."""
    sure("set_steps", project=P, table="cust_rev", steps=[{"type": "source", "table": "orders"}, {
        "type": "group", "by": ["customer_id"], "aggregations": [{"name": "revenue", "function": "sum", "column": "revenue"}]}])
    end = time.monotonic() + 240
    while time.monotonic() < end:
        try:
            if sql("SELECT count(*) FROM cust_rev")[0][0]:
                break
        except abi.ActionError:
            pass
        time.sleep(2)
    abi.call("add_relationship", project=P, **{"from": "cust_rev.customer_id", "to": "customers.customer_id"})
    try:
        assert mine(links, "sql_query", sql="SELECT count(*) FROM cust_rev")["rows"] == [[0]]
        assert "cust_rev" in state(links)["denied"]
        with saved({**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}):
            assert mine(links, "sql_query", sql="SELECT count(*) FROM cust_rev")["rows"] == [[0]]
    finally:
        sure("remove_relationship", project=P, id="cust_rev-customer_id")


COMPOSITE = r"""
import json, sys
from abi import db, security, storage
from abi.project import git
from abi.semantic import model
P = json.loads(sys.stdin.read())["project"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
rows = "SELECT cafe_id, month::INTEGER AS month FROM (SELECT cafe_id FROM lake.model.cafes ORDER BY 1 LIMIT 2), range(1, 4) r(month)"
def write(cur):
    cur.execute(f"CREATE OR REPLACE TABLE lake.model.slots AS SELECT *, 40 AS seats FROM ({rows})")
    cur.execute(f"CREATE OR REPLACE TABLE lake.model.plans AS SELECT *, 100 * month AS target FROM ({rows})")
with git.change(P, owner, "slots and plans"):
    storage._commit(storage.lake(P), write, owner.name, "slots and plans")
    mj = model.load(P)
    for t in ("slots", "plans"):
        mj["tables"][t] = {"kind": "dimension" if t == "slots" else "fact", "key": ["cafe_id", "month"], "description": t}
    mj["relationships"].append({"id": "plans-cafe_id-month", "from": {"table": "plans", "columns": ["cafe_id", "month"]},
                                "to": {"table": "slots", "columns": ["cafe_id", "month"]}})
    model.save(P, mj)
print("{}")
"""


def test_composite_key_follow(links):
    """A follow link over a two-column relationship: slots follow plans (a rule on plans.month), the filter a tuple
    IN over both columns: the Viewer sees the slots of the months they may see, and explain and the key columns say so."""
    r = engine(COMPOSITE, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    o = copy.deepcopy(ONTO)
    o["objects"] += [{"id": "slot", "table": "slots", "name": "Slot", "plural": "Slots", "title": "month"},
                     {"id": "plan", "table": "plans", "name": "Plan", "plural": "Plans", "title": "month"}]
    o["links"].append({"id": "planned", "from": "slot", "to": "plan", "name": "plan", "via": ["plans-cafe_id-month"], "rls": "follow"})
    sure("set_rls_rule", project=P, id="months", table="plans", column="month", users={links["uid"]: [1, 2]})
    try:
        with saved(o):
            got = mine(links, "sql_query", sql="SELECT cafe_id, month FROM slots ORDER BY 1, 2")["rows"]
            assert got == sql("SELECT cafe_id, month FROM slots WHERE month IN (1, 2) ORDER BY 1, 2") and len(got) == 4
            s = state(links, tables=["slots", "plans"])
            assert s["keys"] == {"slots": ["cafe_id", "month"], "plans": ["month"]}, s["keys"]
            assert "-- slots: only rows linked by 'planned' (plans-cafe_id-month) to a row of plans you may see" in s["explain"]
            assert "list_contains([(CAST(" in s["conds"]["slots"] and "SELECT" not in s["conds"]["slots"].upper()
    finally:
        sure("delete_rls_rule", project=P, id="months")
        sure("remove_relationship", project=P, id="plans-cafe_id-month")


def loaded(t: str) -> None:
    end = time.monotonic() + 240
    while time.monotonic() < end:
        try:
            if sql(f"SELECT count(*) FROM {t}")[0][0]:
                return
        except abi.ActionError:
            pass
        time.sleep(2)
    pytest.fail(f"{t} didn't load")


def test_table_built_from_a_follower_is_denied(links, tmp_path):
    """A step table built from a table pointing at F (notes on customers, a loaded table, summed) reads rows the
    follow filter reaches: denied to the Viewer (no rows), as a table built from rule-filtered rows is."""
    duckdb.sql(f"COPY (SELECT printf('C%06d', i) AS customer_id, 1 AS n FROM range(1, 51) r(i)) TO '{tmp_path}/notes.parquet' (FORMAT parquet)")
    try:
        abi.upload(P, "notes", tmp_path / "notes.parquet")
        abi.call("add_relationship", project=P, **{"from": "notes.customer_id", "to": "customers.customer_id"})
        sure("set_steps", project=P, table="notes_sum", steps=[{"type": "source", "table": "notes"}, {
            "type": "group", "by": [], "aggregations": [{"name": "n", "function": "sum", "column": "n"}]}])
        loaded("notes_sum")
        assert "notes_sum" in state(links)["denied"]
        assert mine(links, "sql_query", sql="SELECT count(*) FROM notes_sum")["rows"] == [[0]]
    finally:
        gone("remove_relationship", id="notes-customer_id")
        gone("delete_table", table="notes_sum")  # what it read first, then the table it read
        gone("delete_table", table="notes")


UNSCORE = r"""
import json, sys
from abi import db, security, storage
from abi.project import git
from abi.semantic import model
a = json.loads(sys.stdin.read())
P, t = a["project"], a["table"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
with git.change(P, owner, f"drop {t}"):
    storage._commit(storage.lake(P), lambda cur: cur.execute(f"DROP TABLE IF EXISTS lake.model.{t}"), owner.name, f"drop {t}")
    mj = model.load(P)
    mj["tables"].pop(t, None)
    mj["relationships"] = [r for r in mj["relationships"] if t not in (r["from"]["table"], r["to"]["table"])]
    model.save(P, mj)
print("{}")
"""


def gone(action: str, **args) -> None:
    """A cleanup step: what the test made goes, confirmed here; already gone (or never made) is fine."""
    with contextlib.suppress(abi.ActionError):
        sure(action, project=P, **args)


@pytest.fixture
def churn2(links):
    """What test_new_ml_table_on_f_stays_denied makes goes after it, whatever it asserted: ml_churn2 as SCORE wrote
    it (lake table, model entry, relationship), then the ML model."""
    try:
        yield
    finally:
        r = engine(UNSCORE, timeout=300, stdin=json.dumps({"project": P, "table": "ml_churn2"}))
        assert r.returncode == 0, r.stderr[-2000:]
        gone("delete_ml_model", model="churn2")


@pytest.mark.usefixtures("churn2")
def test_new_ml_table_on_f_stays_denied(links):
    """A new ML model's predictions related to customers stay denied with the follow link."""
    abi.call("save_ml_model", project=P, ml={"id": "churn2", "name": "Churn 2", "task": "classification", "dataset": "custs",
                                             "target": "customers.segment"})
    r = engine(SCORE.replace("ml_churn", "ml_churn2"), timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    assert "ml_churn2" in state(links)["denied"]
    assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_churn2")["rows"] == [[0]]


SIGNUPS = r"""
import json, sys
from abi import db, security, storage
from abi.project import git
from abi.semantic import model
P = json.loads(sys.stdin.read())["project"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
write = lambda cur: cur.execute("CREATE OR REPLACE TABLE lake.model.signups AS SELECT DISTINCT signup_date, 1 AS n FROM lake.model.customers")
with git.change(P, owner, "signups"):
    storage._commit(storage.lake(P), write, owner.name, "signups")
    mj = model.load(P)
    mj["tables"]["signups"] = {"kind": "fact", "key": None, "description": "Signup days."}
    mj["relationships"].append({"id": "signups-signup_date", "from": {"table": "signups", "columns": ["signup_date"]},
                                "to": {"table": "customers", "columns": ["signup_date"]}})  # onto a non-key column
    model.save(P, mj)
print("{}")
"""


def test_non_key_relationship_takes_rule_and_follow_parts(links):
    """A fact related to a column of customers that isn't its key, while customers have a rule part (segment) and a
    follow part (visited): its rows are those the owner's reference read gives through both."""
    r = engine(SIGNUPS, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    sure("set_rls_rule", project=P, id="students", table="customers", column="segment", users={links["uid"]: ["Student"]})
    try:
        want = sql(f"SELECT signup_date FROM signups WHERE signup_date IN (SELECT signup_date FROM customers WHERE segment = 'Student'"
                   f" AND customer_id IN ({VISITED})) ORDER BY 1")
        assert want and len(want) < len(sql("SELECT signup_date FROM signups"))
        assert mine(links, "sql_query", sql="SELECT signup_date FROM signups ORDER BY 1")["rows"] == want
    finally:
        sure("delete_rls_rule", project=P, id="students")
        sure("remove_relationship", project=P, id="signups-signup_date")


def test_hidden_via_join_column_denies_follower(links):
    """M3 follow-up: a join column on the follow link's via hidden from the Viewer (orders.customer_id): no follow
    decision is made from a column they can't read, so no customer is theirs (fail closed); orders stay theirs."""
    sure("set_column_rule", project=P, id="nocust", table="orders", columns=["customer_id"])
    try:
        assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[0]]
        assert mine(links, "sql_query", sql="SELECT count(*) FROM orders")["rows"][0][0] > 0, "only the follower is denied"
    finally:
        sure("delete_rls_rule", project=P, id="nocust")
    assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[len(sql(VISITED))]]


CAPPED = r"""
import json, sys
from abi.semantic import rls
P = json.loads(sys.stdin.read())["project"]
ts = ("cafes", "customers", "orders", "ml_churn")
before = {t: rls.key_columns(P, t) for t in ts}
rls.CAP = -10 ** 6  # past the fixpoint cap at once (it never happens: tags never change)
print(json.dumps({"before": before, "capped": {t: rls.key_columns(P, t) for t in ts}, "filtered": sorted(rls.filtered(P))}))
"""


def test_key_columns_at_fixpoint_cap(links):
    """M3 follow-up: past the cap every table counts as filtered, and its key columns are every column any filter may
    read (each relationship's foreign key, each follow link's tested columns): never fewer than before, so M3.6 shared
    partials never slice by too few columns."""
    r = engine(CAPPED, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert all(set(out["before"][t]) <= set(out["capped"][t]) for t in out["before"]), out
    assert "customer_id" in out["capped"]["customers"] and {"cafe_id", "customer_id", "product_id"} <= set(out["capped"]["orders"]), out
    assert {"cafes", "customers", "orders", "ml_churn"} <= set(out["filtered"])


def test_follow_audience_counts_column_rules(links):
    """M3 follow-up: with only a column rule (no row rule) the Viewer is restricted (D-110), so a follow change's
    confirmation names them among those who will see other rows."""
    sure("set_column_rule", project=P, id="onlycols", table="products", columns=["note"])
    sure("delete_rls_rule", project=P, id="region")
    try:
        plain = {**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}
        with pytest.raises(abi.ActionError) as e:
            abi.call("save_ontology", project=P, ontology=plain)
        body = e.value.body
        assert e.value.status == 409 and "more rows" in body["message"] and links["names"]["viewer"] in body["message"], body
    finally:
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={links["uid"]: ["North"]})
        sure("delete_rls_rule", project=P, id="onlycols")
    assert abi.call("ontology", project=P)["links"][2].get("rls") == "follow", "nothing saved"


def test_derived_f_stays_denied(links):
    """F built from orders (customers' steps merge each one's revenue from a table built from orders): denied to the
    Viewer with the follow link too, as before it (last: it changes the fixture's customers, then puts them back)."""
    assert "sources/customers.json" in files() and "tables/customers.json" not in files()
    steps = [{"type": "source", "source": "customers"}]  # as loaded
    try:
        sure("set_steps", project=P, table="rev_by_cust", steps=[{"type": "source", "table": "orders"}, {
            "type": "group", "by": ["customer_id"], "aggregations": [{"name": "revenue", "function": "sum", "column": "revenue"}]}])
        loaded("rev_by_cust")
        sure("set_steps", project=P, table="customers", steps=[*steps, {"type": "merge", "table": "rev_by_cust", "on": {"customer_id": "customer_id"},
                                                                      "columns": ["revenue"]}])
        end = time.monotonic() + 240
        while "revenue" not in abi.call("sql_query", project=P, sql="SELECT * FROM customers LIMIT 0")["columns"] and time.monotonic() < end:
            time.sleep(2)
        assert "customers" in state(links)["denied"]
        assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[0]]
    finally:
        sure("set_steps", project=P, table="customers", steps=steps)
        gone("delete_table", table="rev_by_cust")
