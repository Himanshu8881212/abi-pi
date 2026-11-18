"""1.2.7 M1 (needs the engine and its worker at :8780, no LLM): the event log as code. processes/<id>.json is checked
like an import (file, JSON path, fix), changes only after the person confirms a preview with the tables its build
writes and what reads them, and is built in the worker by DuckDB SQL: pm_<id>_events (one row per event) and one
pm_<id>_<type> per object type, in the model with keys and relationships, so a restricted Viewer reads only their own
cases and never the whole log. Two logs: Brewline's supply process (bench/brewline.py --supply: an event about several
purchase orders, known answers from the generator) and a helpdesk of three tickets written out below, where every
count, order and duration is known by hand."""
import copy
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

import pytest
from conftest import drop, parquet
from jsonschema import Draft202012Validator
from test_engine import engine
from test_ontology_nobreak import ROUNDTRIP
from test_pages import sure

import abi
import duckdb
from pi_driver import ROOT

sys.path.insert(0, str(ROOT / "bench"))
import brewline  # noqa: E402

P = "pi_process"
SCHEMA = json.loads((ROOT / "engine/schemas/v1/process.json").read_text())
PLAN = {  # the plan's example (docs/plans/1.2.7.md §2.1), as written there
    "$schema": "https://schema.abi.invalid/v1/process.json",
    "id": "supply", "name": "Supply", "description": "From a café's purchase order to the paid invoice.",
    "case": "purchase_order",
    "events": [
        {"table": "supply_events", "id": "event_id", "activity": "activity", "start": "started_at", "end": "ended_at",
         "attributes": ["done_by", "amount"],
         "objects": [{"type": "delivery", "columns": ["delivery_id"]},
                     {"type": "purchase_order", "through": "supply_event_orders", "event": "event_id", "columns": ["po_id"]}]},
        {"table": "purchase_orders", "stamps": {"Order placed": "placed_at", "Order approved": "approved_at"},
         "objects": [{"type": "purchase_order", "columns": ["po_id"]}]},
    ],
    "clean": {"rename": {"GR": "Goods received"}, "drop": ["Heartbeat"],
              "where": [{"column": "started_at", "op": ">=", "value": "2025-01-01"}], "duplicates": "drop"},
    "reference": {"from": "variant", "start": ["Order placed"], "end": ["Invoice paid"],
                  "flows": [["Order placed", "Order approved"], ["Order approved", "Goods received"],
                            ["Goods received", "Invoice received"], ["Invoice received", "Invoice paid"]],
                  "optional": ["Reminder sent"], "any_order": [["Goods received", "Invoice received"]],
                  "repeat": {"Goods received": 2}},
    "sla": [{"id": "delivered_in_5_days", "name": "Delivered within 5 days", "from": "Order approved",
             "to": "Goods received", "within_hours": 120}],
    "refresh_minutes": 60,
}
SUPPLY = {  # what this module saves for Brewline: the example's sources, with every time of an order and no clean-up
    "id": "supply", "name": "Supply", "description": "From a café's purchase order to the paid invoice.", "case": "purchase_order",
    "events": [PLAN["events"][0],
               {"table": "purchase_orders", "stamps": {"Order placed": "placed_at", "Order approved": "approved_at", "Order cancelled": "cancelled_at"},
                "objects": [{"type": "purchase_order", "columns": ["po_id"]}]}],
}
# The helpdesk. Three tickets with the times they were opened, assigned and closed (T2 is assigned the minute it is
# opened and still open; T3 was never assigned), and fourteen rows of ticket_events
TICKETS = """ticket_id,team,opened_at,assigned_at,closed_at
T1,North,2025-03-01 09:00:00,2025-03-01 09:30:00,2025-03-02 10:00:00
T2,North,2025-03-01 10:00:00,2025-03-01 10:00:00,
T3,South,2025-03-03 08:00:00,,2025-03-03 09:00:00
"""
EVENTS = """event_id,ticket_id,activity,started_at,ended_at,agent,minutes
1,T1,Call,2025-03-01 11:00:00,2025-03-01 11:20:00,Ada,20
2,T1,Note,2025-03-01 11:00:00,,Ada,0
3,T2,Reply,2025-03-02 09:00:00,,Bo,5
4,T2,Call,2025-03-02 09:00:00,,Bo,7
5,T2,Reply,2025-03-02 09:00:00,,Bo,3
6,T3,Escalation,2025-03-03 08:00:00,,Di,2
7,T1,CALL,2025-03-01 12:00:00,2025-03-01 12:10:00,Cy,10
8,T1,Heartbeat,2025-03-01 12:30:00,,sys,0
9,T1,Call,,,Cy,4
10,,Call,2025-03-01 13:00:00,,Cy,6
11,T3,Call,2024-12-31 10:00:00,,Cy,8
12,T3,Call,2025-03-03 08:30:00,2025-03-03 08:45:00,Di,15
12,T3,Call,2025-03-03 08:30:00,2025-03-03 08:45:00,Di,15
13,T3,,2025-03-03 08:50:00,,Di,1
"""
DESK = {
    "id": "desk", "name": "Helpdesk", "case": "ticket",
    "events": [{"table": "tickets", "stamps": {"Opened": "opened_at", "Assigned": "assigned_at", "Closed": "closed_at"},
                "objects": [{"type": "ticket", "columns": ["ticket_id"]}]},
               {"table": "ticket_events", "id": "event_id", "activity": "activity", "start": "started_at", "end": "ended_at",
                "attributes": ["minutes"], "objects": [{"type": "ticket", "columns": ["ticket_id"]}]}],
    "clean": {"rename": {"CALL": "Call"}, "drop": ["Heartbeat"], "where": [{"column": "started_at", "op": ">=", "value": "2025-01-01"}],
              "duplicates": "drop"},
}
# Two more sources of events about tickets, each with an id of its own (calls 1 to 3, chats 1 and 2: the same numbers),
# the chats' tickets in a bridge table (chat 1 is about two tickets; its last row names a chat that doesn't exist, though
# a call 3 does). calls also has columns named like the build's own (Seq, __rn) and its minutes are text
CALLS = """call_id,about,activity,started_at,Seq,minutes,__rn
1,T3,Call,2025-03-04 09:00:00,7,five,x
2,T1,Call,2025-03-04 09:00:00,8,ten,y
3,T2,Call,2025-03-04 10:00:00,9,two,z
"""
CHATS = """chat_id,activity,started_at
1,Chat,2025-03-04 09:30:00
2,Chat,2025-03-04 09:30:00
"""
CHAT_TICKETS = """chat,ticket
1,T1
1,T2
2,T3
3,T1
"""
TALK = {"id": "talk", "name": "Talk", "case": "ticket", "events": [
    {"table": "calls", "id": "call_id", "activity": "activity", "start": "started_at", "objects": [{"type": "ticket", "columns": ["about"]}]},
    {"table": "chats", "id": "chat_id", "activity": "activity", "start": "started_at",
     "objects": [{"type": "ticket", "through": "chat_tickets", "event": "chat", "columns": ["ticket"]}]}]}
# A source of instants (TIMESTAMP WITH TIME ZONE, written into its Parquet file in the fixture: 00:30 in Berlin on 1 March,
# and 23:45 UTC the evening before, which is 00:45 in Berlin), one of wall-clock times between them, and a table with
# three times, a ticket and a text column where no two rows say the same
NAPS = """nap_id,about,activity,happened
1,T1,Nap,2025-03-01 00:40:00
"""
TRIPS = """trip_id,ticket_ref,note,booked_at,left_at,back_at
R1,T1,window seat,2025-03-06 08:00:00,2025-03-07 09:00:00,2025-03-08 10:00:00
R2,T2,aisle,2025-03-06 08:30:00,2025-03-07 09:30:00,
R3,T3,none,2025-03-06 09:00:00,,
"""
ZONE = {"id": "zone", "name": "Zones", "case": "ticket", "events": [
    {"table": "zoned", "id": "zid", "activity": "activity", "start": "at_instant", "objects": [{"type": "ticket", "columns": ["about"]}]},
    {"table": "naps", "activity": "activity", "start": "happened", "objects": [{"type": "ticket", "columns": ["about"]}]}],
    "clean": {"where": [{"column": "at_instant", "op": ">=", "value": "2025-03-01"}]}}
ZONED = {"id": "zoned", "name": "Zoned", "case": "ticket", "events": [  # the same instants in a column named like the log's own, filtered on it
    {"table": "zoned", "id": "zid", "activity": "activity", "start": "started_at", "objects": [{"type": "ticket", "columns": ["about"]}]}],
    "clean": {"where": [{"column": "started_at", "op": ">=", "value": "2025-03-01 00:40:00"}]}}
PINGS = 300_000  # rows of pings, every one at the same second and without an id: row n is about ticket T<n % 3 + 1>
TIES = {"id": "ties", "name": "Ties", "case": "ticket", "events": [
    {"table": "pings", "activity": "activity", "start": "started_at", "attributes": ["n"], "objects": [{"type": "ticket", "columns": ["about"]}]}]}
ONTO = {"objects": [
    {"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name"},
    {"id": "supplier", "table": "suppliers", "name": "Supplier", "plural": "Suppliers", "title": "supplier_name"},
    {"id": "purchase_order", "table": "purchase_orders", "name": "Purchase order", "plural": "Purchase orders", "title": "po_id"},
    {"id": "delivery", "table": "deliveries", "name": "Delivery", "plural": "Deliveries", "title": "delivery_id"},
    {"id": "ticket", "table": "tickets", "name": "Ticket", "plural": "Tickets", "title": "ticket_id"}]}
STATE = r"""
import hashlib, json, sys
import abi.app  # noqa: F401
from abi import db
from abi.project import bundle, git
from abi.semantic import rls
a = json.loads(sys.stdin.read())
P = a["project"]
with db.connect() as c:
    runs = [dict(r) for r in c.execute("SELECT id::text AS run, name, key, status, user_id FROM analysis_runs WHERE project = %s ORDER BY created", [P])]
    audit = [dict(r) for r in c.execute("SELECT input->>'process' AS process, input->>'key' AS key, status, user_name, detail FROM audit_log"
                                        " WHERE project = %s AND via = 'worker' AND action = 'build_process' ORDER BY id", [P])]
    reads = [r["query"] for r in c.execute("SELECT query FROM query_log WHERE project = %s AND user_id = %s AND query ? 'propose_process'"
                                           " ORDER BY id", [P, a.get("user", 0)])]
print(json.dumps({"files": {k: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest() for k, v in bundle.bundle(git.path(P)).items()},
                  "runs": runs, "audit": audit, "reads": reads, "denied": rls.unfiltered_outputs(P), "filtered": sorted(rls.filtered(P))}))
"""


def state(user: int = 0) -> dict:
    """The project's files (path: sha256), its build runs and the worker's audit rows of them, what row-level security
    denies and filters, and the proposal's reads logged for `user`: read in the engine."""
    r = engine(STATE, timeout=120, stdin=json.dumps({"project": P, "user": user}))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


READ = r"""
import json, sys
from abi import semantic
a = json.loads(sys.stdin.read())
print(json.dumps([[list(r) for r in semantic.execute(a["project"], q, {"test": "process"})] for q in a["sql"]], default=str))
"""
MEASURES = {"received": "SUM(pm_supply_events.amount)", "log_events": "COUNT(pm_supply_events.event_id)",
            "order_steps": "COUNT(pm_supply_purchase_order.event_id)"}  # an event's own figures, and steps per purchase order


def sql(q: str) -> list:
    """One read as the owner through sql_query (its fixed list of functions)."""
    return abi.call("sql_query", project=P, sql=q)["rows"]


def deep(*qs: str) -> list:
    """Reads with any DuckDB function (string_agg, lead), run in the engine as a system read: one result per query."""
    r = engine(READ, timeout=300, stdin=json.dumps({"project": P, "sql": qs}))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


def refused(status: int, action: str, token: str | None = None, **args) -> dict:
    """The body of ABI's refusal; a question (409 with a token) is answered yes first, as the person would."""
    with abi.as_user(token):
        try:
            sure(action, project=P, **args)
        except abi.ActionError as e:
            assert e.status == status, e.body
            return e.body
    raise AssertionError(f"{action} was not refused")


def errors(p: dict) -> list:
    """(file, JSON path) of every error save_process refuses process `p` with; each says what is wrong and the fix."""
    body = refused(422, "save_process", process=p)
    assert all(e["error"] and e["fix"] for e in body["errors"]), body
    return [(e["file"], e["path"]) for e in body["errors"]]


def shown(pid: str) -> dict | None:
    return next((x for x in abi.call("process", project=P)["processes"] if x["id"] == pid), None)


def build_of(pid: str, after: str | None = None, timeout: float = 600) -> dict:
    """The latest build of process `pid` once it has ended (another one than run `after`)."""
    end = time.monotonic() + timeout
    while (b := shown(pid)["build"]).get("run") == after or b["status"] not in ("ok", "failed"):
        assert time.monotonic() < end, b
        time.sleep(1)
    return b


def save(p: dict) -> dict:
    """save_process with the person's yes, then its build's outcome."""
    was = (shown(p["id"]) or {"build": {}})["build"].get("run")
    assert sure("save_process", project=P, process=p)["build"]
    return build_of(p["id"], after=was)


def measures() -> list:
    return json.loads((Path(abi.ABI_PROJECTS) / P / "model/measures.json").read_text())["measures"]


def idle(timeout: float = 900) -> None:
    """Every build of the project has ended (a project with one queued or running is not deleted)."""
    end = time.monotonic() + timeout
    while busy := [r for r in state()["runs"] if r["status"] in ("queued", "running")]:
        assert time.monotonic() < end, busy
        time.sleep(2)


def model_json() -> dict:
    return json.loads((Path(abi.ABI_PROJECTS) / P / "model/model.json").read_text())


def exported() -> dict:
    """The project's files as a bundle."""
    r = engine(f"import json\nfrom abi.project import bundle, git\nprint(json.dumps(bundle.bundle(git.path('{P}'))))", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


def none_for(log, table: str) -> bool:
    """The restricted Viewer reads no row of `table` in the running server: refused, or empty."""
    try:
        return viewer(log, "sql_query", sql=f"SELECT count(*) FROM {table}")["rows"] == [[0]]
    except abi.ActionError:
        return True


@pytest.fixture(scope="module")
def log(tmp_path_factory):
    """P: Brewline's cafés and its supply tables at the smallest scale, the helpdesk's two tables, an ontology over
    them, and a Viewer who sees the North region's cafés and the North team's tickets (two row rules) and no agent
    names (a column rule on ticket_events)."""
    d = tmp_path_factory.mktemp("process")
    known = brewline.generate(d, 0.005, supply=True)["supply"]
    (d / "tickets.csv").write_text(TICKETS)
    (d / "ticket_events.csv").write_text(EVENTS)
    for f, text in (("calls", CALLS), ("chats", CHATS), ("chat_tickets", CHAT_TICKETS), ("naps", NAPS), ("trips", TRIPS)):
        (d / f"{f}.csv").write_text(text)
    duckdb.sql(f"""COPY (SELECT *, at_instant AS started_at FROM (VALUES (1, 'T1', 'Ping', TIMESTAMPTZ '2025-03-01 00:30:00+01'),
      (2, 'T1', 'Pong', TIMESTAMPTZ '2025-02-28 23:45:00+00')) t(zid, about, activity, at_instant)) TO '{d}/zoned.parquet' (FORMAT parquet)""")
    duckdb.sql(f"""COPY (SELECT i AS n, 'T' || (i % 3 + 1) AS about, CASE WHEN i % 2 = 0 THEN 'Ping' ELSE 'Pong' END AS activity,
      TIMESTAMP '2025-03-05 12:00:00' AS started_at FROM range(1, {PINGS + 1}) t(i)) TO '{d}/pings.parquet' (FORMAT parquet)""")
    if P in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([P])
    abi.load("demo", P, [str(d / f"{t}/{t}.parquet") for t in ("cafes", *brewline.SUPPLY)] 
             + parquet(d / "tickets.csv", d / "ticket_events.csv", d / "calls.csv", d / "chats.csv", d / "chat_tickets.csv", d / "naps.csv",
                       d / "trips.csv") + [str(d / "pings.parquet"), str(d / "zoned.parquet")])
    for t, k in {"cafes": "cafe_id", "suppliers": "supplier_id", "purchase_orders": "po_id", "deliveries": "delivery_id",
                 "supply_events": "event_id", "tickets": "ticket_id"}.items():
        abi.call("set_table", project=P, table=t, key=[k])
    for a, b in (("purchase_orders.cafe_id", "cafes.cafe_id"), ("purchase_orders.supplier_id", "suppliers.supplier_id"),
                 ("deliveries.supplier_id", "suppliers.supplier_id"), ("supply_events.delivery_id", "deliveries.delivery_id"),
                 ("supply_event_orders.event_id", "supply_events.event_id"), ("supply_event_orders.po_id", "purchase_orders.po_id"),
                 ("ticket_events.ticket_id", "tickets.ticket_id")):
        abi.call("add_relationship", project=P, **{"from": a, "to": b})
    sure("save_ontology", project=P, ontology=ONTO)
    name, pw = f"pi_proc_v_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    abi.call("create_user", name=name, password=pw)
    try:
        abi.call("set_role", project=P, user=name, role="viewer")
        uid = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}[name]
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={uid: ["North"]})
        sure("set_rls_rule", project=P, id="team", table="tickets", column="team", users={uid: ["North"]})
        sure("set_column_rule", project=P, id="agent", table="ticket_events", columns=["agent"])
        yield {"viewer": abi.login(name, pw)[1]["token"], "uid": int(uid), "known": known, "dir": d}
    finally:
        sure("delete_user", name=name)
        idle()
        drop([P])


@pytest.fixture(scope="module")
def built(log):
    """Both processes saved (the person's yes given here) and built."""
    for p in (SUPPLY, DESK):
        b = save(p)
        assert b["status"] == "ok", b
        log[p["id"]] = b
    for i, x in MEASURES.items():
        abi.call("set_measure", project=P, id=i, name=i, expression=x)
    return log


def viewer(log, action: str, **args) -> dict:
    with abi.as_user(log["viewer"]):
        return abi.call(action, project=P, **args)


# ---------- #10 the file: schema and import checks ----------

def test_schema_examples_valid():
    Draft202012Validator.check_schema(SCHEMA)
    v = Draft202012Validator(SCHEMA)
    assert SCHEMA["examples"] == [PLAN], "the schema's example is the plan's"
    assert "the build refuses them" in SCHEMA["properties"]["clean"]["properties"]["duplicates"]["description"], "what `keep` means where a source names an id"
    assert not list(v.iter_errors(PLAN)) and all(not list(v.iter_errors({"$schema": PLAN["$schema"], **p})) for p in (SUPPLY, DESK))
    bad = copy.deepcopy(PLAN)
    del bad["events"][0]["start"]  # an event table without its time
    bad["events"][1]["activity"] = "status"  # stamps and an activity column are two kinds of source
    bad["id"] = "my-supply"  # the id is part of the tables' names
    bad["clean"]["duplicates"] = "merge"
    assert {(e.validator, e.json_path) for e in v.iter_errors(bad)} == {
        ("required", "$.events[0]"), ("not", "$.events[1]"), ("pattern", "$.id"), ("enum", "$.clean.duplicates")}


def test_missing_timestamp_column_names_file_field_fix(log):
    before = state()["files"]
    p = copy.deepcopy(SUPPLY)
    p["events"][0]["start"] = "happened_at"
    body = refused(422, "save_process", process=p)
    [e] = body["errors"]
    assert (e["file"], e["path"]) == ("processes/supply.json", "$.events[0].start"), body
    assert e["error"] == "Table 'supply_events' has no column 'happened_at'." and "started_at" in e["fix"] and e["fix"].startswith("Use one of:")
    p["events"][0]["start"] = "done_by"  # there, and text
    [e] = refused(422, "save_process", process=p)["errors"]
    assert e["path"] == "$.events[0].start" and "is VARCHAR, not a date or a time" in e["error"] and "DATE or TIMESTAMP" in e["fix"]
    p = copy.deepcopy(SUPPLY)
    p["events"][0]["activity"], p["events"][0]["end"], p["events"][1]["stamps"]["Order placed"] = "amount", "nope", "amount"
    assert errors(p) == [("processes/supply.json", "$.events[0].activity"), ("processes/supply.json", "$.events[0].end"),
                         ("processes/supply.json", '$.events[1].stamps["Order placed"]')]
    p = copy.deepcopy(SUPPLY)
    p["events"][0]["table"], p["events"][0]["attributes"], p["case"] = "supply_log", ["done_by", "seq"], "supplier"
    assert errors(p) == [("processes/supply.json", "$.case"), ("processes/supply.json", "$.events[0].table")]
    p["events"][0]["table"] = "supply_events"
    assert errors(p) == [("processes/supply.json", "$.case"), ("processes/supply.json", "$.events[0].attributes[1]")]
    p = copy.deepcopy(DESK)
    p["clean"]["where"] = [{"column": "opened", "op": ">=", "value": "2025-01-01"}, {"column": "started_at", "op": "in", "value": "2025-01-01"}]
    p["sla"] = [{"id": "fast", "name": "Closed in a day", "from": "Opened", "to": "Closed", "within_hours": 24}] * 2
    assert errors(p) == [("processes/desk.json", "$.clean.where[0].column"), ("processes/desk.json", "$.clean.where[1].value"),
                         ("processes/desk.json", "$.sla[1].id")]
    assert state()["files"] == before, "a refused save writes nothing"


def test_object_type_key_mismatch_refused(log):
    p = copy.deepcopy(SUPPLY)
    p["events"][0]["objects"][0]["columns"] = ["amount"]  # a DECIMAL where the delivery's key is text
    body = refused(422, "save_process", process=p)
    [e] = body["errors"]
    assert (e["file"], e["path"]) == ("processes/supply.json", "$.events[0].objects[0].columns[0]"), body
    assert "'supply_events.amount' is DECIMAL(12,2)" in e["error"] and "'deliveries.delivery_id', the key of object type 'delivery', is VARCHAR" in e["error"]
    assert "set_type" in e["fix"]
    p = copy.deepcopy(SUPPLY)
    p["events"][0]["objects"][0]["type"] = "truck"  # no such type
    p["events"][0]["objects"][1]["columns"] = ["po_id", "event_id"]  # two columns for a key of one
    p["events"][1]["objects"].append({"type": "purchase_order", "columns": ["po_id"]})  # the same type twice on one row
    assert errors(p) == [("processes/supply.json", "$.events[0].objects[0].type"), ("processes/supply.json", "$.events[0].objects[1].columns"),
                         ("processes/supply.json", "$.events[1].objects[1].type")]
    # a bridge table chains: it needs the event's id, a column of the id's type, and both `through` and `event`
    p = copy.deepcopy(SUPPLY)
    p["events"][0]["objects"][1]["event"] = "po_id"
    del p["events"][0]["id"]
    assert errors(p) == [("processes/supply.json", "$.events[0].objects[1].through")]
    p = copy.deepcopy(DESK)
    p["events"][1]["objects"] = [{"type": "ticket", "through": "supply_event_orders", "event": "event_id", "columns": ["po_id"]},
                                 {"type": "cafe", "through": "tickets", "columns": ["team"]}]
    got = errors(p)
    assert got == [("processes/desk.json", "$.events[1].objects[0].event"), ("processes/desk.json", "$.events[1].objects[1]")], got


# ---------- #11 the build ----------

TRACES = """SELECT po_id, string_agg(activity, ' > ' ORDER BY t, o) AS path FROM (
  SELECT po_id, 'Order placed' AS activity, placed_at AS t, 0 AS o FROM purchase_orders UNION ALL
  SELECT po_id, 'Order approved', approved_at, 1 FROM purchase_orders WHERE approved_at IS NOT NULL UNION ALL
  SELECT po_id, 'Order cancelled', cancelled_at, 2 FROM purchase_orders WHERE cancelled_at IS NOT NULL UNION ALL
  SELECT b.po_id, e.activity, e.started_at, 3 FROM supply_event_orders b JOIN supply_events e USING (event_id)) GROUP BY 1"""


def test_build_writes_events_and_per_type_tables(built):
    k, b = built["known"], built["supply"]
    received = sql("SELECT count(*) FROM supply_events WHERE activity = 'Goods received'")[0][0]
    assert b["tables"] == {"pm_supply_events": k["events"], "pm_supply_delivery": sql("SELECT count(delivery_id) FROM supply_events")[0][0],
                           "pm_supply_purchase_order": k["events"] - k["rows"]["supply_events"] + k["rows"]["supply_event_orders"]}, b
    assert b["events"] == k["events"] == sql("SELECT count(*) FROM pm_supply_events")[0][0] and b["dropped"] == dict.fromkeys(b["dropped"], 0)
    # one snapshot for the three tables, made by the build
    made = [r for r in abi.call("list_snapshots", project=P, limit=200)["rows"] if r[3] == "build process supply"]
    assert [r[0] for r in made] == [b["snapshot"]], made
    cols = lambda t: abi.call("sql_query", project=P, sql=f"SELECT * FROM {t} LIMIT 0")["columns"]  # noqa: E731
    event = ["event_id", "activity", "started_at", "ended_at", "duration_seconds", "done_by", "amount"]
    assert cols("pm_supply_events") == [*event, "ord"]
    assert cols("pm_supply_purchase_order") == ["po_id", *event, "seq", "next_activity", "wait_seconds"]
    assert cols("pm_supply_delivery") == ["delivery_id", *event, "seq", "next_activity", "wait_seconds"]
    happy = " > ".join(k["happy_path"]["steps"])
    same, on_path, order = deep(
        # each order's steps in order are what the source tables say, read another way
        f"""SELECT count(*), count(*) FILTER (a.path IS DISTINCT FROM b.path) FROM ({TRACES}) a FULL JOIN
          (SELECT po_id, string_agg(activity, ' > ' ORDER BY seq) AS path FROM pm_supply_purchase_order GROUP BY 1) b USING (po_id)""",
        f"""SELECT count(*) FROM (SELECT po_id FROM pm_supply_purchase_order GROUP BY 1
          HAVING string_agg(activity, ' > ' ORDER BY seq) = '{happy}')""",
        # seq counts from 1 without gaps, next_activity is the next row's activity, the wait runs to the next row's start
        """SELECT count(*) FROM (SELECT *, row_number() OVER w AS n, lead(activity) OVER w AS nxt, lead(started_at) OVER w AS later
          FROM pm_supply_purchase_order WINDOW w AS (PARTITION BY po_id ORDER BY seq)) WHERE seq <> n OR nxt IS DISTINCT FROM next_activity
          OR wait_seconds IS DISTINCT FROM CASE WHEN later IS NOT NULL THEN greatest(date_diff('second', coalesce(ended_at, started_at), later), 0) END""")
    assert same == [[k["rows"]["purchase_orders"], 0]] and on_path == [[k["happy_path"]["orders"]]] and order == [[0]]
    # the planted answer shows: the slow supplier between the approval and the goods, to the generator's own figure
    slow = sql("""SELECT o.supplier_id, median(date_diff('minute', a.started_at, g.started_at)) / 1440 FROM pm_supply_purchase_order a
      JOIN pm_supply_purchase_order g USING (po_id) JOIN purchase_orders o USING (po_id)
      WHERE a.activity = 'Order approved' AND g.activity = 'Goods received' GROUP BY 1 ORDER BY 2 DESC LIMIT 2""")
    assert [(s, round(d, 2)) for s, d in slow][0] == (k["slow"]["supplier"], k["slow"]["median_days"]) and round(slow[1][1], 2) == k["slow"]["next_slowest_median_days"]
    assert sql("SELECT activity, count(*) FROM pm_supply_delivery GROUP BY 1 ORDER BY 1") == [
        ["Delivery dispatched", k["rows"]["deliveries"]], ["Goods received", received]]
    # in the model: keyed, described, a type's table many-to-one to its object's table and to the events table
    m = abi.call("semantic_model", project=P, all=True)
    assert {t: m["tables"][t]["key"] for t in b["tables"]} == {"pm_supply_events": ["event_id"], "pm_supply_delivery": ["delivery_id", "event_id"],
                                                               "pm_supply_purchase_order": ["po_id", "event_id"]}
    assert all(m["tables"][t]["description"] for t in b["tables"])
    rels = {(r["from"]["table"], *r["from"]["columns"], r["to"]["table"]) for r in m["relationships"] if r["from"]["table"].startswith("pm_supply_")}
    assert rels == {("pm_supply_purchase_order", "po_id", "purchase_orders"), ("pm_supply_purchase_order", "event_id", "pm_supply_events"),
                    ("pm_supply_delivery", "delivery_id", "deliveries"), ("pm_supply_delivery", "event_id", "pm_supply_events")}
    assert not [g for g in abi.call("readiness", project=P)["missing"] if "pm_supply" in g], "keys and relationships hold; nothing is asked for them"
    # the run is in the registry, and the read of the definitions shows it
    got = shown("supply")
    assert got["events"] == SUPPLY["events"] and got["tables"] == {"pm_supply_events": None, "pm_supply_delivery": "delivery",
                                                                   "pm_supply_purchase_order": "purchase_order"}
    assert got["build"]["status"] == "ok" and got["build"]["run"] in {r["run"] for r in state()["runs"] if r["name"] == "supply"}


def test_cleanup_counts_dropped_rows_by_reason(built):
    b = built["desk"]
    # of fourteen rows: 9 has no time, 13 no activity, 11 is before 2025 (where), 8 a Heartbeat (drop), 10 about no
    # ticket, the second 12 the same as the first; a ticket's missing time (T2 not closed, T3 not assigned) is no row
    assert b["dropped"] == {"no_timestamp": 1, "no_activity": 1, "filtered": 1, "activity": 1, "no_object": 1, "duplicate": 1}, b
    assert b["events"] == 7 + 14 - 6 == sql("SELECT count(*) FROM pm_desk_events")[0][0]
    assert sql("SELECT event_id FROM pm_desk_events WHERE event_id NOT LIKE '0.%' ORDER BY CAST(event_id AS INTEGER)") == [[str(i)] for i in (1, 2, 3, 4, 5, 6, 7, 12)]
    assert sql("SELECT activity, count(*) FROM pm_desk_events GROUP BY 1 ORDER BY 1") == [
        ["Assigned", 2], ["Call", 4], ["Closed", 2], ["Escalation", 1], ["Note", 1], ["Opened", 3], ["Reply", 2]], "CALL is a Call, no Heartbeat"
    # the same rows kept twice share an id: the build refuses, says where and how to fix it, and leaves the tables as they were
    keep = copy.deepcopy(DESK)
    keep["clean"]["duplicates"] = "keep"
    bad = save(keep)
    assert bad["status"] == "failed" and bad["error"].startswith("processes/desk.json $.events[1].id: 1 event(s) have the id of another event"), bad
    assert 'clean.duplicates to "drop"' in bad["error"] and sql("SELECT count(*) FROM pm_desk_events")[0][0] == 15
    # a filter's value that is no date: the build says which part of the file and what to give
    typo = copy.deepcopy(DESK)
    typo["clean"]["where"][0]["value"] = "last year"
    bad = save(typo)
    assert bad["status"] == "failed" and bad["error"].startswith("processes/desk.json $.clean.where: a value doesn't fit its column's type"), bad
    again = save(DESK)
    assert again["status"] == "ok" and again["dropped"] == b["dropped"]


def test_unpivot_timestamp_columns(built):
    # tickets has one row per ticket and three time columns: each filled one is an event, an empty one is none
    got = sql("""SELECT t.ticket_id, e.activity, strftime(e.started_at, '%Y-%m-%d %H:%M'), e.ended_at, e.duration_seconds, e.minutes
      FROM pm_desk_events e JOIN pm_desk_ticket t USING (event_id) WHERE e.event_id LIKE '0.%' ORDER BY 1, t.seq""")
    assert got == [["T1", "Opened", "2025-03-01 09:00", None, None, None], ["T1", "Assigned", "2025-03-01 09:30", None, None, None],
                   ["T1", "Closed", "2025-03-02 10:00", None, None, None],
                   ["T2", "Opened", "2025-03-01 10:00", None, None, None], ["T2", "Assigned", "2025-03-01 10:00", None, None, None],
                   ["T3", "Opened", "2025-03-03 08:00", None, None, None], ["T3", "Closed", "2025-03-03 09:00", None, None, None]]
    # Brewline's orders: placed, approved and cancelled, as many events as filled times
    assert sql("SELECT activity, count(*) FROM pm_supply_events WHERE activity LIKE 'Order %' GROUP BY 1 ORDER BY 1") == [
        [a, n] for a, n in zip(("Order approved", "Order cancelled", "Order placed"),
                               sql("SELECT count(approved_at), count(cancelled_at), count(placed_at) FROM purchase_orders")[0])]


def test_start_and_end_give_duration(built):
    # a call from 11:00 to 11:20 lasts 1,200 s; a note has no end and no duration
    assert sql("SELECT event_id, duration_seconds FROM pm_desk_events WHERE event_id IN ('1', '2', '7', '12') ORDER BY CAST(event_id AS INTEGER)") == [
        ["1", 1200.0], ["2", None], ["7", 600.0], ["12", 900.0]]
    # T1 in order, with the wait to the next step measured from this one's end: the first call ends at 11:20, so the
    # second, at 12:00, waited 40 minutes (not the hour since 11:00)
    assert sql("SELECT seq, activity, next_activity, wait_seconds FROM pm_desk_ticket WHERE ticket_id = 'T1' ORDER BY seq") == [
        [1, "Opened", "Assigned", 1800.0], [2, "Assigned", "Note", 5400.0], [3, "Note", "Call", 0.0], [4, "Call", "Call", 2400.0],
        [5, "Call", "Closed", 78600.0], [6, "Closed", None, None]]
    # Brewline: unloading a delivery (10 to 59 minutes) is the only step with an end
    assert sql("""SELECT count(*) FILTER (p.duration_seconds IS DISTINCT FROM date_diff('second', s.started_at, s.ended_at)), count(p.duration_seconds),
      min(p.duration_seconds), max(p.duration_seconds) FROM pm_supply_events p JOIN supply_events s USING (event_id)""") == [
        [0, sql("SELECT count(*) FROM supply_events WHERE activity = 'Goods received'")[0][0], 600.0, 3540.0]]


def test_same_time_order_is_stable(built):
    order = lambda: sql("SELECT ticket_id, seq, activity, event_id FROM pm_desk_ticket ORDER BY 1, 2")  # noqa: E731
    was = order()
    by = lambda t: [(a, e) for x, _, a, e in was if x == t]  # noqa: E731
    # T2: opened and assigned in the same minute stay in the table's column order; three events at 09:00 stay in the rows' order
    assert by("T2") == [("Opened", "0.2.0"), ("Assigned", "0.2.1"), ("Reply", "3"), ("Call", "4"), ("Reply", "5")]
    # T1: at 11:00 the note (no end) comes before the call that ends at 11:20, though the call's row is first
    assert [a for a, _ in by("T1")] == ["Opened", "Assigned", "Note", "Call", "Call", "Closed"] and by("T1")[2:4] == [("Note", "2"), ("Call", "1")]
    # T3: opened at 08:00 and escalated at 08:00: the first source of the file comes first
    assert by("T3") == [("Opened", "0.3.0"), ("Escalation", "6"), ("Call", "12"), ("Closed", "0.3.2")]
    assert sql("SELECT count(*), count(DISTINCT ord), min(ord), max(ord) FROM pm_desk_events") == [[15, 15, 1, 15]]
    again = save(DESK | {"description": "Built a second time."})
    assert again["status"] == "ok" and again["run"] != built["desk"]["run"] and order() == was, "the same order, build after build"
    assert save(DESK)["status"] == "ok"
    # 300,000 rows at one second, with no id (a parallel scan of several row groups): every row is numbered once, so each
    # event is about its own row's ticket, the order is the rows' own, and a second build numbers them the same
    numbered = lambda: deep(  # noqa: E731
        "SELECT count(*), count(*) FILTER (ord <> CAST(split_part(event_id, '.', 2) AS BIGINT)), sum(n * CAST(split_part(event_id, '.', 2) AS BIGINT))"
        " FROM pm_ties_events",
        "SELECT count(*), count(*) FILTER (t.ticket_id <> 'T' || (e.n % 3 + 1)), count(*) FILTER (t.seq <> (e.n + 2) // 3)"
        " FROM pm_ties_ticket t JOIN pm_ties_events e USING (event_id)")
    one = save(TIES)
    assert one["status"] == "ok" and one["events"] == PINGS and one["dropped"] == dict.fromkeys(one["dropped"], 0), one
    first = numbered()
    assert first[0][0][:2] == [PINGS, 0] and first[1] == [[PINGS, 0, 0]], first
    assert first[0][0][2] == sum(i * i for i in range(1, PINGS + 1)), "row n is event 0.n: the rows' own order"
    assert save(TIES | {"description": "Built a second time."})["status"] == "ok" and numbered() == first
    sure("delete_process", project=P, process="ties")


# ---------- #13 object-centric: an event about several objects ----------

def test_event_on_two_objects_counted_once(built):
    k, b = built["known"], built["supply"]
    # the delivery with the most orders: one event "Goods received", once per order in the orders' table, once in the deliveries'
    [[event, orders]] = sql("""SELECT event_id, count(*) FROM supply_event_orders JOIN supply_events USING (event_id)
      WHERE activity = 'Goods received' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1""")
    assert orders == k["most_orders_in_one_delivery"] > 1
    assert sql(f"""SELECT (SELECT count(*) FROM pm_supply_events WHERE event_id = '{event}'), (SELECT count(*) FROM pm_supply_purchase_order
      WHERE event_id = '{event}'), (SELECT count(DISTINCT po_id) FROM pm_supply_purchase_order WHERE event_id = '{event}'),
      (SELECT count(*) FROM pm_supply_delivery WHERE event_id = '{event}')""") == [[1, orders, orders, 1]]
    # the build says how many events are about more than one object of a type
    assert b["several"] == {"delivery": 0, "purchase_order": k["deliveries_with_several_orders"]}, b["several"]
    # an event's own figures, from the events table: the amounts received are the source's total, each event once
    once, received = sql("SELECT sum(amount), count(*) FROM supply_events WHERE activity = 'Goods received'")[0]
    got = abi.call("semantic_query", project=P, measures=["received", "log_events"],
                   filters=[{"column": "pm_supply_events.activity", "op": "=", "value": "Goods received"}])["rows"]
    assert got == [[pytest.approx(float(once)), received]]
    assert abi.call("semantic_query", project=P, measures=["log_events"])["rows"] == [[k["events"]]]
    # a type's table counts per object of that type: steps per purchase order, by the order's own café
    by_region = dict(abi.call("semantic_query", project=P, measures=["order_steps"], dimensions=["cafes.region"])["rows"])
    assert sum(by_region.values()) == b["tables"]["pm_supply_purchase_order"] > k["events"] and len(by_region) == 4


def test_double_counting_measure_refused(built):
    def refuse(expression: str) -> dict:
        body = refused(422, "set_measure", id="double", name="Double", expression=expression)
        [e] = body["errors"]
        assert (e["file"], e["path"]) == ("model/measures.json", f"$.measures[{len(measures())}].expression"), body
        return e

    e = refuse("SUM(pm_supply_purchase_order.amount)")
    assert "an event's own figure" in e["error"] and "once per Purchase order" in e["error"]
    assert e["fix"] == "Use the events table: SUM(pm_supply_events.amount)."
    assert refuse("AVG(pm_supply_delivery.duration_seconds)")["fix"] == "Use the events table: AVG(pm_supply_events.duration_seconds)."
    assert refuse("DIVIDE(SUM(pm_supply_purchase_order.amount), COUNT(pm_supply_purchase_order.po_id))")["fix"].endswith("SUM(pm_supply_events.amount).")
    # a median and a count weigh an event by its objects as a sum does; the smallest, the largest and the number of different values don't
    for fn in ("MEDIAN", "COUNT"):
        assert refuse(f"{fn}(pm_supply_purchase_order.amount)")["fix"] == f"Use the events table: {fn}(pm_supply_events.amount)."
    for i, x in {"largest": "MAX(pm_supply_purchase_order.amount)", "shortest": "MIN(pm_supply_delivery.duration_seconds)",
                 "different": "DISTINCT_COUNT(pm_supply_purchase_order.amount)"}.items():
        abi.call("set_measure", project=P, id=i, name=i, expression=x)
        sure("delete_measure", project=P, id=i)
    assert "double" not in {m["id"] for m in measures()}
    # what it would have said: every delivery's amount once per order it carries, far more than was received
    once, per_order = sql("SELECT (SELECT sum(amount) FROM pm_supply_events), (SELECT sum(amount) FROM pm_supply_purchase_order)")[0]
    assert once == sql("SELECT sum(amount) FROM supply_events")[0][0] and float(per_order) > 1.5 * float(once)
    # the fix it names is the measure the fixture saved, and what counts per object stays allowed: waiting time, objects
    assert MEASURES["received"] == "SUM(pm_supply_events.amount)" and abi.call("semantic_query", project=P, measures=["received"])["rows"] == [
        [pytest.approx(float(once))]]
    for i, x in {"order_wait": "AVG(pm_supply_purchase_order.wait_seconds)", "orders_seen": "DISTINCT_COUNT(pm_supply_purchase_order.po_id)"}.items():
        abi.call("set_measure", project=P, id=i, name=i, expression=x)
        sure("delete_measure", project=P, id=i)


# ---------- #12 in the model, under row rules ----------

def test_restricted_viewer_reads_only_their_cases(built):
    north = {r[0] for r in sql("SELECT po_id FROM purchase_orders JOIN cafes USING (cafe_id) WHERE region = 'North'")}
    every = sql("SELECT count(DISTINCT po_id), count(*) FROM pm_supply_purchase_order")[0]
    mine = viewer(built, "sql_query", sql="SELECT po_id, count(*) FROM pm_supply_purchase_order GROUP BY 1")["rows"]
    assert {r[0] for r in mine} == north and 0 < len(north) < every[0], "the North Viewer's cases: the orders of North cafés, all of them"
    assert sum(r[1] for r in mine) == sql("SELECT count(*) FROM pm_supply_purchase_order JOIN purchase_orders USING (po_id) JOIN cafes USING (cafe_id)"
                                          " WHERE region = 'North'")[0][0] < every[1]
    # through the semantic layer too: steps per purchase order, by region, is the North's alone
    assert viewer(built, "semantic_query", measures=["order_steps"], dimensions=["cafes.region"])["rows"] == [["North", sum(r[1] for r in mine)]]
    # a case they can't see reads like one that isn't there
    far = sql("SELECT min(po_id) FROM purchase_orders JOIN cafes USING (cafe_id) WHERE region = 'South'")[0][0]
    assert viewer(built, "sql_query", sql=f"SELECT count(*) FROM pm_supply_purchase_order WHERE po_id = '{far}'")["rows"] == [[0]]
    assert sql(f"SELECT count(*) FROM pm_supply_purchase_order WHERE po_id = '{far}'")[0][0] > 0
    # deliveries are under no rule: their events are theirs to read in full, as the deliveries are
    assert viewer(built, "sql_query", sql="SELECT count(*) FROM pm_supply_delivery")["rows"] == sql("SELECT count(*) FROM pm_supply_delivery")
    s = state()
    assert "pm_supply_purchase_order" in s["filtered"] and not {"pm_supply_purchase_order", "pm_supply_delivery"} & set(s["denied"])


def test_events_table_denied_to_restricted(built):
    s = state()
    assert {"pm_supply_events", "pm_desk_events"} <= set(s["denied"]), s["denied"]
    assert sql("SELECT count(*) FROM pm_supply_events")[0][0] == built["known"]["events"]
    for q in ("SELECT count(*) FROM pm_supply_events", "SELECT count(*) FROM pm_supply_events e JOIN pm_supply_purchase_order p USING (event_id)",
              "SELECT count(*) FROM pm_desk_events"):
        assert viewer(built, "sql_query", sql=q)["rows"] == [[0]], q
    # a measure over the whole log is empty for them, not the log's
    assert viewer(built, "semantic_query", measures=["log_events"])["rows"] == [[0]]
    assert abi.call("semantic_query", project=P, measures=["log_events"])["rows"] == [[built["known"]["events"]]]
    # denied by design: readiness asks nobody for a rule on it
    assert not [g for g in abi.call("readiness", project=P)["missing"] if "pm_supply_events" in g or "pm_desk_events" in g]
    # the read of the definitions: a restricted reader gets names and the status, no count made of every row, no filter value
    got = viewer(built, "process")["processes"]
    assert sorted(x["id"] for x in got) == ["desk", "supply"] and all(set(x) == {"id", "name", "case", "types", "build"} for x in got)
    assert all(x["build"] == {"status": "ok"} for x in got), got


# ---------- #14 proposal, save, delete ----------

def test_propose_writes_nothing(log):
    before = state()
    got = abi.call("propose_process", project=P)
    after = state()
    assert before["files"] == after["files"] and before["runs"] == after["runs"], "drafts: no file, no build"
    docs, found = {d["case"]: d for d in got["processes"]}, {f["table"]: f for f in got["found"]}
    assert all(not list(Draft202012Validator(SCHEMA).iter_errors(d)) for d in docs.values()), docs
    # one draft per group of sources that share an object type: the supply tables, and the helpdesk's
    assert set(docs) == {"purchase_order", "ticket"} and "not saved" in got["note"]
    by = {s["table"]: s for d in docs.values() for s in d["events"]}
    assert [s["table"] for s in docs["purchase_order"]["events"]] == ["purchase_orders", "supply_events"] or [
        s["table"] for s in docs["purchase_order"]["events"]] == ["supply_events", "purchase_orders"]
    assert sorted(s["table"] for s in docs["ticket"]["events"]) == ["ticket_events", "tickets"]
    # a table with a time, a text column of few values and a way to an object type (its own column, or a bridge table): an event table
    assert by["supply_events"] == {"table": "supply_events", "id": "event_id", "activity": "activity", "start": "started_at", "end": "ended_at",
                                   "attributes": ["done_by", "amount"],
                                   "objects": [{"type": "delivery", "columns": ["delivery_id"]},
                                               {"type": "purchase_order", "through": "supply_event_orders", "event": "event_id", "columns": ["po_id"]}]}
    assert found["supply_events"]["rows"] == sql("SELECT count(*) FROM supply_events")[0][0]
    assert sorted(found["supply_events"]["activities"]) == ["Delivery dispatched", "Goods received", "Invoice paid", "Invoice received"]
    assert by["ticket_events"]["activity"] == "activity" and by["ticket_events"]["objects"] == [{"type": "ticket", "columns": ["ticket_id"]}]
    # an object type's table with three or more time columns: stamps
    assert by["purchase_orders"] == {"table": "purchase_orders", "stamps": {"Placed": "placed_at", "Approved": "approved_at", "Cancelled": "cancelled_at"},
                                     "objects": [{"type": "purchase_order", "columns": ["po_id"]}]}
    assert by["tickets"]["stamps"] == {"Opened": "opened_at", "Assigned": "assigned_at", "Closed": "closed_at"}
    # the clean-up it can see: one activity written two ways
    assert docs["ticket"]["clean"] == {"rename": {"CALL": "Call"}} and "clean" not in docs["purchase_order"]
    # a draft is checked like any save and asks before anything is written
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_process", project=P, process=docs["ticket"] | {"id": "drafted"})
    assert e.value.status == 409 and e.value.body["confirm_token"] and state()["files"] == before["files"]


def test_propose_respects_rls(log):
    mine, full = viewer(log, "propose_process"), abi.call("propose_process", project=P)
    f, m = ({x["table"]: x for x in got["found"]} for got in (full, mine))
    desk = lambda got: next(s for d in got["processes"] for s in d["events"] if s["table"] == "ticket_events")  # noqa: E731
    # the Viewer sees the North team's tickets: eight of the fourteen events (not the one without a ticket), and no escalation
    assert (f["ticket_events"]["rows"], m["ticket_events"]["rows"]) == (14, 8)
    assert "Escalation" in f["ticket_events"]["activities"] and "Escalation" not in m["ticket_events"]["activities"]
    # a column hidden from them is no attribute of their draft, and is in the owner's
    assert "agent" in desk(full)["attributes"] and "agent" not in desk(mine)["attributes"] and "minutes" in desk(mine)["attributes"]
    assert "agent" not in json.dumps(mine)
    # every read of the draft is in the query log as theirs
    reads = state(log["uid"])["reads"]
    assert reads and {r["propose_process"] for r in reads} == {"supply_events", "ticket_events"}, reads
    assert "needs the editor role" in refused(403, "save_process", log["viewer"], process=mine["processes"][0])["message"]


def test_save_asks_confirmation_with_lineage(built):
    path = Path(abi.ABI_PROJECTS) / P / "processes/supply.json"
    was, runs = path.read_text(), state()["runs"]
    abi.call("set_measure", project=P, id="delivery_steps", name="Steps per delivery", expression="COUNT(pm_supply_delivery.event_id)")
    p = SUPPLY | {"description": "Purchase orders, deliveries and invoices."}
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_process", project=P, process=p)
    ask = e.value.body
    assert e.value.status == 409 and ask["confirm_token"] and path.read_text() == was and state()["runs"] == runs, "asked first: nothing saved, nothing built"
    assert ask["preview"]["changes"] == ["Changes process 'supply' (Supply: 2 source(s) of events about delivery, purchase_order): description."]
    assert ask["preview"]["tables"] == ["pm_supply_delivery", "pm_supply_events", "pm_supply_purchase_order"] and ask["preview"]["removes"] == []
    assert all(t in ask["message"] for t in ask["preview"]["tables"]), ask["message"]
    # the lineage downstream: what reads the tables its build writes
    assert "measure:delivery_steps" in ask["preview"]["reads"]["table:pm_supply_delivery"]
    assert {"measure:received", "measure:log_events"} <= set(ask["preview"]["reads"]["table:pm_supply_events"]), ask["preview"]["reads"]
    last = shown("supply")["build"]["run"]
    done = abi.call("save_process", confirm=ask["confirm_token"], project=P, process=p)
    assert json.loads(path.read_text())["description"] == p["description"] and done["build"] != last
    built_now = build_of("supply", after=last)
    assert built_now["status"] == "ok"
    # the same file saved again changes nothing and asks nothing: it builds again
    again = abi.call("save_process", project=P, process=p)
    assert again["changes"] == [] and build_of("supply", after=built_now["run"])["run"] == again["build"] and path.read_text() != was
    # a change that takes a type away takes its table away: a measure reading it refuses the save, naming itself
    orders = copy.deepcopy(SUPPLY)
    del orders["events"][0]["objects"][0]
    body = refused(422, "save_process", process=orders)
    assert [(x["file"], x["path"]) for x in body["errors"]] == [
        ("model/measures.json", f"$.measures[{[m['id'] for m in measures()].index('delivery_steps')}].expression")], body
    sure("delete_measure", project=P, id="delivery_steps")
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_process", project=P, process=orders)
    assert e.value.body["preview"]["removes"] == ["pm_supply_delivery"] and "archived for 30 days" in e.value.body["message"]
    assert save(orders)["status"] == "ok" and "pm_supply_delivery" not in abi.call("semantic_model", project=P, all=True)["tables"]
    with pytest.raises(abi.ActionError):
        sql("SELECT count(*) FROM pm_supply_delivery")
    # deleting asks too, is refused while a measure reads one of its tables, and takes the file, the tables and the runs
    assert {x["file"] for x in refused(422, "delete_process", process="supply")["errors"]} == {"model/measures.json"}
    for mid in MEASURES:
        sure("delete_measure", project=P, id=mid)
    with pytest.raises(abi.ActionError) as e:
        abi.call("delete_process", project=P, process="supply")
    assert e.value.status == 409 and e.value.body["preview"]["removes"] == ["pm_supply_events", "pm_supply_purchase_order"] and path.exists()
    abi.call("delete_process", confirm=e.value.body["confirm_token"], project=P, process="supply")
    assert not path.exists() and [x["id"] for x in abi.call("process", project=P)["processes"]] == ["desk"]
    assert not [r for r in state()["runs"] if r["name"] == "supply"]
    assert not [t for t in abi.call("semantic_model", project=P, all=True)["tables"] if t.startswith("pm_supply")]
    assert refused(404, "delete_process", process="supply")["message"] == f"No process 'supply' in project '{P}'."
    # as it was, for the tests after this one
    assert save(SUPPLY)["status"] == "ok"
    for i, x in MEASURES.items():
        abi.call("set_measure", project=P, id=i, name=i, expression=x)


def test_rebuild_after_refresh(built):
    b = build_of("desk")
    runs = lambda: [r for r in state()["runs"] if r["name"] == "desk"]  # noqa: E731
    n = len(runs())
    # a table the process doesn't read, loaded again: no build
    abi.finished(P, abi.call("refresh", project=P, table="suppliers"))
    assert len(runs()) == n
    # a table it reads, with one more event: one build, named after the load's snapshot, and the event is in the log
    d = built["dir"]
    (d / "ticket_events.csv").write_text(EVENTS + "14,T2,Call,2025-03-02 10:00:00,2025-03-02 10:05:00,Bo,5\n")
    snap = abi.upload(P, "ticket_events", Path(parquet(d / "ticket_events.csv")[0]))["snapshot"]
    new = build_of("desk", after=b["run"])
    assert new["status"] == "ok" and new["events"] == b["events"] + 1 and new["dropped"] == b["dropped"]
    assert [(r["key"], r["status"]) for r in runs()[n:]] == [(f"snapshot-{snap}", "ok")], runs()
    assert new["key"] == f"snapshot-{snap}" and b["key"] is None, "the read of the definitions says what started a build"
    assert sql("SELECT seq, activity, event_id FROM pm_desk_ticket WHERE ticket_id = 'T2' ORDER BY seq DESC LIMIT 1") == [[6, "Call", "14"]]
    # the check for changed tables (the worker's, run here) finds none changed: no load, so no build
    r = engine(f"from abi import ingest\nprint(ingest.refresh_changed(only='{P}'))", timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    time.sleep(3)
    assert len(runs()) == n + 1, "a refresh that changed no source table starts no build"
    # the same check with something to load (a fingerprint it doesn't know is a changed file's): a table the process
    # doesn't read is loaded again and no build starts; one it reads is, and its build starts from the check's own load
    def check(table: str) -> int:
        """The snapshot the check's own load of `table` made."""
        r = engine(f"from abi import db, ingest\nwith db.connect() as c:\n    c.execute(\"UPDATE sources SET fingerprint = NULL WHERE project = '{P}'"
                   f" AND table_name = '{table}'\")\nprint(ingest.refresh_changed(only='{P}'))", timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]
        return max(x[0] for x in abi.call("list_snapshots", project=P, limit=20)["rows"] if x[3] == f"refresh {table}")

    assert check("suppliers") > snap, "the check loaded suppliers again"
    time.sleep(3)
    assert len(runs()) == n + 1
    loaded = check("tickets")
    assert build_of("desk", after=new["run"])["status"] == "ok" and [r["key"] for r in runs()[n:]] == [f"snapshot-{snap}", f"snapshot-{loaded}"]


def test_reserved_table_name_clash_named(built):
    # a person's table already has the name a build would write: the save is refused and names the table, its file and the fix
    sure("create_input_table", project=P, table="pm_late_events", key=["id"], columns=[{"name": "id", "type": "INTEGER"}])
    late = DESK | {"id": "late", "name": "Late tickets"}
    body = refused(422, "save_process", process=late)
    [e] = body["errors"]
    assert (e["file"], e["path"]) == ("processes/late.json", "$.id"), body
    assert "Table 'pm_late_events' is already a table of this project (tables/pm_late_events.json)" in e["error"]
    assert "pm_late_events, pm_late_ticket" in e["fix"] and not (Path(abi.ABI_PROJECTS) / P / "processes/late.json").exists()
    sure("delete_table", project=P, table="pm_late_events")
    # nothing is reserved without a process file: the name was the person's to take, and is free again for the process
    assert save(late)["status"] == "ok" and sql("SELECT count(*) FROM pm_late_ticket") == sql("SELECT count(*) FROM pm_desk_ticket")
    # a person loads a table of that name afterwards: the next build (a refresh of a table it reads) refuses, naming it,
    # and writes nothing over it
    abi.upload(P, "pm_late_ticket", Path(built["dir"] / "tickets.parquet"))
    was = shown("late")["build"]["run"]
    abi.finished(P, abi.call("refresh", project=P, table="tickets"))
    bad = build_of("late", after=was)
    assert bad["status"] == "failed" and bad["error"].startswith("processes/late.json $.id: Table 'pm_late_ticket' is already a table of this project"), bad
    assert sql("SELECT count(*) FROM pm_late_ticket") == [[3]], "the person's table is as they loaded it"
    build_of("desk", after=None)  # the same refresh rebuilt the helpdesk's log
    sure("delete_process", project=P, process="late")
    assert sql("SELECT count(*) FROM pm_late_ticket") == [[3]], "and stays theirs when the process goes"
    sure("delete_table", project=P, table="pm_late_ticket")


def test_roundtrip_export_import_identical(built):
    assert (Path(abi.ABI_PROJECTS) / P / "processes/desk.json").exists()
    r = engine(ROUNDTRIP, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert out["dry"] == [] and out["done"] == [] and out["same"], out
    assert "processes/desk.json" in out["files"] and out["files"] == out["after_build"], "the bundle carries the process; nothing else appears"
    # exported, imported without the file and with it again: back as it was, by the import alone
    r = engine(f"import json\nfrom abi.project import bundle, git\nprint(json.dumps(bundle.bundle(git.path('{P}'))))", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    files = json.loads(r.stdout.splitlines()[-1])
    assert files["processes/desk.json"] == DESK | {"$schema": PLAN["$schema"]}
    assert sure("import_project", project=P, bundle={k: v for k, v in files.items() if k != "processes/desk.json"})["changes"] == ["Removes process 'desk'."]
    # its tables went to the 30-day archive with it: nothing of them is left in the lake for anyone to read, or to deny
    with pytest.raises(abi.ActionError):
        sql("SELECT count(*) FROM pm_desk_ticket")
    assert none_for(built, "pm_desk_events") and none_for(built, "pm_desk_ticket"), "in the running server, while the file is gone"
    assert not {"pm_desk_events", "pm_desk_ticket"} & set(state()["denied"]), "no table left behind"
    assert sure("import_project", project=P, bundle=files)["changes"] == ["Adds process 'desk'."]
    s = state()
    assert s["files"] == {k: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest() for k, v in files.items()}
    build_of("desk")  # the file that came back builds again: its tables are there once more
    assert "pm_desk_events" in s["denied"] and viewer(built, "sql_query", sql="SELECT count(*) FROM pm_desk_events")["rows"] == [[0]]
    assert build_of("desk")["status"] == "ok", "the file that came back builds again"


# ---------- review round 1: what the first review found, each with the test that fails without its fix ----------

def test_tables_left_by_a_revert_stay_denied(built):
    # the running server has worked out what these files deny (readiness reads it), and answered the Viewer with them
    head, before = abi.call("history", project=P)["commits"][0]["id"], state()["files"]
    assert not [g for g in abi.call("readiness", project=P)["missing"] if "pm_ghost" in g]
    assert viewer(built, "sql_query", sql="SELECT count(*) FROM tickets")["rows"] == [[2]]
    ghost = {"id": "ghost", "name": "Ghost", "case": "ticket", "events": [DESK["events"][0]]}  # the tickets' own times (no column of theirs is hidden)
    assert save(ghost)["status"] == "ok"
    assert viewer(built, "sql_query", sql="SELECT count(DISTINCT ticket_id) FROM pm_ghost_ticket")["rows"] == [[2]] and none_for(built, "pm_ghost_events")
    # back to the very files of before (the process was never there): a revert takes its tables to the archive with it, and asks first
    with pytest.raises(abi.ActionError) as e:
        abi.call("revert", project=P, to=head)
    assert e.value.status == 409 and "It removes table(s) pm_ghost_events, pm_ghost_ticket; their data is archived for 30 days." in e.value.body["message"]
    sure("revert", project=P, to=head)
    assert shown("ghost") is None, "the process is gone with the revert"
    with pytest.raises(abi.ActionError):
        sql("SELECT count(*) FROM pm_ghost_ticket")
    # the net under that, for tables no file accounts for: saved and built again, then the files of before put back behind
    # ABI's back (as a restored folder would be), with its tables still in the lake
    assert save(ghost)["status"] == "ok" and viewer(built, "sql_query", sql="SELECT count(DISTINCT ticket_id) FROM pm_ghost_ticket")["rows"] == [[2]]
    r = engine(f"from abi.project import git\nr = git.repo('{P}')\ngit._checkout(r, git._version(r, '{head}').tree_id)", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert state()["files"] == before and shown("ghost") is None and sql("SELECT count(DISTINCT ticket_id) FROM pm_ghost_ticket") == [[3]]
    assert [g for g in abi.call("readiness", project=P)["missing"] if "pm_ghost_ticket" in g and "delete_process" in g]
    assert none_for(built, "pm_ghost_ticket") and none_for(built, "pm_ghost_events"), "nothing says what they were made from: no row for a restricted reader"
    assert viewer(built, "sql_query", sql="SELECT count(*) FROM tickets")["rows"] == [[2]]
    assert save(ghost)["status"] == "ok"  # saved again and deleted: its tables go to the archive
    sure("delete_process", project=P, process="ghost")
    with pytest.raises(abi.ActionError):  # as readiness said
        sql("SELECT count(*) FROM pm_ghost_ticket")


def test_a_definition_that_arrives_is_built(built):
    path = Path(abi.ABI_PROJECTS) / P / "processes/desk.json"
    head, was, files = abi.call("history", project=P)["commits"][0]["id"], build_of("desk"), exported()
    owner = next(r["user_id"] for r in state()["runs"] if r["name"] == "supply")  # who saved it: the owner
    quiet = copy.deepcopy(DESK)
    quiet["clean"]["drop"] = ["Heartbeat", "Note"]  # one event less
    fresh = DESK | {"id": "fresh", "name": "Fresh", "$schema": PLAN["$schema"]}
    # an import changes one definition and brings another that no one saved here: both build, as the importer
    out = sure("import_project", project=P, bundle=files | {"processes/desk.json": quiet | {"$schema": PLAN["$schema"]}, "processes/fresh.json": fresh})
    assert set(out["builds"]) == {"desk", "fresh"}, out
    assert shown("desk")["build"]["run"] == out["builds"]["desk"] != was["run"], "the build of the definition before is not this one's"
    now, new = build_of("desk", after=was["run"]), build_of("fresh")
    assert now["status"] == "ok" and now["events"] == was["events"] - 1 and now["dropped"]["activity"] == was["dropped"]["activity"] + 1, now
    assert new["status"] == "ok" and new["run"] == out["builds"]["fresh"] and "note" not in new, new
    by = {r["run"]: r["user_id"] for r in state()["runs"]}
    assert owner is not None and by[now["run"]] == by[new["run"]] == owner
    m = model_json()
    assert {e["table"]: e["key"] for e in m["entities"] if e["table"].startswith("pm_fresh_")} == {
        "pm_fresh_events": ["event_id"], "pm_fresh_ticket": ["ticket_id", "event_id"]}, "the importer is an Editor: the build wrote the model's entries"
    assert {(r["from"]["columns"][0], r["to"]["table"]) for r in m["relationships"] if r["from"]["table"] == "pm_fresh_ticket"} == {
        ("ticket_id", "tickets"), ("event_id", "pm_fresh_events")}
    # a revert brings the definition of before back: it builds again
    back = sure("revert", project=P, to=head)
    assert set(back["builds"]) == {"desk"} and shown("desk")["build"]["run"] == back["builds"]["desk"] != now["run"], back
    again = build_of("desk", after=now["run"])
    assert again["status"] == "ok" and again["events"] == was["events"]
    with pytest.raises(abi.ActionError):  # the process the import brought is gone with the revert, and its tables with it (to the archive)
        sql("SELECT count(*) FROM pm_fresh_events")
    assert "pm_fresh_ticket" not in state()["denied"], "nothing is left behind"
    # and so does an undo
    abi.call("set_auto_commit", project=P, on=False)
    try:
        assert save(quiet)["events"] == was["events"] - 1
        last = shown("desk")["build"]["run"]
        abi.call("undo", project=P)
        assert json.loads(path.read_text()) == DESK | {"$schema": PLAN["$schema"]} and shown("desk")["build"]["run"] != last
        undone = build_of("desk", after=last)
        assert undone["status"] == "ok" and undone["events"] == was["events"]
    finally:
        abi.call("set_auto_commit", project=P, on=True)


def test_refresh_build_for_someone_no_longer_an_editor_leaves_the_model(built):
    name, pw = f"pi_proc_e_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    abi.call("create_user", name=name, password=pw)
    rel = lambda: [r["id"] for r in model_json()["relationships"] if r["from"]["table"] == "pm_mine_ticket" and r["to"]["table"] == "tickets"]  # noqa: E731
    refresh = lambda: abi.finished(P, abi.call("refresh", project=P, table="tickets"))  # noqa: E731
    try:
        sure("set_role", project=P, user=name, role="editor")
        with abi.as_user(abi.login(name, pw)[1]["token"]):
            assert sure("save_process", project=P, process=DESK | {"id": "mine", "name": "Mine"})["build"]
        first = build_of("mine")
        assert first["status"] == "ok" and "note" not in first and len(rel()) == 1
        # they are a Viewer now, and the relationship their build wrote is gone: the refresh's build writes the tables, not the model
        sure("set_role", project=P, user=name, role="viewer")
        sure("remove_relationship", project=P, id=rel()[0])
        refresh()
        b = build_of("mine", after=first["run"])
        assert b["status"] == "ok" and b["events"] == first["events"] and "no longer an Editor" in b["note"] and "save_process" in b["note"], b
        assert rel() == [], "model.json is not written in the name of someone who may not write it"
        audit = [(a["status"], a["key"] is None) for a in state()["audit"] if a["process"] == "mine" and a["user_name"] == name]
        assert audit == [(200, True), (403, False)], audit  # (the audit log outlives a project: an earlier run's rows are another account's)
        # an Editor again: the next refresh's build writes it
        sure("set_role", project=P, user=name, role="editor")
        refresh()
        c = build_of("mine", after=b["run"])
        assert c["status"] == "ok" and "note" not in c and len(rel()) == 1, c
    finally:
        idle()
        if shown("mine"):
            sure("delete_process", project=P, process="mine")
        sure("delete_user", name=name)


def test_lineage_reads_like_a_model(built):
    lineage = lambda node, way: set(abi.call("lineage", project=P, object=node, direction=way)["objects"])  # noqa: E731
    # downstream of the process: the tables its build writes and what reads them; upstream: what it reads, never its own tables
    assert {"table:pm_supply_events", "table:pm_supply_purchase_order", "table:pm_supply_delivery", "measure:received",
            "measure:order_steps"} <= lineage("process:supply", "downstream")
    up = lineage("process:supply", "upstream")
    assert {"table:supply_events", "table:supply_event_orders", "table:purchase_orders", "object:purchase_order", "object:delivery"} <= up
    assert not [n for n in up if n.startswith("table:pm_")], up
    assert {"process:supply", "table:supply_events", "table:purchase_orders"} <= lineage("table:pm_supply_purchase_order", "upstream")
    assert {"process:supply", "table:pm_supply_events", "measure:log_events"} <= lineage("table:supply_events", "downstream")


LONG = r"""
import json, sys
import abi.app  # noqa: F401
from abi import process
from abi.project import bundle, git
a = json.loads(sys.stdin.read())
files = bundle.bundle(git.path(a["project"])) | {f"processes/{a['process']['id']}.json": a["process"]}
print(json.dumps(process.references(a["project"], json.loads(json.dumps(files).replace('"ticket"', json.dumps(a["type"]))))))
"""


def test_names_are_checked_across_sources_and_against_the_builds_own(built):
    def one(p: dict) -> dict:
        [e] = refused(422, "save_process", process=p)["errors"]
        assert e["file"] == "processes/talk.json" and e["fix"], e
        return e

    # one attribute, two types
    e = one(TALK | {"events": [DESK["events"][1], TALK["events"][0] | {"attributes": ["minutes"]}]})
    assert e["path"] == "$.events[1].attributes[0]" and "'minutes' is VARCHAR here and 'minutes' is BIGINT in events[0]" in e["error"] and "set_type" in e["fix"], e
    # a name the log makes itself, in any case, and a name of the build's own
    for a in ("Seq", "__rn"):
        p = copy.deepcopy(TALK)
        p["events"][0]["attributes"] = [a]
        e = one(p)
        assert e["path"] == "$.events[0].attributes[0]" and f"'{a}' is the name of a column the event log makes itself" in e["error"], e
    # a table a build writes is no source: another process's, or its own
    p = copy.deepcopy(TALK)
    p["events"][0]["table"] = "pm_desk_events"
    e = one(p)
    assert e["path"] == "$.events[0].table" and e["error"].startswith("'pm_desk_events' is a table the build of process 'desk' writes") and "'desk' reads" in e["fix"], e
    p = copy.deepcopy(TALK)
    p["events"][1]["objects"][0]["through"] = "pm_talk_ticket"
    assert one(p)["path"] == "$.events[1].objects[0].through"
    # a hidden table is said to be hidden
    abi.call("set_table", project=P, table="chats", hidden=True)
    try:
        e = one(TALK)
        assert e["path"] == "$.events[1].table" and e["error"] == "Table 'chats' is hidden (staging), so it is out of the model." and "hidden=false" in e["fix"], e
    finally:
        abi.call("set_table", project=P, table="chats", hidden=False)
    # names that would be cut: a process with an id of 31 characters about a type with a long id (the ticket's, renamed in the files checked)
    pid = "p" + "x" * 30

    def long(typ: str) -> list:
        r = engine(LONG, timeout=120, stdin=json.dumps({"project": P, "type": typ, "process": DESK | {"id": pid, "$schema": PLAN["$schema"]}}))
        assert r.returncode == 0, r.stderr[-2000:]
        return [(e["path"], e["error"]) for e in json.loads(r.stdout.splitlines()[-1]) if e["file"] == f"processes/{pid}.json" and e["fix"]]

    assert long("ticket") == []
    got = long("ticket_of_the_desks")  # pm_<31>_<19>: 54 characters, and 64 with -ticket_id
    assert [x[0] for x in got] == ["$.events[0].objects[0].type", "$.events[1].objects[0].type"] and "an ID longer than 63 characters" in got[0][1], got
    got = long("ticket_of_the_desks2")
    assert got[0][0] == "$.id" and "a name longer than 54 characters" in got[0][1], got


def test_two_sources_with_ids_and_a_bridge_of_one(built):
    b = save(TALK)
    assert b["status"] == "ok" and b["events"] == 5 and b["dropped"] == dict.fromkeys(b["dropped"], 0) and b["several"] == {"ticket": 1}, b
    # calls 1 to 3 and chats 1 and 2: each id says whose it is
    assert sql("SELECT event_id, activity FROM pm_talk_events ORDER BY ord") == [
        ["0:1", "Call"], ["0:2", "Call"], ["1:1", "Chat"], ["1:2", "Chat"], ["0:3", "Call"]]
    # the bridge table names chats: chat 1 is about T1 and T2; its row for a chat 3 is about no event, not about call 3
    assert sql("SELECT ticket_id, event_id FROM pm_talk_ticket ORDER BY 1, seq") == [
        ["T1", "0:2"], ["T1", "1:1"], ["T2", "1:1"], ["T2", "0:3"], ["T3", "0:1"], ["T3", "1:2"]]
    sure("delete_process", project=P, process="talk")


def test_what_a_person_set_on_a_built_table_stays(built):
    table = lambda t: next(x for x in model_json()["tables"] if x["table"] == t)  # noqa: E731
    abi.call("set_table", project=P, table="pm_desk_ticket", description="The helpdesk, ticket by ticket.", hidden=True)
    try:
        assert save(DESK | {"description": "Built once more."})["status"] == "ok"
        t = table("pm_desk_ticket")
        assert (t["description"], t["hidden"]) == ("The helpdesk, ticket by ticket.", True), t
        assert next(e["key"] for e in model_json()["entities"] if e["table"] == "pm_desk_ticket") == ["ticket_id", "event_id"]
        assert table("pm_desk_events")["description"].endswith("Written by its build.")
    finally:
        abi.call("set_table", project=P, table="pm_desk_ticket", hidden=False)
    assert save(DESK)["status"] == "ok"
    # a relationship a person adds from an attribute's column of a build's table to another table stays too
    rid = abi.call("add_relationship", project=P, **{"from": "pm_desk_ticket.minutes", "to": "pings.n"})["id"]
    try:
        assert save(DESK | {"description": "Built once more."})["status"] == "ok"
        assert [r["to"]["table"] for r in model_json()["relationships"] if r["id"] == rid] == ["pings"]
    finally:
        sure("remove_relationship", project=P, id=rid)
    assert save(DESK)["status"] == "ok"


QUEUE = r"""
import json, sys, uuid
import abi.app  # noqa: F401
from abi import db, process
a = json.loads(sys.stdin.read())
P, N, out = a["project"], a.get("name", "desk"), {}
with db.connect() as c:
    if a["do"] == "wait":  # a build that waits for the worker (no job behind it: it stays queued), then two more asked for
        out["run"] = str(uuid.uuid4())
        c.execute("INSERT INTO analysis_runs (id, project, kind, name, key, status) VALUES (%s, %s, 'process', %s, 'waiting', 'queued')", [out["run"], P, N])
        c.commit()
        out["got"] = [process._queue(c, P, N, None), process._queue(c, P, N, a.get("user"), "snapshot-0")]
        out["user"] = c.execute("SELECT user_id FROM analysis_runs WHERE id = %s", [out["run"]]).fetchone()["user_id"]
    elif a["do"] == "dead":  # a run queued and one running since 31 minutes: their job is lost, their outcome never written
        for name, key, status in (("desk", "waiting", "queued"), ("desk", "old-running", "running"), ("supply", "old-other", "queued")):
            c.execute("INSERT INTO analysis_runs (id, project, kind, name, key, status, created, started) VALUES (%s, %s, 'process', %s, %s, %s,"
                      " now() - interval '32 minutes', CASE WHEN %s = 'running' THEN now() - interval '31 minutes' END)", [str(uuid.uuid4()), P, name, key, status, status])
    elif a["do"] == "no queue":  # the job can't be queued: the action fails and its transaction rolls back
        from abi import worker
        def no(*x, **k):
            raise RuntimeError("no queue")
        worker.defer = no
        try:
            process._queue(c, P, "desk", None)
        except RuntimeError:
            c.rollback()
        out["queued"] = c.execute("SELECT count(*) AS n FROM analysis_runs WHERE project = %s AND name = 'desk' AND status = 'queued'", [P]).fetchone()["n"]
    elif a["do"] == "old":  # thirty builds of days ago
        for i in range(30):
            c.execute("INSERT INTO analysis_runs (id, project, kind, name, key, status, created) VALUES (%s, %s, 'process', 'desk', %s, 'ok',"
                      " now() - interval '1 day' * %s)", [str(uuid.uuid4()), P, f"old-{i}", i + 1])
    else:
        c.execute("DELETE FROM analysis_runs WHERE project = %s AND (key = 'waiting' OR key LIKE 'old-%%')", [P])
    c.commit()
    out["rows"] = c.execute("SELECT count(*) AS n FROM analysis_runs WHERE project = %s AND name = %s", [P, N]).fetchone()["n"]
print(json.dumps(out))
"""


def test_one_build_waits_at_a_time_and_twenty_runs_are_kept(built):
    def queue(do: str, **more) -> dict:
        r = engine(QUEUE, timeout=120, stdin=json.dumps({"project": P, "do": do} | more))
        assert r.returncode == 0, r.stderr[-2000:]
        return json.loads(r.stdout.splitlines()[-1])

    n = len([r for r in state()["runs"] if r["name"] == "desk"])
    owner = next(r["user_id"] for r in state()["runs"] if r["name"] == "supply")
    try:
        got = queue("wait", user=owner)
        assert got["got"] == [got["run"]] * 2 and got["rows"] == n + 1, "a build that waits is the build asked for: nothing more is queued"
        assert got["user"] == owner, "and it runs for whoever asked last"
        abi.finished(P, abi.call("refresh", project=P, table="suppliers"))
        abi.finished(P, abi.call("refresh", project=P, table="tickets"))
        time.sleep(3)
        assert len([r for r in state()["runs"] if r["name"] == "desk"]) == n + 1 and shown("desk")["build"]["status"] == "queued"
        # a project with a build queued is not deleted (and not renamed): refused, with no question to say yes to
        with pytest.raises(abi.ActionError) as e:
            abi.call("delete_project", project=P)
        assert e.value.status == 409 and "confirm_token" not in e.value.body and "an event log being built" in e.value.body["message"], e.value.body
    finally:
        assert queue("clear")["rows"] == n
    # a save that takes a table away waits for the build that would write it, as a delete does
    orders = copy.deepcopy(SUPPLY)
    del orders["events"][0]["objects"][0]
    try:
        queue("wait", name="supply")
        body = refused(409, "save_process", process=orders)
        assert "is being built right now, and this change takes pm_supply_delivery away" in body["message"] and "confirm_token" not in body, body
    finally:
        queue("clear", name="supply")
    # a run whose job is lost (queued, or running, for longer than a build takes) holds nothing up: the project can be deleted
    # (asked, not done here), and the next save builds
    try:
        queue("dead")
        with pytest.raises(abi.ActionError) as e:
            abi.call("delete_project", project=P)
        assert e.value.status == 409 and e.value.body["confirm_token"], e.value.body
        assert save(DESK | {"description": "After a lost job."})["status"] == "ok"
        # (ended failed; with twenty newer runs they are no longer kept at all)
        assert {r["status"] for r in state()["runs"] if r["key"] in ("waiting", "old-running")} <= {"failed"}
        # another process's run that waits as long (behind a long load, say) is left alone: its job is still to come, and nobody would queue it again
        assert [r["status"] for r in state()["runs"] if r["key"] == "old-other"] == ["queued"]
    finally:
        queue("clear")
    got = queue("no queue")
    assert got["queued"] == 0, "a run whose job could not be queued is not left waiting"
    n = got["rows"]  # the runs kept by now (at most twenty)
    try:
        assert queue("old")["rows"] == n + 30
        assert save(DESK | {"description": "Built once more."})["status"] == "ok"
        runs = [r for r in state()["runs"] if r["name"] == "desk"]
        assert len(runs) == 20 and runs[-1]["run"] == shown("desk")["build"]["run"], "the latest twenty stay"
    finally:
        queue("clear")
    assert save(DESK)["status"] == "ok"


def test_propose_names_no_hidden_id_and_keeps_every_time(built):
    source = lambda got: next(s for d in got["processes"] for s in d["events"] if s["table"] == "supply_events")  # noqa: E731
    sure("set_column_rule", project=P, id="event", table="supply_events", columns=["event_id"])
    try:
        mine, full = source(viewer(built, "propose_process")), source(abi.call("propose_process", project=P))
        # the event's id is hidden from the Viewer: their draft names none, and no bridge table that would join on it
        assert full["id"] == "event_id" and "id" not in mine and "event_id" not in json.dumps(mine), mine
        assert mine["objects"] == [{"type": "delivery", "columns": ["delivery_id"]}] and mine["activity"] == "activity"
    finally:
        sure("delete_rls_rule", project=P, id="event")
    # two time columns that would get one activity name keep their own
    r = engine("import abi.app\nfrom abi import process\nprint(process._stamps(['created_at', 'created_on', 'closed_at']))", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.splitlines()[-1] == "{'Created': 'created_at', 'Created on': 'created_on', 'Closed': 'closed_at'}"


DURING = r"""
import json, sys
import abi.app  # noqa: F401
from abi import db, process, security, storage
from abi.project import git
P = json.loads(sys.stdin.read())["project"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
f, commit, wrote = git.path(P) / "processes/desk.json", storage._commit, []
was = f.read_bytes()
assert b'"Helpdesk"' in was
def during(*x, **k):  # the definition changes while the build writes its tables
    snap = commit(*x, **k)
    f.write_bytes(was.replace(b'"Helpdesk"', b'"Help desk"'))
    return snap
storage._commit, process._model = during, lambda *x: wrote.append(x)
try:
    out = process.build(P, "desk", owner)
finally:
    f.write_bytes(was)
print(json.dumps({"note": out.get("note"), "model": len(wrote), "events": out["events"]}))
"""


def test_a_build_overtaken_by_a_change_writes_no_model_entries(built):
    r = engine(DURING, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert out["model"] == 0 and out["note"].startswith("The process was changed or deleted while its event log was built"), out
    assert out["events"] == shown("desk")["build"]["events"]


LATER = r"""
import json, sys
import abi.app  # noqa: F401
from abi import db, security
from abi.project import bundle, git, imports
P = json.loads(sys.stdin.read())["project"]
runs = lambda c: c.execute("SELECT count(*) AS n FROM analysis_runs WHERE project = %s AND name = 'desk'", [P]).fetchone()["n"]
before = bundle.bundle(git.path(P))
with db.connect() as c:
    owner, n = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"]), runs(c)
    try:
        with git.change(P, owner, "import_project"):  # as an action that does more after the import, and fails there
            imports.apply(c, owner, P, before | {"processes/desk.json": before["processes/desk.json"] | {"description": "Changed."}})
            raise RuntimeError("a later step fails")
    except RuntimeError:
        c.rollback()
    print(json.dumps({"queued": runs(c) - n, "restored": bundle.bundle(git.path(P)) == before}))
"""


def test_no_build_is_queued_for_a_change_that_is_rolled_back(built):
    r = engine(LATER, timeout=300, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == {"queued": 0, "restored": True}, "the import itself queues nothing: whoever changed the files does, last"
    idle()


def test_an_instant_reads_as_the_projects_time_zone(built):
    was = abi.call("project_locale", project=P)["time_zone"]
    abi.call("set_project_locale", project=P, time_zone="Europe/Berlin")
    try:
        # 00:30 in Berlin on 1 March is 23:30 UTC the evening before: on or after the filter's 1 March, and on 1 March in the log
        b = save(ZONE)
        assert b["status"] == "ok" and b["events"] == 3 and b["dropped"]["filtered"] == 0, b
        assert sql("SELECT activity, strftime(started_at, '%Y-%m-%d %H:%M') FROM pm_zone_events ORDER BY ord") == [
            ["Ping", "2025-03-01 00:30"], ["Nap", "2025-03-01 00:40"], ["Pong", "2025-03-01 00:45"]], "the wall-clock source between the two instants"
        assert sql("SELECT activity, next_activity, wait_seconds FROM pm_zone_ticket ORDER BY seq") == [
            ["Ping", "Nap", 600.0], ["Nap", "Pong", 300.0], ["Pong", None, None]]
    finally:
        if shown("zone"):
            idle()
            sure("delete_process", project=P, process="zone")
        abi.call("set_project_locale", project=P, time_zone=was or "")


def test_propose_drafts_stamps_beside_free_text_and_only_what_a_save_takes(built):
    rels = [abi.call("add_relationship", project=P, **{"from": f, "to": "tickets.ticket_id"})["id"] for f in ("trips.ticket_ref", "calls.about")]
    try:
        by = {s["table"]: s for d in abi.call("propose_process", project=P)["processes"] for s in d["events"]}
        # three times, a way to a ticket and a text column where no two rows say the same: the times are the steps
        assert by["trips"] == {"table": "trips", "stamps": {"Booked": "booked_at", "Left": "left_at", "Back": "back_at"},
                               "objects": [{"type": "ticket", "columns": ["ticket_ref"]}]}
        # columns named like the log's own (Seq, __rn) are no attributes of a draft: the save takes it as it is
        assert by["calls"]["activity"] == "activity" and by["calls"]["attributes"] == ["minutes"], by["calls"]
        with pytest.raises(abi.ActionError) as e:
            abi.call("save_process", project=P, process={"id": "drafted", "name": "Drafted", "case": "ticket", "events": [by["calls"]]})
        assert e.value.status == 409 and e.value.body["confirm_token"], e.value.body
    finally:
        for r in rels:
            sure("remove_relationship", project=P, id=r)


def test_a_change_of_the_time_zone_builds_the_log_again(built):
    was = abi.call("project_locale", project=P)["time_zone"]
    abi.call("set_project_locale", project=P, time_zone="UTC")
    try:
        # in UTC both instants are the evening of 28 February: before the filter's 00:40 on 1 March
        b = save(ZONED)
        assert b["status"] == "ok" and b["events"] == 0 and b["dropped"]["filtered"] == 2, b
        # the project moves to Berlin: nobody saves the process, its log is built again and holds Berlin's wall-clock times
        # (00:30 and 00:45 on 1 March; the filter, on the column named like the log's own, keeps the second)
        abi.call("set_project_locale", project=P, time_zone="Europe/Berlin")
        assert shown("zoned")["build"]["run"] != b["run"], "another time zone queues the build"
        again = build_of("zoned", after=b["run"])
        assert again["status"] == "ok" and again["events"] == 1 and again["dropped"]["filtered"] == 1, again
        assert sql("SELECT activity, strftime(started_at, '%Y-%m-%d %H:%M') FROM pm_zoned_events") == [["Pong", "2025-03-01 00:45"]]
        # a filter's value for an instant is that wall-clock time: one with an offset is refused, not read without it
        for v in ("2025-03-01 00:40:00+01", "2025-03-01T00:40:00Z", ["2025-03-01 00:40:00-05:00"],
                  "2025-03-01 00:40:00+01 ", "2025-03-01 00:40:00 UTC", "2025-03-01 00:40:00+01:00:00"):  # M2: a space after, a zone's name, seconds
            late = copy.deepcopy(ZONED)
            late["clean"]["where"][0] |= {"value": v, "op": "in" if isinstance(v, list) else ">="}
            [e] = refused(422, "save_process", process=late)["errors"]
            assert (e["file"], e["path"]) == ("processes/zoned.json", "$.clean.where[0].value") and "has an offset" in e["error"] and e["fix"], e
    finally:
        abi.call("set_project_locale", project=P, time_zone=was or "")
        idle()
        if shown("zoned"):
            sure("delete_process", project=P, process="zoned")


def test_a_revert_names_no_table_a_build_never_wrote(built):
    # M2 (carried from M1's review): a process whose first build failed wrote no table, so taking it away by a revert
    # names none of its tables as going to the archive (there was a question about them before), and asks nothing
    head = abi.call("history", project=P)["commits"][0]["id"]
    never = copy.deepcopy(DESK) | {"id": "never", "name": "Never built"}
    never["clean"]["where"][0]["value"] = "last year"
    assert save(never)["status"] == "failed"
    with pytest.raises(abi.ActionError):
        sql("SELECT count(*) FROM pm_never_events")
    out = abi.call("revert", project=P, to=head)
    assert shown("never") is None and out["archived"] == [], out
