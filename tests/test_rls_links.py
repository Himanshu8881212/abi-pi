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
import re
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
MARK = "[hidden by row-level security]"
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
import json, re, sys
from abi import semantic, storage
from abi.semantic import rls
a = json.loads(sys.stdin.read())
P, uid = a["project"], a["uid"]
read = lambda sql, reads: semantic.execute(P, sql, {"test": "rls links"}, reads=reads)
cols = semantic._columns_of(storage.lake(P).cursor(), "model")
conds = rls.conditions(P, uid, read, cols)
# M10: the same filters with the rules and the follow links iterated all at once (as before), and with the key lists
# built key by key: the same text, the same keys, whatever shape the test set up (a cycle, a chain, a composite key)
rls.SETTLE = False
at_once = rls.conditions(P, uid, read, cols)
rls.IN_SQL = False
by_key = rls.conditions(P, uid, read, cols)
rls.SETTLE, rls.IN_SQL = True, True
lits = lambda s: sorted(re.findall(r"CAST\('(?:[^']|'')*' AS [^()]*(?:\([^()]*\))?\)", s))
same = {"settled_same": conds == at_once, "keys_same": sorted(conds) == sorted(by_key) and all(lits(conds[t]) == lits(by_key[t]) for t in conds)}
print(json.dumps({"explain": rls.explain(P, uid), "filtered": sorted(rls.filtered(P)), "conds": conds, **same,
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
    got = json.loads(r.stdout.splitlines()[-1])
    assert got.pop("settled_same") is True and got.pop("keys_same") is True, "M10: one fixpoint, whichever order it is reached in"
    return got


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
            state(links)  # M10: the cycle with a rule on it settles to the same filters either way
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
        state(links, tables=["cafes", "customers", "orders"])  # M10: the chain back to T settles to the same filters either way
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
        state(links, tables=["cafes", "customers", "orders"])  # M10: a rule part and a follow part into one table: the same either way
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


# ---------- 1.2.6 M9: a prediction on an object is seen only with its object ----------

def ml_run(model: str, action: str) -> dict:
    """train_model or score_model of `model`, once its run ended; it must have worked."""
    run, kind = abi.call(action, project=P, model=model)["run"], {"train_model": "latest_run", "score_model": "latest_score"}[action]
    end = time.monotonic() + 900
    while True:
        r = next(m for m in abi.call("ml_models", project=P)["models"] if m["id"] == model)[kind]
        if r and r["run"] == run and r["status"] in ("ok", "failed"):
            assert r["status"] == "ok", r["error"]
            return r
        assert time.monotonic() < end, r
        time.sleep(1)


def refused(fx, status: int, action: str, **args) -> dict:
    with pytest.raises(abi.ActionError) as e:
        mine(fx, action, **args)
    assert e.value.status == status, e.value.body
    return e.value.body


def test_prediction_hidden_with_object(links):
    """A regression on cafés (the rule is on cafes.region) and one on customers (who follow the cafés rule), trained
    and scored by the Owner. The North Viewer reads a North café's prediction on every path and a South café's on
    none: the café's page (get_object), the list, semantic_query and SQL on the predictions table, a report, and an
    alert per café. The customers' predictions come from rows the Viewer's rule filters through a link, so they stay
    denied to restricted Viewers altogether (a follow link never opens a derived table). A hidden key column hides
    the table (its copy of the key too, once the Owner gives the table a column rule of its own)."""
    from test_object_flows import TICK

    names = dict(sql("SELECT cafe_id, cafe_name FROM cafes"))
    north = sorted(x[0] for x in sql("SELECT cafe_id FROM cafes WHERE region = 'North'"))
    n1 = north[0]
    s1 = next(k for k, in sql("SELECT cafe_id FROM cafes WHERE region = 'South' ORDER BY 1") if names[k] not in {names[x] for x in north})
    value = lambda r, col: dict(zip(r["columns"], r["rows"][0]))[col]  # noqa: E731
    try:
        abi.call("save_dataset", project=P, id="cafe_vals", name="Café values", query={
            "measures": ["revenue", "orders_rows"], "dimensions": ["cafes.cafe_id", "cafes.seats"]})
        abi.call("save_ml_model", project=P, ml={"id": "cafe_value", "name": "Expected revenue", "task": "regression", "dataset": "cafe_vals",
                                                 "object": "cafe", "target": "revenue", "features": ["orders_rows", "cafes.seats"]})
        ml_run("cafe_value", "train_model")
        ml_run("cafe_value", "score_model")
        preds = dict(sql("SELECT cafes_cafe_id, prediction FROM ml_cafe_value"))
        assert set(north) < set(preds) and s1 in preds and all(v is not None for v in preds.values())
        s = state(links, tables=["ml_cafe_value"])
        assert "ml_cafe_value" in s["filtered"] and "ml_cafe_value" not in s["denied"] and s["keys"]["ml_cafe_value"] == ["cafes_cafe_id"]
        assert "-- ml_cafe_value: only rows whose cafes_cafe_id is a row of cafes you may see" in s["explain"].splitlines()

        # 1. the café's page reads get_object as the person: the property with its café, a hidden café like a missing one
        mineo = mine(links, "ontology")["objects"][0]
        assert {"column": "ml_cafe_value.prediction", "type": "DOUBLE", "name": "Expected revenue", "meaning": "quantity"}.items() <= next(
            p for p in mineo["properties"] if p["column"] == "ml_cafe_value.prediction").items()
        assert value(mine(links, "get_object", type="cafe", key=[n1]), "ml_cafe_value.prediction") == pytest.approx(preds[n1])
        assert value(abi.call("get_object", project=P, type="cafe", key=[s1]), "ml_cafe_value.prediction") == pytest.approx(preds[s1]), "the Owner sees it"
        hidden, missing = refused(links, 404, "get_object", type="cafe", key=[s1]), refused(links, 404, "get_object", type="cafe", key=["CF-NONE"])
        assert hidden["message"].replace(s1, "?") == missing["message"].replace("CF-NONE", "?") and str(preds[s1]) not in json.dumps(hidden)

        # 2. the list: North cafés only; a prediction is no column of it and no filter
        listed = mine(links, "list_objects", type="cafe", limit=1000)
        assert sorted(dict(zip(listed["columns"], r))["cafe_id"] for r in listed["rows"]) == north and not [c for c in listed["columns"] if "ml_" in c]
        refused(links, 400, "list_objects", type="cafe", filters=[{"property": "ml_cafe_value.prediction", "op": ">", "value": 0}])

        # 3. the predictions table itself, through the semantic engine and in SQL
        by_key = mine(links, "semantic_query", measures=["cafe_value_prediction"], dimensions=["ml_cafe_value.cafes_cafe_id"])["rows"]
        assert dict(map(tuple, by_key)) == pytest.approx({k: preds[k] for k in north})
        [[total]] = mine(links, "semantic_query", measures=["cafe_value_prediction"])["rows"]
        assert total == pytest.approx(sum(preds[k] for k in north)) and total < sum(preds.values())
        by_name = mine(links, "semantic_query", measures=["cafe_value_prediction"], dimensions=["cafes.cafe_name"])["rows"]
        assert {r[0] for r in by_name} == {names[k] for k in north}
        assert dict(map(tuple, mine(links, "sql_query", sql="SELECT cafes_cafe_id, prediction FROM ml_cafe_value")["rows"])) == {k: preds[k] for k in north}
        assert mine(links, "sql_query", sql=f"SELECT count(*) FROM ml_cafe_value WHERE cafes_cafe_id = '{s1}'")["rows"] == [[0]]

        # 3b. the other kinds of prediction on cafés, each read as the Viewer: a probability, a group, a flag and a score,
        # a forecast's measure. A North café's are the Owner's; no row of another café shows in any of their tables
        sides = {"measures": ["revenue", "orders_rows"], "dimensions": ["cafes.cafe_id", "cafes.seats", "cafes.region"],
                 "filters": [{"column": "cafes.region", "op": "in", "value": ["North", "South"]}]}
        for ds, q in (("cafe_sides", sides), ("cafe_months", {"measures": ["revenue"], "dimensions": ["cafes.cafe_id", "calendar.year", "calendar.month"]})):
            abi.call("save_dataset", project=P, id=ds, name=ds, query=q)
        more = [{"id": "cafe_north", "name": "Northern", "task": "classification", "dataset": "cafe_sides", "target": "cafes.region", "positive": "North",
                 "features": ["revenue", "cafes.seats"]},
                {"id": "cafe_kind", "name": "Café group", "task": "clustering", "dataset": "cafe_vals", "features": ["revenue", "cafes.seats"], "k": 3},
                {"id": "cafe_odd", "name": "Unusual café", "task": "anomaly", "dataset": "cafe_vals", "features": ["revenue", "orders_rows"], "method": "isolation_forest"},
                {"id": "cafe_ahead", "name": "Revenue outlook", "task": "forecast", "dataset": "cafe_months", "target": "revenue", "time": "calendar.month",
                 "horizon": 3, "method": "lightgbm"}]
        for ml in more:
            abi.call("save_ml_model", project=P, ml=ml | {"object": "cafe"})
            ml_run(ml["id"], "train_model")
            ml_run(ml["id"], "score_model")
        theirs, all_ = (dict(zip(r["columns"], r["rows"][0])) for r in (mine(links, "get_object", type="cafe", key=[n1]), abi.call("get_object", project=P, type="cafe", key=[n1])))
        shown = ["ml_cafe_north.probability", "ml_cafe_kind.cluster", "ml_cafe_odd.anomaly", "ml_cafe_odd.score", "cafe_ahead_prediction"]
        assert all(all_[c] is not None for c in shown) and [theirs[c] for c in shown] == [all_[c] for c in shown], (theirs, all_)
        for t in ("ml_cafe_north", "ml_cafe_kind", "ml_cafe_odd", "ml_cafe_ahead"):
            assert s1 in {x[0] for x in sql(f"SELECT DISTINCT cafes_cafe_id FROM {t}")}, t
            got = {x[0] for x in mine(links, "sql_query", sql=f"SELECT DISTINCT cafes_cafe_id FROM {t}")["rows"]}
            assert got == set(north), (t, got)
        ahead = mine(links, "semantic_query", measures=["cafe_ahead_prediction"], dimensions=["cafes.cafe_id"])["rows"]
        assert {r[0] for r in ahead if r[1] is not None} == set(north)
        chance = mine(links, "semantic_query", measures=["cafe_north_probability"], dimensions=["cafes.cafe_id"])["rows"]
        assert {r[0] for r in chance if r[1] is not None} == set(north)

        # 4. a report over the prediction
        sure("save_report", project=P, report={"id": "values", "name": "Values", "pages": [{"id": "main", "name": "Main", "visuals": [
            {"id": "by-cafe", "type": "table", "title": "Expected revenue by café", "layout": {"x": 0, "y": 0, "w": 12, "h": 6},
             "query": {"measures": ["cafe_value_prediction"], "dimensions": ["cafes.cafe_id", "cafes.cafe_name"]}}]}]})
        theirs, all_ = mine(links, "visual_data", report="values", page="main")["visuals"][0], abi.call("visual_data", project=P, report="values", page="main")["visuals"][0]
        assert sorted(r[0] for r in theirs["rows"]) == north and {r[0]: r[2] for r in theirs["rows"]} == pytest.approx({k: preds[k] for k in north})
        assert s1 in {r[0] for r in all_["rows"]} and names[s1] not in json.dumps(theirs)

        # 5. an alert per café on the prediction: both cafés meet it; the Viewer hears of the North one only
        was = len(mine(links, "inbox")["messages"])
        sure("save_flow", project=P, flow={"id": "value_alert", "name": "Value alert", "enabled": False, "trigger": {
            "type": "data_condition", "per": "cafe", "measure": "cafe_value_prediction", "op": ">", "value": min(preds[n1], preds[s1]) / 2,
            "query": {"measures": ["cafe_value_prediction"], "filters": [{"column": "cafes.cafe_id", "op": "in", "value": [n1, s1]}]}},
            "steps": [{"id": "post", "type": "action", "action": "post_message", "input": {
                "channel": "inbox", "text": "Expected revenue of {{ data.trigger.title }}: {{ data.trigger.object.ml_cafe_value.prediction }}",
                "data": {"measures": ["cafe_value_prediction"], "dimensions": ["cafes.cafe_name"]}}}]})
        r = engine(TICK, timeout=600, stdin=json.dumps({"project": P, "flows": ["value_alert"], "state": "value_alert", "ahead": 0}))
        assert r.returncode == 0, r.stderr[-2000:]
        end = time.monotonic() + 300
        while True:
            runs = abi.call("flow_runs", project=P, flow="value_alert")["runs"]
            if len(runs) == 2 and all(x["status"] not in ("queued", "running") for x in runs):
                break
            assert time.monotonic() < end, runs
            time.sleep(1)
        assert {x["status"] for x in runs} == {"ok"} and sorted(x["object"]["key"][0] for x in runs) == sorted([n1, s1])
        said = lambda ms: {m["text"].split(" of ", 1)[1].rsplit(": ", 1)[0]: float(m["text"].rsplit(": ", 1)[1])  # noqa: E731
                           for m in ms if m["text"].startswith("Expected revenue of ")}
        assert said(abi.call("inbox", project=P)["messages"]) == pytest.approx({names[k]: preds[k] for k in (n1, s1)})
        got = mine(links, "inbox")["messages"]
        got = got[:len(got) - was]  # newest first: the ones since
        assert len(got) == 1 and said(got) == pytest.approx({names[n1]: preds[n1]}), got
        assert got[0]["data"]["rows"] == [{"cafes.cafe_name": names[n1], "cafe_value_prediction": pytest.approx(preds[n1])}], got
        assert names[s1] not in json.dumps(got) and str(preds[s1])[:10] not in json.dumps(got)
        assert mine(links, "flow_runs", flow="value_alert")["runs"] == [], "nor does the run history say which cafés met it"

        # 6. predictions on customers, who follow the cafés rule: made from rows the rule filters through a link, so no
        # restricted Viewer sees any of them, with the customer visible or not (M3: a follow link only ever adds denials)
        abi.call("save_ml_model", project=P, ml={"id": "cust_value", "name": "Customer value", "task": "regression", "dataset": "custs",
                                                 "object": "customer", "target": "revenue", "features": ["customers.segment"]})
        ml_run("cust_value", "train_model")
        ml_run("cust_value", "score_model")
        cust = sql(VISITED)[0][0]
        assert value(abi.call("get_object", project=P, type="customer", key=[cust]), "ml_cust_value.prediction") is not None
        seen = mine(links, "get_object", type="customer", key=[cust])
        assert seen["key"] == [cust] and value(seen, "ml_cust_value.prediction") == MARK, "the customer shows; its prediction is hidden, and said so"
        # an alert per customer on that prediction: the Owner hears of it; the Viewer, who sees the customer but may not
        # read its prediction, is left out (named in the step's trace) and never learns that it is above the value.
        # An alert on a figure they do read (the customer's revenue) reaches them, the prediction in its text hidden
        was = len(mine(links, "inbox")["messages"])
        only = [{"column": "customers.customer_id", "op": "=", "value": cust}]
        for fid, measure in (("cust_alert", "cust_value_prediction"), ("cust_note", "revenue")):
            sure("save_flow", project=P, flow={"id": fid, "name": fid, "enabled": False, "trigger": {
                "type": "data_condition", "per": "customer", "measure": measure, "op": ">", "value": 0, "query": {"measures": [measure], "filters": only}},
                "steps": [{"id": "post", "type": "action", "action": "post_message", "input": {
                    "channel": "inbox", "text": fid + " {{ data.trigger.title }}: {{ data.trigger.object.ml_cust_value.prediction }}"}}]})
            r = engine(TICK, timeout=600, stdin=json.dumps({"project": P, "flows": [fid], "state": fid, "ahead": 0}))
            assert r.returncode == 0, r.stderr[-2000:]
        end, done = time.monotonic() + 300, {}
        while len(done) < 2:
            assert time.monotonic() < end, done
            time.sleep(1)
            done = {f: x[0] for f in ("cust_alert", "cust_note") if len(x := abi.call("flow_runs", project=P, flow=f)["runs"]) == 1 and x[0]["status"] == "ok"}
        post = lambda f: next(x for x in done[f]["steps"] if x["step"] == "post")["output"]  # noqa: E731
        assert post("cust_alert") == {"sent": True, "channel": "inbox", "skipped": [links["names"]["viewer"]]} and "skipped" not in post("cust_note")
        assert sorted(m["text"].split(" ")[0] for m in abi.call("inbox", project=P)["messages"] if m["text"].startswith("cust_")) == ["cust_alert", "cust_note"]
        got = mine(links, "inbox")["messages"]
        assert [m["text"] for m in got[:len(got) - was]] == [f"cust_note {cust}: {MARK}"], got[:len(got) - was]
        assert "ml_cust_value" in state(links, tables=["ml_cust_value"])["denied"]
        assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_cust_value")["rows"] == [[0]]
        assert mine(links, "semantic_query", measures=["cust_value_prediction"])["rows"] in ([], [[None]])
        with saved({**ONTO, "links": [{k: v for k, v in ln.items() if k != "rls"} for ln in ONTO["links"]]}):  # the fixture's types: no property
            assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_cust_value")["rows"] == [[0]], "denied without the link too"
            assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_cafe_value")["rows"] == [[len(north)]]

        # 7. the café's key hidden from Viewers: the predictions table is denied (D-110); once the Owner gives it a
        # column rule of its own its rows show again, but never its copy of the hidden key
        sure("set_column_rule", project=P, id="pi_key", table="cafes", columns=["cafe_id"])
        try:
            assert "ml_cafe_value" in state(links, tables=["ml_cafe_value"])["denied"]
            assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_cafe_value")["rows"] == [[0]]
            refused(links, 403, "get_object", type="cafe", key=[n1])
            sure("set_column_rule", project=P, id="pi_own", table="ml_cafe_value", columns=["cafes_seats"])
            try:
                assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_cafe_value")["rows"] == [[len(north)]]
                assert "cafes_cafe_id" not in mine(links, "sql_query", sql="SELECT * FROM ml_cafe_value LIMIT 1")["columns"]
                with pytest.raises(abi.ActionError) as e:
                    mine(links, "sql_query", sql="SELECT cafes_cafe_id FROM ml_cafe_value")
                assert e.value.status in (400, 403) and n1 not in json.dumps(e.value.body), e.value.body
                assert abi.call("sql_query", project=P, sql="SELECT cafes_cafe_id FROM ml_cafe_value LIMIT 1")["columns"] == ["cafes_cafe_id"], "the Owner reads it"
            finally:
                sure("delete_rls_rule", project=P, id="pi_own")
        finally:
            sure("delete_rls_rule", project=P, id="pi_key")

        # 8. the model deleted: its table, key and measure stay in the model, but nothing says any more what it was made
        # from, so it is denied to restricted Viewers: no row through SQL, the measure, or its copy of the key
        assert mine(links, "sql_query", sql="SELECT count(*) FROM ml_cafe_value")["rows"] == [[len(north)]], "before: their cafés' rows"
        sure("delete_ml_model", project=P, model="cafe_value")
        assert "ml_cafe_value" in state(links, tables=["ml_cafe_value"])["denied"]
        assert mine(links, "sql_query", sql="SELECT cafes_cafe_id, prediction FROM ml_cafe_value")["rows"] == []
        assert all(r[-1] is None for q in ({}, {"dimensions": ["ml_cafe_value.cafes_cafe_id"]}, {"dimensions": ["cafes.cafe_id"]})
                   for r in mine(links, "semantic_query", measures=["cafe_value_prediction"], **q)["rows"])
        assert not mine(links, "semantic_query", measures=["cafe_value_prediction"], dimensions=["ml_cafe_value.cafes_cafe_id"])["rows"]
        assert len(abi.call("sql_query", project=P, sql="SELECT cafes_cafe_id FROM ml_cafe_value")["rows"]) == len(preds), "the Owner still reads it"
        assert any("ml_cafe_value" in g for g in abi.call("readiness", project=P)["missing"]), "and readiness says what to do"
    finally:  # the tests after this one find the fixture's project: no model that a reload of customers would score
        for act, args in (("delete_flow", {"flow": "value_alert"}), ("delete_report", {"report": "values"}),
                          ("delete_flow", {"flow": "cust_alert"}), ("delete_flow", {"flow": "cust_note"}),
                          *(("delete_ml_model", {"model": x}) for x in ("cafe_value", "cust_value", "cafe_north", "cafe_kind", "cafe_odd", "cafe_ahead"))):
            with contextlib.suppress(abi.ActionError):
                sure(act, project=P, **args)
        sure("save_ontology", project=P, ontology=ONTO)


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


# the same person's filters, with a follow link's key list written by DuckDB and, as before M10, key by key in Python
BOTH = r"""
import json, re, sys
from abi import semantic, storage
from abi.semantic import rls
a = json.loads(sys.stdin.read())
P, uid = a["project"], a["uid"]
read = lambda sql, reads: semantic.execute(P, sql, {"test": "rls links"}, reads=reads)
cols = semantic._columns_of(storage.lake(P).cursor(), "model")
out, reads = {}, {}
def counted(sql, r):
    n[0] += "string_agg(" in sql
    return read(sql, r)
for way in ((True, True), (False, False), (True, False)):  # (the list written by DuckDB, rules settled first); as before M10; in between
    rls.IN_SQL, rls.SETTLE = way
    n = [0]
    out[way] = rls.conditions(P, uid, counted, cols)
    reads[str(way)] = n[0]
new, old, mid = out[(True, True)], out[(False, False)], out[(True, False)]
lits = lambda s: sorted(re.findall(r"CAST\('(?:[^']|'')*' AS [^()]*(?:\([^()]*\))?\)", s))
shape = lambda s: re.sub(r"CAST\('(?:[^']|'')*' AS [^()]*(?:\([^()]*\))?\)(?:, )?", "", s)
print(json.dumps({"tables": sorted(new), "same_tables": sorted(new) == sorted(old),
                  "same_keys": [t for t in new if lits(new[t]) != lits(old[t])],
                  "same_shape": [t for t in new if shape(new[t]) != shape(old[t])],
                  "n": {t: len(lits(new[t])) for t in new}, "settled_is_the_same_text": new == mid, "key_reads": reads}))
"""


def test_follow_keys_written_by_the_lake_are_the_keys_read_one_by_one(links):
    """M10 (item 32): a follow link's key list is written by DuckDB in one read; it holds exactly the keys the earlier
    key-by-key way gives, for every table of the person's filters, and the rest of each filter is the same text. A
    customer's key with a quote in it is one of them, written as the same literal."""
    odd = "C'9\\9"
    [cust] = [abi.call("sql_query", project=P, sql="SELECT * FROM customers LIMIT 1")]
    one = dict(zip(cust["columns"], cust["rows"][0])) | {"customer_id": odd}
    order = dict(zip(*(lambda r: (r["columns"], r["rows"][0]))(abi.call("sql_query", project=P, sql=(
        "SELECT o.* FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = 'North' LIMIT 1"))))) | {"customer_id": odd, "order_id": "ORD-ODD-1"}
    abi.call("edit_cells", project=P, table="customers", add=[one])
    abi.call("edit_cells", project=P, table="orders", add=[order])
    try:
        r = engine(BOTH, timeout=300, stdin=json.dumps({"project": P, "uid": links["uid"]}))
        assert r.returncode == 0, r.stderr[-3000:]
        got = json.loads(r.stdout.splitlines()[-1])
        assert got["same_tables"] and got["same_keys"] == [] and got["same_shape"] == [], got
        assert got["settled_is_the_same_text"] is True, "rules settled first, then the follow links: the same filters, to the byte"
        assert got["key_reads"] == {"(True, True)": 1, "(False, False)": 0, "(True, False)": 2}, got["key_reads"]  # the link's keys: read once
        assert got["n"]["customers"] == len(sql(VISITED)) > 1000, got["n"]
        assert mine(links, "get_object", type="customer", key=[odd])["key"] == [odd], "fresh after an edit: the new visitor is seen at once"
    finally:
        abi.call("edit_cells", project=P, table="orders", delete=[["ORD-ODD-1"]])
        abi.call("edit_cells", project=P, table="customers", delete=[[odd]])
    with pytest.raises(abi.ActionError) as e:
        mine(links, "get_object", type="customer", key=[odd])
    assert e.value.status == 404, "and gone again with the order"
    # fresh after a rule change: the same Viewer moved to the South follows the South's visitors
    south = sql(VISITED.replace("'North'", "'South'"))
    sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={links["uid"]: ["South"]})
    try:
        assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[len(south)]]
    finally:
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={links["uid"]: ["North"]})
    assert mine(links, "sql_query", sql="SELECT count(*) FROM customers")["rows"] == [[len(sql(VISITED))]]


LISTS = r"""
import json, re
import duckdb
from abi.semantic import rls
con = duckdb.connect()
con.execute("CREATE TYPE mood AS ENUM ('ok', 'it''s) OR true --')")
con.execute("CREATE TABLE t (i INTEGER, d DECIMAL(10,2), day DATE, s VARCHAR, nothing INTEGER, m mood)")
con.execute("INSERT INTO t VALUES (1, 1.50, DATE '2026-01-02', 'it''s', NULL, 'ok'), (2, -0.05, DATE '2026-01-03', 'a\\b', NULL, 'ok'),"
            " (2, 1.50, NULL, NULL, NULL, NULL), (NULL, NULL, NULL, '', NULL, NULL), (10, 100.00, DATE '1999-12-31', 'ü', NULL, NULL)")
out = {}
for c in ("i", "d", "day", "s", "nothing"):
    for src in ("t", "t WHERE false"):
        plain, got = con.execute(rls._list_sql(c, src)).fetchone()
        assert plain is not False, (c, plain)
        new = f'list_contains([{got}], "{c}")' if got else "FALSE"
        keys = sorted(con.execute(f'SELECT DISTINCT CAST("{c}" AS VARCHAR), typeof("{c}") FROM {src}').fetchall(), key=str)
        old = rls._in((c,), keys)
        lits = lambda s: sorted(re.findall(r"CAST\('(?:[^']|'')*' AS [^()]*(?:\([^()]*\))?\)", s))
        rows = con.execute(f"SELECT count(*) FILTER (WHERE {new}), count(*) FILTER (WHERE {old}), count(*) FILTER (WHERE ({new}) IS DISTINCT FROM ({old})) FROM t").fetchone()
        out[f"{c} {src}"] = [lits(new) == lits(old), len(lits(new)), list(rows), new == "FALSE", old == "FALSE"]
out["enum"] = con.execute(rls._list_sql("m", "t")).fetchone()[0]  # a type whose name holds its values: not written this way
print(json.dumps(out))
"""


def test_a_key_list_written_by_the_lake_equals_the_one_built_key_by_key():
    """M10: `rls._list_sql` writes the list `rls._in` builds, for an INTEGER, a DECIMAL, a DATE and a text column (a
    quote, a backslash, an empty string, a non-ASCII letter): the same literals, and the two filters keep the same
    rows. A column of NULLs only, and no rows at all, deny in both. An ENUM column (its type's name holds its values, quotes
    and all) is not written this way: `conditions.listed` then takes the key-by-key way."""
    r = engine(LISTS, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.splitlines()[-1])
    assert got == {"i t": [True, 3, [4, 4, 0], False, False], "i t WHERE false": [True, 0, [0, 0, 0], True, True],
                   "d t": [True, 3, [4, 4, 0], False, False], "d t WHERE false": [True, 0, [0, 0, 0], True, True],
                   "day t": [True, 3, [3, 3, 0], False, False], "day t WHERE false": [True, 0, [0, 0, 0], True, True],
                   "s t": [True, 4, [4, 4, 0], False, False], "s t WHERE false": [True, 0, [0, 0, 0], True, True],
                   "nothing t": [True, 0, [0, 0, 0], True, True], "nothing t WHERE false": [True, 0, [0, 0, 0], True, True], "enum": False}, got


# ---------- 1.2.7 M0 item 2: a follow link's keys are an ENUM type the view casts to, not a list in the view ----------

# One restricted person's views as the engine builds them, in a process of its own (its own lake connection):
#   look     the views' text, the key types, the plan of a read of customers
#   equal    every view's filter against the inlined lists (`conditions` without a key type, both ways of writing them)
#   share    several people one after another: their schemas and the key types after each
#   drop     a new snapshot with other keys: the older type stays KEEP_S, then goes, and its views fail closed
KT = r"""
import json, sys
import duckdb
import abi.app  # noqa: F401  registers every action, as the engine does
from abi import actions, db, security, semantic, storage
from abi.semantic import rls
a = json.loads(sys.stdin.read())
P, con, q = a["project"], storage.lake(a["project"]), storage._q
def look(uid):
    snap = semantic._snapshot(con)
    schema = semantic._rls(P, uid, con, snap, None)[0]
    cur = con.cursor()
    semantic._effective(cur, con, snap, schema)
    views = dict(cur.execute("SELECT view_name, sql FROM duckdb_views() WHERE database_name = 'memory' AND schema_name = ?", [schema]).fetchall())
    types = sorted(r[0] for r in cur.execute("SELECT type_name FROM duckdb_types() WHERE database_name = 'memory' AND schema_name = 'rls_keys'").fetchall())
    return snap, schema, views, types, cur
out = {}
if a["mode"] == "look":
    snap, schema, views, types, cur = look(a["uid"])
    q1 = "EXPLAIN SELECT * FROM customers LIMIT 10 OFFSET 100"
    plan, seen = cur.execute(q1).fetchall()[0][1], cur.execute("SELECT count(*) FROM customers").fetchone()[0]
    semantic._effective(cur, con, snap)  # the same read of the source rows (an edited table's own view may join its edits)
    out = {"views": views, "types": types, "joins": plan.upper().count("JOIN") - cur.execute(q1).fetchall()[0][1].upper().count("JOIN"),
           "explain": rls.explain(P, a["uid"]), "seen": seen}
elif a["mode"] == "equal":
    snap, schema, views, types, cur = look(a["uid"])
    semantic._effective(cur, con, snap)  # the source rows: each filter is tried on its own table
    read = lambda sql, reads: semantic.execute(P, sql, {"test": "key types"}, reads=reads)
    cols = semantic._columns_of(con.cursor(), "model")
    plain = {t: c for t, c in cols.items() if t not in semantic._edited(con, snap)}
    for way in ((True, True), (False, False)):
        rls.IN_SQL, rls.SETTLE = way
        old = rls.conditions(P, a["uid"], read, plain)
        for t in views:
            new, was = rls.where(con, schema, t), old.get(t)
            if new is None or was is None:
                out[f"{t} {way}"] = [new, was]
                continue
            out[f"{t} {way}"] = list(cur.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {was}), count(*) FILTER (WHERE {new}),"
                f" count(*) FILTER (WHERE coalesce(({new}), false) <> coalesce(({was}), false)) FROM {q(t)}").fetchone()) + [
                "rls_keys" in new, "list_contains" in was or was == "FALSE"]
    rls.IN_SQL, rls.SETTLE = True, True
elif a["mode"] == "share":
    for w, uid in a["uids"].items():
        snap, schema, views, types, cur = look(uid)
        out[w] = {"schema": schema, "types": types, "seen": cur.execute("SELECT count(*) FROM customers").fetchone()[0],
                  "key_reads": len(rls._keys[con]["names"])}
elif a["mode"] == "drop":
    with db.connect() as c:
        owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
    snap1, schema1, _, types1, cur = look(a["uid"])
    n1 = cur.execute("SELECT count(*) FROM customers").fetchone()[0]
    batch = actions.run("edit_cells", {"project": P, "table": "orders", "delete": [[a["order"]]]}, owner, "rest")["batch"]
    snap2, schema2, _, types2, cur = look(a["uid"])  # the new snapshot's build: the older type is kept for a read that began before it
    n2 = cur.execute("SELECT count(*) FROM customers").fetchone()[0]
    old = con.cursor().execute(f"SELECT count(*) FROM memory.{schema1}.customers").fetchone()[0]
    rls.KEEP_S = 0
    rls._ctx[con].clear()
    snap3, schema3, _, types3, cur = look(a["uid"])
    try:
        con.cursor().execute(f"SELECT count(*) FROM memory.{schema1}.customers").fetchall()
        gone = "still reads"
    except duckdb.Error as e:
        gone = type(e).__name__
    out = {"batch": batch, "snaps": [snap1, snap2, snap3], "schemas": [schema1, schema2, schema3], "types": [types1, types2, types3],
           "seen": [n1, n2, old, cur.execute("SELECT count(*) FROM customers").fetchone()[0]], "old_view": gone}
print(json.dumps(out, default=str))
"""


def kt(mode: str, **more) -> dict:
    r = engine(KT, timeout=300, stdin=json.dumps({"project": P, "mode": mode, **more}))
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.splitlines()[-1])


ODD = ["C'9\\9", "a,b", "Zoë ☕ 東京", "", "x" * 5000, '"q"', "it's) OR true --"]  # a quote, a backslash, a comma, other scripts, no text, long text


@contextlib.contextmanager
def odd_customers():
    """Customers whose keys are ODD, each with one order at a North café (so the North Viewer follows to them)."""
    [cust] = [abi.call("sql_query", project=P, sql="SELECT * FROM customers LIMIT 1")]
    one = dict(zip(cust["columns"], cust["rows"][0]))
    order = dict(zip(*(lambda r: (r["columns"], r["rows"][0]))(abi.call("sql_query", project=P, sql=(
        "SELECT o.* FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = 'North' LIMIT 1")))))
    abi.call("edit_cells", project=P, table="customers", add=[one | {"customer_id": k} for k in ODD])
    abi.call("edit_cells", project=P, table="orders", add=[order | {"customer_id": k, "order_id": f"ORD-ODD-{i}"} for i, k in enumerate(ODD)])
    try:
        yield
    finally:
        abi.call("edit_cells", project=P, table="orders", delete=[[f"ORD-ODD-{i}"] for i in range(len(ODD))])
        abi.call("edit_cells", project=P, table="customers", delete=[[k] for k in ODD])


def test_follow_view_holds_no_key_list(links):
    """The view of customers tests the key against an ENUM type in memory.rls_keys instead of holding the keys: its
    text is far under 4 KB (it was about 25 KB here and 684 KB at 25,000 customers), it has no join (so the rows keep
    their order) and no literal of a key; the Viewer sees the same customers; explain never names the type."""
    got = kt("look", uid=links["uid"])
    text = got["views"]["customers"]
    assert len(text) < 4096 and all(len(v) < 4096 for t, v in got["views"].items() if t != "cafes"), {t: len(v) for t, v in got["views"].items()}
    assert "TRY_CAST(" in text and re.search(r'memory\.rls_keys\."?k_[0-9a-f]{32}', text) and "list_contains" not in text and "CAST('" not in text, text
    assert " JOIN " not in text.upper() and " IN (" not in text.upper(), text
    assert got["joins"] == 0, "the filter adds no join to the plan of a read of customers"
    assert len(got["types"]) == 1 and re.fullmatch(r"k_[0-9a-f]{32}", got["types"][0]), got["types"]
    assert got["seen"] == len(sql(VISITED)) > 1000
    assert "rls_keys" not in got["explain"] and got["types"][0] not in got["explain"]


def test_follow_key_type_equals_inlined_list(links):
    """Every filter of the Viewer's views keeps exactly the rows the inlined key lists keep (`conditions` without a key
    type: the list written by DuckDB, and key by key as before 1.2.6 M10), row for row, also for keys with a quote, a
    backslash, a comma, other scripts, no text at all and 5,000 characters; a customer without an order at a North café
    stays hidden; nothing raises for a key the type lacks."""
    with odd_customers():
        got = kt("equal", uid=links["uid"])
        assert {k.split(" ")[0] for k in got} >= {"cafes", "customers", "orders"}, sorted(got)
        for k, v in got.items():
            total, was, new, differ, typed, listed = v
            assert differ == 0 and was == new, (k, v)
            if k.split(" ")[0] in ("cafes", "customers", "orders"):  # (other tables here are denied: made from filtered rows)
                assert 0 < new < total and listed and typed == k.startswith("customers"), (k, v)
            else:
                assert new == 0, (k, v)
        seen = {r[0] for r in mine(links, "sql_query", sql="SELECT customer_id FROM customers")["rows"]}
        assert set(ODD) <= seen and len(seen) == len(sql(VISITED)), "the odd keys are followed to, as written"
        for k in ODD:
            assert mine(links, "get_object", type="customer", key=[k])["key"] == [k]
    hidden = sql(f"SELECT min(customer_id) FROM customers WHERE customer_id NOT IN ({VISITED})")[0][0]
    for k in (hidden, "no such customer", ODD[0]):  # a hidden one, one nobody has, one that is gone again: all alike
        with pytest.raises(abi.ActionError) as e:
            mine(links, "get_object", type="customer", key=[k])
        assert e.value.status == 404, (k, e.value.body)
    assert mine(links, "sql_query", sql=f"SELECT count(*) FROM customers WHERE customer_id = '{hidden}'")["rows"] == [[0]]


NAMES = ["SELECT * FROM memory.rls_keys.{k}", 'SELECT * FROM "memory"."rls_keys"."{k}"', "SELECT * FROM rls_keys.{k}", "SELECT * FROM {k}",
         "SELECT type_name FROM duckdb_types()", "SELECT * FROM duckdb_types() WHERE schema_name = 'rls_keys'",
         "SELECT * FROM information_schema.schemata", "SELECT * FROM pg_catalog.pg_type", "SELECT * FROM pg_type",
         "SELECT enum_range(NULL::memory.rls_keys.{k})", "SELECT enum_first(NULL::memory.rls_keys.{k})",
         "SELECT enum_last(NULL::memory.rls_keys.{k})", "SELECT enum_code(customer_id::memory.rls_keys.{k}) FROM customers",
         "SELECT enum_range_boundary(NULL::memory.rls_keys.{k}, NULL)", "SELECT sql FROM duckdb_views()", "DESCRIBE memory.rls_keys.{k}",
         "SELECT c.customer_id FROM customers c, memory.rls_keys.{k} k", "SELECT (SELECT count(*) FROM duckdb_types()) FROM customers"]
CASTS = ["SELECT CAST('C1' AS memory.rls_keys.{k})", "SELECT TRY_CAST('C1' AS memory.rls_keys.{k}) IS NOT NULL",
         "SELECT 'C1'::memory.rls_keys.{k}", "SELECT TRY_CAST(customer_id AS memory.rls_keys.{k}) FROM customers",
         "SELECT CAST('C1' AS rls_keys.{k})", "SELECT CAST('C1' AS {k})", "SELECT 'C1'::{k}", 'SELECT CAST(\'C1\' AS "memory"."rls_keys"."{k}")',
         "SELECT count(*) FROM customers WHERE customer_id::{k} IS NOT NULL", "SELECT [customer_id]::{k}[] FROM customers",
         "SELECT CAST(customer_id AS STRUCT(a {k})) FROM customers", "WITH x AS (SELECT 'C1'::{k} AS v) SELECT * FROM x"]


def refusals(links, forms: list, name: str) -> list:
    """Each form as the Viewer's sql_query: [status, what was said, DuckDB's "did you mean" aside]. SQL that reads a
    model table is refused by `rls.check` (403). SQL that reads none never runs on the lake: sql_query answers it in an
    empty catalog sandbox (for BI tools' introspection) that has no memory catalog and so no key type; there it is
    refused, fails, or lists the sandbox's own catalog, and nothing it says names a key type."""
    out = []
    for f in forms:
        try:
            got = [200, json.dumps(mine(links, "sql_query", sql=f.format(k=name)))]
        except abi.ActionError as e:
            got = [e.value.status if hasattr(e, "value") else e.status, e.body["message"].split("\nDid you mean")[0].replace(name, "<name>")]
        assert got[0] in (200, 400, 403) and (got[0] == 403 or "customers" not in f), (f, got)
        assert got[0] != 200 or (name not in got[1] and "rls_keys" not in got[1] and not re.search(r"k_[0-9a-f]{32}", got[1])), (f, got)
        out.append(got if got[0] != 200 else [200, "answered from the empty sandbox"])
    return out


def test_follow_key_type_not_nameable(links):
    """No SQL a restricted person writes reaches a key type: not its name as a table, not the catalog's lists of types,
    schemas or views, not an ENUM function. The type's name is 128 random-looking bits made with a salt of the engine's
    process, and it is in nothing the person is shown (their rules, explain, a column's type)."""
    real = kt("look", uid=links["uid"])["types"][0]
    said = refusals(links, NAMES, real)
    assert all(m.startswith("Row-level security: this read names a table or function outside the model tables") for st, m in said if st == 403
               and "one SELECT" not in m) and sum(st == 403 for st, _ in said) >= 10, said
    assert said == refusals(links, NAMES, "k_" + "0" * 32), "a name that exists and one that doesn't are refused alike"
    assert mine(links, "sql_query", sql="SELECT DISTINCT typeof(customer_id) FROM customers")["rows"] == [["VARCHAR"]]
    assert real not in json.dumps(mine(links, "rls_rules")) and "rls_keys" not in json.dumps(mine(links, "rls_rules"))
    again = kt("look", uid=links["uid"])["types"][0]
    assert again != real, "another process, another salt: the same keys get another name"


def test_restricted_sql_cast_to_a_key_type_refused(links):
    """A cast to a key type would say which keys someone else sees ("is C1 one of theirs?"), also through a guessed
    name. A restricted person's SQL may cast to DuckDB's own types only: `::`, CAST and TRY_CAST to any other type are
    refused before anything runs, with the same answer whether the type exists or not; the casts ABI's own SQL makes
    still pass. Every door a person's or an agent's SQL comes through is `semantic.execute`, which asks `rls.check`."""
    real = kt("look", uid=links["uid"])["types"][0]
    said = refusals(links, CASTS, real)
    assert said == refusals(links, CASTS, "k_" + "0" * 32), said
    assert all("outside the model tables" in m for st, m in said if st == 403) and sum(st == 403 for st, _ in said) >= 4, said
    ok = mine(links, "sql_query", sql="SELECT CAST(count(*) AS VARCHAR), TRY_CAST('1.5' AS DOUBLE), '2026-01-02'::DATE,"
                                      " CAST(max(customer_id) AS VARCHAR) IS NOT NULL FROM customers")["rows"]
    assert ok[0][0] == str(len(sql(VISITED))) and ok[0][3] is True, ok
    r = engine(CHECKS, timeout=120, stdin=json.dumps({"project": P, "uid": links["uid"], "name": real, "forms": NAMES + CASTS}))
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.splitlines()[-1])
    assert got["refused"] == len(NAMES + CASTS) and got["passed"] == [], got


CHECKS = r"""
import json, sys
from abi.actions import ActionFailed
from abi.semantic import rls
a = json.loads(sys.stdin.read())
known, refused, passed = {"customers", "cafes", "orders"}, 0, []
for f in a["forms"]:
    for name in (a["name"], a["name"].upper(), "k_" + "0" * 32):
        try:
            rls.check(a["project"], a["uid"], f.format(k=name), known)
            passed.append(f)
        except ActionFailed as e:
            assert e.status == 403, e.status
    refused += f not in passed
print(json.dumps({"refused": refused, "passed": passed}))
"""

TYPED = r"""
import json
import duckdb
from abi.semantic import rls
con = duckdb.connect()
con.execute("CREATE SCHEMA rls_keys")
con.execute("CREATE TYPE mood AS ENUM ('ok', 'it''s) OR true --', 'a,b')")
con.execute("CREATE TABLE t (i INTEGER, d DECIMAL(10,2), day DATE, s VARCHAR, nothing INTEGER, m mood, pick BOOLEAN)")
con.execute("INSERT INTO t VALUES (1, 1.50, DATE '2026-01-02', 'it''s', NULL, 'ok', true), (2, -0.05, DATE '2026-01-03', 'a\\b', NULL, 'it''s) OR true --', true),"
            " (2, 1.50, NULL, NULL, NULL, NULL, true), (NULL, NULL, NULL, '', NULL, NULL, true), (10, 100.00, DATE '1999-12-31', 'ü', NULL, 'a,b', false),"
            " (11, 7.00, DATE '2000-01-01', repeat('x', 5000), NULL, 'ok', true), (12, 1.50, DATE '2026-01-02', 'a,b', NULL, NULL, true)")
out, n = {}, 0
def typed(cols, src):
    global n
    n += 1
    con.execute(f"CREATE TYPE memory.rls_keys.k_{n} AS ENUM (SELECT DISTINCT k FROM (SELECT {rls._key_text(cols)} AS k FROM {src}) WHERE k IS NOT NULL)")
    return rls._key_test(cols, f"memory.rls_keys.k_{n}")
for cols in (("i",), ("d",), ("day",), ("s",), ("nothing",), ("m",), ("i", "s"), ("m", "d"), ("s", "s")):
    for src in ("t WHERE pick", "t WHERE false"):
        new = typed(cols, src)
        sel = ", ".join(f'CAST("{c}" AS VARCHAR), typeof("{c}")' for c in cols)
        if "m" in cols:  # the earlier way wrote an ENUM column's type name into the filter; the reference here is plain SQL
            old = f"({', '.join(cols)}) IN (SELECT {', '.join(cols)} FROM {src})"
        else:
            old = rls._in(cols, sorted(con.execute(f"SELECT DISTINCT {sel} FROM {src}").fetchall(), key=str))
        rows = con.execute(f"SELECT count(*) FILTER (WHERE {new}), count(*) FILTER (WHERE {old}),"
                           f" count(*) FILTER (WHERE coalesce(({new}), false) <> coalesce(({old}), false)) FROM t").fetchone()
        out[f"{'+'.join(cols)} {src[8:]}"] = [list(rows), "ENUM" not in new and "mood" not in new and "'" not in new.replace("':'", "")]
print(json.dumps(out))
"""


def test_follow_enum_key():
    """A key column of an ENUM type (its type's name holds its values, quotes and all; 1.2.6 fell back to writing that
    name into the filter unquoted) is tested like any other: by its label's text, cast to the key type, with no type
    name and no value in the filter. The same for an INTEGER, a DECIMAL, a DATE, a text column (a quote, a backslash, a
    comma, no text, 5,000 characters) and two columns together (each text with its length, so ('a,b', 'c') is not
    ('a', 'b,c')): the rows the inlined list keeps. A NULL key, a column of NULLs and an empty set keep no row."""
    r = engine(TYPED, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    got = json.loads(r.stdout.splitlines()[-1])
    want = {"i pick": 5, "d pick": 5, "day pick": 4, "s pick": 5, "nothing pick": 0, "m pick": 3, "i+s pick": 4, "m+d pick": 3, "s+s pick": 5}
    for k, (rows, clean) in got.items():
        new, old, differ = rows
        assert differ == 0 and new == old and clean, (k, rows, clean)
        assert new == (want[k] if k.endswith(" pick") else 0), (k, rows)


ORDERED = r"""
import hashlib, json
import duckdb
from abi.semantic import rls
con = duckdb.connect(config={"threads": 4})
con.execute("CREATE SCHEMA rls_keys")
con.execute("CREATE TABLE t AS SELECT i AS id, 'C' || (i * 7919 % 5000) AS k, i % 97 AS m FROM range(600000) r(i)")  # 5 row groups
src = "t WHERE id < 1500"
con.execute(f"CREATE TYPE memory.rls_keys.k_t AS ENUM (SELECT DISTINCT k FROM (SELECT {rls._key_text(('k',))} AS k FROM {src}) WHERE k IS NOT NULL)")
keys = sorted(con.execute(f"SELECT DISTINCT CAST(k AS VARCHAR), typeof(k) FROM {src}").fetchall(), key=str)
ways = {"list": rls._in(("k",), keys), "type": rls._key_test(("k",), "memory.rls_keys.k_t"), "join": "k IN (SELECT DISTINCT k FROM t WHERE id < 1500)"}
out = {}
for w, cond in ways.items():
    con.execute(f"CREATE OR REPLACE VIEW v AS SELECT * FROM t WHERE {cond}")
    pages = [hashlib.sha256(json.dumps(con.execute(f"SELECT COLUMNS(*)::VARCHAR FROM v LIMIT 200 OFFSET {off}").fetchall()).encode()).hexdigest()
             for _ in range(3) for off in (0, 30000, 90000, 170000)]
    ids = [r[0] for r in con.execute("SELECT id FROM v").fetchall()]
    out[w] = {"pages": pages, "rows": len(ids), "in_order": ids == sorted(ids), "ids": hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
              "joins": con.execute("EXPLAIN SELECT * FROM v LIMIT 200 OFFSET 170000").fetchall()[0][1].upper().count("JOIN")}
print(json.dumps(out))
"""


def test_follow_row_order_kept():
    """GATE-4-fix-1 for the key type: over a table of several row groups, read by four threads, the grid's pages
    (LIMIT 200 OFFSET n, no ORDER BY) through a view that casts to the key type are the pages through the inlined list,
    read after read, and the whole view comes in the table's own order. The cast is in the scan: the plan has no join
    (the semi-join the plan first named has one, which is why it was not built: bench/RESULTS.md, 1.2.7 M0)."""
    r = engine(ORDERED, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    got = json.loads(r.stdout.splitlines()[-1])
    lst, typ = got["list"], got["type"]
    assert typ["rows"] == lst["rows"] > 150000 and typ["in_order"] and lst["in_order"] and typ["ids"] == lst["ids"], (typ["rows"], lst["rows"])
    assert typ["pages"] == lst["pages"] and typ["pages"][:4] == typ["pages"][4:8] == typ["pages"][8:] and len(set(typ["pages"][:4])) == 4
    assert typ["joins"] == 0 == lst["joins"] and got["join"]["joins"] >= 1 and got["join"]["rows"] == typ["rows"]


def test_follow_key_type_dropped_with_snapshot(links):
    """A key type lives as long as its snapshot's views: after new data (a customer's only order at a North café goes:
    other keys, so another type and other views) the older type stays for KEEP_S, for a read that began before, and
    the older views still read through it; then it is dropped, and a view that still names it fails instead of showing
    anything. A type is kept per key set, not per build or per table that tests it."""
    [(order,)] = sql("SELECT min(o.order_id) FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = 'North' AND o.customer_id IN ("
                     "SELECT o2.customer_id FROM orders o2 JOIN cafes c2 USING (cafe_id) WHERE c2.region = 'North' AND o2.customer_id IS NOT NULL"
                     " GROUP BY 1 HAVING count(*) = 1)")
    n = len(sql(VISITED))
    got = kt("drop", uid=links["uid"], order=order)
    abi.call("undo_edits", project=P, table="orders", batch=got["batch"])  # the order is back
    (t1,), t2, t3 = got["types"]
    assert got["snaps"][0] < got["snaps"][1] == got["snaps"][2] and got["schemas"][0] != got["schemas"][1] == got["schemas"][2], got
    assert len(t2) == 2 and t1 in t2 and got["seen"][:3] == [n, n - 1, n], "kept beside the new one; the older views read the older keys"
    assert len(t3) == 1 and t1 not in t3 and t3[0] in t2 and got["seen"][3] == n - 1, got["types"]
    assert got["old_view"] in ("CatalogException", "BinderException"), got["old_view"]
    assert len(sql(VISITED)) == n


def test_two_people_same_condition_share_one_key_type(links):
    """Two Viewers of the same region read through the same views and one key type, read once; a Viewer of another
    region gets their own. No one's type holds a key they may not see: each sees exactly their region's visitors."""
    made, uid = {}, {}
    try:
        for w in ("twin", "south"):
            made[w] = f"pi_links_{w}_{time.strftime('%H%M%S')}"
            abi.call("create_user", name=made[w], password=secrets.token_urlsafe(16))
            abi.call("set_role", project=P, user=made[w], role="viewer")
        ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}
        uid = {"first": links["uid"], "twin": ids[made["twin"]], "south": ids[made["south"]]}
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region",
             users={uid["first"]: ["North"], uid["twin"]: ["North"], uid["south"]: ["South"]})
        got = kt("share", uids=uid)
    finally:
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={links["uid"]: ["North"]})
        for n in made.values():
            sure("delete_user", name=n)
    first, twin, south = got["first"], got["twin"], got["south"]
    assert first["schema"] == twin["schema"] and first["types"] == twin["types"] and len(first["types"]) == 1, (first, twin)
    assert first["key_reads"] == twin["key_reads"] >= 1 and south["key_reads"] > twin["key_reads"], "the twin's build read no keys"
    assert south["schema"] != first["schema"] and len(south["types"]) == 2 and first["types"][0] in south["types"]
    assert first["seen"] == twin["seen"] == len(sql(VISITED)) and south["seen"] == len(sql(VISITED.replace("'North'", "'South'"))) != first["seen"]
