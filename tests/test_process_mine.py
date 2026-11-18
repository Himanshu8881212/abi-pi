"""1.2.7 M2 (needs the engine and its worker at :8780, no LLM): reading a process's event log as the person. The map
(directly-follows graph), the variants, the performance and the usual way (the heuristic miner) are each ONE query
through the semantic engine as the caller, over one object type's table: row rules apply and the read is one row of
their query log. One `filters` shape for all of them, each a semi-join on the case key, composing with AND. The logs:
test_process_log's helpdesk (every figure known by hand), Brewline's supply process (known answers from the generator
and from the source tables) and a log of jobs with a planted model below."""
import copy
import json
import statistics
from pathlib import Path

import pytest
from conftest import parquet
from jsonschema import Draft202012Validator
from test_engine import engine
from test_pages import sure
from test_process_log import DESK, ONTO, P, ROOT, SCHEMA, SUPPLY, TRACES, build_of, built, deep, log, save, shown, sql, viewer  # noqa: F401

import abi

# The planted model: A then B then C, B repeated once in ten jobs (a loop of length one), D and E twice round (a loop of
# length two: D E D), and one job going from A straight to C (a pair seen once)
PLANTED = [("A B C", 10), ("A B B C", 10), ("A D E D C", 10), ("A C", 1)]
JOBS = {"id": "jobs", "name": "Jobs", "case": "job", "events": [
    {"table": "job_events", "id": "event_id", "activity": "activity", "start": "at", "objects": [{"type": "job", "columns": ["job_id"]}]}]}
UNION = TRACES.split("FROM (", 1)[1].rsplit(") GROUP BY 1", 1)[0]  # each purchase order's steps in order, from the source tables
# the helpdesk's three tickets as variants (test_process_log: T1, T2 and T3 in order)
T1, T2, T3 = ("Opened → Assigned → Note → Call → Call → Closed", "Opened → Assigned → Reply → Call → Reply", "Opened → Escalation → Call → Closed")
LOGGED = r"""
import json, sys
from abi import db
a = json.loads(sys.stdin.read())
with db.connect() as c:
    if a.get("after") is None:
        print(json.dumps(c.execute("SELECT coalesce(max(id), 0) AS n FROM query_log").fetchone()["n"]))
    else:
        print(json.dumps([dict(r) for r in c.execute("SELECT user_id, query FROM query_log WHERE id > %s AND project = %s ORDER BY id",
                                                     [a["after"], a["project"]])], default=str))
"""


def logged(after: int | None = None):
    r = engine(LOGGED, timeout=120, stdin=json.dumps({"project": P, "after": after}))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


@pytest.fixture(scope="module")
def mined(built):
    """test_process_log's project with both its processes built, and the jobs' process."""
    d, ev = built["dir"], ["event_id,job_id,activity,at"]
    jobs = [(f"J{i + 1:02d}", trace) for i, trace in enumerate(t for t, n in PLANTED for _ in range(n))]
    for i, (job, trace) in enumerate(jobs):  # ten minutes between steps, each job its own hour
        ev += [f"{len(ev) + j},{job},{a},2025-01-{1 + i % 28:02d} {8 + i // 28:02d}:{10 * j:02d}:00" for j, a in enumerate(trace.split())]
    (d / "jobs.csv").write_text("job_id\n" + "\n".join(j for j, _ in jobs) + "\n")
    (d / "job_events.csv").write_text("\n".join(ev) + "\n")
    for t in ("jobs", "job_events"):
        abi.upload(P, t, Path(parquet(d / f"{t}.csv")[0]))
    for t, k in (("jobs", "job_id"), ("job_events", "event_id")):  # into the model, keyed
        abi.call("set_table", project=P, table=t, key=[k])
    sure("save_ontology", project=P, ontology={"objects": [*ONTO["objects"], {"id": "job", "table": "jobs", "name": "Job", "plural": "Jobs", "title": "job_id"}]})
    b = save(JOBS)
    assert b["status"] == "ok", b
    return built | {"jobs": dict(jobs)}


def read(action: str, token: str | None = None, **args) -> dict:
    with abi.as_user(token):
        return abi.call(action, project=P, **args)


def cases(process: str = "desk", token: str | None = None, **args) -> set:
    """The variants of the cases the filters keep: for the helpdesk each ticket has its own."""
    return {r[0] for r in read("process_variants", token, process=process, top=100, **args)["variants"]["rows"]}


def refused(action: str, **args) -> dict:
    with pytest.raises(abi.ActionError) as e:
        read(action, **args)
    return e.value


# ---------- #16 discovery: the directly-follows graph ----------

def test_dfg_counts_match_known_answers(mined):
    m = read("process_map", process="desk")
    assert (m["type"], m["per"], m["totals"]["rows"]) == ("ticket", "per Ticket", [[3, 15]])
    # by hand (test_process_log: T1 Opened Assigned Note Call Call Closed, T2 Opened Assigned Reply Call Reply, T3 Opened
    # Escalation Call Closed); only calls have an end: 20, 10 and 15 minutes (the fourth has none), so a median of 0.25 h
    assert m["activities"]["columns"] == ["activity", "cases", "events", "median_duration_hours"]
    assert m["activities"]["rows"] == [["Call", 3, 4, 0.25], ["Opened", 3, 3, None], ["Assigned", 2, 2, None], ["Closed", 2, 2, None],
                                       ["Reply", 1, 2, None], ["Escalation", 1, 1, None], ["Note", 1, 1, None]]
    assert m["pairs"]["columns"] == ["from", "to", "cases", "count", "median_wait_hours", "p90_wait_hours"]
    waits = {("Call", "Closed"): [78600, 900], ("Opened", "Assigned"): [1800, 0], ("Assigned", "Note"): [5400], ("Assigned", "Reply"): [82800],
             ("Call", "Call"): [2400], ("Call", "Reply"): [0], ("Escalation", "Call"): [1800], ("Note", "Call"): [0], ("Opened", "Escalation"): [0],
             ("Reply", "Call"): [0]}
    assert [tuple(r[:2]) for r in m["pairs"]["rows"]] == [("Call", "Closed"), ("Opened", "Assigned"), *sorted(k for k, w in waits.items() if len(w) == 1)]
    for a, b, n, count, med, p90 in m["pairs"]["rows"]:
        w = sorted(waits[a, b])
        assert (n, count) == (len(w), len(w)) and med == pytest.approx(statistics.median(w) / 3600, abs=0.006), (a, b)
        assert p90 == pytest.approx((w[0] + 0.9 * (w[-1] - w[0])) / 3600, abs=0.006), (a, b)  # interpolated, as quantile_cont
    # Brewline per purchase order: every pair and its counts as the source tables say, read another way
    oracle, acts = deep(
        f"""SELECT a, b, count(DISTINCT po_id), count(*) FROM (SELECT po_id, activity AS a, lead(activity) OVER (PARTITION BY po_id ORDER BY t, o) AS b
          FROM ({UNION})) WHERE b IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC, 4 DESC, 1, 2""",
        f"SELECT activity, count(DISTINCT po_id) FROM ({UNION}) GROUP BY 1 ORDER BY 1")
    s = read("process_map", process="supply")
    assert [r[:4] for r in s["pairs"]["rows"]] == oracle and s["per"] == "per Purchase order"
    assert sorted([r[0], r[1]] for r in s["activities"]["rows"]) == acts
    assert s["totals"]["rows"][0][0] == mined["known"]["rows"]["purchase_orders"]


def test_start_and_end_activities(mined):
    m = read("process_map", process="desk")
    assert (m["start"], m["end"]) == ({"columns": ["activity", "cases"], "rows": [["Opened", 3]]},
                                      {"columns": ["activity", "cases"], "rows": [["Closed", 2], ["Reply", 1]]})
    [last] = deep(f"""SELECT a, count(*) FROM (SELECT activity AS a, row_number() OVER (PARTITION BY po_id ORDER BY t DESC, o DESC) AS r
      FROM ({UNION})) WHERE r = 1 GROUP BY 1 ORDER BY 2 DESC, 1""")
    s = read("process_map", process="supply")
    assert s["start"]["rows"] == [["Order placed", mined["known"]["rows"]["purchase_orders"]]]
    assert s["end"]["rows"] == last and last[0][0] == "Invoice paid"
    # each case starts once and ends once
    assert sum(r[1] for r in s["start"]["rows"]) == sum(r[1] for r in s["end"]["rows"]) == s["totals"]["rows"][0][0]


def test_detail_says_what_it_left_out(mined):
    m = read("process_map", process="desk", detail=0.5)
    # the most frequent activities until they hold half of the 13 visits (cases summed): Call 3, Opened 3, Assigned 2;
    # then the pairs among them until half their cases: Opened → Assigned (2 of 3)
    assert [r[0] for r in m["activities"]["rows"]] == ["Call", "Opened", "Assigned"]
    assert [r[:2] for r in m["pairs"]["rows"]] == [["Opened", "Assigned"]] and m["left_out"] == {"activities": 4, "pairs": 9}
    assert "3 of 7 activities and 1 of 10 pairs are shown; 4 activities and 9 pairs are left out" in m["note"]
    assert m["start"]["rows"] == [["Opened", 3]] and m["end"]["rows"] == [], "the start and end of what is shown"
    whole = read("process_map", process="desk")
    assert whole["left_out"] == {"activities": 0, "pairs": 0} and "left out" not in whole["note"]


# ---------- #17 variants ----------

def test_variants_by_string_agg_shares_sum_to_100(mined):
    v = read("process_variants", process="desk")
    assert v["variants"]["columns"] == ["variant", "id", "cases", "share_pct", "median_cycle_hours"] and v["totals"]["rows"] == [[3, 3]]
    # one ticket each, the text in order (ties by the text); the cycle from the first start to the last end: 25 h, 23 h, 1 h
    assert [[r[0], r[2], r[3], r[4]] for r in v["variants"]["rows"]] == [[T1, 1, 33.3333, 25.0], [T2, 1, 33.3333, 23.0], [T3, 1, 33.3333, 1.0]]
    two = read("process_variants", process="desk", top=2)["variants"]["rows"]  # (review round 1: ties at the cut go by ID, not by text)
    assert {r[0] for r in two[:2]} < {T1, T2, T3} and two[2][0] == "other variants (1)" and two[2][1:4] == [None, 1, 33.3333]
    # Brewline: the happy path first, as many orders as the generator planted; the shares of all rows make 100
    k = mined["known"]
    s = read("process_variants", process="supply", top=3)
    rows, [[[paths]]] = s["variants"]["rows"], deep(f"SELECT count(DISTINCT path) FROM ({TRACES})")
    assert rows[0][0] == " → ".join(k["happy_path"]["steps"]) and rows[0][2] == k["happy_path"]["orders"]
    assert abs(rows[0][3] - 100 * k["happy_path"]["share"]) < 0.01
    assert len(rows) == 4 and rows[3][0] == f"other variants ({paths - 3})" and s["totals"]["rows"] == [[k["rows"]["purchase_orders"], paths]]
    assert sum(r[2] for r in rows) == k["rows"]["purchase_orders"] and sum(r[3] for r in rows) == pytest.approx(100, abs=0.001)


def test_variant_id_is_stable(mined):
    ids = {r[0]: r[1] for r in read("process_variants", process="desk")["variants"]["rows"]}
    # a hash of the activities in their places, by MD5 (review round 1: worked out in the build, not from the text):
    # the same wherever, whoever and whenever it is read
    vid = lambda v: ("SELECT substring(md5(CAST(" + " + ".join(  # noqa: E731
        f"md5_number_lower('{i}:{a}')::HUGEINT" for i, a in enumerate(v.split(" → "), 1)) + " AS VARCHAR)), 1, 12)")
    assert ids == {v: i for v, [[i]] in zip((T1, T2, T3), deep(*map(vid, (T1, T2, T3))))}
    happy = " → ".join(mined["known"]["happy_path"]["steps"])
    mine = {r[0]: r[1] for r in read("process_variants", mined["viewer"], process="supply")["variants"]["rows"]}
    assert mine[happy] == deep(vid(happy))[0][0][0], "the restricted Viewer's id is the owner's"
    assert save(DESK | {"description": "Built again."})["status"] == "ok" and save(DESK)["status"] == "ok"
    assert {r[0]: r[1] for r in read("process_variants", process="desk")["variants"]["rows"]} == ids, "a rebuild keeps the ids"
    assert cases(filters=[{"kind": "variant", "in": [ids[T2]]}]) == {T2}
    assert cases(filters=[{"kind": "variant", "in": [ids[T2]], "not": True}]) == {T1, T3}


# ---------- #18 a model from the log: the heuristic miner ----------

def test_heuristic_miner_finds_planted_model(mined):
    got = read("discover_process", process="jobs")
    assert got["reference"] == {"from": "discovered", "start": ["A"], "end": ["C"],
                                "flows": [["A", "B"], ["A", "D"], ["B", "B"], ["B", "C"], ["D", "C"], ["D", "E"], ["E", "D"]], "repeat": {"B": 2, "D": 2}}
    assert not list(Draft202012Validator(SCHEMA).iter_errors({"$schema": SCHEMA["$id"], **JOBS, "reference": got["reference"]})), "a process's reference"
    assert got["activities"]["rows"] == [["A", 31, 1, True], ["C", 31, 1, True], ["B", 20, 2, True], ["D", 10, 2, True], ["E", 10, 1, True]]
    assert "nothing is saved" in got["note"] and "reference" not in json.loads((Path(abi.ABI_PROJECTS) / P / "processes/jobs.json").read_text())
    # Brewline's orders placed before October 2025 (all of them complete): the planted happy path with its rework loop;
    # the three planted deviations (1 % of the orders and less) are noise
    b = read("discover_process", process="supply", filters=[{"kind": "time", "when": "started", "to": "2025-10-01"}])["reference"]
    assert b == {"from": "discovered", "start": ["Order placed"], "end": ["Invoice paid"], "repeat": {"Invoice received": 2},
                 "flows": [["Goods received", "Invoice received"], ["Invoice received", "Invoice paid"], ["Invoice received", "Invoice received"],
                           ["Order approved", "Goods received"], ["Order placed", "Order approved"]]}, b


def test_miner_drops_rare_pair_below_threshold(mined):
    pairs = {(a, b): r for a, b, *r in read("discover_process", process="jobs")["pairs"]["rows"]}
    # A to C once: a dependency of 1 / (1 + 0 + 1), below 0.9; kept with a threshold under it
    assert pairs["A", "C"] == [1, 0.5, False] and pairs["A", "B"] == [20, 0.9524, True] and pairs["D", "C"] == [10, 0.9091, True]
    low = read("discover_process", process="jobs", threshold=0.4)["reference"]["flows"]
    assert ["A", "C"] in low and ["A", "C"] not in read("discover_process", process="jobs")["reference"]["flows"]
    # Brewline: paying before the goods came is a dependable pair (it never goes the other way) but rare: under 2 % of the orders
    s = {(a, b): r for a, b, *r in read("discover_process", process="supply")["pairs"]["rows"]}
    n, dep, kept = s["Order approved", "Invoice paid"]
    assert dep >= 0.9 and not kept and n < 0.02 * mined["known"]["rows"]["purchase_orders"], s["Order approved", "Invoice paid"]


def test_miner_keeps_short_loops(mined):
    got = read("discover_process", process="jobs")
    pairs = {(a, b): r for a, b, *r in got["pairs"]["rows"]}
    # B after B ten times: 10 / 11; D and E follow each other ten times each way, so their own dependency is (10 - 10) /
    # 21 = 0, and the loop of length two keeps both: D E D ten times, (10 + 0) / (10 + 0 + 1)
    assert pairs["B", "B"] == [10, 0.9091, True]
    assert pairs["D", "E"] == [10, 0.9091, True] and pairs["E", "D"] == [10, 0.9091, True]
    assert got["reference"]["repeat"] == {"B": 2, "D": 2}, "how often a step in a loop came at most in one job"
    # without the loop's own measure (a threshold it doesn't reach) D and E no longer follow each other
    strict = read("discover_process", process="jobs", threshold=0.95)["reference"]
    assert ["D", "E"] not in strict["flows"] and ["B", "B"] not in strict["flows"] and ["A", "B"] in strict["flows"]


# ---------- #19 performance and bottlenecks ----------

def test_throughput_per_month_known(mined):
    p = read("process_performance", process="desk", period="day")
    # T1 from 1 to 2 March, T2 from 1 to 2 March, T3 on 3 March; with no reference nothing says a case has ended: each
    # counts in the period of its last event so far (review round 1: no longer named `ended`)
    assert p["throughput"] == {"columns": ["period", "started", "last_event"], "rows": [["2025-03-01", 2, 0], ["2025-03-02", 0, 2], ["2025-03-03", 1, 1]]}
    assert "nothing says which cases have ended" in p["note"]
    # cycle times of 1, 23 and 25 hours, interpolated as quantile_cont
    assert p["cycle"]["rows"] == [[3, 16.33, 5.4, 12.0, 23.0, 24.0, 24.6, 25.0]]
    started, ended = deep(
        "SELECT strftime(date_trunc('month', placed_at), '%Y-%m-%d'), count(*) FROM purchase_orders GROUP BY 1 ORDER BY 1",
        # a case ends at its last event's end (an unloading has one), or its start
        f"""SELECT strftime(date_trunc('month', m), '%Y-%m-%d'), count(*) FROM (SELECT po_id, max(t) AS m FROM
          ({UNION.replace('e.started_at, 3', 'coalesce(e.ended_at, e.started_at), 3')}) GROUP BY 1) GROUP BY 1 ORDER BY 1""")
    s = read("process_performance", process="supply")["throughput"]["rows"]
    assert [[m, n] for m, n, _ in s if n] == started and [[m, n] for m, _, n in s if n] == ended
    assert sum(r[1] for r in s) == sum(r[2] for r in s) == mined["known"]["rows"]["purchase_orders"]
    # with a reference, a case has ended once its last activity is one of its ends: T2 (last a reply) is running, counted
    # where it started, and the cycle times are T1's and T3's (25 h and 1 h)
    ref = {"from": "drawn", "start": ["Opened"], "end": ["Closed"], "flows": [["Opened", "Assigned"]]}
    assert save(DESK | {"id": "deskend", "name": "Helpdesk ended", "reference": ref})["status"] == "ok"
    e = read("process_performance", process="deskend", period="day")
    assert e["throughput"] == {"columns": ["period", "started", "ended", "running"],
                               "rows": [["2025-03-01", 2, 0, 1], ["2025-03-02", 0, 1, 0], ["2025-03-03", 1, 1, 0]]}
    assert e["cycle"]["rows"] == [[2, 13.0, 3.4, 7.0, 13.0, 19.0, 22.6, 25.0]] and "running` the others" in e["note"]
    # Brewline: an order has ended once paid (the planted prepaid ones end with an invoice: still running); the paid
    # ones by the month of their last event, the others by the month they were placed
    paid = SUPPLY | {"id": "paid", "name": "Paid", "reference": {"from": "drawn", "start": ["Order placed"], "end": ["Invoice paid"], "flows": []}}
    assert save(paid)["status"] == "ok"
    last = f"""(SELECT po_id, activity AS a, t, row_number() OVER (PARTITION BY po_id ORDER BY t DESC, o DESC) AS r
      FROM ({UNION.replace('e.started_at, 3', 'coalesce(e.ended_at, e.started_at), 3')}))"""
    ended, running, [[n]] = deep(
        f"SELECT strftime(date_trunc('month', t), '%Y-%m-%d'), count(*) FROM {last} WHERE r = 1 AND a = 'Invoice paid' GROUP BY 1 ORDER BY 1",
        f"""SELECT strftime(date_trunc('month', placed_at), '%Y-%m-%d'), count(*) FROM purchase_orders WHERE po_id IN
          (SELECT po_id FROM {last} WHERE r = 1 AND a <> 'Invoice paid') GROUP BY 1 ORDER BY 1""",
        f"SELECT count(*) FROM {last} WHERE r = 1 AND a <> 'Invoice paid'")
    b = read("process_performance", process="paid")
    s = b["throughput"]["rows"]
    assert [[m, x] for m, _, x, _ in s if x] == ended and [[m, x] for m, _, _, x in s if x] == running
    assert sum(r[3] for r in s) == n >= mined["known"]["deviations"]["paid_before_goods"] > 0
    assert b["cycle"]["rows"][0][0] == sum(r[2] for r in s) == mined["known"]["rows"]["purchase_orders"] - n
    for pid in ("deskend", "paid"):
        sure("delete_process", project=P, process=pid)


def test_waiting_and_working_time_known(mined):
    p = read("process_performance", process="desk")
    waits = {("Assigned", "Reply"): [82800], ("Call", "Closed"): [78600, 900], ("Assigned", "Note"): [5400], ("Call", "Call"): [2400],
             ("Escalation", "Call"): [1800], ("Opened", "Assigned"): [1800, 0], ("Call", "Reply"): [0], ("Note", "Call"): [0],
             ("Opened", "Escalation"): [0], ("Reply", "Call"): [0]}
    total = sum(map(sum, waits.values()))
    assert [tuple(r[:2]) for r in p["waiting"]["rows"]] == list(waits), "by total waiting time, the most first (ties by name)"
    for a, b, n, med, p90, tot, share in p["waiting"]["rows"]:
        w = sorted(waits[a, b])
        assert (n, med, tot, share) == (len(w), pytest.approx(statistics.median(w) / 3600, abs=0.006), pytest.approx(sum(w) / 3600, abs=0.006),
                                        pytest.approx(100 * sum(w) / total, abs=0.006)), (a, b)
    # the calls' own times, once each: 20, 10 and 15 minutes
    assert p["working"]["rows"] == [["Call", 3, 0.25, 0.75]]
    # Brewline: unloading a delivery is one event about several orders; per purchase order it still counts once
    [[[n, hours]]] = deep("""SELECT count(*), sum(date_diff('second', started_at, ended_at)) / 3600 FROM supply_events
      WHERE activity = 'Goods received' AND event_id IN (SELECT event_id FROM supply_event_orders)""")
    for typ in ("purchase_order", "delivery"):
        [w] = read("process_performance", process="supply", type=typ)["working"]["rows"]
        assert w[:2] == ["Goods received", n] and w[3] == pytest.approx(hours, abs=0.01), (typ, w)


def test_bottleneck_is_planted_pair(mined):
    slow = mined["known"]["slow"]
    mine = read("process_performance", process="supply", filters=[{"kind": "property", "column": "supplier_id", "op": "=", "value": slow["supplier"]}])
    rest = read("process_performance", process="supply", filters=[{"kind": "property", "column": "supplier_id", "op": "<>", "value": slow["supplier"]}])
    top = mine["waiting"]["rows"][0]
    # the planted supplier's orders wait longest from the approval to the goods: its median is the generator's own
    assert tuple(top[:2]) == (slow["from"], slow["to"]) and top[6] > 30, mine["waiting"]["rows"][:3]
    assert top[3] / 24 == pytest.approx(slow["median_days"], rel=0.03)
    assert tuple(rest["waiting"]["rows"][0][:2]) != (slow["from"], slow["to"]), "for the others it is no bottleneck"
    wait = {tuple(r[:2]): r[3] for r in rest["waiting"]["rows"]}
    assert wait[slow["from"], slow["to"]] / 24 < slow["next_slowest_median_days"] + 0.5
    whole = read("process_performance", process="supply")["waiting"]["rows"]
    assert sum(r[6] for r in whole) == pytest.approx(100, abs=0.1) and whole == sorted(whole, key=lambda r: -r[5])


# ---------- #20 filtering ----------

def test_filter_time_variant_attribute(mined):
    started = lambda **r: cases(filters=[{"kind": "time", **r}])  # noqa: E731
    assert started(when="started", **{"from": "2025-03-01", "to": "2025-03-02"}) == {T1, T2}
    assert started(when="started", **{"from": "2025-03-01 10:00"}) == {T2, T3}, "from is included"
    assert started(when="ended", **{"from": "2025-03-03"}) == {T3}
    # open in a range: started before its end, ended at or after its start (T1 ended at 10:00 on 2 March, T2 at 09:00)
    assert started(when="open", **{"from": "2025-03-02 09:30", "to": "2025-03-03"}) == {T1}
    ids = {r[0]: r[1] for r in read("process_variants", process="desk")["variants"]["rows"]}
    assert cases(filters=[{"kind": "variant", "in": [ids[T1], ids[T3]]}]) == {T1, T3}
    # an event attribute: calls of 10 minutes or more were made for T1 and T3; a property of the ticket: T3 is South's
    assert cases(filters=[{"kind": "attribute", "column": "minutes", "op": ">=", "value": 10}]) == {T1, T3}
    assert cases(filters=[{"kind": "attribute", "column": "minutes", "op": "in", "value": [5, 7]}]) == {T2}
    assert cases(filters=[{"kind": "property", "column": "team", "op": "=", "value": "South"}]) == {T3}
    m = read("process_map", process="desk", filters=[{"kind": "property", "column": "team", "op": "=", "value": "North"}])
    assert m["totals"]["rows"] == [[2, 11]] and m["start"]["rows"] == [["Opened", 2]]


def test_filter_start_end_activity_and_duration(mined):
    assert cases(filters=[{"kind": "end", "activities": ["Closed"]}]) == {T1, T3}
    assert cases(filters=[{"kind": "end", "activities": ["Closed"], "not": True}]) == {T2}
    assert cases(filters=[{"kind": "start", "activities": ["Opened", "Escalation"]}]) == {T1, T2, T3}
    assert cases(filters=[{"kind": "start", "activities": ["Escalation"]}]) == set(), "Escalation is never first"
    # cycle times: T1 25 h, T2 23 h, T3 1 h
    duration = lambda op, h: cases(filters=[{"kind": "duration", "op": op, "hours": h}])  # noqa: E731
    assert (duration(">", 24), duration("<=", 23), duration("<", 1), duration("<=", 1)) == ({T1}, {T2, T3}, set(), {T3})


def test_filter_follows_and_contains(mined):
    follows = lambda a, b, **no: cases(filters=[{"kind": "follows", "from": a, "to": b, **no}])  # noqa: E731
    assert (follows("Call", "Call"), follows("Reply", "Call"), follows("Call", "Closed", **{"not": True})) == ({T1}, {T2}, {T2})
    assert follows("Closed", "Call") == set(), "the other way round never happens"
    assert cases(filters=[{"kind": "contains", "activity": "Escalation"}]) == {T3}
    assert cases(filters=[{"kind": "contains", "activity": "Assigned", "not": True}]) == {T3}


def test_filters_compose(mined):
    both = [{"kind": "time", "when": "started", "from": "2025-03-01", "to": "2025-03-02"}, {"kind": "contains", "activity": "Call"},
            {"kind": "end", "activities": ["Closed"], "not": True}]
    assert cases(filters=both) == {T2} and cases(filters=both[:2]) == {T1, T2}
    # each filter is a semi-join on the case: the same one twice, or two that hold for the same cases, count nothing twice
    once = read("process_map", process="desk", filters=both[1:2])
    assert read("process_map", process="desk", filters=both[1:2] * 2) == once and once["totals"]["rows"] == [[3, 15]]
    # Brewline: the slow supplier's cancelled orders, as the source tables say
    [[[n]]] = deep(f"SELECT count(*) FROM purchase_orders WHERE supplier_id = '{mined['known']['slow']['supplier']}' AND cancelled_at IS NOT NULL")
    got = read("process_map", process="supply", filters=[{"kind": "property", "column": "supplier_id", "op": "=", "value": mined["known"]["slow"]["supplier"]},
                                                         {"kind": "contains", "activity": "Order cancelled"}])
    assert got["totals"]["rows"][0][0] == n > 0 and [r[:2] for r in got["start"]["rows"]] == [["Order placed", n]]


def test_filters_are_checked_with_file_path_and_fix(mined):
    def errors(filters: list, process: str = "desk") -> list:
        e = refused("process_map", process=process, filters=filters)
        assert e.status == 422 and all(x["file"] == "filters" and x["error"] and x["fix"] for x in e.body["errors"]), e.body
        return [(x["path"], x["error"], x["fix"]) for x in e.body["errors"]]

    [(at, err, fix)] = errors([{"kind": "time"}])
    assert at == "$[0]" and "`when`" in err and "`when`" in fix
    [(at, err, fix)] = errors([{"kind": "when"}])
    assert at == "$[0].kind" and '"time"' in err and '"follows"' in err, err  # the kinds there are
    assert errors([{"kind": "time", "when": "started"}])[0][:2] == ("$[0]", "A time filter needs `from`, `to` or both.")
    assert errors([{"kind": "contains", "activity": "Call"}, {"kind": "variant", "in": ["T1"]}])[0][0] == "$[1].in[0]"
    assert errors([{"kind": "time", "when": "started", "from": "2025-02-30"}])[0][0] == "$[0].from"
    assert errors([{"kind": "time", "when": "started", "from": "2025-03-01 00:00+01"}])[0][0] == "$[0].from", "an offset is refused"
    [(at, err, fix)] = errors([{"kind": "property", "column": "nope", "op": "=", "value": 1}])
    assert at == "$[0].column" and "Ticket" in err and "team" in fix
    [(at, err, fix)] = errors([{"kind": "attribute", "column": "agent", "op": "=", "value": "Ada"}])
    assert at == "$[0].column" and fix == "Use one of: minutes."
    assert errors([{"kind": "attribute", "column": "minutes", "op": "=", "value": "many"}])[0][0] == "$[0].value"
    assert errors([{"kind": "attribute", "column": "minutes", "op": "in", "value": 5}])[0][0] == "$[0].value"
    e = refused("process_map", process="desk", type="delivery")
    assert e.status == 400 and "about ticket" in e.body["message"]
    assert refused("process_map", process="nope").status == 404


# ---------- the person: row-level security, the query log ----------

def test_restricted_map_equals_map_of_own_cases(mined):
    north = [r[0] for r in sql("SELECT cafe_id FROM cafes WHERE region = 'North' ORDER BY 1")]
    theirs = [{"kind": "property", "column": "cafe_id", "op": "in", "value": north}]
    for action in ("process_map", "process_variants", "process_performance", "discover_process"):
        mine, owners = read(action, mined["viewer"], process="supply"), read(action, process="supply", filters=theirs)
        assert {k: v for k, v in mine.items() if k != "note"} == {k: v for k, v in owners.items() if k != "note"}, action
    every = read("process_map", process="supply")
    assert 0 < read("process_map", mined["viewer"], process="supply")["totals"]["rows"][0][0] < every["totals"]["rows"][0][0]
    # deliveries are under no rule: their map is the whole one
    assert read("process_map", mined["viewer"], process="supply", type="delivery") == read("process_map", process="supply", type="delivery")
    # each read is one query in the reader's query log, theirs (the filter's own key reads are the system's)
    for token, uid in ((mined["viewer"], mined["uid"]), (None, None)):
        for action in ("process_map", "process_variants", "process_performance", "discover_process"):
            before = logged()
            read(action, token, process="supply", filters=[{"kind": "contains", "activity": "Order cancelled", "not": True}])
            mine = [r for r in logged(before) if r["user_id"] is not None]
            assert len(mine) == 1 and mine[0]["query"][action] is True and (uid is None or mine[0]["user_id"] == uid), (action, mine)
    # a type table row-level security denies them (made from a table with a column rule, and no rule of its own) is
    # refused, saying why and who can change it: never read unfiltered, never an empty map
    e = refused("process_map", token=mined["viewer"], process="desk")
    assert e.status == 403 and "the events of each Ticket in process 'Helpdesk'" in e.body["message"] and "An Owner" in e.body["message"], e.body
    assert read("process_map", process="desk")["totals"]["rows"] == [[3, 15]]


def test_delivery_view_and_order_view_do_not_add(mined):
    k = mined["known"]
    orders, deliveries = (read("process_map", process="supply", type=t) for t in ("purchase_order", "delivery"))
    assert (orders["per"], deliveries["per"]) == ("per Purchase order", "per Delivery")
    by = lambda m: {r[0]: r[1:3] for r in m["activities"]["rows"]}  # noqa: E731
    o, d = by(orders), by(deliveries)
    # one unloading is one event in both views; its cases are orders in one and deliveries in the other
    [[received, carried]] = sql("SELECT count(*), (SELECT count(*) FROM supply_event_orders b JOIN supply_events e USING (event_id)"
                                " WHERE e.activity = 'Goods received') FROM supply_events WHERE activity = 'Goods received'")
    assert o["Goods received"] == [carried, received] and d["Goods received"] == [received, received] and carried > received
    assert set(d) == {"Delivery dispatched", "Goods received"} and "Delivery dispatched" not in o
    # each view counts its own events once; together they hold every event of the log, the shared ones once
    assert orders["totals"]["rows"][0][1] + deliveries["totals"]["rows"][0][1] - received == k["events"]
    assert (orders["totals"]["rows"][0][0], deliveries["totals"]["rows"][0][0]) == (k["rows"]["purchase_orders"], k["rows"]["deliveries"])


def test_process_reads_are_viewer_reads():
    import re
    ts = (ROOT / ".pi/extensions/abi/index.ts").read_text()
    reads = set(re.findall(r'"(\w+)"', re.search(r"const READS = new Set\(\[(.*?)\]\);", ts, re.S)[1]))
    new = {"process_map", "process_variants", "process_performance", "discover_process"}
    assert new <= reads and not {"save_process", "delete_process"} & reads
    r = engine("import json\nimport abi.app\nfrom abi import actions, agent\nprint(json.dumps({'tools': {t['function']['name']: t['function']['description']"
               " for t in agent.tools()}, 'production': sorted(actions.PRODUCTION_READS)}))", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.splitlines()[-1])
    assert new <= set(got["production"]) and all(0 < len(got["tools"][t]) <= 300 for t in new), {t: len(got["tools"][t]) for t in new}
    areas = json.loads((ROOT / "harness/areas.json").read_text())
    assert new <= set(areas["areas"]["model"]["tools"])
    for f in (ROOT / ".pi/agents").glob("*.md"):
        head = f.read_text().split("---")[1]
        assert ("mcp__abi__process," in head) == all(f"mcp__abi__{t}" in head for t in new), f.name


# ---------- review round 1 ----------

def test_case_figures_count_once_and_are_the_builds_names(mined):
    # each case's own figures and flags are on every one of its rows: a sum, an average, a count or a median over them is
    # refused like an event's own figure; the smallest, the largest and the number of different values are not
    for fn in ("SUM", "AVG", "COUNT", "MEDIAN"):
        with pytest.raises(abi.ActionError) as e:
            abi.call("set_measure", project=P, id="cyc", name="cyc", expression=f"{fn}(pm_supply_purchase_order.cycle_seconds)")
        [x] = e.value.body["errors"]
        assert "a Purchase order's own figure written on each of its events" in x["error"] and x["fix"].startswith("Use MIN, MAX or DISTINCT_COUNT"), x
    for i, ex in {"longest": "MAX(pm_supply_purchase_order.cycle_seconds)", "kinds": "DISTINCT_COUNT(pm_supply_purchase_order.variant_id)"}.items():
        abi.call("set_measure", project=P, id=i, name=i, expression=ex)
        sure("delete_measure", project=P, id=i)
    # and no attribute may have their names
    for a in ("variant_id", "Cycle_Seconds", "first_pair"):
        p = copy.deepcopy(JOBS)
        p["events"][0]["attributes"] = [a]
        with pytest.raises(abi.ActionError) as e:
            abi.call("save_process", project=P, process=p)
        assert e.value.status == 422 and "makes itself" in e.value.body["errors"][0]["error"], e.value.body


STALE = r"""
import json, sys
from abi import storage
p = json.loads(sys.stdin.read())["project"]
storage._commit(storage.lake(p), lambda cur: cur.execute("ALTER TABLE lake.model.pm_jobs_job DROP COLUMN first_pair"), "test", "an older build")
"""


def test_a_build_older_than_the_file_or_the_reads_is_named(mined):
    # an attribute in the file that its last build did not write (that build failed): answered as unknown, not a fault
    odd = copy.deepcopy(DESK) | {"description": "Agents too, and a typo."}
    odd["events"][1]["attributes"] = ["minutes", "agent"]
    odd["clean"]["where"][0]["value"] = "last year"
    assert save(odd)["status"] == "failed"
    try:
        e = refused("process_map", process="desk", filters=[{"kind": "attribute", "column": "agent", "op": "=", "value": "Ada"}])
        assert e.status == 422 and e.body["errors"][0]["path"] == "$[0].column" and e.body["errors"][0]["fix"] == "Use one of: minutes.", e.body
    finally:
        assert save(DESK)["status"] == "ok"
    # a table built before the reads' columns (as by an earlier version): the read says so and how to build it again
    r = engine(STALE, timeout=120, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    e = refused("process_map", process="jobs")
    assert e.status == 409 and "save the process again (save_process)" in e.body["message"], e.body
    assert save(JOBS | {"description": "Built again."})["status"] == "ok" and read("process_map", process="jobs")["totals"]["rows"] == [[31, 122]]


MINE = r"""
import json
import abi.app  # noqa: F401
from abi import process
acts = {"A": (300, 1), "X": (300, 1500)} | {f"S{i}": (300, 1) for i in range(250)}
pairs = {("X", "X"): 1499 * 300} | {(f"S{i}", f"S{j}"): 300 for i in range(70) for j in range(70) if i < j}
ref, _, _, cut = process.mine(300, acts, pairs, {}, {f"S{i}": 300 for i in range(250)}, {f"S{i}": 300 for i in range(250)}, 0.9, 0)
print(json.dumps({"ref": {k: len(v) if isinstance(v, list) else v for k, v in ref.items()}, "cut": cut}))
"""


def test_the_usual_way_fits_a_reference(mined):
    # a step repeated 1,500 times in a case, 2,416 dependable pairs and 250 starts and ends: what save_process takes
    # (repeat at most 1,000, at most 2,000 flows and 200 starts and ends), the most frequent kept, and the note says so
    r = engine(MINE, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.splitlines()[-1])
    assert got["ref"] == {"from": "discovered", "start": 200, "end": 200, "flows": 2000, "repeat": {"X": 1000}}, got
    assert got["cut"] == ["start activities", "end activities", "flows", "the repeats of X"], got
    assert "keeps only the most frequent" not in read("discover_process", process="jobs")["note"]


SLOW = r"""
import json, sys
import abi.app  # noqa: F401
from abi import db, process, security, semantic
from abi.actions import ActionFailed
p = json.loads(sys.stdin.read())["project"]
semantic.TIMEOUT = 0.5
with db.connect() as c:
    u = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
try:
    process._run(p, "SELECT count(*) FROM range(100000000000) a", {"test": "slow"}, u)
    print(json.dumps(None))
except ActionFailed as e:
    print(json.dumps([e.status, e.body["message"]]))
"""


def test_a_read_stopped_at_the_time_limit_says_how_to_read_less(mined):
    r = engine(SLOW, timeout=120, stdin=json.dumps({"project": P}))
    assert r.returncode == 0, r.stderr[-2000:]
    status, msg = json.loads(r.stdout.splitlines()[-1])
    assert status == 400 and "longer than the 0.5 s limit" in msg and "`time` filter" in msg, msg


def test_a_hidden_column_reads_as_an_unknown_one(mined):
    # a property of the delivery and an attribute of its events hidden from the Viewer (a column rule on the type's own
    # table keeps its other columns readable): the same answer as for a column that isn't there, the fix lists only what they may read
    sure("set_column_rule", project=P, id="transport", table="deliveries", columns=["transport"])
    sure("set_column_rule", project=P, id="who", table="pm_supply_delivery", columns=["done_by"])
    try:
        def fix(**f) -> dict:
            e = refused("process_map", token=mined["viewer"], process="supply", type="delivery", filters=[{"op": "=", "value": "x", **f}])
            assert e.status == 422, e.body
            [x] = e.body["errors"]
            return x

        hid, none = fix(kind="property", column="transport"), fix(kind="property", column="nope")
        assert hid["fix"] == none["fix"] and "transport" not in hid["fix"] and "supplier_id" in hid["fix"]
        assert hid["error"].replace("transport", "nope") == none["error"]
        hid, none = fix(kind="attribute", column="done_by"), fix(kind="attribute", column="nope")
        assert hid["fix"] == none["fix"] == "Use one of: amount." and hid["error"].replace("done_by", "nope") == none["error"]
        # the owner reads them
        assert read("process_map", process="supply", type="delivery", filters=[{"kind": "property", "column": "transport", "op": "<>", "value": "x"}])
    finally:
        for i in ("transport", "who"):
            sure("delete_rls_rule", project=P, id=i)


SHIFTS = {"id": "shifts", "name": "Shifts", "case": "shift", "events": [
    {"table": "shift_events", "id": "event_id", "activity": "activity", "start": "at", "objects": [{"type": "shift", "columns": ["site", "shift_day"]}]}]}


def test_a_case_with_a_key_of_two_columns(mined):
    # a shift is a site and a day: S1 (A, 1 Jan) Open Serve Close, S2 (A, 2 Jan) Open Close, S3 (B, 1 Jan) Open Serve Serve Close
    d = mined["dir"]
    (d / "shifts.csv").write_text("site,shift_day,team\nA,2025-01-01,red\nA,2025-01-02,red\nB,2025-01-01,blue\n")
    (d / "shift_events.csv").write_text("event_id,site,shift_day,activity,at\n1,A,2025-01-01,Open,2025-01-01 08:00:00\n"
                                        "2,A,2025-01-01,Serve,2025-01-01 09:00:00\n3,A,2025-01-01,Close,2025-01-01 17:00:00\n"
                                        "4,A,2025-01-02,Open,2025-01-02 08:00:00\n5,A,2025-01-02,Close,2025-01-02 12:00:00\n"
                                        "6,B,2025-01-01,Open,2025-01-01 07:00:00\n7,B,2025-01-01,Serve,2025-01-01 08:00:00\n"
                                        "8,B,2025-01-01,Serve,2025-01-01 10:00:00\n9,B,2025-01-01,Close,2025-01-01 15:00:00\n")
    for t in ("shifts", "shift_events"):
        abi.upload(P, t, Path(parquet(d / f"{t}.csv")[0]))
    for t, k in (("shifts", ["site", "shift_day"]), ("shift_events", ["event_id"])):
        abi.call("set_table", project=P, table=t, key=k)
    sure("save_ontology", project=P, ontology={"objects": [*ONTO["objects"], {"id": "job", "table": "jobs", "name": "Job", "plural": "Jobs", "title": "job_id"},
                                                           {"id": "shift", "table": "shifts", "name": "Shift", "plural": "Shifts", "title": "site"}]})
    assert save(SHIFTS)["status"] == "ok"
    try:
        m = read("process_map", process="shifts")
        assert m["totals"]["rows"] == [[3, 9]] and m["activities"]["rows"] == [["Close", 3, 3, None], ["Open", 3, 3, None], ["Serve", 2, 3, None]]
        assert [r[:4] for r in m["pairs"]["rows"]] == [["Open", "Serve", 2, 2], ["Serve", "Close", 2, 2], ["Open", "Close", 1, 1], ["Serve", "Serve", 1, 1]]
        v = {r[0]: r[2] for r in read("process_variants", process="shifts")["variants"]["rows"]}
        assert v == {"Open → Serve → Close": 1, "Open → Close": 1, "Open → Serve → Serve → Close": 1}
        red = [{"kind": "property", "column": "team", "op": "=", "value": "red"}, {"kind": "contains", "activity": "Serve"}]
        assert read("process_map", process="shifts", filters=red)["totals"]["rows"] == [[1, 3]]
        assert read("process_performance", process="shifts", filters=[{"kind": "duration", "op": ">=", "hours": 8}])["cycle"]["rows"][0][:2] == [2, 8.5]
        assert read("discover_process", process="shifts", threshold=0.5)["reference"]["start"] == ["Open"]
    finally:
        sure("delete_process", project=P, process="shifts")
