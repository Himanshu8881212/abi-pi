"""1.2.6 M2 (needs the engine at :8780, no LLM): objects on Brewline. get_object reads one object by its key (a list, in
key order, cast to the key's types) with its properties, one-to-one properties, its type's measures filtered to it and
its neighbors along links; list_objects filters a type on its own properties and on properties reached through link
chains, each object once. Every read is the person's (row-level security views, rls.check): a hidden object answers
like a missing one, a hidden column is absent. The owner's sql_query is the reference for every set of rows."""
import contextlib
import copy
import json
import re
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
        yield {"viewer": abi.login(name, pw)[1]["token"], "north": north, "south": south, "name": name, "pw": pw}
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


# ---------- 1.2.6 M9: ML on objects (needs the engine's heavy worker; trains small models, no LLM) ----------

ROOT = abi.ROOT
MARK = "[hidden by row-level security]"
DOC = [json.loads(b) for b in re.findall(r"```json (?:dataset|ml)\n(.*?)\n```", (ROOT / "docs/objects.md").read_text(), re.S)]
MADE: dict = {}  # model ID -> its object type after the first scoring: each model trains once per module


def model_json() -> dict:
    return json.loads((abi.ABI_PROJECTS / P / "model/model.json").read_text())


def ended(model: str, kind: str, run: str) -> dict:
    """The model's training ("latest_run") or scoring ("latest_score") run `run`, once it ended."""
    end = time.monotonic() + 900
    while True:
        r = next(m for m in abi.call("ml_models", project=P)["models"] if m["id"] == model)[kind]
        if r and r["run"] == run and r["status"] in ("ok", "failed"):
            return r
        assert time.monotonic() < end, r
        time.sleep(1)


def score(model: str) -> dict:
    r = ended(model, "latest_score", abi.call("score_model", project=P, model=model)["run"])
    assert r["status"] == "ok", r["error"]
    return r


def otype(i: str) -> dict:
    return next(x for x in abi.call("ontology", project=P)["objects"] if x["id"] == i)


def scored(ml: dict, query: dict | None = None) -> dict:
    """The object type of ML model `ml` after it was saved (with its dataset), trained and scored, once per module."""
    if ml["id"] not in MADE:
        if query:
            abi.call("save_dataset", project=P, id=ml["dataset"], name=ml["dataset"].replace("_", " ").capitalize(), query=query)
        abi.call("save_ml_model", project=P, ml=ml)
        r = ended(ml["id"], "latest_run", abi.call("train_model", project=P, model=ml["id"])["run"])
        assert r["status"] == "ok", r["error"]
        score(ml["id"])
        MADE[ml["id"]] = otype(ml["object"])
    return MADE[ml["id"]]


def prop(t: dict, column: str) -> dict:
    [p] = [p for p in t["properties"] if p["column"] == column]
    return p


def one_per(table: str, key: str, of: str) -> int:
    """The rows of predictions table `table`: one for each object, none without one."""
    [(n, d, nulls)] = sql(f"SELECT count(*), count(DISTINCT {key}), count(*) FILTER (WHERE {key} IS NULL) FROM {table}")
    assert n == d and nulls == 0 and d == sql(f"SELECT count(DISTINCT {key.split('_', 1)[1]}) FROM {of}")[0][0], (n, d, nulls)
    return n


def revenue() -> dict:
    """docs/objects.md "A prediction on an object": its dataset and model, as written there."""
    assert [d.get("$schema", "").rsplit("/", 1)[-1] for d in DOC] == ["", "ml.json"] and DOC[1]["object"] == "cafe"
    return scored({k: v for k, v in DOC[1].items() if k != "$schema"}, DOC[0]["query"]) if DOC[1]["id"] not in MADE else MADE[DOC[1]["id"]]


def flow_runs(fid: str, n: int) -> list:
    """The flow's runs, oldest first, once there are `n` and each has ended."""
    end = time.monotonic() + 300
    while True:
        got = abi.call("flow_runs", project=P, flow=fid, limit=200)["runs"][::-1]
        if len(got) >= n and all(r["status"] not in ("queued", "running") for r in got):
            assert len(got) == n, [(r["id"], r["status"], r["error"]) for r in got]
            return got
        assert time.monotonic() < end, [(r["id"], r["status"], r["error"]) for r in got]
        time.sleep(1)


def test_ml_object_regression(objs):
    """The docs' example: a regression on cafés writes one prediction per café into a table keyed by the café's key,
    and the café type gets it as a property under the model's name. A name or meaning a person gives the property
    stays when the model is scored again. The model's file carries `object` (Git, export); the published copy, which
    never scores, loses the property with the table."""
    import io
    import zipfile

    import app
    import transfer

    t = revenue()
    p = prop(t, "ml_expected_revenue.prediction")
    assert (p["name"], p["meaning"], p["type"]) == ("Expected revenue", "quantity", "DOUBLE") and "Expected revenue" in p["description"], p
    m = model_json()
    assert {"id": "ml_expected_revenue", "table": "ml_expected_revenue", "key": ["cafes_cafe_id"]} in m["entities"]
    assert {"id": "ml_expected_revenue-cafes_cafe_id", "from": {"table": "ml_expected_revenue", "columns": ["cafes_cafe_id"]},
            "to": {"table": "cafes", "columns": ["cafe_id"]}} in m["relationships"]
    assert one_per("ml_expected_revenue", "cafes_cafe_id", "orders") > 100
    c = objs["north"]
    [(want,)] = sql(f"SELECT prediction FROM ml_expected_revenue WHERE cafes_cafe_id = '{c}'")
    assert want > 0 and row(abi.call("get_object", project=P, type="cafe", key=[c]))["ml_expected_revenue.prediction"] == pytest.approx(want)
    by_measure = abi.call("semantic_query", project=P, measures=["expected_revenue_prediction"], filters=[{"column": "cafes.cafe_id", "op": "=", "value": c}])
    assert by_measure["rows"] == [[pytest.approx(want)]], "the measure reaches the café through the table's relationship"
    # a person renames the property and calls it money: the next scoring keeps both, and adds no second one
    o = json.loads((abi.ABI_PROJECTS / P / "model/ontology.json").read_text())
    mine = next(q for x in o["objects"] if x["id"] == "cafe" for q in x["properties"] if q["column"] == "ml_expected_revenue.prediction")
    mine |= {"name": "Revenue we expect", "meaning": "money"}
    sure("save_ontology", project=P, ontology=o)
    score("expected_revenue")
    p = prop(otype("cafe"), "ml_expected_revenue.prediction")
    assert (p["name"], p["meaning"]) == ("Revenue we expect", "money")
    # as code: the definition is a project file, in the export; the predictions are data and aren't
    z = zipfile.ZipFile(io.BytesIO(transfer.export(P)))
    bundle = {n.removeprefix("abi/"): json.loads(z.read(n)) if n.endswith(".json") else z.read(n).decode() for n in z.namelist() if n.startswith("abi/")}
    assert bundle["ml/expected_revenue.json"] == DOC[1] and not [n for n in z.namelist() if "parquet" in n or "ml_expected" in n.rsplit("/", 1)[-1]]
    assert mine in next(x for x in bundle["model/ontology.json"]["objects"] if x["id"] == "cafe")["properties"]
    abi.call("import_project", project=P, bundle=bundle, dry_run=True)  # the bundle passes ABI's import checks as it is
    app.owner_only(bundle)
    assert "ml_expected_revenue" not in json.dumps(bundle["model/ontology.json"]) + json.dumps(bundle["model/model.json"])
    assert {q["column"] for x in bundle["model/ontology.json"]["objects"] if x["id"] == "cafe" for q in x["properties"]} >= {"city", "cafe_contacts.manager"}


LOYAL = {"id": "loyal", "name": "Loyalty chance", "task": "classification", "dataset": "customer_features", "object": "customer",
         "target": "customers.loyalty_member", "features": ["revenue", "orders_rows", "customers.segment"]}
LOYAL_ROWS = {"measures": ["revenue", "orders_rows"], "dimensions": ["customers.customer_id", "customers.segment", "customers.loyalty_member"]}


def test_ml_object_classification(objs):
    """A yes/no classification on customers: the property is the probability of yes, one per customer with orders (the
    orders without a customer get none), beside the predicted class. A per-customer alert compares it: one run for each
    customer above the value, with the probability in the run's own words."""
    from test_object_flows import TICK

    t = scored(LOYAL, LOYAL_ROWS)
    p = prop(t, "ml_loyal.probability")
    assert (p["name"], p["meaning"], p["type"]) == ("Loyalty chance", "probability", "DOUBLE") and "true" in p["description"].lower(), p
    assert not [q for q in t["properties"] if q["column"] == "ml_loyal.prediction"], "one property per model: the probability"
    assert sql("SELECT count(*) FROM orders WHERE customer_id IS NULL")[0][0] > 0, "the dataset has a row of no customer"
    n = one_per("ml_loyal", "customers_customer_id", "orders")
    [(lo, hi, off)] = sql("SELECT min(probability), max(probability), count(*) FILTER (WHERE (probability > 0.5) <> (prediction = 'true'"
                         " OR prediction = 'True')) FROM ml_loyal")
    assert 0 <= lo < hi <= 1 and off == 0, (lo, hi, off, n)
    top = sql("SELECT customers_customer_id, probability FROM ml_loyal ORDER BY probability DESC, 1 LIMIT 40")
    cust, want = top[0]
    assert row(abi.call("get_object", project=P, type="customer", key=[cust]))["ml_loyal.probability"] == pytest.approx(want)
    assert abi.call("semantic_query", project=P, measures=["loyal_probability"], dimensions=["customers.customer_id"],
                    filters=[{"column": "customers.customer_id", "op": "=", "value": cust}])["rows"] == [[cust, pytest.approx(want)]]
    # an alert per customer on the prediction: those above the value, each once
    below = next((v for _, v in top if v < want), None)
    assert below is not None, top
    cut = (below + want) / 2  # between the top value and the next: the customers at the top are the ones above it
    above = sorted(k for k, v in top if v > cut)
    sure("save_flow", project=P, flow={"id": "likely_loyal", "name": "Likely loyal", "enabled": False, "trigger": {
        "type": "data_condition", "query": {"measures": ["loyal_probability"]}, "measure": "loyal_probability", "op": ">", "value": cut, "per": "customer"},
        "steps": [{"id": "post", "type": "action", "action": "post_message", "input": {
            "channel": "inbox", "text": "Likely loyal: {{ data.trigger.title }} at {{ data.trigger.object.ml_loyal.probability }}"}}]})
    try:
        r = engine(TICK, timeout=600, stdin=json.dumps({"project": P, "flows": ["likely_loyal"], "state": "likely_loyal", "ahead": 0}))
        assert r.returncode == 0, r.stderr[-2000:]
        got = flow_runs("likely_loyal", len(above))
        assert {x["status"] for x in got} == {"ok"} and sorted(x["object"]["key"][0] for x in got) == above, got
        said = [m["text"] for m in abi.call("inbox", project=P)["messages"] if m["text"].startswith("Likely loyal: ")]
        assert {x.split(": ", 1)[1].split(" at ")[0]: float(x.rsplit(" at ", 1)[1]) for x in said} == pytest.approx({k: v for k, v in top if v > cut}), said
    finally:
        sure("delete_flow", project=P, flow="likely_loyal")


def test_ml_object_clustering(objs):
    """Groups of similar cafés: each café's group is a category property under the model's name."""
    t = scored({"id": "cafe_groups", "name": "Café group", "task": "clustering", "dataset": "cafe_sizes", "object": "cafe",
                "features": ["revenue", "cafes.seats"], "k": 3},
               {"measures": ["revenue", "orders_rows"], "dimensions": ["cafes.cafe_id", "cafes.seats"]})
    p = prop(t, "ml_cafe_groups.cluster")
    assert (p["name"], p["meaning"], p["type"]) == ("Café group", "category", "INTEGER"), p
    assert one_per("ml_cafe_groups", "cafes_cafe_id", "orders") > 100
    groups = dict(sql("SELECT cafes_cafe_id, cluster FROM ml_cafe_groups"))
    assert len(set(groups.values())) == 3
    c = objs["north"]
    assert row(abi.call("get_object", project=P, type="cafe", key=[c]))["ml_cafe_groups.cluster"] == groups[c]
    assert model_json()["entities"].count({"id": "ml_cafe_groups", "table": "ml_cafe_groups", "key": ["cafes_cafe_id"]}) == 1


def test_ml_object_anomaly(objs):
    """Unusual cafés: a flag and a score per café, two properties named after the model."""
    t = scored({"id": "odd_cafes", "name": "Unusual café", "task": "anomaly", "dataset": "cafe_sizes", "object": "cafe",
                "features": ["revenue", "orders_rows"], "method": "isolation_forest"})
    flag, sc = prop(t, "ml_odd_cafes.anomaly"), prop(t, "ml_odd_cafes.score")
    assert (flag["name"], flag["meaning"], flag["type"]) == ("Unusual café", "flag", "BOOLEAN"), flag
    assert (sc["name"], sc["meaning"], sc["type"]) == ("Unusual café score", "quantity", "DOUBLE"), sc
    assert one_per("ml_odd_cafes", "cafes_cafe_id", "orders") > 100
    c, is_odd, score_ = sql("SELECT cafes_cafe_id, anomaly, score FROM ml_odd_cafes ORDER BY score DESC, 1 LIMIT 1")[0]
    got = row(abi.call("get_object", project=P, type="cafe", key=[c]))
    assert (got["ml_odd_cafes.anomaly"], got["ml_odd_cafes.score"]) == (is_odd, pytest.approx(score_)) and isinstance(is_odd, bool)
    [(flagged, least)] = sql("SELECT count(*) FILTER (WHERE anomaly), min(score) FILTER (WHERE anomaly) FROM ml_odd_cafes")
    assert flagged == 0 or sql(f"SELECT count(*) FROM ml_odd_cafes WHERE NOT anomaly AND score > {least}") == [[0]], "the flag follows the score"


FORECAST = {"id": "cafe_outlook", "name": "Revenue outlook", "task": "forecast", "dataset": "cafe_months", "object": "cafe",
            "target": "revenue", "time": "calendar.month", "horizon": 3, "method": "lightgbm"}
MONTHS = {"measures": ["revenue"], "dimensions": ["cafes.cafe_id", "calendar.year", "calendar.month"]}


def test_ml_object_forecast(objs):
    """A forecast on cafés: a table keyed by café and period, joined to the café (and the calendar); no property (a café
    has several periods), but the forecast's measure joins the café's measures, so its page shows the total ahead."""
    t = scored(FORECAST, MONTHS)
    assert not [p for p in t["properties"] if p["column"].startswith("ml_cafe_outlook.")]
    assert t["measures"] == ["revenue", "orders_rows", "cafe_outlook_prediction"], t["measures"]
    m = model_json()
    assert {"id": "ml_cafe_outlook", "table": "ml_cafe_outlook", "key": ["cafes_cafe_id", "period"]} in m["entities"]
    to = {r["to"]["table"]: r for r in m["relationships"] if r["from"]["table"] == "ml_cafe_outlook"}
    assert set(to) == {"cafes", "calendar"} and to["cafes"]["from"]["columns"] == ["cafes_cafe_id"] and to["cafes"]["to"]["columns"] == ["cafe_id"]
    c = objs["north"]
    [(want,)] = sql(f"SELECT sum(prediction) FROM ml_cafe_outlook WHERE cafes_cafe_id = '{c}'")
    got = abi.call("get_object", project=P, type="cafe", key=[c])
    assert "measures_error" not in got and row(got)["cafe_outlook_prediction"] == pytest.approx(want), got
    assert row(got)["revenue"] is not None and row(got)["orders_rows"] > 0, "beside its other measures"
    assert next(x["name"] for x in abi.call("semantic_model", project=P)["measures"] if x["id"] == "cafe_outlook_prediction") == "Revenue outlook forecast"
    score("cafe_outlook")
    assert otype("cafe")["measures"].count("cafe_outlook_prediction") == 1, "scored again: listed once"


def test_forecast_one_series_per_object(objs):
    """One series per café: every café that sold gets the same next three months, once each, after the data's last month."""
    scored(FORECAST, MONTHS)
    cafes = [x[0] for x in sql("SELECT DISTINCT cafe_id FROM orders ORDER BY 1")]
    [(last,)] = sql("SELECT max(date_trunc('month', order_date))::DATE::VARCHAR FROM orders")
    y, mth = int(last[:4]), int(last[5:7])
    ahead = [f"{y + (mth + i - 1) // 12}-{(mth + i - 1) % 12 + 1:02d}-01" for i in (1, 2, 3)]
    rows = sql("SELECT cafes_cafe_id, period::VARCHAR, prediction FROM ml_cafe_outlook ORDER BY 1, 2")
    assert len(rows) == 3 * len(cafes) and len({(r[0], r[1]) for r in rows}) == len(rows)
    assert [(r[0], r[1]) for r in rows] == [(c, p) for c in cafes for p in ahead], "each café: its own three periods"
    assert all(r[2] is not None for r in rows) and len({r[2] for r in rows}) > len(cafes), "each café its own numbers"


def test_ml_features_through_link(objs):
    """A feature from another type: the customers who visited each café, a measure over customers that reaches cafés
    only through the link `visited` (many-to-many), named in the dataset's query. The model trains on it and the café
    gets the prediction."""
    abi.call("set_measure", project=P, id="visitors", name="Visitors", expression="COUNT(customers.customer_id)")
    q = {"measures": ["visitors"], "dimensions": ["cafes.cafe_id", "cafes.seats", "cafes.format"], "links": ["visited"]}
    with pytest.raises(abi.ActionError) as e:  # without the link the planner has no way from customers to cafés
        abi.call("semantic_query", project=P, **{k: v for k, v in q.items() if k != "links"})
    assert e.value.status == 400 and "links: [visited]" in e.value.body["message"], e.value.body
    t = scored({"id": "seats_fit", "name": "Seats to expect", "task": "regression", "dataset": "cafe_visitors", "object": "cafe",
                "target": "cafes.seats", "features": ["visitors", "cafes.format"]}, q)
    assert prop(t, "ml_seats_fit.prediction")["name"] == "Seats to expect"
    run = next(m for m in abi.call("ml_models", project=P)["models"] if m["id"] == "seats_fit")["latest_run"]
    assert run["query"]["links"] == ["visited"] and {x["feature"] for x in run["importance"]} == {"visitors", "cafes.format"}, run
    c = objs["north"]
    [(want,)] = sql(f"SELECT count(DISTINCT customer_id) FROM orders WHERE cafe_id = '{c}' AND customer_id IS NOT NULL")
    assert abi.call("semantic_query", project=P, **q, filters=[{"column": "cafes.cafe_id", "op": "=", "value": c}])["rows"][0][-1] == want, \
        "the feature the model read: each café's visitors, each once"
    assert one_per("ml_seats_fit", "cafes_cafe_id", "orders") > 100
    [(pred,)] = sql(f"SELECT prediction FROM ml_seats_fit WHERE cafes_cafe_id = '{c}'")
    assert row(abi.call("get_object", project=P, type="cafe", key=[c]))["ml_seats_fit.prediction"] == pytest.approx(pred)
    # the North Viewer sees this café, but the model read customers (a hidden column, and rows their rule filters through
    # the link): its prediction is hidden from them, said so, and the table shows them no row
    theirs = viewer(objs, "get_object", type="cafe", key=[c])
    assert theirs["key"] == [c] and row(theirs)["ml_seats_fit.prediction"] == MARK
    assert viewer(objs, "sql_query", sql="SELECT count(*) FROM ml_seats_fit")["rows"] == [[0]]
    assert all(r[1] is None for r in viewer(objs, "semantic_query", measures=["seats_fit_prediction"], dimensions=["cafes.cafe_id"])["rows"])


AFTER = {"id": "after_scoring", "name": "After scoring", "enabled": True, "trigger": {"type": "after_refresh", "tables": ["ml_expected_revenue"]},
         "steps": [{"id": "post", "type": "action", "action": "post_message", "input": {"channel": "inbox", "text": "Expected revenue was scored again."}}]}


def test_score_starts_after_refresh_flow(objs):
    """Scoring writes a new snapshot of the predictions table: a flow that triggers on that table runs once for it."""
    revenue()
    sure("save_flow", project=P, flow=AFTER)
    try:
        assert flow_runs("after_scoring", 0) == []
        snap = score("expected_revenue")["snapshot"]
        [run] = flow_runs("after_scoring", 1)
        assert run["status"] == "ok" and run["id"].endswith(f"/snapshot-{snap}"), run
        snap2 = score("expected_revenue")["snapshot"]
        assert snap2 > snap and [r["id"].rsplit("/", 1)[1] for r in flow_runs("after_scoring", 2)] == [f"snapshot-{snap}", f"snapshot-{snap2}"]
    finally:
        sure("delete_flow", project=P, flow="after_scoring")


def test_score_in_flow_starts_nothing(objs):
    """A flow's own `score` step writes the predictions too, but starts no flow (D-23: flows can't start each other
    through data); the same scoring by a person, right after, does."""
    revenue()
    sure("save_flow", project=P, flow=AFTER | {"id": "after_a_step"})  # its own flow: run history outlives a deleted one
    sure("save_flow", project=P, flow={"id": "rescore", "name": "Rescore", "enabled": True, "trigger": {"type": "manual"},
                                       "steps": [{"id": "score", "type": "score", "model": "expected_revenue"}]})
    try:
        before = next(m for m in abi.call("ml_models", project=P)["models"] if m["id"] == "expected_revenue")["latest_score"]
        abi.call("run_flow", project=P, flow="rescore")
        [run] = flow_runs("rescore", 1)
        out = next(s for s in run["steps"] if s["step"] == "score")["output"]
        assert run["status"] == "ok" and out["status"] == "ok" and out["run"] != before["run"] and out["snapshot"] > before["snapshot"], run
        assert flow_runs("after_a_step", 0) == [], "the step's scoring started nothing"
        # a flow starts before its scoring run ends, so the person's scoring below proves it: its run is there, and it is the only one
        snap = score("expected_revenue")["snapshot"]
        assert [r["id"].rsplit("/", 1)[1] for r in flow_runs("after_a_step", 1)] == [f"snapshot-{snap}"]
        assert snap > out["snapshot"] and not [r for r in abi.call("flow_runs", project=P, flow="after_a_step")["runs"] if r["id"].endswith(f"/snapshot-{out['snapshot']}")]
    finally:
        for f in ("after_a_step", "rescore"):
            sure("delete_flow", project=P, flow=f)


def test_a_wrong_object_model_names_the_file_the_field_and_the_fix(objs):
    """Saving an ML model on an object checks it like an import: an unknown type, a type with a two-column key, a
    dataset not grouped by the type's key, and date columns outside a forecast are refused with file, field and fix.
    So is a dataset grouped by a column of another table (several rows per object): at the save, not when it is scored."""
    revenue()
    base = {"id": "bad", "name": "Bad", "task": "regression", "dataset": "cafe_features", "object": "cafe", "target": "revenue"}

    def errors(**change) -> list:
        with pytest.raises(abi.ActionError) as e:
            abi.call("save_ml_model", project=P, ml=base | change)
        assert e.value.status == 422, e.value.body
        return [(x["file"], x["path"], x["error"], x["fix"]) for x in e.value.body["errors"]]

    [(f, path, err, fix)] = errors(object="shop")
    assert (f, path, err) == ("ml/bad.json", "$.object", "Unknown object type 'shop'.") and "cafe, " in fix
    [(f, path, err, fix)] = errors(object="target")
    assert path == "$.object" and "identified by 2 columns (cafe_targets.cafe_id, cafe_targets.month)" in err and "one key column" in fix
    [(f, path, err, fix)] = errors(object="customer")
    assert (path, err) == ("$.object", "Dataset 'cafe_features' isn't grouped by customers.customer_id, the key of object type 'customer'.")
    assert fix == "Add customers.customer_id to the dataset's dimensions (save_dataset), or remove `object`."
    abi.call("save_dataset", project=P, id="cafe_months", name="Cafe months", query=MONTHS)
    [(f, path, err, fix)] = errors(dataset="cafe_months")
    assert path == "$.object" and "predicts once per Café" in err and "calendar.year" in err and "only a forecast" in fix
    assert not (abi.ABI_PROJECTS / P / "ml/bad.json").exists()
    o = {k: v for k, v in json.loads((abi.ABI_PROJECTS / P / "model/ontology.json").read_text()).items() if k != "$schema"}
    with pytest.raises(abi.ActionError) as e:  # an object type a model predicts for can't go
        sure("save_ontology", project=P, ontology=o | {"objects": [x for x in o["objects"] if x["id"] != "cafe"]})
    assert e.value.status == 422 and any(x["file"] == "ml/expected_revenue.json" and x["path"] == "$.object" for x in e.value.body["errors"]), e.value.body
    # several rows per café (one per product category): refused at the save, saying what to keep
    abi.call("save_dataset", project=P, id="cafe_categories", name="Café categories",
             query={"measures": ["revenue"], "dimensions": ["cafes.cafe_id", "cafes.seats", "products.category"]})
    [(f, path, err, fix)] = errors(id="per_category", name="Per category", dataset="cafe_categories")
    assert (f, path) == ("ml/per_category.json", "$.object") and "also grouped by products.category, so it has several rows for one Café" in err, err
    assert "Keep only columns of table 'cafes' as its dimensions" in fix and "is a measure" in fix, fix
    assert "ml_per_category" not in json.dumps(model_json()) and not [p for p in otype("cafe")["properties"] if "per_category" in p["column"]]


def test_object_page_shows_the_prediction(objs, tmp_path):
    """The app: a café's page shows its predictions among its properties under the models' names (money as money), a
    customer's page the probability as a percentage; no table, column or model id anywhere. The North Viewer sees a
    North café's prediction, a South café's page not at all, and a customer's prediction as "hidden for your access"
    (made from rows their rules filter), never as an empty value. At a phone's width nothing overflows. Screenshots beside (tmp_path)."""
    import app
    from test_pages import OWNER, Browser, drive, okey

    revenue()
    scored(LOYAL, LOYAL_ROWS)
    c, hubs = objs["north"], set(app.HUBS)
    [(cust, chance)] = sql("SELECT customers_customer_id, probability FROM ml_loyal JOIN orders ON customer_id = customers_customer_id"
                           f" WHERE cafe_id = '{c}' ORDER BY 1 LIMIT 1")
    label = next(p.get("name") for p in otype("cafe")["properties"] if p["column"] == "ml_expected_revenue.prediction")
    props = "Object.fromEntries([...document.querySelectorAll('main .okv > div')].map(d => [d.querySelector('b').textContent, d.querySelector('span').textContent]))"
    page = lambda t, k, shot: [["show", okey(t, k)], ["wait", "main .okv"], ["wait", "main .kpis .kpi"], ["get", "props", props], ["get", "text", "document.querySelector('main').innerText"],  # noqa: E731
                                       ["get", "wide", "document.documentElement.scrollWidth > innerWidth + 1"], ["shot", str(tmp_path / shot)]]
    srv = app.serve(P, 0, open_browser=False, signin=True)
    try:
        owner, north = Browser(srv.server_address[1], "admin", OWNER, P), Browser(srv.server_address[1], objs["name"], objs["pw"], P)
        cafe = drive(owner, tmp_path, page("cafe", c, "cafe-1440.png"))
        assert re.fullmatch(r"[\d,.]+", cafe["props"][label]) and cafe["wide"] is False, cafe["props"]
        assert not re.search(r"ml_|\.prediction|expected_revenue|cafe_id", cafe["text"]), "business names only"
        person = drive(owner, tmp_path, page("customer", cust, "customer-1440.png"))
        assert person["props"]["Loyalty chance"] == f"{chance * 100:.1f}%" and not re.search(r"ml_|probability|loyal\b", person["text"]), person["props"]
        narrow = drive(owner, tmp_path, page("cafe", c, "cafe-390.png"), width=390)
        assert narrow["props"][label] == cafe["props"][label] and narrow["wide"] is False
        theirs = drive(north, tmp_path, page("cafe", c, "viewer-cafe-1440.png"))
        assert theirs["props"][label] == cafe["props"][label], "a café they see: its prediction too"
        assert drive(north, tmp_path, page("customer", cust, "viewer-customer-1440.png"))["props"]["Loyalty chance"] == "hidden for your access"
        gone = drive(north, tmp_path, [["show", okey("cafe", objs["south"])], ["wait", "main .emptyx"],
                                       ["get", "text", "document.querySelector('main').innerText"], ["shot", str(tmp_path / "viewer-hidden-390.png")]], width=390)
        assert "Not available" in gone["text"] and "Open cafés" in gone["text"] and not re.search(r"\d+\.\d\d", gone["text"]), gone["text"]
        assert all((tmp_path / f"{x}.png").stat().st_size > 10_000 for x in ("cafe-1440", "customer-1440", "cafe-390", "viewer-cafe-1440"))
    finally:
        srv.shutdown()
        for k in set(app.HUBS) - hubs:
            app.HUBS.pop(k, None)


def test_a_probability_says_which_outcome_it_is_for(objs):
    """A target with two outcomes that aren't yes/no (a café's region, North or South): without `positive` the save
    answers with a note naming the two, and scoring writes the predicted outcome only, no probability. A `positive`
    that is neither is refused. With it, the probability is that outcome's (the other one's is the rest to 1), named in
    business words on the property and the measure. A model named like a property the type has gets another name; a
    measure a person made under the probability's ID is not replaced; a property a person removed stays away; taking
    `object` off the model, or regrouping its dataset, takes its property off the type. Its table is no object type."""
    q = {"measures": ["revenue", "orders_rows"], "dimensions": ["cafes.cafe_id", "cafes.seats", "cafes.region"],
         "filters": [{"column": "cafes.region", "op": "in", "value": ["North", "South"]}]}
    ml = {"id": "side", "name": "Region", "task": "classification", "dataset": "cafe_sides", "object": "cafe", "target": "cafes.region",
          "features": ["revenue", "cafes.seats"]}
    abi.call("save_dataset", project=P, id="cafe_sides", name="Café sides", query=q)
    note = abi.call("save_ml_model", project=P, ml=ml)["note"]
    assert note.startswith("ml/side.json $.positive: ") and '"positive": "North" or "positive": "South"' in note, note
    r = ended("side", "latest_run", abi.call("train_model", project=P, model="side")["run"])
    assert r["status"] == "ok", r["error"]
    score("side")
    mine = [p for p in otype("cafe")["properties"] if p["column"].startswith("ml_side.")]
    assert [(p["column"], p["name"], p["meaning"]) for p in mine] == [("ml_side.prediction", "Region (predicted)", "category")], \
        "the outcome only, and not under the name the café's own region has"
    assert "probability" not in abi.call("sql_query", project=P, sql="SELECT * FROM ml_side LIMIT 1")["columns"]
    measures = lambda: {x["id"]: x for x in abi.call("semantic_model", project=P)["measures"]}  # noqa: E731
    assert "side_probability" not in measures()
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_ml_model", project=P, ml=ml | {"positive": "Paris"})
    assert e.value.status == 400 and "ml/side.json $.positive: 'Paris' is not an outcome" in e.value.body["message"] and '"North" or "South"' in e.value.body["message"]
    # a measure of the person's own under the ID the probability takes: scoring refuses, and replaces nothing
    abi.call("set_measure", project=P, id="side_probability", name="Mine", expression="COUNT(cafes.cafe_id)")
    assert "note" not in abi.call("save_ml_model", project=P, ml=ml | {"positive": "North"})
    r = ended("side", "latest_score", abi.call("score_model", project=P, model="side")["run"])
    assert r["status"] == "failed" and "measure 'side_probability' exists and isn't this model's probability" in r["error"], r
    assert measures()["side_probability"]["expression"] == "COUNT(cafes.cafe_id)"
    sure("delete_measure", project=P, id="side_probability")
    # the person removes the outcome property; the next scoring adds the probability and leaves the removed one out
    o = {k: v for k, v in json.loads((abi.ABI_PROJECTS / P / "model/ontology.json").read_text()).items() if k != "$schema"}
    for x in o["objects"]:
        x["properties"] = [p for p in x.get("properties", []) if p["column"] != "ml_side.prediction"]
    sure("save_ontology", project=P, ontology=o)
    score("side")
    [p] = [p for p in otype("cafe")["properties"] if p["column"].startswith("ml_side.")]
    assert (p["column"], p["name"], p["meaning"]) == ("ml_side.probability", "Region (predicted)", "probability")
    assert p["description"].startswith("How likely Region is North, from 0 to 1.") and measures()["side_probability"]["description"] == "How likely Region is North, from 0 to 1."
    north = dict(sql("SELECT cafes_cafe_id, probability FROM ml_side"))
    assert sql("SELECT count(*) FROM ml_side WHERE (probability > 0.5 AND prediction <> 'North') OR (probability < 0.5 AND prediction = 'North')") == [[0]]
    abi.call("save_ml_model", project=P, ml=ml | {"positive": "South"})
    score("side")
    south = dict(sql("SELECT cafes_cafe_id, probability FROM ml_side"))
    assert south == pytest.approx({k: 1 - v for k, v in north.items()}) and len(set(north.values())) > 1, "the other outcome's probability"
    assert measures()["side_probability"]["description"] == "How likely Region is South, from 0 to 1."
    assert not [x for x in abi.call("propose_ontology", project=P)["ontology"]["objects"] if x["table"].startswith("ml_")], "a predictions table is no object type"
    # off its object: the property leaves the type in the same change
    abi.call("save_ml_model", project=P, ml={k: v for k, v in ml.items() if k != "object"})
    assert not [p for p in otype("cafe")["properties"] if p["column"].startswith("ml_side.")]
    # a property a person lists on the table by hand goes when the table stops being one row per café (the dataset regrouped)
    o = {k: v for k, v in json.loads((abi.ABI_PROJECTS / P / "model/ontology.json").read_text()).items() if k != "$schema"}
    next(x for x in o["objects"] if x["id"] == "cafe")["properties"].append({"column": "ml_side.prediction", "name": "Side by hand"})
    sure("save_ontology", project=P, ontology=o)
    assert row(abi.call("get_object", project=P, type="cafe", key=[objs["north"]]))["ml_side.prediction"] in ("North", "South")
    abi.call("save_dataset", project=P, id="cafe_sides", name="Café sides", query=q | {"dimensions": ["cafes.seats", "cafes.region"]})
    r = ended("side", "latest_run", abi.call("train_model", project=P, model="side")["run"])
    assert r["status"] == "ok", r["error"]
    score("side")
    assert not [p for p in otype("cafe")["properties"] if p["column"].startswith("ml_side.")]
    assert not [g for g in abi.call("readiness", project=P)["missing"] if "ml_side" in g and ("Ontology" in g or "compile" in g)], "nothing left dangling"
    assert "side_probability" not in measures(), "nor its probability's measure"
    # on cafés again, scored, then deleted: its property goes with the model
    abi.call("save_dataset", project=P, id="cafe_sides", name="Café sides", query=q)
    abi.call("save_ml_model", project=P, ml=ml | {"positive": "North"})
    r = ended("side", "latest_run", abi.call("train_model", project=P, model="side")["run"])
    assert r["status"] == "ok", r["error"]
    score("side")
    assert {p["column"] for p in otype("cafe")["properties"] if p["column"].startswith("ml_side.")} == {"ml_side.probability"}
    sure("delete_ml_model", project=P, model="side")
    assert not [p for p in otype("cafe")["properties"] if p["column"].startswith("ml_side.")]
