"""1.2.6 M12 (needs the engine at :8780, no LLM): optimisers. An optimiser is optimizers/<id>.json: one number decided
per object of a type, a total to make as high or as low as possible, limits on sums over the objects. run_optimizer
works the best plan out in the worker over the objects the person sees, with a time limit and a size cap; the plan is
stored as a write-back proposal a person approves and is never applied by a run, an agent or a flow.

P's cafés (region is the row rule's column: the Viewer sees South). Worked by hand:

 café    region  revenue/hour  wage/hour  max hours  planned now  visits' revenue
 Nord    North   50            30         40         10           300
 Hafen   North   40            25         40         10           200
 Süd     South   30            20         50         10           100
 Markt   South   20            16         50         10           50
 West    South   8             8          50         0            none

 `hours`: whole hours per café, at most its max hours, 100 in total, 60 per region, the most revenue.
   Everyone: Nord 40 (its most), then Hafen 20 (North is full at 60), then Süd 40 (the total is full at 100):
             40*50 + 20*40 + 40*30 = 4,000 (now 10*50 + 10*40 + 10*30 + 10*20 = 1,400).
   South only (the Viewer): Süd 50 (its most), Markt 10 (South is full at 60): 50*30 + 10*20 = 1,700 (now 500).
 `units`: whole units per café, at most 10, 12 in total, the most of (the café's visit revenue x units): the measure.
   Everyone: Nord 10, Hafen 2: 3,000 + 400 = 3,400.  South only: Süd 10, Markt 2: 1,000 + 100 = 1,100.
 `wage`: hours (fractions allowed), at most max hours, wages at most 2,510, the most revenue. Revenue per wage:
   Nord 1.67, Hafen 1.6, Süd 1.5, so Nord 40 (1,200), Hafen 40 (1,000), Süd 310/20 = 15.5: 2,000 + 1,600 + 465 = 4,065.
"""
import json
import random
import re
import secrets
import time
from pathlib import Path

import pytest
from conftest import ROOT, drop, parquet
from test_engine import engine
from test_pages import sure

import abi

P = "pi_opt"
CAFES = """cafe_id,name,region,revenue_per_hour,hourly_wage,max_hours,planned_hours,budget_hours,units
1,Café Nord,North,50,30,40,10,10.5,0
2,Café Hafen,North,40,25,40,10,10.5,0
3,Café Süd,South,30,20,50,10,10.5,0
4,Café Markt,South,20,16,50,10,10.5,0
5,Café West,South,8,8,50,0,0.5,0
"""
VISITS = """visit_id,cafe_id,amount
1,1,200
2,1,100
3,2,200
4,3,100
5,4,50
"""
NOW = {1: 10, 2: 10, 3: 10, 4: 10, 5: 0}
ONTO = {"objects": [
    {"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "name", "measures": ["revenue"],
     "properties": [{"column": "region", "name": "Region", "meaning": "category"}, {"column": "hourly_wage", "name": "Wage per hour"}]},
    {"id": "slot", "table": "slots", "name": "Slot", "plural": "Slots", "title": "name"},
    {"id": "thing", "table": "things", "name": "Thing", "plural": "Things", "title": "name"}]}
TOTAL, REGION = {"name": "Total hours", "max": 100}, {"name": "Hours per region", "group": "region", "max": 60}
HOURS = {"id": "hours", "name": "Hours per café", "over": {"type": "cafe"},
         "decide": {"name": "Hours", "write": "planned_hours", "max": {"property": "max_hours"}, "integer": True},
         "objective": {"goal": "maximize", "name": "Revenue", "per_unit": {"property": "revenue_per_hour"}}, "limits": [TOTAL, REGION]}
UNITS = {"id": "units", "name": "Units per café", "over": {"type": "cafe"}, "decide": {"name": "Units", "write": "units", "max": 10, "integer": True},
         "objective": {"goal": "maximize", "name": "Revenue", "per_unit": {"measure": "revenue"}}, "limits": [{"name": "Units to hand out", "max": 12}]}
WAGE = {"id": "wage", "name": "Hours within the wage budget", "over": {"type": "cafe"},
        "decide": {"name": "Hours", "write": "budget_hours", "max": {"property": "max_hours"}},
        "objective": {"goal": "maximize", "name": "Revenue", "per_unit": {"property": "revenue_per_hour"}},
        "limits": [{"name": "Wage budget", "per_unit": {"property": "hourly_wage"}, "max": 2510}]}
# no plan: 60 hours in each of two regions are 120, and the total allows 100; the wage budget has nothing to do with it
TIGHT = HOURS | {"id": "tight", "name": "Staffed regions", "limits": [
    TOTAL, {"name": "Wage budget", "per_unit": {"property": "hourly_wage"}, "max": 100000}, {"name": "Staffed regions", "group": "region", "min": 60}]}
FLOOR = HOURS | {"id": "floor", "name": "A floor for every café", "decide": {"name": "Hours", "write": "planned_hours", "min": 30, "integer": True},
                 "limits": [TOTAL]}
OPEN = HOURS | {"id": "open", "name": "No end", "decide": {"name": "Hours", "write": "planned_hours", "integer": True}, "limits": []}
FEW = HOURS | {"id": "few", "name": "Cafés that are not there", "over": {"type": "cafe", "filters": [{"property": "region", "op": "=", "value": "East"}]}}
# 60 yes/no picks whose six weighted sums must each hit exactly half: no solver settles that in two seconds
RND = random.Random(7)
WEIGHTS = [[RND.randint(1, 99) for _ in range(60)] for _ in range(6)]
SPLIT = {"id": "split", "name": "An even split", "over": {"type": "slot"}, "decide": {"name": "Picked", "write": "pick", "max": 1, "integer": True},
         "objective": {"goal": "minimize", "name": "Picks", "per_unit": 1}, "time_limit_s": 2,
         "limits": [{"name": f"Balance {i + 1}", "per_unit": {"property": f"w{i + 1}"}, "equal": sum(w) // 2} for i, w in enumerate(WEIGHTS)]}
MANY = {"id": "many", "name": "Too many things", "over": {"type": "thing"}, "decide": {"name": "Amount", "write": "amount", "max": 1},
        "objective": {"goal": "maximize", "name": "Worth", "per_unit": {"property": "worth"}}}
ALL = (HOURS, UNITS, WAGE, TIGHT, FLOOR, OPEN, FEW, SPLIT, MANY)
# an LLM's call (via "agent"), with no token and with a made-up one: what ABI answers each time
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
        out.append(actions.run(a["action"], a["args"], u, "agent", confirm))
    except actions.ActionFailed as e:
        out.append([e.status, "confirm_token" in e.body])
print(json.dumps(out, default=str))
"""
BUNDLE = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, security
from abi.project import bundle, git
P = json.loads(sys.stdin.read())["project"]
with db.connect() as c:
    owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
b = bundle.bundle(git.path(P))
dry = actions.run("import_project", {"project": P, "bundle": b, "dry_run": True}, owner, "rest")
bad = {**b, "optimizers/hours.json": {**b["optimizers/hours.json"], "over": {"type": "nope"}},
       "model/measures.json": {**b["model/measures.json"], "measures": b["model/measures.json"]["measures"] + [
           {"id": "pi_new", "name": "pi new", "expression": "SUM(visits.amount)"}]}}
try:
    actions.run("import_project", {"project": P, "bundle": bad}, owner, "rest")
    err = None
except actions.ActionFailed as e:
    err = [e.status, e.body["errors"]]
print(json.dumps({"file": b.get("optimizers/hours.json"), "dry": dry["changes"], "err": err, "after": bundle.bundle(git.path(P)) == b}))
"""


def call(action: str, **args):
    return abi.call(action, project=P, **args)


def run(optimizer: str, token: str | None = None) -> dict:
    """run_optimizer as the owner (or `token`'s person), queued for the worker and waited for: that run as `optimizers`
    shows it to them."""
    with abi.as_user(token):
        q = call("run_optimizer", optimizer=optimizer)
        assert q["status"] == "queued" and q["rows"] == [[q["run"], "queued"]], "it runs in the worker, not in the call"
        end = time.monotonic() + 600
        while (r := call("optimizers", optimizer=optimizer))["status"] in ("queued", "running"):
            assert time.monotonic() < end, r
            time.sleep(1)
    assert r["run"] == q["run"], r
    return r


def cells(column: str = "planned_hours") -> dict:
    """What queries read in `column` for each café."""
    return {r[0]: r[1] for r in call("sql_query", sql=f"SELECT cafe_id, {column} FROM cafes ORDER BY 1")["rows"]}


def waiting() -> dict:
    return {p["batch"]: p for p in call("edit_proposals")["proposals"]}


def objects(r: dict) -> dict:
    """A run's plan: {object's name: its planned number}."""
    return {x[1]: x[2] for x in r["rows"] if x[5] is not None}


@pytest.fixture(scope="module")
def _opt(tmp_path_factory):
    """P: the cafés and visits above, the slots and things, a South-region Viewer (a row rule on cafes.region), the
    measure, the object types and the optimisers."""
    d = tmp_path_factory.mktemp("opt")
    (d / "cafes.csv").write_text(CAFES)
    (d / "visits.csv").write_text(VISITS)
    (d / "slots.csv").write_text("slot_id,name," + ",".join(f"w{i + 1}" for i in range(6)) + ",pick\n" + "".join(
        f"{j + 1},Slot {j + 1}," + ",".join(str(w[j]) for w in WEIGHTS) + ",0\n" for j in range(60)))
    (d / "things.csv").write_text("thing_id,name,worth,amount\n" + "".join(f"{i},Thing {i},{i % 7 + 1},0.5\n" for i in range(1, 1002)))
    if P in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([P])
    abi.load("demo", P, parquet(*(d / f"{t}.csv" for t in ("cafes", "visits", "slots", "things"))))
    name, pw = f"pi_opt_viewer_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    try:
        for t in ("cafe", "visit", "slot", "thing"):
            call("set_table", table=f"{t}s", key=[f"{t}_id"])
        call("add_relationship", **{"from": "visits.cafe_id", "to": "cafes.cafe_id"})
        call("set_measure", id="revenue", name="Visit revenue", expression="SUM(visits.amount)")
        abi.call("create_user", name=name, password=pw)
        abi.call("set_role", project=P, user=name, role="viewer")
        uid = {u["name"]: u["id"] for u in call("rls_rules")["accounts"]}[name]
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={uid: ["South"]})
        sure("save_ontology", project=P, ontology=ONTO)
        for o in ALL:
            assert call("save_optimizer", optimizer=o) == {"id": o["id"]}
        yield {"token": abi.login(name, pw)[1]["token"], "name": name, "pw": pw}
    finally:
        sure("delete_user", name=name)
        drop([P])


@pytest.fixture
def opt(_opt):
    """P as loaded: every proposal an earlier test left is rejected and every applied edit undone, so a test runs
    alone or in any order."""
    for p in waiting().values():
        call("reject_edit", table=p["table"], batch=p["batch"])
    for t in ("cafes", "slots", "things"):
        try:
            h = call("edit_history", table=t, limit=1000)
        except abi.ActionError:  # never edited
            continue
        at = h["columns"].index
        for b in dict.fromkeys(r[at("batch_id")] for r in h["entries"] if r[at("status")] == "active"):
            call("undo_edits", table=t, batch=b)
    assert cells() == NOW
    return _opt


# ---------- #39, #41: the best plan, exactly ----------

def test_opt_allocation_optimal(opt):
    """The hand-worked allocation: the exact plan, its total, the limits it uses up, in rows and in a sentence of
    business words. A measure per object and a plan with fractions come out as worked by hand too."""
    r = run("hours")
    assert (r["status"], r["outcome"], r["time_limit_s"]) == ("ok", "best", 60), "the default time limit is 60 s"
    assert r["columns"] == ["what", "name", "plan", "now", "limit", "key"] and r["rows"] == [
        ["Total", "Revenue", 4000, 1400, None, None],
        ["Limit fully used", "Total hours", 100, None, 100, None],
        ["Limit fully used", "Hours per region: North", 60, None, 60, None],
        ["Limit", "Hours per region: South", 40, None, 60, None],
        ["Café", "Café Nord", 40, 10, "at its most", [1]],
        ["Café", "Café Süd", 40, 10, None, [3]],
        ["Café", "Café Hafen", 20, 10, None, [2]],
        ["Café", "Café Markt", 0, 10, None, [4]],
        ["Café", "Café West", 0, 0, None, [5]]], r["rows"]
    assert all(type(x[2]) is int for x in r["rows"][4:]), "whole hours"
    assert r["text"] == ("The best plan of “Hours per café” gives 40 Hours to Café Nord, 40 to Café Süd, 20 to Café Hafen. Revenue: 4,000 (now 1,400)."
                         " Fully used: Total hours, Hours per region: North. At their most Hours: 1 of the 5 Cafés."
                         " Proposed as 4 changes for a person to approve; nothing is changed until then.")
    assert not re.search(r"cafe_id|planned_hours|revenue_per_hour|\bcafes\b|optimi[sz]er|solver|constraint|ontology|object type|\bLP\b|\bMIP\b", r["text"])
    # every figure of the sentence is a value of the rows: what the agent's grounding check reads
    have = {float(v) for x in r["rows"] for v in x[2:5] if isinstance(v, int | float)} | {1, 5, 4}  # ... and counts of listed things
    assert {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", r["text"])} <= have
    u = run("units")  # the measure, read per café: Café West has no visits and counts as 0
    assert u["rows"][0] == ["Total", "Revenue", 3400, 0, None, None] and objects(u) == {
        "Café Nord": 10, "Café Hafen": 2, "Café Süd": 0, "Café Markt": 0, "Café West": 0}
    assert u["rows"][1] == ["Limit fully used", "Units to hand out", 12, None, 12, None]
    w = run("wage")  # fractions
    assert objects(w) == pytest.approx({"Café Nord": 40, "Café Hafen": 40, "Café Süd": 15.5, "Café Markt": 0, "Café West": 0}, abs=1e-6)
    assert w["rows"][0][:4] == ["Total", "Revenue", pytest.approx(4065, abs=1e-6), pytest.approx(1400 + 0.5 * (50 + 40 + 30 + 20) + 0.5 * 8)]
    assert w["rows"][1] == ["Limit fully used", "Wage budget", pytest.approx(2510, abs=1e-6), None, 2510, None]
    assert "15.5 to Café Süd" in w["text"] and "Revenue: 4,065 (now 1,474)" in w["text"]
    assert cells() == NOW and cells("units") == dict.fromkeys(NOW, 0), "a run changes nothing"


# ---------- #41: no plan, and why ----------

def test_opt_infeasible_explained(opt):
    """No plan fits: the answer names the limits that can't hold together, in the file's business words, and leaves out
    the ones that have nothing to do with it; nothing is proposed. A limit per object is named too. A plan with no end
    and a plan over nothing say what to do."""
    was = waiting()
    r = run("tight")
    assert (r["status"], r["outcome"]) == ("ok", "infeasible") and "proposal" not in r
    assert r["text"] == ("No plan fits: these limits can't all hold: Total hours (at most 100); Staffed regions (at least 60, for each Region)."
                         " Relax one of them, then run it again.")
    assert r["rows"] == [["Limit in conflict", "Total hours (at most 100)", None, None, 100, None],
                         ["Limit in conflict", "Staffed regions (at least 60, for each Region)", None, None, 60, None]]
    assert "Wage budget" not in r["text"] and not re.search(r"infeasib|constraint|solver|\bregion\b|planned_hours", r["text"])
    f = run("floor")  # 5 cafés x at least 30 hours are 150
    assert f["outcome"] == "infeasible" and f["text"] == (
        "No plan fits: these limits can't all hold: Hours per Café: at least 30; Total hours (at most 100). Relax one of them, then run it again.")
    o = run("open")
    assert o["outcome"] == "unbounded" and "proposal" not in o and o["rows"] == []
    assert o["text"] == "“No end” has no best plan: Revenue can grow without end. Add a limit: the most Hours per Café, or a limit on a total."
    e = run("few")
    assert (e["outcome"], e["rows"]) == ("empty", []) and e["text"] == "There is nothing to plan: “Cafés that are not there” found no Cafés you can see that match its filters."
    assert waiting() == was and cells() == NOW, "no plan: nothing proposed, nothing changed"


# ---------- #41: a plan is a proposal a person approves ----------

def test_opt_result_is_proposal(opt):
    """An Editor's best plan is stored as one write-back proposal with old and new values; no value changes. Approving it
    is the person's Confirm: an agent (with no token or a made-up one), the harness's replay of an agent's call and a
    plain call are all asked first. Their yes applies it as one undoable batch; the next run has nothing to propose.
    An agent's run and a flow's run make a proposal too, never a change; an alert for one object may not run one."""
    r = run("hours")
    p = r["proposal"]
    assert p == {"table": "cafes", "batch": p["batch"], "changes": 4} and cells() == NOW, "the run applied nothing"
    mine = waiting()[p["batch"]]
    assert mine["comment"] == "The best plan of “Hours per café”" and mine["table"] == "cafes"
    assert sorted((c["row"]["cafe_id"], c["column"], c["old"], c["new"]) for c in mine["changes"]) == [
        (1, "planned_hours", "10", "40"), (2, "planned_hours", "10", "20"), (3, "planned_hours", "10", "40"), (4, "planned_hours", "10", "0")]
    ask = {"table": "cafes", "batch": p["batch"]}
    a = engine(AGENT, timeout=120, stdin=json.dumps({"action": "approve_edit", "args": {"project": P, **ask}}))
    assert a.returncode == 0, a.stderr[-2000:]
    assert json.loads(a.stdout.splitlines()[-1]) == [[409, True], [409, True]], "an agent can't approve it, with any token"
    for confirm in (None, "ask"):  # a person's call, and the harness replaying an agent's (X-Abi-Confirm: ask): asked first
        with pytest.raises(abi.ActionError) as e:
            abi.call("approve_edit", confirm=confirm, project=P, **ask)
        assert e.value.status == 409 and e.value.body["confirm_token"] and "Apply 4 proposed change(s)" in e.value.body["message"]
    assert cells() == NOW and p["batch"] in waiting(), "nothing applied without the person's yes"
    done = sure("approve_edit", project=P, **ask)
    assert done["changes"] == 4 and cells() == {1: 40, 2: 20, 3: 40, 4: 0, 5: 0} and p["batch"] not in waiting()
    again = run("hours")
    assert again["outcome"] == "best" and "proposal" not in again and again["rows"][0][:4] == ["Total", "Revenue", 4000, 4000]
    assert again["text"].endswith("The Cafés already have these values, so there is nothing to propose.")
    call("undo_edits", table="cafes", batch=p["batch"])  # one batch, undone in one step
    assert cells() == NOW
    # an agent's run: queued like a person's, and its plan waits too
    a = engine(AGENT, timeout=120, stdin=json.dumps({"action": "run_optimizer", "args": {"project": P, "optimizer": "units"}}))
    assert a.returncode == 0, a.stderr[-2000:]
    first, second = json.loads(a.stdout.splitlines()[-1])
    assert first["status"] == "queued" and second == [409, False], "one run at a time for a person; the second is told so"
    end = time.monotonic() + 600
    while (u := call("optimizers", optimizer="units"))["status"] in ("queued", "running"):
        assert time.monotonic() < end, u
        time.sleep(1)
    assert u["run"] == first["run"] and u["proposal"]["changes"] == 2 and cells("units") == dict.fromkeys(NOW, 0)
    # a flow's run (a schedule runs the same step): a proposal, never a change
    sure("save_flow", project=P, flow={"id": "plan_hours", "name": "Plan the hours", "enabled": False, "trigger": {"type": "manual"},
                                       "steps": [{"id": "plan", "type": "action", "action": "run_optimizer", "input": {"optimizer": "hours"}}]})
    fr, end = call("run_flow", flow="plan_hours")["run"], time.monotonic() + 600
    while (x := call("flow_runs", run=fr)["runs"][0])["status"] in ("queued", "running"):
        assert time.monotonic() < end, x
        time.sleep(1)
    sure("delete_flow", project=P, flow="plan_hours")
    assert x["status"] == "ok" and '"outcome": "best"' in json.dumps(x) and cells() == NOW, x
    flows = [q for q in waiting().values() if q["comment"] == "The best plan of “Hours per café”"]
    assert len(flows) == 1 and len(flows[0]["changes"]) == 4 and flows[0]["batch"] in json.dumps(x), "the flow's plan waits for a person"
    # an alert for one object reaches only the people who see it: it may not run a plan (a proposal shows its values to every reviewer)
    with pytest.raises(abi.ActionError) as e:
        call("save_flow", flow={"id": "sneak", "name": "Sneak", "enabled": False, "steps": [
            {"id": "plan", "type": "action", "action": "run_optimizer", "input": {"optimizer": "hours"}}], "trigger": {
            "type": "data_condition", "query": {"measures": ["revenue"]}, "measure": "revenue", "op": ">", "value": 0, "per": "cafe", "cooldown_minutes": 60}})
    assert e.value.status == 422 and "(run_optimizer) isn't one an alert for one object may run" in json.dumps(e.value.body["errors"])


# ---------- #42: limits ----------

def test_opt_time_limit(opt):
    """A run stops at its time limit (2 s in the file; 60 s without) and says so; nothing is proposed. More objects than
    one run takes are refused with what to do, before anything is worked out."""
    was = waiting()
    r = run("split")
    assert (r["status"], r["outcome"], r["time_limit_s"]) == ("ok", "timeout", 2) and "proposal" not in r and r["rows"] == []
    assert 2 <= r["seconds"] < 30, "stopped at the limit, not long after"
    assert r["text"] == ("“An even split” found no plan within 2 seconds, so nothing is proposed. Plan for fewer Slots at a time,"
                         " or allow it more time (at most 600 seconds): ask the agent to change this plan.")
    big = run("many")
    assert big["status"] == "failed" and big["rows"] == [] and big["error"] == (
        "“Too many things” would plan for more than 1,000 Things, the most one run takes. Plan for fewer at a time, for example one region"
        " or one period per plan: ask the agent to narrow this plan.")
    for bad, why in ((0, "minimum"), (601, "maximum"), (1.5, "type")):  # the limit itself is bounded by the schema
        with pytest.raises(abi.ActionError) as e:
            call("save_optimizer", optimizer=SPLIT | {"time_limit_s": bad})
        assert e.value.status == 422 and [(x["file"], x["path"]) for x in e.value.body["errors"]] == [("optimizers/split.json", "$.time_limit_s")]
        assert why in e.value.body["errors"][0]["error"]
    assert waiting() == was


def test_opt_rls_scope(opt):
    """A restricted Viewer's plan is made of the cafés they see: its objects, its total, the limits it uses and why no
    plan fits never come from a hidden café, and nothing is proposed. They read their own runs only, never one made
    over every row; everyone else never reads theirs. A figure hidden from them is never read for them."""
    tok = opt["token"]

    def theirs() -> dict:
        with abi.as_user(tok):
            return {o["id"]: (o["latest_run"] or {}).get("run") for o in call("optimizers")["optimizers"]}

    before = theirs()
    full = run("hours")
    assert theirs() == before and full["run"] not in before.values(), "the owner's runs aren't theirs to read"
    r = run("hours", tok)
    assert r["rows"] == [
        ["Total", "Revenue", 1700, 500, None, None],
        ["Limit", "Total hours", 60, None, 100, None],
        ["Limit fully used", "Hours per region: South", 60, None, 60, None],
        ["Café", "Café Süd", 50, 10, "at its most", [3]],
        ["Café", "Café Markt", 10, 10, None, [4]],
        ["Café", "Café West", 0, 0, None, [5]]], r["rows"]
    assert r["text"] == ("The best plan of “Hours per café” gives 50 Hours to Café Süd, 10 to Café Markt. Revenue: 1,700 (now 500)."
                         " Fully used: Hours per region: South. At their most Hours: 1 of the 3 Cafés."
                         " This is the plan for the Cafés you see; nothing is changed. An Editor's run proposes it as changes for approval.")
    assert "proposal" not in r and not re.search(r"Nord|Hafen|North|4,?000|1,?400", json.dumps(r, ensure_ascii=False))
    u = run("units", tok)  # the measure follows their rows too
    assert u["rows"][0] == ["Total", "Revenue", 1100, 0, None, None] and objects(u) == {"Café Süd": 10, "Café Markt": 2, "Café West": 0}
    # everyone's plan has no answer because of the North (60 hours there and 60 in the South exceed 100): theirs has one
    assert run("tight")["outcome"] == "infeasible"
    t = run("tight", tok)
    assert t["outcome"] == "best" and objects(t) == {"Café Süd": 50, "Café Markt": 50, "Café West": 0} and "North" not in json.dumps(t)
    f = run("floor", tok)  # 3 cafés x 30 are 90: it fits, where everyone's 150 doesn't
    assert f["outcome"] == "best" and sum(objects(f).values()) == 100 and len(objects(f)) == 3
    assert waiting().keys() == {full["proposal"]["batch"]}, "a Viewer's runs propose nothing"
    now = call("optimizers", optimizer="hours")
    assert now["run"] == full["run"] and now["rows"][0][:3] == ["Total", "Revenue", 4000], "the owner still reads the plan over every café"
    with abi.as_user(tok), pytest.raises(abi.ActionError) as e:  # their role: no definition, no approval
        call("save_optimizer", optimizer=HOURS)
    assert e.value.status == 403
    with abi.as_user(tok), pytest.raises(abi.ActionError) as e:
        call("approve_edit", table="cafes", batch=full["proposal"]["batch"])
    assert e.value.status == 403 and cells() == NOW
    # a column hidden from them: the plan that needs it isn't worked out for them, and its figures stay out of the answer
    sure("set_column_rule", project=P, id="no_wage", table="cafes", columns=["hourly_wage"])
    try:
        w = run("wage", tok)
        assert w["status"] == "failed" and w["rows"] == [] and w["error"] == (
            "“Hours within the wage budget” can't be worked out for you: it needs Wage per hour of the Cafés, which isn't there or which you can't read.")
        assert run("wage")["outcome"] == "best", "the owner's still is"
    finally:
        sure("delete_rls_rule", project=P, id="no_wage")


# ---------- #40: as data ----------

def test_opt_schema_and_bundle(opt):
    """An optimiser is a project file: checked against its schema and the model when saved (file, field and fix; nothing
    saved), one undoable Git change, in the bundle, and imported whole or not at all. The schema's three templates are
    valid files."""
    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "engine/schemas/v1/optimizer.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert [x["id"] for x in schema["examples"]] == ["stock-allocation", "staffing", "maintenance-slots"]
    assert all(not list(Draft202012Validator(schema).iter_errors(x)) for x in schema["examples"]), "the templates are valid"
    folder = Path(abi.ABI_PROJECTS) / P / "optimizers"
    doc = json.loads((folder / "hours.json").read_text())
    assert doc == HOURS | {"$schema": "https://schema.abi.invalid/v1/optimizer.json"}
    before = sorted(f.name for f in folder.iterdir())
    bad = {
        "$": {k: v for k, v in HOURS.items() if k != "objective"},
        "$.limits[0]": HOURS | {"limits": [{"name": "No bound"}]},
        "$.decide": HOURS | {"decide": HOURS["decide"] | {"sql": "1; DROP TABLE cafes"}},
        "$.objective.per_unit": HOURS | {"objective": HOURS["objective"] | {"per_unit": "revenue_per_hour * 2"}},
        "$.over.type": HOURS | {"over": {"type": "nope"}},
        "$.objective.per_unit.property": HOURS | {"objective": HOURS["objective"] | {"per_unit": {"property": "name"}}},
        "$.decide.max.property": HOURS | {"decide": HOURS["decide"] | {"max": {"property": "nope"}}},
        "$.limits[1].group": HOURS | {"limits": [TOTAL, REGION | {"group": "nope"}]},
        "$.limits[0].per_unit.measure": HOURS | {"limits": [TOTAL | {"per_unit": {"measure": "nope"}}]},
        "$.decide.write": HOURS | {"decide": HOURS["decide"] | {"write": "cafe_id"}},
        "$.decide.integer": HOURS | {"decide": {k: v for k, v in HOURS["decide"].items() if k != "integer"}},
        "$.over.filters": HOURS | {"over": {"type": "cafe", "filters": [{"property": "region", "op": "in", "value": "South", "link": "No Such"}]}},
    }
    for path, o in bad.items():
        with pytest.raises(abi.ActionError) as e:
            call("save_optimizer", optimizer=o | {"id": "broken"})
        errs = e.value.body.get("errors") or []
        assert e.value.status == 422 and [(x["file"], x["path"]) for x in errs] == [("optimizers/broken.json", path)], (path, e.value.body)
        assert errs[0]["error"] and errs[0]["fix"], "what is wrong and what to do"
    assert sorted(f.name for f in folder.iterdir()) == before, "nothing was saved"
    # each change is one commit in the project's Git history, in words
    commits = len(call("history")["commits"])
    assert call("save_optimizer", optimizer=HOURS | {"id": "second", "name": "A second one"}) == {"id": "second"}
    assert (folder / "second.json").is_file() and "second" in {o["id"] for o in call("optimizers")["optimizers"]}
    log = call("history")["commits"]
    assert len(log) == commits + 1 and log[0]["message"] == "save_optimizer: optimizers/second.json", log[0]
    with pytest.raises(abi.ActionError) as e:  # deleting one asks first
        call("delete_optimizer", optimizer="second")
    assert e.value.status == 409 and e.value.body["confirm_token"] and (folder / "second.json").is_file()
    sure("delete_optimizer", project=P, optimizer="second")
    assert sorted(f.name for f in folder.iterdir()) == before and call("history")["commits"][0]["message"] == "delete_optimizer: optimizers/second.json"
    # in the bundle, and an import with a broken one is refused whole
    b = engine(BUNDLE, timeout=300, stdin=json.dumps({"project": P}))
    assert b.returncode == 0, b.stderr[-2000:]
    out = json.loads(b.stdout.splitlines()[-1])
    assert out["file"] == doc and out["dry"] == [] and out["after"], out
    assert out["err"][0] == 422 and [(x["file"], x["path"]) for x in out["err"][1]] == [("optimizers/hours.json", "$.over.type")]
    assert "pi_new" not in {m["id"] for m in call("semantic_model")["measures"]}, "refused whole: the measure beside it wasn't added"


def test_a_project_without_optimizers_reads_as_before():
    """Section 4: a project without optimisers has an empty list, no folder for them and nothing to run."""
    other = "pi_opt_none"
    if other in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([other])
    abi.call("create_project", project=other, name="No optimisers")
    try:
        assert abi.call("optimizers", project=other) == {"optimizers": []} and not (Path(abi.ABI_PROJECTS) / other / "optimizers").exists()
        with pytest.raises(abi.ActionError) as e:
            abi.call("run_optimizer", project=other, optimizer="hours")
        assert e.value.status == 404 and e.value.body["message"] == f"No optimizer 'hours' in project '{other}'."
    finally:
        drop([other])


def test_plans_page_in_the_app(opt, tmp_path):
    """Automate > Plans: each plan in a sentence of business words, one primary button; pressing it works the plan out
    and shows its sentence, the limits and the cafés, each name a link to its page; the changes wait under Edits and
    nothing is applied. A Viewer reads their own plan and has no button. Nothing overflows at a phone's width.
    Screenshots beside (tmp_path)."""
    import app
    from test_pages import OWNER, Browser, drive

    run("hours", opt["token"])
    text, wide = "document.querySelector('main').innerText", "document.documentElement.scrollWidth > innerWidth + 1"
    card = "[...document.querySelectorAll('main .card')].find(c => c.querySelector('h3').textContent.includes('Hours per café'))"
    hubs, srv = set(app.HUBS), app.serve(P, 0, open_browser=False, signin=True)
    try:
        owner, viewer = Browser(srv.server_address[1], "admin", OWNER, P), Browser(srv.server_address[1], opt["name"], opt["pw"], P)
        page = drive(owner, tmp_path, [
            ["show", "plans"], ["wait", "main .card .mtext"], ["get", "primary", "document.querySelectorAll('main .btn.pri').length"],
            ["get", "lede", f"{card}.querySelector('.mtext').textContent"], ["get", "pressed", f"{card}.querySelector('.acts .btn').click()"],
            ["until", f"{card}.querySelector('.msg').textContent === 'Done.'"], ["get", "text", f"{card}.innerText"], ["get", "wide", wide],
            ["get", "names", f"[...{card}.querySelectorAll('.oopen')].map(b => b.textContent)"],
            ["get", "buttons", f"[...{card}.querySelectorAll('.btn')].map(b => b.textContent)"], ["shot", str(tmp_path / "plans-1440.png")],
            ["click", "main .card .oopen"], ["wait", "main .okv"], ["text", "opened", ".pghead h1"]])
        assert page["lede"] == ("Decides Hours for each café so that Revenue is as high as it can be, within these limits: Total hours, Hours per region.")
        assert page["primary"] == 1 and page["wide"] is False and page["opened"] == "Café Nord", "a name opens that café's page"
        assert page["names"] == ["Café Nord", "Café Süd", "Café Hafen", "Café Markt", "Café West"]
        assert "Revenue: 4,000 (now 1,400)" in page["text"] and "Total hours: 100 of 100 (fully used)" in page["text"] and "at its most" in page["text"]
        assert "Review the changes" in page["buttons"] and "Find the best plan" in page["buttons"]
        assert not re.search(r"cafe_id|planned_hours|\bcafes\b|optimi[sz]er|solver|constraint|ontology|object type|\bLP\b|\bMIP\b|[0-9a-f]{32}", page["text"])
        assert cells() == NOW and len(waiting()) == 1, "the button proposed the plan; nothing changed"
        phone = drive(owner, tmp_path, [["show", "plans"], ["wait", "main .card .mtext"], ["get", "wide", wide], ["get", "text", text],
                                        ["shot", str(tmp_path / "plans-390.png")]], width=390)
        assert phone["wide"] is False and "Hours per café" in phone["text"]
        theirs = drive(viewer, tmp_path, [["show", "plans"], ["wait", "main .card .mtext"], ["get", "text", f"{card}.innerText"],
                                          ["get", "buttons", "document.querySelectorAll('main .card .btn').length"],
                                          ["shot", str(tmp_path / "plans-viewer-1440.png")]])
        assert "Revenue: 1,700 (now 500)" in theirs["text"] and theirs["buttons"] == 0 and not re.search(r"Nord|Hafen|4,000", theirs["text"])
        assert all((tmp_path / f"{x}.png").stat().st_size > 10_000 for x in ("plans-1440", "plans-390", "plans-viewer-1440"))
    finally:
        srv.shutdown()
        for k in set(app.HUBS) - hubs:
            app.HUBS.pop(k, None)
