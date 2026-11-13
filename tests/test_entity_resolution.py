"""1.2.6 M11 (needs the engine at :8780, no LLM): entity resolution. An object type declares `resolve` (exact columns,
fuzzy columns with thresholds, a block column); find_same proposes groups with each pair's evidence and merges nothing;
decide_same is a person's confirmed decision (same, different, split), one undoable write-back batch in the hidden
input table same_as_<table>; after it queries, measures, object reads and people's SQL read the group once under one
stable ID, while the grid keeps the source rows. A restricted Viewer's group is made of the records they see only.
The owner's sql_query is the reference for the expected figures.

P's customers (region is the row rule's column: the Viewer sees North; the 9xx keys and 77 are South):
 101 / 902  one company in two systems, the same VAT number written differently        (exact)
 103 / 104  a bakery with a typo in its name, same address                             (fuzzy)
 905 / 906  the same name and address, two VAT numbers                                 (never proposed)
 107, 108   nobody's duplicate (108 shares 101's postcode and first letters)
 109 / 110 / 112  one name written three ways, same address                            (fuzzy; reject, undo, split)
  77 / 114 / 115  one VAT number; the record with the first key (the group's ID) is hidden from the Viewer
 120 / 121  the same email                                                             (exact; the count's delta)
"""
import json
import re
import secrets
import time

import duckdb
import pytest
from conftest import ROOT, drop, parquet
from test_engine import engine
from test_pages import sure

import abi

P, BIG, KEYS = "pi_er", "pi_er_big", "pi_er_keys"
S = "same_as_customers"
CUSTOMERS = """customer_id,name,vat,email,phone,postcode,address,region,source
101,Acme GmbH,DE 123 456 789,sales@acme.example,+49 30 1234567,10115,Hauptstr. 1,North,crm
902,ACME Gmbh,de123456789,,030 1234567,10115,Hauptstrasse 1,South,erp
103,Bäckerei Müller,,info@mueller.example,,20095,Marktplatz 5,North,crm
104,Bäckerei Müler,,,,20095,Marktplatz 5,North,erp
905,Nordwind AG,DE999000111,a@nordwind.example,,80331,Ring 2,South,crm
906,Nordwind AG,DE888000222,b@nordwind.example,,80331,Ring 2,South,erp
107,Solo Ltd,,,,50667,Domplatz 1,North,crm
108,Acme Holding,,,,10115,Lindenweg 9,North,crm
109,Kaffee Kontor,,,,28195,Am Wall 3,North,crm
110,Kaffee Kontor GmbH,,,,28195,Am Wall 3,North,erp
112,Kaffee-Kontor,,,,28195,Am Wall 3,North,web
77,Hansa Kontor,HH-555-1,,,20457,Kai 1,South,crm
114,Hansa Kontor Nord,HH5551,,,20457,Kai 1,North,erp
115,Hansa Kontor N.,hh 555 1,,,20457,Kai 1,North,web
120,Tee Haus,,Hello@TeeHaus.example,,60311,Zeil 10,North,crm
121,Teehaus Frankfurt,,hello@teehaus.example,,60311,Zeil 12,North,erp
"""
ORDERS = """order_id,customer_id,amount
1,101,100
2,101,50
3,902,30
4,103,10
5,104,20
6,905,5
7,107,7
8,906,1
9,109,9
10,110,11
11,112,13
12,77,40
13,114,41
14,115,42
15,120,15
16,121,16
17,108,8
"""
RESOLVE = {"exact": [{"column": "vat"}, {"column": "email", "as": "email"}, {"column": "phone", "as": "phone"}],
           "fuzzy": [{"column": "name", "threshold": 0.9}, {"column": "address", "threshold": 0.85}],
           "block": ["postcode"], "source": "source"}
ONTO = {
    "objects": [
        {"id": "customer", "table": "customers", "name": "Customer", "plural": "Customers", "title": "name",
         "properties": [{"column": "vat", "name": "VAT number"}, {"column": "region", "meaning": "category"}],
         "measures": ["revenue"], "resolve": RESOLVE},
        {"id": "order", "table": "orders", "name": "Order", "plural": "Orders", "title": "order_id"},
    ],
    "links": [{"id": "order_customer", "from": "order", "to": "customer", "name": "customer", "inverse": "orders", "via": ["orders-customer_id"]}],
}
# an agent's call (via "agent"), with no token and with a made-up one: ABI answers 409 both times and changes nothing
AGENT = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, security
a = json.loads(sys.stdin.read())
with db.connect() as c:
    u = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
out = []
for confirm in (None, "9999999999.made-up"):
    try:
        actions.run("decide_same", a, u, "agent", confirm)
        out.append("ran")
    except actions.ActionFailed as e:
        out.append([e.status, "confirm_token" in e.body])
print(json.dumps(out))
"""


# an agent's undo of a decision: ABI's question, in the person's words
UNDO = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, security
a = json.loads(sys.stdin.read())
with db.connect() as c:
    u = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
try:
    actions.run("undo_edits", a, u, "agent")
    print(json.dumps("ran"))
except actions.ActionFailed as e:
    print(json.dumps([e.status, "confirm_token" in e.body, e.body["message"]]))
"""
# the worker builds the query copies too; built here so the first timing reads them
COPIES = r"""
import sys, time
from abi import semantic, storage
P = sys.stdin.read().strip()
for i in range(5):
    try:
        semantic.build_copies(P)
        break
    except Exception:
        if i == 4:
            raise
        time.sleep(2)
print(" ".join(sorted(semantic._columns_of(storage.lake(P).cursor(), "query"))))
"""
# the decisions log read whole and through a small window, and what an undo of the oldest decision would say
WINDOWED = r"""
import json, sys
import abi.app  # noqa: F401
from abi import db, security
from abi.semantic import same
a = json.loads(sys.stdin.read())
full, cut = same._log(a["project"], a["table"], None), same._log(a["project"], a["table"], a["window"])
with db.connect() as c:
    u = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
    words = same.undo_words(c, u, a["project"], a["table"], full[-1]["batch"])
show = lambda log: [[d["batch"], sorted(d["rows"])] for d in log]
print(json.dumps({"full": show(full), "cut": show(cut), "oldest": words, "default": same.WINDOW}))
"""
CONTACTS = """contact_id,name,phone
1,Kim,12345678
2,Kim A,+45 12345678
3,Lou,030 123456
4,Lou B,+49 (0)30 123456
5,Max,(212) 555-0199
6,Mia,(312) 555-0199
"""
PEOPLE = """person_id,name,phone,email,vat
1,Ann Lee,(212) 555-0123,,
2,Bob Ray,(312) 555-0123,,
3,Cy Dole,+1 212 555 0123,,
4,Dee Fox,,d@x.example,DE111
5,Dee Foxx,,d@x.example,DE222
6,Eve Hall,0049 30 1234567,,
7,Eva Hull,030 1234567,,
"""


def call(action: str, **args):
    return abi.call(action, project=P, **args)


def sql(q: str) -> list:
    return call("sql_query", sql=q)["rows"]


def found(**args) -> tuple[dict, list]:
    """find_same's answer, and its groups as sorted lists of keys."""
    r = call("find_same", type="customer", **args)
    return r, sorted(sorted(o["key"] for o in g["objects"]) for g in r["groups"])


def decide(decision: str, *keys) -> dict:
    return sure("decide_same", project=P, type="customer", keys=list(keys), decision=decision)


def one(*keys) -> None:
    """These records are one customer (the person confirms); fine when they already are."""
    try:
        decide("same", *keys)
    except abi.ActionError as e:
        if e.status != 409 or "already one" not in e.body["message"]:
            raise


def stored(token: str | None = None) -> list:
    """The same-as table's rows (a, b, same) as its grid shows them to the owner, or to `token`'s person."""
    with abi.as_user(token):
        rows = abi._open("GET", f"/api/projects/{P}/tables/{S}/rows?offset=0&limit=1000")[1]["rows"]
    return sorted((int(a), int(b), yes == "true") for a, b, yes in rows)


def newest() -> str:
    """The batch of the newest decision."""
    return found(limit=1)[0]["decisions"][0]["batch"]


def refused(batch: str, table: str = S) -> str:
    """Undoing `batch` is refused (409) and nothing changes: ABI's message."""
    was = stored() if table == S else None
    with pytest.raises(abi.ActionError) as e:
        call("undo_edits", table=table, batch=batch)
    assert e.value.status == 409 and (was is None or stored() == was), e.value.body
    return e.value.body["message"]


def flow_fails(action: str, args: dict, why: str) -> None:
    """A flow whose one step runs `action` fails with `why`."""
    sure("save_flow", project=P, flow={"id": "sneak", "name": "Sneak", "enabled": False, "trigger": {"type": "manual"},
                                       "steps": [{"id": "s", "type": "action", "action": action, "input": args}]})
    run, end = call("run_flow", flow="sneak")["run"], time.monotonic() + 300
    while (x := call("flow_runs", run=run)["runs"][0])["status"] in ("queued", "running"):
        assert time.monotonic() < end, x
        time.sleep(1)
    sure("delete_flow", project=P, flow="sneak")
    assert x["status"] == "failed" and why in json.dumps(x), x


def by(measure: str, dim: str = "customers.customer_id", **q) -> dict:
    return {r[0]: r[1] for r in call("semantic_query", measures=[measure], dimensions=[dim], **q)["rows"]}


def total(measure: str) -> float:
    return call("semantic_query", measures=[measure])["rows"][0][0]


def keys_of(rows: dict) -> list:
    return sorted(rows["rows"][i][rows["columns"].index("customer_id")] for i in range(len(rows["rows"])))


@pytest.fixture(scope="module")
def _er(tmp_path_factory):
    """P: the customers and orders above, a North-region Viewer (a row rule on customers.region), the measures and the
    ontology with `resolve`."""
    d = tmp_path_factory.mktemp("er")
    (d / "customers.csv").write_text(CUSTOMERS)
    (d / "orders.csv").write_text(ORDERS)
    if P in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([P])
    abi.load("demo", P, parquet(d / "customers.csv", d / "orders.csv"))
    name, pw = f"pi_er_viewer_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    try:
        for t, k in (("customers", "customer_id"), ("orders", "order_id")):
            call("set_table", table=t, key=[k])
        call("add_relationship", **{"from": "orders.customer_id", "to": "customers.customer_id"})
        for i, x in {"revenue": "SUM(orders.amount)", "customers_n": "COUNT(customers.customer_id)",
                     "buyers": "DISTINCT_COUNT(orders.customer_id)"}.items():
            call("set_measure", id=i, name=i, expression=x)
        abi.call("create_user", name=name, password=pw)
        abi.call("set_role", project=P, user=name, role="viewer")
        uid = {u["name"]: u["id"] for u in call("rls_rules")["accounts"]}[name]
        sure("set_rls_rule", project=P, id="region", table="customers", column="region", users={uid: ["North"]})
        sure("save_ontology", project=P, ontology=ONTO)
        yield {"token": abi.login(name, pw)[1]["token"], "name": name, "pw": pw}
    finally:
        sure("delete_user", name=name)
        drop([P])


@pytest.fixture
def er(_er):
    """P with nothing merged: every decision an earlier test left is undone first, so a test runs alone or in any order."""
    try:
        h = call("edit_history", table=S, limit=1000)
    except abi.ActionError:  # no decision yet: no table
        return _er
    at = h["columns"].index
    for b in dict.fromkeys(r[at("batch_id")] for r in h["entries"] if r[at("status")] == "active"):
        call("undo_edits", table=S, batch=b)
    return _er


def mine(er, action: str, **args):
    with abi.as_user(er["token"]):
        return call(action, **args)


# ---------- #35, #37: an exact key proposes, a person's yes merges ----------

def test_er_exact_key_merges(er):
    """The same VAT number, written differently in two systems, proposes the two records as one customer, with the
    evidence in the type's words. Nothing is merged by the proposal. The person's confirmed decision stores the group in
    the same-as table (a hidden input table with a relationship from each end onto the customer, in the project's
    files) under one stable ID, the first record's key; the customer then lists once, the merged record's key opens the
    customer it became, and the pair is no longer proposed. Wrong `resolve` references are refused with file, field and fix."""
    r, groups = found()
    assert ["101", "902"] in groups and ["120", "121"] in groups, groups
    assert ["905", "906"] not in groups, "the same name and address under two VAT numbers is never proposed"
    assert not [g for g in groups if "107" in g or "108" in g], groups
    [g] = [g for g in r["groups"] if {o["key"] for o in g["objects"]} == {"101", "902"}]
    assert {o["title"] for o in g["objects"]} == {"Acme GmbH", "ACME Gmbh"}
    assert g["pairs"][0]["why"][:2] == ["same VAT number", "same phone"], g["pairs"]
    [mail] = [g for g in r["groups"] if {o["key"] for o in g["objects"]} == {"120", "121"}]
    assert mail["pairs"][0]["why"][0] == "same email"
    assert ["Acme GmbH", "ACME Gmbh"] in [row[1:3] for row in r["rows"]], "names and evidence are quotable rows"
    # a proposal merges nothing, and the decision is never taken without the person's yes
    assert S not in call("semantic_model", all=True)["tables"] and len(call("list_objects", type="customer")["rows"]) == 16
    with pytest.raises(abi.ActionError) as e:
        call("decide_same", type="customer", keys=[101, 902], decision="same")
    assert e.value.status == 409 and e.value.body["confirm_token"], e.value.body
    said = e.value.body["message"]
    assert "Treat these 2 customers as the same one" in said and "“Acme GmbH”" in said and "“ACME Gmbh”" in said and "You can undo this." in said
    assert not re.search(r"same_as|customer_id|\b101\b|\b902\b", said), "business words only"
    assert len(call("list_objects", type="customer")["rows"]) == 16
    done = decide("same", 101, 902)
    assert done["id"] == "101" and done["table"] == S and re.fullmatch(r"[0-9a-f]{32}", done["batch"])
    assert stored() == [(101, 902, True)]
    with pytest.raises(abi.ActionError) as e:  # hidden (staging): not a table of the model, so no SQL reads it
        sql(f"SELECT * FROM {S}")
    assert e.value.status == 403
    # stored as the project's own files: an input table, hidden, keyed by the pair, related to the customer from both ends
    files = abi.ABI_PROJECTS / P
    t = json.loads((files / f"tables/{S}.json").read_text())
    assert t["key"] == ["a", "b"] and t["steps"] == [] and [c["name"] for c in t["columns"]] == ["a", "b", "same"]
    m = json.loads((files / "model/model.json").read_text())
    assert next(x for x in m["tables"] if x["table"] == S)["hidden"] is True
    assert sorted((x["from"]["columns"], x["to"]) for x in m["relationships"] if x["from"]["table"] == S) == [
        (["a"], {"table": "customers", "columns": ["customer_id"]}), (["b"], {"table": "customers", "columns": ["customer_id"]})]
    assert S not in call("semantic_model")["tables"], "hidden: not part of the semantic model"
    # one customer now, under the first record's key; the other key opens it too
    listed = keys_of(call("list_objects", type="customer", limit=1000))
    assert 101 in listed and 902 not in listed and len(listed) == 15
    got = call("get_object", type="customer", key=[902])
    assert got["key"] == [101] and got["title"] == "Acme GmbH"
    assert ["101", "902"] not in found()[1]
    one_ = call("find_same", type="customer", key=101)
    one_["decisions"] = [d for d in one_["decisions"] if not d["undone"]]  # (an earlier test's, undone by the fixture)
    assert [(x["key"], x["title"], x["source"]) for x in one_["records"]] == [("101", "Acme GmbH", "crm"), ("902", "ACME Gmbh", "erp")]
    assert [(d["decision"], [o["title"] for o in d["objects"]], d["undone"]) for d in one_["decisions"]] == [("same", ["Acme GmbH", "ACME Gmbh"], False)]
    # the grid keeps the source rows
    grid = abi._open("GET", f"/api/projects/{P}/tables/customers/rows?offset=0&limit=100")[1]
    assert grid["total"] == 16 and len(grid["rows"]) == 16
    # `resolve` is checked like the rest of the ontology: schema, then references, each with file, field and fix
    bad = lambda rs: {**ONTO, "objects": [{**ONTO["objects"][0], "resolve": rs}, ONTO["objects"][1]]}  # noqa: E731
    with pytest.raises(abi.ActionError) as e:
        call("save_ontology", ontology=bad({**RESOLVE, "fuzzy": [{"column": "nam", "threshold": 0.9}]}))
    [err] = e.value.body["errors"]
    assert e.value.status == 422 and (err["file"], err["path"]) == ("model/ontology.json", "$.objects[0].resolve.fuzzy[0].column")
    assert "has no column 'nam'" in err["error"] and "Use one of:" in err["fix"]
    for wrong in ({"block": ["postcode"]}, {**RESOLVE, "fuzzy": [{"column": "name"}]}, {**RESOLVE, "exact": [{"column": "vat", "as": "iban"}]}):
        with pytest.raises(abi.ActionError) as e:
            call("save_ontology", ontology=bad(wrong))
        assert e.value.status == 422 and any("resolve" in str(x.get("path")) for x in e.value.body["errors"]), e.value.body
    assert json.loads((files / "model/ontology.json").read_text())["objects"][0]["resolve"] == RESOLVE, "nothing of that was saved"


def test_er_fuzzy_needs_approval(er):
    """A typo in the name with the same address is proposed with how alike each field is, and stays two customers until
    a person says yes: an agent's call gets ABI's question (409 with a token it never passes on) with or without a
    made-up token, and nothing changes. After the person's yes they are one."""
    r, groups = found()
    assert ["103", "104"] in groups and ["109", "110", "112"] in groups, groups
    [g] = [g for g in r["groups"] if {o["key"] for o in g["objects"]} == {"103", "104"}]
    [why] = [p["why"] for p in g["pairs"]]
    assert not [w for w in why if w.startswith("same ")] and len(why) == 2, why
    name, address = (re.fullmatch(rf"{lab} (\d+)% alike", w) for lab, w in zip(("name", "address"), why))
    assert 90 <= int(name[1]) < 100 and int(address[1]) == 100, why
    assert r["compared"] < 16 * 15 / 2, "only the pairs that share a block are scored"
    before = by("revenue")
    assert before[103] == 10 and before[104] == 20 and total("customers_n") == len(call("list_objects", type="customer")["rows"])
    ran = engine(AGENT, timeout=120, stdin=json.dumps({"project": P, "type": "customer", "keys": [103, 104], "decision": "same"}))
    assert ran.returncode == 0, ran.stderr[-2000:]
    assert json.loads(ran.stdout.splitlines()[-1]) == [[409, True], [409, True]]
    assert by("revenue") == before and ["103", "104"] in found()[1], "an agent's call alone changes nothing"
    assert decide("same", 103, 104)["id"] == "103"
    after = by("revenue")
    assert after[103] == 30 and 104 not in after
    assert call("get_object", type="customer", key=[103])["title"] == "Bäckerei Müller"
    # the harness keeps the token from the model for this action as for every other: nothing names it as an exception
    ts = (ROOT / ".pi/extensions/abi/index.ts").read_text()
    reads = set(re.findall(r'"(\w+)"', re.search(r"const READS = new Set\(\[(.*?)\]\);", ts, re.S)[1]))
    assert "find_same" in reads and "decide_same" not in reads, "Viewer mode proposes, never decides"
    import app
    assert "find_same" in app.READS and "decide_same" in app.WRITES and "decide_same" not in app.READS | app.READS_ONLY


# ---------- #38: everything reads the resolved customer ----------

def test_er_counts_once(er):
    """After two records are one customer, the count of customers and the distinct buyers drop by one, revenue is
    unchanged in total and adds up under the stable ID, by key and by name; the customer's page shows the orders of
    both records and its measure; a dataset (what ML trains on) and people's SQL read the same."""
    one(101, 902)
    n, buyers, revenue = total("customers_n"), total("buyers"), total("revenue")
    assert revenue == sql("SELECT sum(amount) FROM orders")[0][0] == 418
    assert n == len(call("list_objects", type="customer", limit=1000)["rows"]) == sql("SELECT count(*) FROM customers")[0][0]
    one(120, 121)
    assert (total("customers_n"), total("buyers"), total("revenue")) == (n - 1, buyers - 1, revenue), "one customer fewer, the same money"
    rev = by("revenue")
    assert rev[101] == 180 and 902 not in rev and rev[120] == 31 and 121 not in rev, rev
    names = by("revenue", "customers.name")
    assert names["Acme GmbH"] == 180 and "ACME Gmbh" not in names and names["Tee Haus"] == 31 and "Teehaus Frankfurt" not in names
    assert by("customers_n", "customers.region") == {r: c for r, c in sql("SELECT region, count(*) FROM customers GROUP BY 1")}
    assert sum(by("customers_n", "customers.region").values()) == n - 1
    got = call("get_object", type="customer", key=[101], links=["order_customer"])
    assert got["rows"][0][got["columns"].index("revenue")] == 180
    assert sorted(r[0] for r in got["links"][0]["rows"]) == [1, 2, 3], "the orders of both records"
    assert sql("SELECT customer_id, sum(amount) FROM orders WHERE customer_id IN (101, 902, 120, 121) GROUP BY 1 ORDER BY 1") == [[101, 180], [120, 31]]
    call("save_dataset", id="per_customer", name="Per customer", query={"measures": ["revenue"], "dimensions": ["customers.customer_id"]})
    data = call("dataset_data", dataset="per_customer")
    assert {r[0]: r[1] for r in data["rows"]} == rev, "a dataset has one row per resolved customer"
    # the source rows are still there: the grid, and the decisions say which records make the customer
    assert abi._open("GET", f"/api/projects/{P}/tables/orders/rows?offset=0&limit=100")[1]["total"] == 17
    assert [x["key"] for x in call("find_same", type="customer", key=120)["records"]] == ["120", "121"]


# ---------- #37: reject, split, undo; every decision in the history ----------

def test_er_reject_and_undo(er):
    """Three spellings of one name are proposed as one group. "Different" for two of them takes that pair out of the
    proposals until it is undone. A merge is undone as one step and both customers are back with their own numbers. A
    record joining a group keeps the group's ID; taking a record out makes it its own customer again, and when the
    record that carries the ID leaves, the rest go on under the first of them. Every decision is listed with who, when
    and what, undone ones too. Refusals say what to do next."""
    grp = lambda: [g for g in found()[1] if set(g) & {"109", "110", "112"}]  # noqa: E731
    old = {d["batch"] for d in found(limit=1)[0]["decisions"]}  # decisions of earlier tests (undone by the fixture)
    assert grp() == [["109", "110", "112"]]
    no = decide("different", 110, 112)
    assert grp() == [["109", "110"]], "112 is like 109 too, but never in one group with 110"
    assert not [p for g in found()[0]["groups"] for p in g["pairs"] if {p["a"], p["b"]} == {"110", "112"}], "the pair itself is gone"
    no2 = decide("different", 109, 110)
    assert grp() == [["109", "112"]]
    with pytest.raises(abi.ActionError) as e:
        decide("different", 109, 110)
    assert e.value.status == 409 and "marked as different already" in e.value.body["message"]
    [call("undo_edits", table=S, batch=x["batch"]) for x in (no2, no)]
    assert grp() == [["109", "110", "112"]], "undone: proposed again"
    # a merge and its undo
    before, n = by("revenue"), total("customers_n")
    yes = decide("same", 109, 110)
    assert yes["id"] == "109" and by("revenue")[109] == 20 and total("customers_n") == n - 1
    call("undo_edits", table=S, batch=yes["batch"])
    assert by("revenue") == before and total("customers_n") == n and call("get_object", type="customer", key=[110])["key"] == [110]
    # a third record joins through a merged one: the ID stays; all three are one
    decide("same", 109, 110)
    assert decide("same", 112, 110)["id"] == "109"
    assert by("revenue")[109] == 33 and total("customers_n") == n - 2
    assert [x["key"] for x in call("find_same", type="customer", key=109)["records"]] == ["109", "110", "112"]
    with pytest.raises(abi.ActionError) as e:
        decide("same", 110, 112)
    assert e.value.status == 409 and "already one customer" in e.value.body["message"]
    with pytest.raises(abi.ActionError) as e:
        decide("different", 109, 112)
    assert e.value.status == 409 and "'split' first" in e.value.body["message"]
    # split: one record out; then the one that carries the ID out
    out = decide("split", 112)
    assert by("revenue")[109] == 20 and by("revenue")[112] == 13 and grp() == [], "taken out, and not proposed again"
    with pytest.raises(abi.ActionError) as e:  # taking out says "not this one": merging it back needs that undone
        decide("same", 109, 112)
    assert e.value.status == 409 and "“Kaffee Kontor” and “Kaffee-Kontor” were marked as not the same customer" in e.value.body["message"]
    call("undo_edits", table=S, batch=out["batch"])
    decide("split", 109)
    rev = by("revenue")
    assert rev[109] == 9 and rev[110] == 24 and 112 not in rev, "110 and 112 go on as one, under the first of them"
    decide("split", 112)
    assert by("revenue") == before and total("customers_n") == n
    with pytest.raises(abi.ActionError) as e:
        decide("split", 107)
    assert e.value.status == 409 and "nothing to take out" in e.value.body["message"]
    with pytest.raises(abi.ActionError) as e:
        decide("same", 109, 4242)
    assert e.value.status == 404 and "use the keys find_same lists" in e.value.body["message"]
    # the history: every decision, newest first, with who and when; the undone ones say so
    hist = [d for d in found()[0]["decisions"] if {o["key"] for o in d["objects"]} & {"109", "110", "112"} and d["batch"] not in old]
    assert [d["decision"] for d in hist] == ["split", "split", "split", "same", "same", "same", "different", "different"]
    assert len({d["user"] for d in hist}) == 1 and hist[0]["user"] and all(re.match(r"\d{4}-\d\d-\d\d \d\d:", d["at"]) for d in hist)
    assert [d["undone"] for d in hist] == [False, False, True, False, False, True, True, True]
    assert {o["title"] for o in hist[-1]["objects"]} == {"Kaffee Kontor GmbH", "Kaffee-Kontor"}
    raw = call("edit_history", table=S, limit=1000)
    assert {r[raw["columns"].index("batch_id")] for r in raw["rows"]} >= {no["batch"], yes["batch"]}, "the same log write-back keeps"
    # a type without rules says what to add
    with pytest.raises(abi.ActionError) as e:
        call("find_same", type="order")
    assert e.value.status == 409 and "Add `resolve` to object type 'order'" in e.value.body["message"]


# ---------- #38: row-level security ----------

def test_er_rls_hides_source_rows(er):
    """The Viewer sees North. A customer merged from a North and a South record shows them the North record only: its
    name, its values, its orders, counted once; the South record's name, key, values and orders appear nowhere, and
    its key opens nothing. Where the record that carries the group's ID is the hidden one, the customer shows under the
    first record they see, never under the hidden key. Their read of the same-as table holds only pairs whose two
    records they see. Proposals, decisions and the history of decisions are for Editors: refused, not filtered."""
    theirs = lambda q: mine(er, "sql_query", sql=q)["rows"]  # noqa: E731
    vby = lambda m, d="customers.customer_id": {r[0]: r[1] for r in mine(er, "semantic_query", measures=[m], dimensions=[d])["rows"]}  # noqa: E731
    one(103, 104)
    was = vby("revenue")  # before this test's merges across the regions
    one(101, 902)
    one(77, 114, 115)
    assert call("get_object", type="customer", key=[114])["key"] == [77], "the owner: one customer under the first record's key"
    assert by("revenue")[77] == 123 and by("revenue")[101] == 180
    # North + South under the North record's key: the Viewer's figures are the North record's own
    rev = vby("revenue")
    assert rev[101] == 150 and 902 not in rev, "the South record's order (30) is not theirs"
    got = mine(er, "get_object", type="customer", key=[101], links=["order_customer"])
    row = dict(zip(got["columns"], got["rows"][0]))
    assert (got["title"], row["region"], row["vat"], row["revenue"]) == ("Acme GmbH", "North", "DE 123 456 789", 150)
    assert sorted(r[0] for r in got["links"][0]["rows"]) == [1, 2]
    # the group's ID is a hidden record's key: they see the group under their first record, with their records' orders
    assert 77 not in rev and 115 not in rev and rev[114] == 83, rev
    seen = mine(er, "list_objects", type="customer", limit=1000)
    listed = keys_of(seen)
    assert 114 in listed and not {77, 115, 902} & set(listed)
    text = json.dumps([seen, mine(er, "semantic_query", measures=["revenue", "customers_n"], dimensions=["customers.name", "customers.region", "customers.vat"]),
                       theirs("SELECT * FROM customers"), theirs("SELECT * FROM orders")], ensure_ascii=False)
    for hidden in ("ACME Gmbh", "de123456789", "HH-555-1", '"Hansa Kontor"', "South", r"\b77\b", r"\b902\b", "Nordwind", "Hauptstrasse"):
        assert not re.search(hidden, text), hidden
    assert mine(er, "get_object", type="customer", key=[115])["key"] == [114], "their own merged record opens their customer"
    for k in (77, 902):
        with pytest.raises(abi.ActionError) as e:
            mine(er, "get_object", type="customer", key=[k])
        assert e.value.status == 404
    # counted once among what they see; a merge never adds a row, a value or an order to what they saw before
    assert mine(er, "semantic_query", measures=["customers_n"])["rows"][0][0] == len(listed) == theirs("SELECT count(*) FROM customers")[0][0]
    assert {k: v for k, v in rev.items() if k not in (114,)} == {k: v for k, v in was.items() if k not in (114, 115)}
    assert rev[114] == was[114] + was[115] and sum(rev.values()) == sum(was.values())
    # the decisions table holds the customers' key values: a restricted Viewer reads no row of it, in the grid ...
    pairs = stored(er["token"])
    assert pairs == [] and (103, 104, True) in stored(), pairs
    assert {(77, 114, True), (77, 115, True), (101, 902, True)} <= set(stored()), "the owner reads them all"
    with pytest.raises(abi.ActionError) as e:
        theirs(f"SELECT * FROM {S}")
    assert e.value.status == 403
    hist = mine(er, "edit_history", table=S, limit=1000)["entries"]  # ... and in its history
    assert hist == [] and len(call("edit_history", table=S, limit=1000)["entries"]) >= 4, "no decision at all, about anyone"
    # the grid: their source rows, the merged ones too (115 is a row of its own there)
    grid = abi_rows(er, "customers")
    ids = sorted(int(r[[c["name"] for c in grid["columns"]].index("customer_id")]) for r in grid["rows"])
    assert ids == [101, 103, 104, 107, 108, 109, 110, 112, 114, 115, 120, 121] and grid["total"] == 12 and "South" not in json.dumps(grid)
    # fail closed: without its relationships onto the customer nothing filters the decisions table, so they read none of it
    for c in "ab":
        sure("remove_relationship", project=P, id=f"{S}-{c}")
    assert stored(er["token"]) == [] and len(stored()) >= 4 and vby("revenue")[104] == 20 and by("revenue")[902] == 30, "and nothing is merged without them"
    for c in "ab":
        call("add_relationship", **{"from": f"{S}.{c}", "to": "customers.customer_id"})
    assert stored(er["token"]) == [] and (103, 104, True) in stored() and vby("revenue") == rev
    # ... nor when the customers' key is hidden from Viewers (its values are this table's a and b), nor with one relationship gone
    sure("set_column_rule", project=P, id="no_key", table="customers", columns=["customer_id"])
    sure("remove_relationship", project=P, id=f"{S}-a")
    try:
        assert stored(er["token"]) == [] and mine(er, "edit_history", table=S, limit=1000)["entries"] == []
    finally:
        sure("delete_rls_rule", project=P, id="no_key")
        call("add_relationship", **{"from": f"{S}.a", "to": "customers.customer_id"})
    # the type's rules name columns: a restricted Viewer's `ontology` leaves them out
    assert "resolve" not in mine(er, "ontology")["objects"][0] and call("ontology")["objects"][0]["resolve"] == RESOLVE
    assert not [g for g in call("readiness")["missing"] if "same_as" in g], "the decisions table is no gap of the model"
    # proposing and deciding are an Editor's: a Viewer gets neither the list, the evidence nor the history
    for action, args in (("find_same", {}), ("find_same", {"key": 101}), ("decide_same", {"keys": [107, 108], "decision": "same"})):
        with pytest.raises(abi.ActionError) as e:
            mine(er, action, type="customer", **args)
        assert e.value.status == 403 and "needs the editor role" in e.value.body["message"], e.value.body


def abi_rows(er, table: str) -> dict:
    with abi.as_user(er["token"]):
        return abi._open("GET", f"/api/projects/{P}/tables/{table}/rows?offset=0&limit=100")[1]


# ---------- round 2: "not the same" holds, chains, the decisions table is decide_same's only ----------

def test_er_different_blocks_a_merge(er):
    """"Not the same" holds until it is undone: the two can't be merged, directly or through a record one of them was
    merged with since, and the refusal names both and says what to do. Once it is undone, they can."""
    no = decide("different", 109, 110)
    with pytest.raises(abi.ActionError) as e:
        decide("same", 109, 110)
    said = e.value.body["message"]
    assert e.value.status == 409 and "“Kaffee Kontor” and “Kaffee Kontor GmbH” were marked as not the same customer" in said
    assert "Undo that decision first (under Decided" in said and not re.search(r"same_as|\b109\b|\b110\b", said)
    assert decide("same", 110, 112)["id"] == "110", "110 and 112 were never marked as different"
    for keys in ((109, 110), (109, 112), (112, 109, 107)):  # through 110's group, whichever record is named
        with pytest.raises(abi.ActionError) as e:
            decide("same", *keys)
        assert e.value.status == 409 and "“Kaffee Kontor” and “Kaffee Kontor GmbH” were marked as not the same" in e.value.body["message"], keys
    assert not [g for g in found()[1] if "109" in g], "and no proposal puts them in one group"
    assert by("revenue")[109] == 9 and by("revenue")[110] == 24
    # undo goes newest first: the merge of 110 and 112 is about 110 too
    assert refused(no["batch"]) == "A newer decision builds on this one: undo “Kaffee Kontor GmbH and Kaffee-Kontor are the same customer” first."
    [call("undo_edits", table=S, batch=b) for b in (newest(), no["batch"])]
    assert decide("same", 109, 110, 112)["id"] == "109" and by("revenue")[109] == 33, "undone: they can be one customer"


def test_er_undo_under_a_newer_decision(er):
    """Merge 109 + 110, take 110 out, merge 110 + 112, undo the take-out: 110 is a record of 109 again and 112 a record
    of 110, so all three are one customer, as Decided lists them (two merges in force). The numbers, the page and the
    records agree; taking one out of the chain leaves the other two together."""
    n = total("customers_n")
    decide("same", 109, 110)
    out = decide("split", 110)
    assert decide("same", 110, 112)["id"] == "110" and by("revenue")[110] == 24 and by("revenue")[109] == 9
    # the take-out can't be undone under the newer merge of 110 (110 would be a record of 109 and the ID of its own group)
    assert refused(out["batch"]) == "A newer decision builds on this one: undo “Kaffee Kontor GmbH and Kaffee-Kontor are the same customer” first."
    [call("undo_edits", table=S, batch=b) for b in (newest(), out["batch"])]
    decide("same", 110, 112)
    assert {(a, b) for a, b, yes in stored() if yes} == {(109, 110), (109, 112)}, "in order: both are records of 109"
    rev = by("revenue")
    assert rev[109] == 33 and 110 not in rev and 112 not in rev and total("customers_n") == n - 2
    assert call("get_object", type="customer", key=[112])["key"] == [109]
    one_ = call("find_same", type="customer", key=112)
    assert [x["key"] for x in one_["records"]] == ["109", "110", "112"]
    assert [(d["decision"], d["undone"]) for d in one_["decisions"] if not d["undone"]] == [("same", False), ("same", False)]
    assert sorted(r[0] for r in call("get_object", type="customer", key=[109], links=["order_customer"])["links"][0]["rows"]) == [9, 10, 11]
    decide("split", 112)
    assert by("revenue")[109] == 20 and by("revenue")[112] == 13 and total("customers_n") == n - 1


def test_er_only_decide_same_writes_decisions(er):
    """The decisions table is changed by decide_same only. edit_cells and propose_edit refuse it and say what to use; a
    flow's step can neither merge by writing it nor take a decision back, and nothing changes; an agent's undo asks the
    person in business words. The person's own undo is one click. A confirmation is for the change it showed: after
    another decision landed, the old token asks again."""
    yes = decide("same", 120, 121)
    was = stored()
    row = {"table": S, "add": [{"a": 107, "b": 108, "same": True}]}
    for action in ("edit_cells", "propose_edit"):
        with pytest.raises(abi.ActionError) as e:
            call(action, **row)
        assert e.value.status == 409 and "Decide with decide_same" in e.value.body["message"] and "Possible duplicates" in e.value.body["message"]
    for i, (action, args, why) in enumerate((("edit_cells", row, "Decide with decide_same"),
                                             ("undo_edits", {"table": S, "batch": yes["batch"]}, "a flow can't take a decision back"))):
        sure("save_flow", project=P, flow={"id": f"sneak{i}", "name": f"Sneak {i}", "enabled": False, "trigger": {"type": "manual"},
                                           "steps": [{"id": "s", "type": "action", "action": action, "input": args}]})
        run, end = call("run_flow", flow=f"sneak{i}")["run"], time.monotonic() + 300
        while (x := call("flow_runs", run=run)["runs"][0])["status"] in ("queued", "running"):
            assert time.monotonic() < end, x
            time.sleep(1)
        assert x["status"] == "failed" and why in json.dumps(x), x
        sure("delete_flow", project=P, flow=f"sneak{i}")
    assert stored() == was and by("revenue")[120] == 31 and 108 in by("revenue"), "nothing changed"
    r = engine(UNDO, timeout=120, stdin=json.dumps({"project": P, "table": S, "batch": yes["batch"]}))
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == [409, True, "Undo: Tee Haus and Teehaus Frankfurt are the same customer. Confirm to proceed."]
    assert stored() == was
    # the harness replays an agent's call over REST with X-Abi-Confirm: ask. That is no person's click either: ABI asks
    with pytest.raises(abi.ActionError) as e:
        abi.call("undo_edits", confirm="ask", project=P, table=S, batch=yes["batch"])
    assert e.value.status == 409 and e.value.body["confirm_token"] and stored() == was
    assert e.value.body["message"] == "Undo: Tee Haus and Teehaus Frankfurt are the same customer. Confirm to proceed."
    edit = call("edit_cells", table="customers", edits=[{"key": [107], "column": "address", "value": "Domplatz 2"}])
    with pytest.raises(abi.ActionError) as e:  # any table's undo, replayed that way
        abi.call("undo_edits", confirm="ask", project=P, table="customers", batch=edit["batch"])
    assert e.value.status == 409 and e.value.body["confirm_token"]
    call("undo_edits", table="customers", batch=edit["batch"])
    # the token signs the rows the decision changes
    with pytest.raises(abi.ActionError) as e:
        call("decide_same", type="customer", keys=[103, 104, 120], decision="same")
    token = e.value.body["confirm_token"]
    call("undo_edits", table=S, batch=yes["batch"])  # the person's own undo: no question
    assert stored() == [] and by("revenue")[121] == 16
    with pytest.raises(abi.ActionError) as e:
        abi.call("decide_same", confirm=token, project=P, type="customer", keys=[103, 104, 120], decision="same")
    assert e.value.status == 409 and e.value.body["confirm_token"] != token and stored() == [], "what it showed is no longer what it would do"


def test_er_decisions_come_from_decide_same_only(er, tmp_path):
    """Nothing but decide_same puts a row into a decisions table. No load and no steps fill the one of a type with
    `resolve`, and an input table can't take its name. A table of that shape under a type without `resolve` (columns
    a, b, same in any order) takes no write-back either, so no row waits there to become a decision; and a table of
    that name with another shape, which does take edits, keeps `resolve` from being saved until it is deleted."""
    decide("same", 120, 121)
    was = stored()
    for action, args in (("set_steps", {"table": S, "steps": [{"type": "source", "source": "customers"}]}),
                         ("add_data", {"table": S, "location": str(tmp_path / "x.parquet")}),
                         ("create_input_table", {"table": S, "columns": [{"name": "a", "type": "BIGINT"}], "key": ["a"]})):
        with pytest.raises(abi.ActionError) as e:
            call(action, **args)
        assert e.value.status == 409 and "decisions about which Customers are the same one" in e.value.body["message"], (action, e.value.body)
    assert stored() == was and by("revenue")[120] == 31
    # under the order type, which has no `resolve`: the same shape, its columns in another order
    t, row = "same_as_orders", {"a": 1, "b": 2, "same": True}
    call("create_input_table", table="same_as_things", key=["b", "a"], columns=[{"name": c, "type": ty} for c, ty in (("b", "BIGINT"), ("a", "BIGINT"), ("same", "BOOLEAN"))])
    try:
        for action in ("edit_cells", "propose_edit"):
            with pytest.raises(abi.ActionError) as e:
                call(action, table="same_as_things", add=[row])
            assert e.value.status == 409 and "Decide with decide_same" in e.value.body["message"]
        r = engine("from abi.semantic import rls\nprint(rls.same_as('same_as_orders', ['b', 'a', 'same']), rls.same_as('same_as_orders', ['a', 'b', 'same', 'note']),"
                   " rls.same_as('orders', ['a', 'b', 'same']))")
        assert r.returncode == 0 and r.stdout.split()[-3:] == ["True", "False", "False"], r.stderr[-2000:]
    finally:
        sure("delete_table", project=P, table="same_as_things")
    with_resolve = {**ONTO, "objects": [ONTO["objects"][0], {**ONTO["objects"][1], "resolve": {"exact": [{"column": "customer_id"}]}}]}
    # the name taken by a table with one column more: it takes edits by hand, so `resolve` can't be added over it
    call("create_input_table", table=t, key=["a", "b"], columns=[{"name": c, "type": ty} for c, ty in (
        ("a", "BIGINT"), ("b", "BIGINT"), ("same", "BOOLEAN"), ("note", "VARCHAR"))])
    try:
        # the name is reserved whatever the columns: by hand, as a flow's step (which would be in force with no click)
        # and through a proposal, nothing gets a row into it; the comment a caller writes proves nothing
        hand = {"table": t, "add": [row | {"note": "by hand"}], "comment": "same"}
        with pytest.raises(abi.ActionError) as e:
            call("edit_cells", **hand)
        assert e.value.status == 409 and "Tables named same_as_… are kept for decisions" in e.value.body["message"] and "rename the table" in e.value.body["message"]
        assert "Possible duplicates" not in e.value.body["message"]
        flow_fails("edit_cells", hand, "Tables named same_as_")
        with pytest.raises(abi.ActionError) as e:
            call("approve_edit", table=t, batch="0" * 32)
        assert e.value.status == 409 and "Tables named same_as_" in e.value.body["message"]
        assert abi._open("GET", f"/api/projects/{P}/tables/{t}/rows?offset=0&limit=10")[1]["total"] == 0
        with pytest.raises(abi.ActionError) as e:
            call("save_ontology", ontology=with_resolve)
        [err] = [x for x in e.value.body["errors"] if x["path"] == "$.objects[1].resolve"]
        assert e.value.status == 422 and "is no decisions table" in err["error"] and f"Delete table '{t}' first" in err["fix"], err
        assert len(call("list_objects", type="order", limit=1000)["rows"]) == 17, "and no order was merged"
    finally:
        sure("delete_table", project=P, table=t)


def test_er_undo_of_an_older_decision(er):
    """Undoing an older decision either does what it says or is refused with the newer decision to undo first, in the
    person's words; never "undone" with nothing changed. (A) two groups merged into one: undoing one of the first
    merges takes that record out again. (B) after the record that carried the ID was taken out, the first merge can't
    be undone before the take-out. (C) a merge can't be undone under a take-out from it; in order, both go, and no
    notification about the decisions table is ever posted."""
    decide("same", 103, 104)
    cd = decide("same", 109, 110)
    assert decide("same", 103, 109)["id"] == "103" and by("revenue")[103] == 50
    assert refused(cd["batch"]) == "A newer decision builds on this one: undo “Bäckerei Müller and Kaffee Kontor are the same customer” first."
    [call("undo_edits", table=S, batch=b) for b in (newest(), cd["batch"])]
    rev = by("revenue")
    assert rev[103] == 30 and rev[110] == 11 and rev[109] == 9, "(A) in order: the group merge, then 109 and 110"
    # (B)
    first = decide("same", 120, 121)
    decide("same", 120, 107)
    out = decide("split", 120)
    was = stored()
    with pytest.raises(abi.ActionError) as e:
        call("undo_edits", table=S, batch=first["batch"])
    said = e.value.body["message"]
    assert e.value.status == 409 and said == "A newer decision builds on this one: undo “Tee Haus taken out of Solo Ltd” first.", said
    assert stored() == was and by("revenue")[107] == 23 and by("revenue")[120] == 15
    [d] = [d for d in found()[0]["decisions"] if d["batch"] == out["batch"]]
    assert ([o["title"] for o in d["out"]], d["of"]["title"], d["undone"]) == (["Tee Haus"], "Solo Ltd", False)
    # (C)
    merge = decide("same", 101, 902)
    take = decide("split", 902)
    with pytest.raises(abi.ActionError) as e:
        call("undo_edits", table=S, batch=merge["batch"])
    assert e.value.status == 409 and e.value.body["message"] == "A newer decision builds on this one: undo “ACME Gmbh taken out of Acme GmbH” first."
    assert (101, 902, False) in stored(), "the take-out stands: still not the same"
    call("undo_edits", table=S, batch=take["batch"])
    assert by("revenue")[101] == 180
    call("undo_edits", table=S, batch=merge["batch"])
    assert by("revenue")[101] == 150 and not [r for r in stored() if 902 in r[:2]]
    assert "same_as" not in json.dumps(call("inbox")), "no feed line about the decisions table"


def test_er_undo_goes_newest_first(er):
    """One rule: a decision is undone only when no newer decision in force is about its records or the customers they
    are in now; the refusal names the newest such decision, so following it always ends. (1) two groups merged, then a
    record taken out (which rewrites the group): the group merge can't be undone before the take-out. (2) a record
    taken out and then merged elsewhere, as a record of another customer or as the one others are records of: the
    take-out can't be undone before that merge. Decided marks such a decision as blocked. No record ever has two
    customers above it."""
    one_parent = lambda: len({b for _, b, yes in stored() if yes}) == len([b for _, b, yes in stored() if yes])  # noqa: E731
    decide("same", 103, 104)
    decide("same", 109, 110)
    group = decide("same", 103, 109)
    take = decide("split", 104)
    assert by("revenue")[103] == 30 and by("revenue")[104] == 20 and one_parent()
    assert refused(group["batch"]) == "A newer decision builds on this one: undo “Bäckerei Müler taken out of Bäckerei Müller” first."
    [d] = [d for d in found()[0]["decisions"] if d["batch"] == group["batch"]]
    assert d["blocked"] is True and not d["undone"], "in force, and not to be undone yet"
    [call("undo_edits", table=S, batch=b) for b in (take["batch"], group["batch"])]
    rev = by("revenue")
    assert rev[103] == 30 and rev[109] == 20 and 104 not in rev and 110 not in rev and one_parent(), "in order: the two first groups again"
    # (2) 121 is taken out of 120, then becomes a record of 107
    decide("same", 120, 121)
    out = decide("split", 121)
    assert decide("same", 121, 107)["id"] == "107"
    assert refused(out["batch"]) == "A newer decision builds on this one: undo “Solo Ltd and Teehaus Frankfurt are the same customer” first."
    assert one_parent() and by("revenue")[107] == 23 and by("revenue")[120] == 15
    call("undo_edits", table=S, batch=newest())
    # ... or the one a South customer is a record of
    assert decide("same", 121, 905)["id"] == "121"
    assert refused(out["batch"]) == "A newer decision builds on this one: undo “Teehaus Frankfurt and Nordwind AG are the same customer” first."
    [call("undo_edits", table=S, batch=b) for b in (newest(), out["batch"])]
    assert by("revenue")[120] == 31 and by("revenue")[905] == 5 and one_parent()
    with pytest.raises(abi.ActionError) as e:  # a key that isn't one of the column's values is a key no customer has
        decide("same", 120, "abc")
    assert e.value.status == 404 and "No customer with key abc" in e.value.body["message"]


def test_er_undo_stays_a_persons_without_resolve(er):
    """With `resolve` taken off the type, its decisions table is still kept for decisions: a flow's step can't undo a
    take-out (which would merge again once `resolve` is back), a replayed agent call is asked, and nothing writes it."""
    decide("same", 120, 121)
    out = decide("split", 121)
    was = stored()
    sure("save_ontology", project=P, ontology={**ONTO, "objects": [{k: v for k, v in ONTO["objects"][0].items() if k != "resolve"}, ONTO["objects"][1]]})
    try:
        flow_fails("undo_edits", {"table": S, "batch": out["batch"]}, "a flow can't take a decision back")
        with pytest.raises(abi.ActionError) as e:
            abi.call("undo_edits", confirm="ask", project=P, table=S, batch=out["batch"])
        assert e.value.status == 409 and e.value.body["confirm_token"]
        assert e.value.body["message"] == "Undo: a decision about which records are the same one. Confirm to proceed."
        with pytest.raises(abi.ActionError) as yes:  # M10: asked first, in the person's words; after their yes still refused, with what to do
            abi.call("undo_edits", confirm=e.value.body["confirm_token"], project=P, table=S, batch=out["batch"])
        assert yes.value.status == 409 and "confirm_token" not in yes.value.body
        assert yes.value.body["message"].startswith("These decisions belong to a type whose matching rules were removed: put the rules back first")
        with pytest.raises(abi.ActionError) as e:
            call("edit_cells", table=S, add=[{"a": 107, "b": 108, "same": True}], comment="same")
        assert e.value.status == 409 and "Tables named same_as_" in e.value.body["message"]
        assert stored() == was
    finally:
        sure("save_ontology", project=P, ontology=ONTO)
    assert stored() == was and by("revenue")[120] == 15 and by("revenue")[121] == 16, "back with `resolve`: still taken out"


def test_er_undo_fails_closed(er):
    """An undo that can't be checked against the newer decisions is not done. (1) With `resolve` taken off the type
    (through import_project) the person's own undo of a take-out is refused with what to do, and nothing changes; with
    the rules back it works by the normal rule. A table of that name that never held a decision keeps the ordinary
    undo. (2) Decided reads a window of the log and never half a decision; an undo reads all of it, so the oldest
    decision is still named and still waits for the newer one."""
    import io
    import zipfile

    import transfer

    decide("same", 120, 121)
    out = decide("split", 121)
    was = stored()
    z = zipfile.ZipFile(io.BytesIO(transfer.export(P)))
    bundle = {n.removeprefix("abi/"): json.loads(z.read(n)) if n.endswith(".json") else z.read(n).decode() for n in z.namelist() if n.startswith("abi/")}
    rules = bundle["model/ontology.json"]
    assert rules["objects"][0]["resolve"] == RESOLVE
    sure("import_project", project=P, bundle={**bundle, "model/ontology.json": {**rules, "objects": [
        {k: v for k, v in x.items() if k != "resolve"} for x in rules["objects"]]}})
    try:
        assert "resolve" not in call("ontology")["objects"][0]
        said = refused(out["batch"])
        assert said.startswith("These decisions belong to a type whose matching rules were removed: put the rules back first, then undo.")
        assert "`resolve` on the object type over table 'customers' in model/ontology.json" in said
        call("create_input_table", table="same_as_notes", key=["id"], columns=[{"name": "id", "type": "BIGINT"}])
        with pytest.raises(abi.ActionError) as e:  # no decision was ever kept there: the ordinary undo answers
            call("undo_edits", table="same_as_notes", batch="0" * 32)
        assert e.value.status == 404 and "No applied edit batch" in e.value.body["message"]
        sure("delete_table", project=P, table="same_as_notes")
    finally:
        sure("import_project", project=P, bundle=bundle)
    assert stored() == was and by("revenue")[120] == 15, "the rules are back and the take-out still stands"
    call("undo_edits", table=S, batch=out["batch"])
    assert by("revenue")[120] == 31, "and now it is undone, by the normal rule"
    # (2) three more decisions, read through a window of 3 log rows (each has 2)
    decide("same", 103, 104)
    decide("same", 109, 110)
    decide("same", 103, 109)
    r = engine(WINDOWED, timeout=120, stdin=json.dumps({"project": P, "table": S, "window": 3}))
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.splitlines()[-1])
    assert got["default"] == 1000 and len(got["full"]) >= 5 and all(len(rows) == 1 for _, rows in got["full"][:3])
    assert got["cut"] == got["full"][:1], "the decision the window cuts in two is left out whole"
    oldest = got["full"][-1][0]
    assert oldest not in [b for b, _ in got["cut"]] and got["oldest"][0].endswith("the same customer") and got["oldest"][2] is None
    first = next(b for b, rows in got["full"] if rows == [["103", "104"]])
    assert refused(first) == "A newer decision builds on this one: undo “Bäckerei Müller and Kaffee Kontor are the same customer” first."


# another Editor's undo lands between an undo's check and its write
RACE = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, security
from abi.semantic import same
a = json.loads(sys.stdin.read())
with db.connect() as c:
    u = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
real, once = same.undo_words, []
def words(conn, user, project, t, batch):
    out = real(conn, user, project, t, batch)
    if not once:
        once.append(1)
        actions.run("undo_edits", {"project": project, "table": t, "batch": a["other"]}, u, "rest")
    return out
same.undo_words = words
try:
    actions.run("undo_edits", {"project": a["project"], "table": a["table"], "batch": a["batch"]}, u, "rest")
    print(json.dumps("ran"))
except actions.ActionFailed as e:
    print(json.dumps([e.status, e.body["message"]]))
"""


def test_er_undo_checks_again_inside_its_write(er):
    """M10 (round 5's LOWs): the newest-first rule is judged on the log an undo read; when the log changed before its
    write (two Editors at once) nothing is undone and the person is told to look again. What is no decision of the
    table's is refused by name, and a rejected proposal's rows never count as decisions."""
    first, other = decide("same", 103, 104), decide("same", 109, 110)
    was = stored()
    r = engine(RACE, timeout=120, stdin=json.dumps({"project": P, "table": S, "batch": first["batch"], "other": other["batch"]}))
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == [409, "Someone else decided about these records at the same moment, so nothing is undone."
                                                          " Look at Decided again, then undo."]
    rev = by("revenue")
    assert rev[103] == 30 and 104 not in rev, "the first decision stands"
    assert rev[109] == 9 and rev[110] == 11 and stored() != was, "the other Editor's undo landed"
    assert refused("0" * 32) == "This isn't a decision about customers that can be undone: take one of those listed under Decided."
    call("undo_edits", table=S, batch=first["batch"])
    assert by("revenue")[104] == 20


def test_er_phone_conflicting_keys_and_production(tmp_path):
    """Its own small project. Two phones are the same with and without the country code or the leading zeros, never
    when they differ in their first digits (the last 9 digits alone would say so). The same email under two VAT numbers
    is still proposed, after the clear groups, and its evidence says what differs. In a production environment the
    first decision is refused with the right next step (its table comes by a promote); once the table is there,
    decisions are made in production."""
    (tmp_path / "people.csv").write_text(PEOPLE)
    (tmp_path / "contacts.csv").write_text(CONTACTS)
    drop([x for x in (KEYS, KEYS + "_pub") if x in {x["project"] for x in abi.call("list_projects")["projects"]}])
    k = lambda action, **a: abi.call(action, project=KEYS, **a)  # noqa: E731
    try:
        abi.load("demo", KEYS, parquet(tmp_path / "people.csv", tmp_path / "contacts.csv"))
        k("set_table", table="people", key=["person_id"])
        k("set_table", table="contacts", key=["contact_id"])
        sure("save_ontology", project=KEYS, ontology={"objects": [{
            "id": "contact", "table": "contacts", "name": "Contact", "plural": "Contacts", "title": "name",
            "resolve": {"exact": [{"column": "phone", "as": "phone"}]}}, {
            "id": "person", "table": "people", "name": "Person", "plural": "People", "title": "name",
            "properties": [{"column": "vat", "name": "VAT number"}],
            "resolve": {"exact": [{"column": "phone", "as": "phone"}, {"column": "email", "as": "email"}, {"column": "vat"}]}}]})
        r = k("find_same", type="person")
        groups = [sorted(o["key"] for o in g["objects"]) for g in r["groups"]]
        assert groups == [["1", "3"], ["6", "7"], ["4", "5"]], "Bob's number differs in its area code; the doubtful group is last"
        assert [g["pairs"][0]["why"] for g in r["groups"]] == [["same phone"], ["same phone"], ["same email", "different VAT number"]]
        # short national numbers with and without the country code share a block (their last 7 digits) and are the same
        short = k("find_same", type="contact")
        assert [sorted(o["key"] for o in g["objects"]) for g in short["groups"]] == [["1", "2"], ["3", "4"]], short["groups"]
        assert {w for g in short["groups"] for p in g["pairs"] for w in p["why"]} == {"same phone"}
        # where a published version exists, the question says that it keeps showing them separately
        with pytest.raises(abi.ActionError) as e:
            k("decide_same", type="person", keys=[1, 3], decision="same")
        assert "published version" not in e.value.body["message"]
        abi.new_project("demo", KEYS + "_pub")
        with pytest.raises(abi.ActionError) as e:
            k("decide_same", type="person", keys=[1, 3], decision="same")
        assert e.value.status == 409 and "People reading the published version keep seeing them separately" in e.value.body["message"]
        sure("set_environment", project=KEYS, branch="main", production=True)
        with pytest.raises(abi.ActionError) as e:
            k("decide_same", type="person", keys=[1, 3], decision="same")
        assert e.value.status == 409 and "make the first decision in the development project and promote it" in e.value.body["message"]
        assert "confirm_token" not in e.value.body and len(k("list_objects", type="person")["rows"]) == 7
        sure("set_environment", project=KEYS, branch="main", production=False)
        assert sure("decide_same", project=KEYS, type="person", keys=[1, 3], decision="same")["id"] == "1"
        sure("set_environment", project=KEYS, branch="main", production=True)
        assert sure("decide_same", project=KEYS, type="person", keys=[6, 7], decision="same")["id"] == "6", "decisions are data: made in production"
        assert len(k("list_objects", type="person")["rows"]) == 5 and [g["pairs"][0]["why"] for g in k("find_same", type="person")["groups"]] == [
            ["same email", "different VAT number"]]
        with pytest.raises(abi.ActionError) as e:  # the design stays dev's
            k("save_ontology", ontology={"objects": []})
        assert e.value.status == 403 and "production environment" in e.value.body["message"]
    finally:
        sure("set_environment", project=KEYS, branch="main", production=False)
        drop([x for x in (KEYS, KEYS + "_pub") if x in {x["project"] for x in abi.call("list_projects")["projects"]}])


# ---------- the app: Possible duplicates on a type's list, the records on a customer's page ----------

def test_duplicates_card_in_the_app(er, tmp_path):
    """Customers' list shows an Editor "Possible duplicates": the group in a sentence, each name a link to its page, why
    in words, one primary button; the button asks ABI, whose question the person answers (the page never confirms by
    itself), and the two are one customer. A merged customer's page lists its records with where each came from and
    "Take out", and the decisions with "Undo". Business names only: no key, table or column. A phone (read-only, as for
    forms) and a Viewer get the merged list and no such card; nothing overflows. Screenshots beside (tmp_path)."""
    import app
    from test_pages import OWNER, Browser, drive, okey

    one(101, 902)
    one(103, 104)
    one(77, 114, 115)
    try:  # one proposal is left for the page: Tee Haus
        decide("different", 109, 110, 112)
    except abi.ActionError as e:
        assert e.status == 409, e.body
    for d in found()[0]["decisions"]:  # ... apart again if another test merged them
        if d["decision"] == "same" and not d["undone"] and {o["key"] for o in d["objects"]} == {"120", "121"}:
            call("undo_edits", table=S, batch=d["batch"])
    text = "document.querySelector('main').innerText"
    wide = "document.documentElement.scrollWidth > innerWidth + 1"
    card = [["show", "obj:customer"], ["wait", "main .card .item"], ["get", "text", text], ["get", "wide", wide],
            ["get", "primary", "document.querySelectorAll('main .btn.pri').length"],
            ["all", "names", "main .item .oopen"], ["all", "buttons", "main .item .btn"]]
    hubs, srv = set(app.HUBS), app.serve(P, 0, open_browser=False, signin=True)
    try:
        owner, viewer = Browser(srv.server_address[1], "admin", OWNER, P), Browser(srv.server_address[1], er["name"], er["pw"], P)
        lst = drive(owner, tmp_path, [*card, ["shot", str(tmp_path / "duplicates-1440.png")],
                                      ["click", "main .item .oopen"], ["wait", "main .okv"], ["text", "opened", ".pghead h1"]])
        assert "These 2 customers look like the same one:" in lst["text"] and "same email" in lst["text"] and "Possible duplicates" in lst["text"]
        assert {"Tee Haus", "Teehaus Frankfurt"} <= set(lst["names"]) and lst["opened"] == "Tee Haus", "a name opens that customer's page"
        assert lst["primary"] == 1 and lst["buttons"][:2] == ["Same customer", "Not the same"] and "Undo" in lst["buttons"] and lst["wide"] is False
        assert "Decided" in lst["text"] and "The same one:" in lst["text"]
        assert not re.search(r"same_as|customer_id|\b902\b|cluster|ontology|object type|\blink\b", lst["text"]), "business names only"
        # a phone reads (the app is read-only there, as for forms): the merged list, nothing to decide, nothing overflows
        phone = [["show", "obj:customer"], ["wait", "main .pick tr[data-r]"], ["get", "text", text], ["get", "wide", wide],
                 ["get", "cards", "document.querySelectorAll('main .item').length"]]
        narrow = drive(owner, tmp_path, [*phone, ["shot", str(tmp_path / "list-390.png")]], width=390)
        assert narrow["wide"] is False and narrow["cards"] == 0 and "Acme GmbH" in narrow["text"] and "ACME Gmbh" not in narrow["text"]
        # the primary button: ABI's question in the person's words, then their yes
        yes = drive(owner, tmp_path, [*card[:2], ["click", "main .item .btn.pri"], ["wait", "#confirm[open]"], ["text", "ask", "#c-msg"],
                                      ["text", "confirm", "#c-yes"], ["shot", str(tmp_path / "confirm-1440.png")],
                                      ["get", "apart", "api('get_object',{type:'customer',key:[121]}).then(o=>o.key[0])"], ["click", "#c-yes"],
                                      ["until", "![...document.querySelectorAll('main .item .txt')].some(d => d.textContent.startsWith('These 2'))"],
                                      ["get", "one", "api('get_object',{type:'customer',key:[121]}).then(o=>o.key[0])"]])
        assert yes["ask"].startswith("Treat these 2 customers as the same one: “Tee Haus”, “Teehaus Frankfurt”.") and "Confirm to proceed" not in yes["ask"]
        assert (yes["apart"], yes["one"], yes["confirm"]) == (121, 120, "Confirm"), "nothing changed before the person's yes"
        page = [["show", okey("customer", 101)], ["wait", "main .okv"], ["wait", "main .card .item"], ["get", "text", text], ["get", "wide", wide],
                ["all", "buttons", "main .item .btn"]]
        obj = drive(owner, tmp_path, [*page, ["shot", str(tmp_path / "customer-1440.png")]])
        assert "Made of 2 records" in obj["text"] and "ACME Gmbh · erp" in obj["text"] and "Acme GmbH · crm" in obj["text"]
        assert obj["buttons"].count("Take out") == 2 and "Undo" in obj["buttons"] and obj["wide"] is False
        assert not re.search(r"same_as|customer_id|\b902\b", obj["text"])
        old = drive(owner, tmp_path, [["show", okey("customer", 902)], ["wait", "main .okv"], ["until", "document.querySelectorAll('main .pick tr[data-r]').length >= 3"],
                                      ["text", "name", ".pghead h1"], ["get", "orders", "document.querySelectorAll('main .pick tr[data-r]').length"],
                                      ["all", "amounts", "main .pick tr[data-r] td:last-child"]])
        assert (old["name"], old["orders"], sorted(old["amounts"])) == ("Acme GmbH", 3, ["100", "30", "50"]), "the merged record's key: the customer's page, with the orders of both"
        small = drive(owner, tmp_path, [page[0], page[1], ["wait", "main .kpis .kpi"], ["get", "wide", wide], ["get", "text", text],
                                        ["shot", str(tmp_path / "customer-390.png")]], width=390)
        assert small["wide"] is False and "180" in small["text"], "the merged customer's revenue, on a phone too"
        theirs = drive(viewer, tmp_path, [*phone, ["shot", str(tmp_path / "viewer-1440.png")]])
        assert theirs["cards"] == 0 and "look like the same" not in theirs["text"] and "ACME Gmbh" not in theirs["text"]
        assert all((tmp_path / f"{x}.png").stat().st_size > 10_000 for x in ("duplicates-1440", "list-390", "confirm-1440", "customer-1440", "customer-390"))
    finally:
        srv.shutdown()
        for k in set(app.HUBS) - hubs:
            app.HUBS.pop(k, None)


# ---------- #36: blocking scales ----------

def test_er_blocking_scales(tmp_path, record_property):
    """100,000 companies with 1,000 planted duplicates (500 with a typo in the name in the same postcode, 500 under
    another name and postcode with the same VAT number written differently): find_same proposes every planted pair
    and few others, in under 60 s, scoring a small fraction of all pairs (never every pair). The measured time is
    recorded (pytest's record_property, and printed): 1.1 s for 1,034,781 candidate pairs on pve1, 12 Nov 2026."""
    n, dup = 100_000, 1_000
    f = tmp_path / "companies.parquet"
    duckdb.sql(f"""COPY (
        WITH base AS (SELECT i, translate(substr(md5(i::VARCHAR), 1, 10), '0123456789', 'ghijklmnop') AS w,
                             10000 + (i * 7919) % 5000 AS postcode FROM range({n - dup}) t(i))
        SELECT i AS company_id, substr(w, 1, 6) || ' ' || substr(w, 7) || ' GmbH' AS name, 'DE' || (100000000 + i) AS vat, postcode FROM base
        UNION ALL
        SELECT {n - dup} + i, CASE WHEN i % 2 = 0 THEN substr(w, 1, 6) || ' ' || substr(w, 7, 3) || ' GmbH' ELSE 'zz ' || reverse(w) END,
               CASE WHEN i % 2 = 1 THEN 'de ' || (100000000 + i) END, postcode + i % 2 FROM base WHERE i < {dup}
        ORDER BY 1) TO '{f}' (FORMAT parquet)""")
    g = tmp_path / "orders.parquet"  # 1,000,000 orders of those companies
    duckdb.sql(f"COPY (SELECT i AS order_id, (i * 7919) % {n} AS company_id, 1 + i % 97 AS amount FROM range(1000000) t(i)) TO '{g}' (FORMAT parquet)")
    if BIG in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([BIG])
    try:
        abi.load("demo", BIG, [str(f), str(g)])
        abi.call("set_table", project=BIG, table="companies", key=["company_id"])
        abi.call("set_table", project=BIG, table="orders", key=["order_id"])
        abi.call("add_relationship", project=BIG, **{"from": "orders.company_id", "to": "companies.company_id"})
        abi.call("set_measure", project=BIG, id="sales", name="Sales", expression="SUM(orders.amount)")
        sure("save_ontology", project=BIG, ontology={"objects": [{
            "id": "company", "table": "companies", "name": "Company", "plural": "Companies", "title": "name",
            "resolve": {"exact": [{"column": "vat"}], "fuzzy": [{"column": "name", "threshold": 0.92}], "block": ["postcode"]}}]})
        start = time.perf_counter()
        r = abi.call("find_same", project=BIG, type="company", limit=2000)
        secs = time.perf_counter() - start
        print(f"\nM11 #36 benchmark: find_same over {n:,} rows: {secs:.1f} s, {r['compared']:,} candidate pairs scored"
              f" of {n * (n - 1) // 2:,} possible, {len(r['groups']):,} groups proposed")
        record_property("er_blocking", {"rows": n, "seconds": round(secs, 1), "compared": r["compared"], "groups": len(r["groups"])})
        assert secs < 60, f"{secs:.1f} s"
        why = {(int(p["a"]), int(p["b"])): p["why"] for g in r["groups"] for p in g["pairs"]}
        assert {(i, n - dup + i) for i in range(dup)} <= why.keys() and not r["more"], "every planted pair is proposed"
        assert len(why) < 1.1 * dup, f"{len(why)} pairs: few beside the planted ones (random names that happen to be alike)"
        assert r["compared"] < 50 * n, "blocking: far fewer than every pair"
        assert re.fullmatch(r"name 9\d% alike", why[0, n - dup][0]) and why[1, n - dup + 1][0] == "same vat"
        # C6, measured: the same aggregate (sales by postcode, 1,000,000 orders joined to 100,000 companies) before any
        # decision, with one merge in force (the tables then read through the resolved views, without query copies), and
        # after that merge is undone (as before again). Each is a first, uncached read of its snapshot
        built = engine(COPIES, timeout=600, stdin=BIG)
        assert built.returncode == 0, built.stderr[-2000:]
        assert [c for c in built.stdout.split() if c.startswith("orders__")], f"no query copy of orders was built: {built.stdout[-500:]}"

        def timed() -> tuple[float, dict]:
            t0 = time.perf_counter()
            rows = abi.call("semantic_query", project=BIG, measures=["sales"], dimensions=["companies.postcode"])["rows"]
            return time.perf_counter() - t0, {r[0]: r[1] for r in rows}
        (t_before, before), yes = timed(), sure("decide_same", project=BIG, type="company", keys=[0, n - dup], decision="same")
        t_merged, merged = timed()
        abi.call("undo_edits", project=BIG, table="same_as_companies", batch=yes["batch"])
        t_undone, undone = timed()
        print(f"M11 C6 measurement: sales by postcode over 1,000,000 orders: {t_before:.2f} s before any decision,"
              f" {t_merged:.2f} s with one merge in force, {t_undone:.2f} s after it is undone")
        record_property("er_views_cost", {"before": round(t_before, 2), "merged": round(t_merged, 2), "undone": round(t_undone, 2)})
        assert merged == before == undone and sum(before.values()) == sum(1 + i % 97 for i in range(1000000)), "the pair shares a postcode: same figures"
        assert t_merged < 5, f"{t_merged:.2f} s"
        # a pair in different postcodes (company 1 and its copy under another name, one postcode on): the copy's orders
        # move to company 1's postcode, so the figures show that the merge is in force
        here, there = 10000 + 7919 % 5000, 10000 + 7919 % 5000 + 1
        moves = sum(1 + i % 97 for i in range(1000000) if (i * 7919) % n == n - dup + 1)
        yes = sure("decide_same", project=BIG, type="company", keys=[1, n - dup + 1], decision="same")
        t_moved, moved = timed()
        abi.call("undo_edits", project=BIG, table="same_as_companies", batch=yes["batch"])
        t_back, back = timed()
        print(f"M11 C-3 measurement, pair in two postcodes: {t_moved:.2f} s with the merge in force, {t_back:.2f} s after it is undone")
        record_property("er_views_cost_moved", {"merged": round(t_moved, 2), "undone": round(t_back, 2)})
        assert moves > 0 and moved[here] == before[here] + moves and moved[there] == before[there] - moves, (moves, before[here], moved[here])
        assert back == before and sum(moved.values()) == sum(before.values()) and t_moved < 5
    finally:
        drop([BIG])


# ---------- the docs' example ----------

def test_the_docs_example_is_a_valid_type(er):
    """docs/objects.md "The same thing twice": its object type passes the ontology's schema."""
    from jsonschema import Draft202012Validator

    [doc] = [json.loads(b) for b in re.findall(r"```json resolve\n(.*?)\n```", (ROOT / "docs/objects.md").read_text(), re.S)]
    schema = json.loads((ROOT / "engine/schemas/v1/ontology.json").read_text())
    assert not list(Draft202012Validator(schema).iter_errors({"$schema": schema["$id"], "objects": [doc]}))
