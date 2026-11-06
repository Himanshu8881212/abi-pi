"""1.2.6 M2 (needs the engine at :8780, no LLM): objects on Brewline. get_object reads one object by its key (a list, in
key order, cast to the key's types) with its properties, one-to-one properties, its type's measures filtered to it and
its neighbors along links; list_objects filters a type on its own properties and on properties reached through link
chains, each object once. Every read is the person's (row-level security views, rls.check): a hidden object answers
like a missing one, a hidden column is absent. The owner's sql_query is the reference for every set of rows."""
import contextlib
import copy
import json
import secrets
import time

import pytest
from conftest import drop
from test_ontology import ONTO
from test_engine import engine
from test_ontology_nobreak import brew
from test_pages import sure

import abi

P = "pi_objects"


def sql(q: str) -> list:
    return abi.call("sql_query", project=P, sql=q)["rows"]


@pytest.fixture(scope="module")
def objs(tmp_path_factory):
    """P: Brewline with the plan's ontology, cafe_contacts (one manager for the first North café), cafe_targets (keyed
    by café and month: a composite key) and a North-region Viewer (row rule on cafes.region, home_country hidden)."""
    brew(P, tmp_path_factory.mktemp("objects"))
    name, pw = f"pi_obj_v_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    abi.call("create_user", name=name, password=pw)
    try:
        abi.call("set_role", project=P, user=name, role="viewer")
        uid = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}[name]
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={uid: ["North"]})
        sure("set_column_rule", project=P, id="country", table="customers", columns=["home_country"])
        north, south = (sql(f"SELECT min(cafe_id) FROM cafes WHERE region = '{r}'")[0][0] for r in ("North", "South"))
        sure("create_input_table", project=P, table="cafe_contacts", key=["cafe_id"],
             columns=[{"name": "cafe_id", "type": "VARCHAR"}, {"name": "manager", "type": "VARCHAR"}, {"name": "visit_reason", "type": "VARCHAR"}])
        abi.call("add_relationship", project=P, **{"from": "cafe_contacts.cafe_id", "to": "cafes.cafe_id"})
        abi.call("edit_cells", project=P, table="cafe_contacts", add=[{"cafe_id": north, "manager": "Ada Example"}])
        sure("create_input_table", project=P, table="cafe_targets", key=["cafe_id", "month"],
             columns=[{"name": "cafe_id", "type": "VARCHAR"}, {"name": "month", "type": "INTEGER"}, {"name": "target", "type": "INTEGER"}])
        abi.call("add_relationship", project=P, **{"from": "cafe_targets.cafe_id", "to": "cafes.cafe_id"})
        abi.call("edit_cells", project=P, table="cafe_targets", add=[{"cafe_id": north, "month": m, "target": 100 * m} for m in (1, 2, 3)])
        o = copy.deepcopy(ONTO)
        o["objects"].append({"id": "target", "table": "cafe_targets", "name": "Target", "plural": "Targets", "title": "month"})
        o["links"].append({"id": "target_cafe", "from": "target", "to": "cafe", "name": "café", "inverse": "targets", "via": ["cafe_targets-cafe_id"]})
        sure("save_ontology", project=P, ontology=o)
        yield {"viewer": abi.login(name, pw)[1]["token"], "north": north, "south": south}
    finally:
        sure("delete_user", name=name)
        drop([P])


def viewer(objs, action: str, **args) -> dict:
    with abi.as_user(objs["viewer"]):
        return abi.call(action, project=P, **args)


def refused(status: int, action: str, token: str | None = None, **args) -> dict:
    with pytest.raises(abi.ActionError) as e:
        if token:
            with abi.as_user(token):
                abi.call(action, project=P, **args)
        else:
            abi.call(action, project=P, **args)
    assert e.value.status == status, e.value.body
    return e.value.body


def every(token: str | None = None, **args) -> list:
    """Every row of list_objects, page by page (1,000 a page)."""
    rows, offset = [], 0
    while True:
        r = viewer({"viewer": token}, "list_objects", **args, limit=1000, offset=offset) if token else \
            abi.call("list_objects", project=P, **args, limit=1000, offset=offset)
        rows += r["rows"]
        if not r["more"]:
            return rows
        offset += 1000


def row(r: dict) -> dict:
    assert len(r["rows"]) == 1
    return dict(zip(r["columns"], r["rows"][0]))


# ---------- #10 get_object ----------

def test_get_cafe(objs):
    c = objs["north"]
    r = abi.call("get_object", project=P, type="cafe", key=[c], links=["visited", "order_cafe", "contact_cafe"], limit=5)
    [(name, region, city)] = sql(f"SELECT cafe_name, region, city FROM cafes WHERE cafe_id = '{c}'")
    p = row(r)
    assert (r["type"], r["key"], r["title"]) == ("cafe", [c], name) and (p["region"], p["city"]) == (region, city)
    assert p["cafe_contacts.manager"] == "Ada Example", "a one-to-one property, read from its own table"
    visitors = [x[0] for x in sql(f"SELECT DISTINCT customer_id FROM orders WHERE cafe_id = '{c}' AND customer_id IS NOT NULL ORDER BY 1")]
    orders = [x[0] for x in sql(f"SELECT order_id FROM orders WHERE cafe_id = '{c}' ORDER BY 1")]
    ways = {ln["link"]: ln for ln in r["links"]}
    assert ways["visited"]["to"] == "customer" and ways["visited"]["columns"] == ["customer_id"]
    assert [x[0] for x in ways["visited"]["rows"]] == visitors[:5] and ways["visited"]["more"] is (len(visitors) > 5)
    assert ways["order_cafe"]["to"] == "order" and [x[0] for x in ways["order_cafe"]["rows"]] == orders[:5], "the inverse of order_cafe"
    assert ways["contact_cafe"]["rows"] == [[c, "Ada Example"]] and ways["contact_cafe"]["more"] is False
    assert len(abi.call("get_object", project=P, type="cafe", key=[c], links=["visited"])["links"][0]["rows"]) == min(20, len(visitors)), \
        "20 neighbors by default"
    assert "not cafe" in refused(400, "get_object", type="cafe", key=[c], links=["bought"])["message"]
    assert "No object type 'shop'" in refused(404, "get_object", type="shop", key=[c])["message"]


def test_hidden_row_404_same_as_missing(objs):
    south = objs["south"]
    assert abi.call("get_object", project=P, type="cafe", key=[south])["key"] == [south], "the owner sees every café"
    hidden = refused(404, "get_object", objs["viewer"], type="cafe", key=[south])
    missing = refused(404, "get_object", objs["viewer"], type="cafe", key=["CF-NONE"])
    assert hidden["message"].replace(south, "?") == missing["message"].replace("CF-NONE", "?") and set(hidden) == set(missing)
    assert viewer(objs, "get_object", type="cafe", key=[objs["north"]])["key"] == [objs["north"]]
    regions = {dict(zip(r["columns"], x))["region"] for r in [viewer(objs, "list_objects", type="cafe")] for x in r["rows"]}
    assert regions == {"North"}


def test_hidden_column_absent(objs):
    # a customer the North Viewer sees: one who visited a North café (customers follow the cafés rule, 1.2.6 M3)
    cust = sql("SELECT min(customer_id) FROM orders JOIN cafes USING (cafe_id) WHERE region = 'North'")[0][0]
    assert "home_country" in row(abi.call("get_object", project=P, type="customer", key=[cust]))
    mine = viewer(objs, "get_object", type="customer", key=[cust])
    assert "home_country" not in mine["columns"] and "segment" in mine["columns"] and mine["key"] == [cust]
    listed = viewer(objs, "list_objects", type="customer", limit=3)
    assert "home_country" not in listed["columns"] and len(listed["rows"]) == 3
    hidden = refused(400, "list_objects", objs["viewer"], type="customer", filters=[{"property": "home_country", "op": "=", "value": "DE"}])
    nope = refused(400, "list_objects", objs["viewer"], type="customer", filters=[{"property": "nope", "op": "=", "value": "DE"}])
    assert hidden["message"].replace("home_country", "?") == nope["message"].replace("nope", "?"), "a hidden column reads like a missing one"


def test_composite_key(objs):
    c = objs["north"]
    r = abi.call("get_object", project=P, type="target", key=[c, 2], links=["target_cafe"])
    assert (r["key"], r["title"], row(r)["target"]) == ([c, 2], 2, 200) and r["links"][0]["rows"][0][0] == c
    assert row(abi.call("get_object", project=P, type="target", key=[c, "3"]))["target"] == 300, "text cast to the key's type"
    assert "keyed by cafe_id, month" in refused(400, "get_object", type="target", key=[c])["message"]
    refused(404, "get_object", type="target", key=[c, 4])
    refused(400, "get_object", type="target", key=[c, "three"])  # not an INTEGER
    got = abi.call("list_objects", project=P, type="target", filters=[{"property": "month", "op": ">=", "value": 2}])
    assert [x[:2] for x in got["rows"]] == [[c, 2], [c, 3]] and got["key"] == ["cafe_id", "month"]


def test_measures_filtered_to_object(objs):
    for t, col, key in (("cafe", "cafes.cafe_id", objs["north"]), ("customer", "customers.customer_id",
                                                                   sql("SELECT min(customer_id) FROM orders")[0][0])):
        r = abi.call("get_object", project=P, type=t, key=[key])
        q = abi.call("semantic_query", project=P, measures=["revenue", "orders_rows"], filters=[{"column": col, "op": "=", "value": key}])
        p = row(r)
        assert [p["revenue"], p["orders_rows"]] == q["rows"][0] and r["measures"] == q["measures"], (t, p, q["rows"])
    every_cafe = abi.call("semantic_query", project=P, measures=["revenue"])["rows"][0][0]
    assert row(abi.call("get_object", project=P, type="cafe", key=[objs["north"]]))["revenue"] != every_cafe
    assert row(viewer(objs, "get_object", type="cafe", key=[objs["north"]]))["revenue"] == \
        row(abi.call("get_object", project=P, type="cafe", key=[objs["north"]]))["revenue"], "a North café: all of its orders are visible"


# ---------- #11 list_objects ----------

VISITED_NORTH = ("SELECT DISTINCT o.customer_id FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = '{}'"
                 " AND o.customer_id IS NOT NULL ORDER BY 1")


def test_customers_who_visited_north(objs):
    got = every(type="customer", filters=[{"link": "visited", "property": "region", "op": "=", "value": "North"}], order=[{"property": "customer_id"}])
    want = [x[0] for x in sql(VISITED_NORTH.format("North"))]
    assert len(want) > 1000 and [x[0] for x in got] == want, "paged by offset, ordered as asked"


def test_two_hop_chain(objs):
    """Products sold in North cafés (order_product inverse, then order_cafe), and customers who visited the café Ada
    manages (visited, then contact_cafe's inverse: three relationship hops)."""
    got = every(type="product", filters=[{"link": "order_product.order_cafe", "property": "region", "op": "=", "value": "North"}])
    want = sql("SELECT DISTINCT o.product_id FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = 'North'")
    assert sorted(x[0] for x in got) == sorted(x[0] for x in want) and want
    got = every(type="customer", filters=[{"link": "visited.contact_cafe", "property": "manager", "op": "=", "value": "Ada Example"}])
    want = sql(f"SELECT DISTINCT customer_id FROM orders WHERE cafe_id = '{objs['north']}' AND customer_id IS NOT NULL")
    assert sorted(x[0] for x in got) == sorted(x[0] for x in want) and want
    assert not every(type="customer", filters=[{"link": "visited.contact_cafe", "property": "manager", "op": "=", "value": "Nobody"}])
    assert "not product" in refused(400, "list_objects", type="product", filters=[
        {"link": "visited", "property": "region", "op": "=", "value": "North"}])["message"]


def test_m2m_no_duplicates(objs):
    two = {"link": "visited", "property": "region", "op": "in", "value": ["North", "South"]}
    got = [x[0] for x in every(type="customer", filters=[two])]
    want = {x[0] for x in sql(VISITED_NORTH.format("North")) + sql(VISITED_NORTH.format("South"))}
    assert len(got) == len(set(got)) == len(want) and set(got) == want, "each customer once, however many cafés and orders"
    nb = abi.call("get_object", project=P, type="cafe", key=[objs["north"]], links=["visited"], limit=1000)["links"][0]["rows"]
    assert len(nb) == len({x[0] for x in nb}) and not any(None in x for x in nb), "neighbors once each; anonymous orders link to no one"
    both = every(type="customer", filters=[two, {"property": "segment", "op": "=", "value": sql("SELECT min(segment) FROM customers")[0][0]}])
    assert len(both) == len({x[0] for x in both}) < len(got), "filters all hold"


def test_restricted_sql_passes_rls_check(objs):
    """As the North Viewer: the semi-joins and ILIKE pass rls.check (no 403) and read through their views, so a walk
    through orders sees only North cafés' orders (and, since 1.2.6 M3, customers follow visited: only North cafés' visitors)."""
    north = {"link": "visited", "property": "region", "op": "=", "value": "North"}
    args = {"type": "customer", "filters": [north], "search": "c0001"}
    mine, owners = every(objs["viewer"], **args), every(**args)
    assert [x[0] for x in mine] == [x[0] for x in owners] and mine and all("c0001" in x[0].lower() for x in mine)
    south = {"link": "visited", "property": "region", "op": "=", "value": "South"}
    assert every(type="customer", filters=[south]) and not every(objs["viewer"], type="customer", filters=[south])
    r = viewer(objs, "get_object", type="customer", key=[mine[0][0]], links=["visited", "bought"])
    cafes = {x[0] for ln in r["links"] if ln["link"] == "visited" for x in ln["rows"]}
    assert cafes and cafes <= {x[0] for x in sql("SELECT cafe_id FROM cafes WHERE region = 'North'")}
    assert viewer(objs, "list_objects", type="cafe", order=[{"property": "seats", "desc": True}], search="%")["rows"]


# ---------- what the reader can't read: hidden join and key columns, one-to-one tables, tables not loaded ----------

@contextlib.contextmanager
def hiding(table: str, columns: list[str]):
    """A column rule for this block only: Viewers don't see `columns` of `table`."""
    sure("set_column_rule", project=P, id="pi_hide", table=table, columns=columns)
    try:
        yield
    finally:
        sure("delete_rls_rule", project=P, id="pi_hide")


def test_hidden_join_column_reads_like_missing(objs):
    cust, cat = sql("SELECT min(customer_id) FROM orders")[0][0], sql("SELECT min(category) FROM products")[0][0]
    bought = [{"link": "bought", "property": "category", "op": "=", "value": cat}]
    with hiding("orders", ["product_id"]):
        listed = refused(400, "list_objects", objs["viewer"], type="customer", filters=bought)
        got = refused(400, "get_object", objs["viewer"], type="customer", key=[cust], links=["bought"])
        assert listed["message"] == got["message"] == "Link 'bought' joins on a column you can't read.", (listed, got)
        assert abi.call("list_objects", project=P, type="customer", filters=bought, limit=1)["rows"], "the owner walks it"


def test_hidden_one_to_one_property_skipped(objs):
    c = objs["north"]
    with hiding("cafe_contacts", ["cafe_id"]):
        assert "cafe_contacts.manager" not in viewer(objs, "get_object", type="cafe", key=[c])["columns"], "its join column is hidden"
        assert row(abi.call("get_object", project=P, type="cafe", key=[c]))["cafe_contacts.manager"] == "Ada Example"


def test_hidden_key_refused_plainly(objs):
    c = objs["north"]
    with hiding("cafe_targets", ["month"]):
        for action, args in (("get_object", {"key": [c, 2]}), ("list_objects", {})):
            body = refused(403, action, objs["viewer"], type="target", **args)
            assert "identifies them is hidden" in body["message"] and "month" not in body["message"], body
        assert row(abi.call("get_object", project=P, type="target", key=[c, 2]))["target"] == 200


NOT_LOADED = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, security
from abi.semantic import model
P = json.loads(sys.stdin.read())["project"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
real = model.lake_columns
model.lake_columns = lambda p, *a: {t: v for t, v in real(p, *a).items() if t != "cafe_targets"}  # as before its first load
out = []
for name, args in (("get_object", {"type": "target", "key": ["x", 1]}), ("list_objects", {"type": "target"})):
    try:
        actions.run(name, {"project": P, **args}, owner, "rest")
        out.append(None)
    except actions.ActionFailed as e:
        out.append([e.status, str(e)])
print(json.dumps(out))
"""


def test_table_not_loaded_says_so(objs):
    r = engine(NOT_LOADED, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    for status, message in json.loads(r.stdout.splitlines()[-1]):
        assert status == 404 and "aren't loaded yet" in message and "cafe_targets" in message, message


WALK2 = r"""
import json
import duckdb, sqlglot
from abi.semantic import ontology, rls
c = duckdb.connect()
c.execute("CREATE TABLE t (cafe_id VARCHAR, month INTEGER, target INTEGER); INSERT INTO t VALUES ('A', 1, 10), ('A', 2, 20), ('B', 1, 30)")
c.execute("CREATE TABLE n (cafe_id VARCHAR, month INTEGER, note VARCHAR); INSERT INTO n VALUES ('A', 2, 'x'), ('A', 2, 'y'), ('B', 2, 'z')")
r = {"id": "n-cafe_id-month", "from": {"table": "n", "columns": ["cafe_id", "month"]}, "to": {"table": "t", "columns": ["cafe_id", "month"]}}
hops = ontology.walk({"relationships": [r], "tables": []}, "n", [r["id"]])
sql = f"SELECT cafe_id, month, target FROM t WHERE {ontology._semi(hops, 'n', 'note IN (' + chr(39) + 'x' + chr(39) + ', ' + chr(39) + 'y' + chr(39) + ')')} ORDER BY 1, 2"
back = f"SELECT note FROM n WHERE {ontology._semi([(x, not a) for x, a in reversed(hops)], 't', 'target >= 20')} ORDER BY 1"
print(json.dumps({"rows": c.execute(sql).fetchall(), "back": c.execute(back).fetchall(), "sql": sql,
                  "refused": sorted(rls.refused_calls(sql, sqlglot.parse_one(sql, read="duckdb")))}))
"""


def test_two_column_relationship_walk():
    """A relationship on two columns walks as a tuple IN semi-join, both ways: the target with two notes once, and back
    from targets to their notes; rls.check's function allow-list passes it."""
    r = engine(WALK2)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert '("cafe_id", "month") IN (SELECT "cafe_id", "month" FROM "n"' in out["sql"], out["sql"]
    assert out["rows"] == [["A", 2, 20]] and out["back"] == [["x"], ["y"]] and out["refused"] == [], out
