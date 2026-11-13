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
 West    South   8             8          50         0            10

 `hours`: whole hours per café, at most its max hours, 100 in total, 60 per region, the most revenue.
   Everyone: Nord 40 (its most), then Hafen 20 (North is full at 60), then Süd 40 (the total is full at 100):
             40*50 + 20*40 + 40*30 = 4,000 (now 10*50 + 10*40 + 10*30 + 10*20 = 1,400).
   South only (the Viewer): Süd 50 (its most), Markt 10 (South is full at 60): 50*30 + 10*20 = 1,700 (now 500).
 `units`: whole units per café, at most 10, 12 in total, the most of (the café's visit revenue x units): the measure.
   Everyone: Nord 10, Hafen 2: 3,000 + 400 = 3,400.  South only: Süd 10, Markt 2: 1,000 + 100 = 1,100.
 `wage`: hours (fractions allowed), at most max hours, wages at most 2,510, the most revenue. Revenue per wage:
   Nord 1.67, Hafen 1.6, Süd 1.5, so Nord 40 (1,200), Hafen 40 (1,000), Süd 310/20 = 15.5: 2,000 + 1,600 + 465 = 4,065.
 `cheap`: whole hours, at most max hours, exactly 100 in total, at least 10 per region, the lowest wage cost.
   North's 10 go to Hafen (25 an hour, Nord costs 30), the other 90 to the South's cheapest: West 50 (its most), Markt 40:
   10*25 + 50*8 + 40*16 = 1,290 (now 10*30 + 10*25 + 10*20 + 10*16 = 910).
 Tips are recorded for every café but West: a plan that needs each café's tips stops and names Café West.
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
CAFES = """cafe_id,name,region,revenue_per_hour,hourly_wage,max_hours,planned_hours,budget_hours,units,hours_needed,floor_hours,hours_2dp,hours_0dp
1,Café Nord,North,50,30,40,10,10.5,0,5,45,0,0
2,Café Hafen,North,40,25,40,10,10.5,0,5,0,0,0
3,Café Süd,South,30,20,50,10,10.5,0,5,0,0,0
4,Café Markt,South,20,16,50,10,10.5,0,,0,0,0
5,Café West,South,8,8,50,0,0.5,0,,0,0,0
"""
VISITS = """visit_id,cafe_id,amount
1,1,200
2,1,100
3,2,200
4,3,100
5,4,50
6,5,10
"""
TIPS = """tip_id,cafe_id,amount
1,1,5
2,2,4
3,3,3
4,4,2
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
BY_TIPS = UNITS | {"id": "by_tips", "name": "Units by tips", "objective": {"goal": "maximize", "name": "Tips", "per_unit": {"measure": "tips"}}}
CHEAP = {"id": "cheap", "name": "Cheapest cover", "over": {"type": "cafe"},
         "decide": {"name": "Hours", "write": "planned_hours", "max": {"property": "max_hours"}, "integer": True},
         "objective": {"goal": "minimize", "name": "Wage cost", "per_unit": {"property": "hourly_wage"}},
         "limits": [{"name": "Hours to cover", "equal": 100}, {"name": "Staffed regions", "group": "region", "min": 10}]}
DOWN = OPEN | {"id": "down", "name": "Down", "objective": {"goal": "minimize", "name": "Cost", "per_unit": -1}}
# 400 yes/no picks under five weighted limits, the most value: a plan is found at once, the best one not in two seconds
RND2 = random.Random(11)
VALUE = [round(RND2.uniform(10, 99), 2) for _ in range(1001)]
THING = ["Thing " + "".join("abcdefghij"[int(c)] for c in str(i)) for i in range(1, 1002)]  # names without figures; THING.index(name) + 1 = its key
ROOM = [[RND2.randint(10, 99) for _ in range(1001)] for _ in range(5)]
PICK = {"id": "pick", "name": "What to take", "over": {"type": "thing", "filters": [{"property": "thing_id", "op": "<=", "value": 400}]},
        "decide": {"name": "Taken", "write": "amount", "max": 1, "integer": True}, "time_limit_s": 2,
        "objective": {"goal": "maximize", "name": "Value", "per_unit": {"property": "value"}},
        "limits": [{"name": f"Room {i + 1}", "per_unit": {"property": f"v{i + 1}"}, "max": sum(v[:400]) // 3} for i, v in enumerate(ROOM)]}
RANGE = HOURS | {"id": "range", "name": "Hours within a range", "limits": [{"name": "Total hours", "min": 90, "max": 100}, REGION]}
# every café at its most is 230 hours: the range's least is out of reach, its most has nothing to do with it
RANGE_NO = HOURS | {"id": "range_no", "name": "A range out of reach", "limits": [{"name": "Total hours", "min": 250, "max": 300}]}
NEED = HOURS | {"id": "need", "name": "Hours needed first", "decide": HOURS["decide"] | {"min": {"property": "hours_needed"}}}
BOUNDS = HOURS | {"id": "bounds", "name": "A floor above the ceiling", "decide": HOURS["decide"] | {"min": {"property": "floor_hours"}}}
# hours_2dp holds two decimals. The cheapest hours first use 3,200 of the wages; Nord's 30 an hour get the other 310.08:
# 10.336 hours, held as 10.34, which is 310.20: the rounded plan uses 3,510.20 of 3,510.08
ROUND = {"id": "round", "name": "Hours to hand out", "over": {"type": "cafe"},
         "decide": {"name": "Hours", "write": "hours_2dp", "max": {"property": "max_hours"}},
         "objective": {"goal": "maximize", "name": "Hours given", "per_unit": 1},
         "limits": [{"name": "Wage budget", "per_unit": {"property": "hourly_wage"}, "max": 3510.08}]}
# 60 groups of one slot each: more limit rows than an agent is shown rows
GROUPS = {"id": "groups", "name": "Five slots", "over": {"type": "slot"}, "decide": {"name": "Picked", "write": "pick", "max": 1, "integer": True},
          "objective": {"goal": "maximize", "name": "Weight", "per_unit": {"property": "w1"}},
          "limits": [{"name": "Per slot", "group": "name", "max": 1}, {"name": "Slots to pick", "max": 5}]}
ALL = (HOURS, UNITS, WAGE, TIGHT, FLOOR, OPEN, FEW, SPLIT, MANY, BY_TIPS, CHEAP, DOWN, PICK, RANGE, RANGE_NO, NEED, BOUNDS, ROUND, GROUPS)
# an LLM's call (via "agent"), with no token and with a made-up one: what ABI answers each time
AGENT = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, security
from abi import worker
a = json.loads(sys.stdin.read())
with db.connect() as c:
    u = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
out, held, defer = [], [], worker.defer
worker.defer = lambda task, **kw: held.append((task, kw))  # a job is queued only after both calls: the second meets the first still open
for confirm in (None, "9999999999.made-up"):
    try:
        out.append(actions.run(a["action"], a["args"], u, "agent", confirm))
    except actions.ActionFailed as e:
        out.append([e.status, "confirm_token" in e.body])
for task, kw in held:
    defer(task, **kw)
print(json.dumps(out, default=str))
"""
# the registry, read and written where no action reaches: rows that look like runs a dead worker left, or a full queue
REGISTRY = r"""
import json, sys, uuid
from abi import db, optimize
a = json.loads(sys.stdin.read())
out = {}
with db.connect() as c:
    u = c.execute("SELECT id FROM users WHERE name = %s", [a["user"]]).fetchone()["id"] if a.get("user") else c.execute(
        "SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"]
    if a["do"] == "count":
        out = c.execute("SELECT count(*) AS n FROM optimizer_runs WHERE project = %s AND optimizer = %s", [a["project"], a["optimizer"]]).fetchone()
    elif a["do"] == "fake":  # `n` open runs, `age` minutes old
        ids = [str(uuid.uuid4()) for _ in range(a["n"])]
        for i, x in enumerate(ids):
            c.execute("INSERT INTO optimizer_runs (id, project, optimizer, user_id, restricted, spec, status, created, started) VALUES"
                      " (%s, %s, %s, %s, false, '{}', %s, now() - make_interval(mins => %s), now() - make_interval(mins => %s))",
                      [x, a["project"], a["optimizer"] + (str(i) if a["n"] > 1 else ""), u, a.get("status", "running"), a["age"], a["age"]])
        out = {"ids": ids}
    elif a["do"] == "read":
        out = {"rows": [[r["status"], r["error"]] for r in c.execute("SELECT status, error FROM optimizer_runs WHERE id = ANY(%s::uuid[]) ORDER BY id", [a["ids"]])]}
    elif a["do"] == "drop":
        c.execute("DELETE FROM optimizer_runs WHERE id = ANY(%s::uuid[])", [a["ids"]])
if a["do"] == "interrupted":
    optimize.interrupted(a["id"])
print(json.dumps(out))
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


def registry(do: str, **args) -> dict:
    r = engine(REGISTRY, timeout=120, stdin=json.dumps({"do": do, "project": P, **args}))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.splitlines()[-1])


def grounded(r: dict, first: int | None = None, names: bool = False) -> bool:
    """Every figure the run's sentence states is a value of the run's rows (plan, now or limit): what the agent's
    grounding check reads. Nothing else grounds one: no count, no key, no time. `first`: of its first rows only (an
    agent is shown 20). `names`: the objects' own names hold digits (Slot 12): they are names, taken out first."""
    have, text = {float(v) for x in r["rows"][:first] for v in x[2:5] if isinstance(v, int | float)}, r["text"]
    for x in sorted((x[1] for x in r["rows"] if x[5] is not None), key=len, reverse=True) if names else ():
        text = text.replace(x, "")
    return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)} <= have


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
    (d / "tips.csv").write_text(TIPS)
    (d / "slots.csv").write_text("slot_id,name," + ",".join(f"w{i + 1}" for i in range(6)) + ",pick\n" + "".join(
        f"{j + 1},Slot {j + 1}," + ",".join(str(w[j]) for w in WEIGHTS) + ",0\n" for j in range(60)))
    (d / "things.csv").write_text("thing_id,name,worth,amount,value," + ",".join(f"v{k + 1}" for k in range(5)) + "\n" + "".join(
        f"{i},{THING[i - 1]},{i % 7 + 1},{0 if i <= 400 else 0.5},{VALUE[i - 1]}," + ",".join(str(v[i - 1]) for v in ROOM) + "\n" for i in range(1, 1002)))
    if P in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([P])
    import duckdb  # the cafés' own Parquet: two of its columns are DECIMAL, with two decimals and with none
    duckdb.sql(f"COPY (SELECT * REPLACE (CAST(hours_2dp AS DECIMAL(10,2)) AS hours_2dp, CAST(hours_0dp AS DECIMAL(10,0)) AS hours_0dp)"
               f" FROM read_csv('{d / 'cafes.csv'}')) TO '{d / 'cafes.parquet'}' (FORMAT parquet)")
    abi.load("demo", P, [str(d / "cafes.parquet"), *parquet(*(d / f"{t}.csv" for t in ("visits", "tips", "slots", "things")))])
    name, pw = f"pi_opt_viewer_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    try:
        for t in ("cafe", "visit", "tip", "slot", "thing"):
            call("set_table", table=f"{t}s", key=[f"{t}_id"])
        for t in ("visits", "tips"):
            call("add_relationship", **{"from": f"{t}.cafe_id", "to": "cafes.cafe_id"})
        call("set_measure", id="revenue", name="Visit revenue", expression="SUM(visits.amount)")
        call("set_measure", id="tips", name="Tips", expression="SUM(tips.amount)")
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
        ["Changes", "Proposed for approval", 4, None, None, None],
        ["Limit fully used", "Total hours", 100, None, 100, None],
        ["Limit fully used", "Hours per region: North", 60, None, 60, None],
        ["Café", "Café Nord", 40, 10, "at its most", [1]],
        ["Café", "Café Süd", 40, 10, None, [3]],
        ["Café", "Café Hafen", 20, 10, None, [2]],
        ["Café", "Café Markt", 0, 10, None, [4]],
        ["Café", "Café West", 0, 0, None, [5]],
        ["Limit", "Hours per region: South", 40, None, 60, None]], r["rows"]
    assert all(type(x[2]) is int for x in r["rows"][4:]), "whole hours"
    assert r["text"] == ("The best plan of “Hours per café” gives 40 Hours to Café Nord, 40 to Café Süd, 20 to Café Hafen. Revenue: 4,000 (now 1,400)."
                         " Fully used: Total hours, Hours per region: North. At their most Hours: Café Nord."
                         " Proposed as 4 changes for a person to approve; nothing is changed until then.")
    assert not re.search(r"cafe_id|planned_hours|revenue_per_hour|\bcafes\b|optimi[sz]er|solver|constraint|ontology|object type|\bLP\b|\bMIP\b", r["text"])
    # every figure of the sentence is a value of the rows: what the agent's grounding check reads
    have = {float(v) for x in r["rows"] for v in x[2:5] if isinstance(v, int | float)}
    assert {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", r["text"])} <= have
    u = run("units")  # the measure, read per café
    assert u["rows"][0] == ["Total", "Revenue", 3400, 0, None, None] and objects(u) == {
        "Café Nord": 10, "Café Hafen": 2, "Café Süd": 0, "Café Markt": 0, "Café West": 0}
    assert u["rows"][2] == ["Limit fully used", "Units to hand out", 12, None, 12, None], "a limit the sentence names: before the objects"
    w = run("wage")  # fractions
    assert objects(w) == pytest.approx({"Café Nord": 40, "Café Hafen": 40, "Café Süd": 15.5, "Café Markt": 0, "Café West": 0}, abs=1e-6)
    assert w["rows"][0][:4] == ["Total", "Revenue", pytest.approx(4065, abs=1e-6), pytest.approx(1400 + 0.5 * (50 + 40 + 30 + 20) + 0.5 * 8)]
    assert w["rows"][2] == ["Limit fully used", "Wage budget", pytest.approx(2510, abs=1e-6), None, 2510, None]
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
    assert (e["outcome"], e["rows"]) == ("empty", []) and e["text"] == (
        "There is nothing to plan: “Cafés that are not there” found no Cafés you can see that match its filters."
        " Run it again once there are some, or ask the agent to change which Cafés this plan covers.")
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
    assert r["text"] == ("“An even split” found no plan in the time it is allowed, so nothing is proposed. Plan for fewer Slots at a time,"
                         " or ask the agent to allow this plan more time.")
    big = run("many")
    assert (big["status"], big["outcome"]) == ("ok", "too_many") and big["rows"] == [["Limit", "Things in one run", None, None, 1000, None]] and big["text"] == (
        "“Too many things” would plan for more than 1,000 Things, the most one run takes. Plan for fewer at a time, for example one region"
        " or one period per plan: ask the agent to narrow this plan.")
    assert grounded(big) and "proposal" not in big
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
        ["Limit fully used", "Hours per region: South", 60, None, 60, None],
        ["Café", "Café Süd", 50, 10, "at its most", [3]],
        ["Café", "Café Markt", 10, 10, None, [4]],
        ["Café", "Café West", 0, 0, None, [5]],
        ["Limit", "Total hours", 60, None, 100, None]], r["rows"]
    assert r["text"] == ("The best plan of “Hours per café” gives 50 Hours to Café Süd, 10 to Café Markt. Revenue: 1,700 (now 500)."
                         " Fully used: Hours per region: South. At their most Hours: Café Süd."
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
            "“Hours within the wage budget” can't be worked out for you: it needs a figure of the Cafés that isn't there or that you can't read.")
        with abi.as_user(tok):  # nor does the file tell them: business names only, no column, filter or figure
            listed = call("optimizers")["optimizers"]
        assert next(o for o in listed if o["id"] == "wage") | {"latest_run": None} == {
            "id": "wage", "name": "Hours within the wage budget", "over": {"type": "cafe"}, "decide": {"name": "Hours"},
            "objective": {"goal": "maximize", "name": "Revenue"}, "limits": [{"name": "Wage budget"}], "latest_run": None}
        assert not re.search(r"hourly_wage|revenue_per_hour|budget_hours|planned_hours|max_hours|2510|East", json.dumps([o | {"latest_run": None} for o in listed]))
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


def test_opt_minimise_equal_and_at_least(opt):
    """The lowest total: an exact limit and an at-least limit, each said as what it is (never "fully used" for an
    at-least one), every figure of the sentence in the rows. A cost with no lower end says which way it runs."""
    r = run("cheap")
    assert (r["status"], r["outcome"]) == ("ok", "best") and r["rows"] == [
        ["Total", "Wage cost", 1290, 910, None, None],
        ["Changes", "Proposed for approval", 4, None, None, None],
        ["At least, just met", "Staffed regions: North", 10, None, 10, None],
        ["Café", "Café West", 50, 0, "at its most", [5]],
        ["Café", "Café Markt", 40, 10, None, [4]],
        ["Café", "Café Hafen", 10, 10, None, [2]],
        ["Café", "Café Nord", 0, 10, None, [1]],
        ["Café", "Café Süd", 0, 10, None, [3]],
        ["Exactly", "Hours to cover", 100, None, 100, None],
        ["At least", "Staffed regions: South", 90, None, 10, None]], r["rows"]
    assert r["text"] == ("The best plan of “Cheapest cover” gives 50 Hours to Café West, 40 to Café Markt, 10 to Café Hafen. Wage cost: 1,290 (now 910)."
                         " Just met: Staffed regions: North. At their most Hours: Café West."
                         " Proposed as 4 changes for a person to approve; nothing is changed until then.")
    assert grounded(r) and "fully used" not in r["text"].lower()
    d = run("down")
    assert d["outcome"] == "unbounded" and d["text"] == (
        "“Down” has no best plan: Cost can fall without end. Add a limit: the most or the least Hours per Café, or a limit on a total.")
    for name in ("tight", "floor", "open", "few", "split", "range_no", "bounds"):  # no plan: still no figure of its own
        assert grounded(run(name)), name


def test_opt_time_limit_keeps_the_plan_found(opt):
    """When the time runs out with a plan that holds every limit, that plan is the answer, said to be the best found in
    the time and maybe not the best; it is proposed like any other and nothing changes. Measured: 400 yes/no picks
    under five limits, 2 seconds."""
    r = run("pick")
    assert (r["status"], r["outcome"], r["time_limit_s"]) == ("ok", "found", 2) and 2 <= r["seconds"] < 60, (r["outcome"], r["seconds"])
    assert r["text"].startswith("The best plan of “What to take” found in the time it is allowed gives 1 Taken to Thing ")
    assert " A better plan may exist: allow it more time to be sure." in r["text"] and grounded(r)
    taken = objects(r)
    assert len(taken) == 400 and set(taken.values()) == {0, 1} and r["rows"][0][2] == pytest.approx(sum(VALUE[THING.index(k)] for k, v in taken.items() if v))
    rooms = [x for x in r["rows"] if x[1].startswith("Room ")]
    assert len(rooms) == 5 and all(x[2] <= x[4] for x in rooms), "the plan found holds every limit"
    for i, x in enumerate(rooms):
        assert x[2] == sum(ROOM[i][THING.index(k)] for k, v in taken.items() if v)
    assert r["proposal"]["changes"] == sum(taken.values()) and r["proposal"]["batch"] in waiting(), "proposed, for a person to approve"
    assert call("sql_query", sql="SELECT count(*) FROM things WHERE amount = 1")["rows"] == [[0]]


def test_opt_needs_every_number(opt):
    """A measure that has no rows for one object is not a 0: the run stops and names the object and the measure, as it
    does for a property without a number. Numbers a file can't hold are refused when it is saved, never a server error."""
    r = run("by_tips")
    assert r["status"] == "failed" and r["rows"] == [] and r["error"] == (
        "“Units by tips” can't be worked out: Café “Café West” has no number for Tips. Ask the agent to leave such Cafés out of this plan,"
        " or to use a measure that gives 0 for them.")
    n = run("need")  # a property: two cafés have no hours needed
    assert n["status"] == "failed" and n["rows"] == [] and n["error"] == (
        "“Hours needed first” can't be worked out: Cafés “Café Markt”, “Café West” have no number for Hours needed."
        " Fill it in, or ask the agent to leave such Cafés out of this plan.")
    assert "proposal" not in r and waiting() == {}
    with pytest.raises(abi.ActionError) as e:
        call("save_optimizer", optimizer=HOURS | {"id": "huge", "limits": [TOTAL | {"max": 1e16}]})
    assert e.value.status == 422 and [(x["file"], x["path"], x["fix"]) for x in e.value.body["errors"]] == [
        ("optimizers/huge.json", "$.limits[0].max", "Use a larger unit (thousands, millions).")]
    big = HOURS | {"id": "huge", "limits": [TOTAL | {"max": 1e15}], "over": {"type": "cafe", "filters": [{"property": "cafe_id", "op": "<", "value": 1e17}]}}
    assert call("save_optimizer", optimizer=big) == {"id": "huge"}, "the largest number a file takes; a filter's value is the data's"
    sure("delete_optimizer", project=P, optimizer="huge")
    for bad in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(abi.ActionError) as e:
            call("save_optimizer", optimizer=HOURS | {"id": "huge", "objective": HOURS["objective"] | {"per_unit": bad}})
        assert 400 <= e.value.status < 500, e.value.body
    assert not (Path(abi.ABI_PROJECTS) / P / "optimizers" / "huge.json").exists()


def test_opt_one_open_proposal(opt):
    """A new run withdraws the proposal an earlier run of the same optimiser left open and says so: one optimiser, one
    open proposal. Another optimiser's proposal stays. A flow's step whose run finds no plan fails with the reason."""
    first, other = run("hours"), run("units")
    assert set(waiting()) == {first["proposal"]["batch"], other["proposal"]["batch"]}
    assert "withdrawn" not in first["text"]
    second = run("hours")
    assert second["text"].endswith(" Proposed as 4 changes for a person to approve; nothing is changed until then."
                                   " An earlier proposal of this plan that was still open is withdrawn.")
    assert set(waiting()) == {second["proposal"]["batch"], other["proposal"]["batch"]} and cells() == NOW and grounded(second)
    h = call("edit_history", table="cafes", limit=1000)
    at = h["columns"].index
    assert {r[at("status")] for r in h["entries"] if r[at("batch_id")] == first["proposal"]["batch"]} == {"rejected"}
    sure("save_flow", project=P, flow={"id": "plan_tight", "name": "Plan the tight one", "enabled": False, "trigger": {"type": "manual"},
                                       "steps": [{"id": "plan", "type": "action", "action": "run_optimizer", "input": {"optimizer": "tight"}}]})
    fr, end = call("run_flow", flow="plan_tight")["run"], time.monotonic() + 600
    while (x := call("flow_runs", run=fr)["runs"][0])["status"] in ("queued", "running"):
        assert time.monotonic() < end, x
        time.sleep(1)
    sure("delete_flow", project=P, flow="plan_tight")
    assert x["status"] == "failed" and "No plan fits: these limits can't all hold: Total hours (at most 100)" in json.dumps(x, ensure_ascii=False), x
    assert call("optimizers", optimizer="tight")["outcome"] == "infeasible", "the run itself is kept with its reason"


def test_opt_registry_stays_small_and_never_stuck(opt):
    """One run at a time per person and optimiser and ten open in a project, refused in words without a tool's name; the
    latest five finished runs are kept; a run a dead worker left open ends failed, at once when its job is found
    stalled and at the latest after 30 minutes, and blocks nothing; a project with a run open isn't deleted."""
    for _ in range(7):
        assert run("few")["outcome"] == "empty"
    assert registry("count", optimizer="few")["n"] == 5
    stale = registry("fake", optimizer="few", n=1, age=31)["ids"] + registry("fake", optimizer="ghost", n=1, age=31)["ids"]
    assert run("few")["outcome"] == "empty", "a run left open for longer than a run can take blocks nothing"
    assert registry("read", ids=stale[1:])["rows"] == [["failed", "The run did not finish. Run it again."]]
    assert registry("read", ids=stale[:1])["rows"] == [] and registry("count", optimizer="few")["n"] == 5, "and goes with the old runs"
    live = registry("fake", optimizer="few", n=1, age=0)["ids"]
    try:
        with pytest.raises(abi.ActionError) as e:
            call("run_optimizer", optimizer="few")
        assert e.value.status == 409 and e.value.body["message"] == "“Cafés that are not there” is already being worked out for you; its plan shows in a moment."
        with pytest.raises(abi.ActionError) as e:
            abi.call("delete_project", project=P)
        assert e.value.status == 409 and "queued or running" in e.value.body["message"]
        registry("interrupted", id=live[0])  # what the worker does for a run whose job stalled
        assert registry("read", ids=live)["rows"] == [["failed", "The run did not finish. Run it again."]]
        assert run("few")["outcome"] == "empty"
    finally:
        registry("drop", ids=stale + live)
    # one person's runs don't fill the project's places: three at a time each (the fourth is told so) ...
    three = registry("fake", optimizer="mine", n=3, age=0)["ids"] + registry("fake", optimizer="waiting", n=1, age=300, status="queued")["ids"]
    try:
        with pytest.raises(abi.ActionError) as e:
            call("run_optimizer", optimizer="few")
        assert e.value.status == 409 and e.value.body["message"] == "You already have several plans being worked out. Try again when one of them is done."
        assert [x[0] for x in registry("read", ids=three)["rows"]].count("queued") == 1, "a run that waits is not stale after 30 minutes"
    finally:
        registry("drop", ids=three)
    waited = registry("fake", optimizer="waited", n=1, age=361, status="queued")["ids"]
    assert run("few")["outcome"] == "empty" and registry("read", ids=waited)["rows"] == [["failed", "The run did not finish. Run it again."]]
    registry("drop", ids=waited)
    # ... and ten in the project, whoever they are
    full = registry("fake", optimizer="queue", n=10, age=0, status="queued", user=opt["name"])["ids"]
    try:
        with pytest.raises(abi.ActionError) as e:
            call("run_optimizer", optimizer="hours")
        assert e.value.status == 409 and e.value.body["message"] == "Too many plans are being worked out in this project right now. Try again in a moment."
        assert not re.search(r"optimizers|run_optimizer", e.value.body["message"])
    finally:
        registry("drop", ids=full)


def test_opt_restricted_since_reads_no_old_run(opt):
    """A person who ran a plan over every row as an Editor and is a restricted Viewer now doesn't keep reading it."""
    name, pw = f"pi_opt_editor_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    abi.call("create_user", name=name, password=pw)
    try:
        abi.call("set_role", project=P, user=name, role="editor")
        tok = abi.login(name, pw)[1]["token"]
        mine = run("hours", tok)
        assert mine["rows"][0][:3] == ["Total", "Revenue", 4000]
        sure("set_role", project=P, user=name, role="viewer")
        with abi.as_user(tok):
            now = call("optimizers", optimizer="hours")
        assert now["status"] == "never run" and now["rows"] == [] and "4000" not in json.dumps(now) and "Nord" not in json.dumps(now, ensure_ascii=False)
    finally:
        sure("delete_user", name=name)


def test_published_copies_carry_no_plans(opt):
    """Publish leaves optimizers/ out, as it leaves flows out: a plan is worked out and approved in the project."""
    import app

    abi.call("commit", project=P, message="plans") if call("changes").get("files") else None
    pub, bundle, _ = app.publish_bundle(P)
    try:
        assert (Path(abi.ABI_PROJECTS) / P / "optimizers" / "hours.json").is_file()
        assert "project.json" in bundle and "model/ontology.json" in bundle and not [rel for rel in bundle if rel.startswith(("optimizers/", "flows/"))]
    finally:
        if pub in {x["project"] for x in abi.call("list_projects")["projects"]}:
            drop([pub])


def test_opt_first_rows_hold_the_plan(opt):
    """An agent is shown a list's first 20 rows: the total, the number of changes, the limits the sentence names (up
    to three of a kind, then "and other limits") and the objects come first, the other limits after them, so with 60
    limits the sentence's figures and names and the plan's own are all in what the agent sees."""
    r = run("groups")
    rows = r["rows"]
    assert r["outcome"] == "best" and rows[0] == ["Total", "Weight", sum(sorted(WEIGHTS[0])[-5:]), 0, None, None]
    assert rows[1] == ["Changes", "Proposed for approval", 5, None, None, None]
    assert all(x[0] == "Limit fully used" and x[1].startswith("Per slot: Slot ") and x[1] in r["text"] for x in rows[2:5]), "the limits the sentence names"
    assert r["text"].count("Per slot: Slot ") == 3 and " and other limits. " in r["text"], "six are fully used: three are named"
    assert all(x[0] == "Slot" and x[5] is not None for x in rows[5:65]) and [x[2] for x in rows[5:11]] == [1, 1, 1, 1, 1, 0]
    assert len(rows) == 2 + 60 + 61 and all(x[0].startswith("Limit") for x in rows[65:]), "60 limits of one slot each, and the five to pick"
    assert grounded(r, first=20, names=True) and r["text"].startswith("The best plan of “Five slots” gives 1 Picked to Slot ")
    assert "Proposed as 5 changes for a person to approve" in r["text"]
    for name in ("hours", "cheap", "round", "pick"):  # and so for every plan: its sentence stands on its first rows
        assert grounded(run(name), first=20), name


def test_opt_decimal_column_and_rounding(opt):
    """A plan's numbers are held as its column holds them. Which limit is fully used is judged before rounding; when
    the rounded plan goes past a limit, the sentence says so with both figures. A column without decimals takes
    whole numbers only."""
    r = run("round")
    assert r["outcome"] == "best" and r["rows"] == [
        ["Total", "Hours given", 200.34, 0, None, None],
        ["Changes", "Proposed for approval", 5, None, None, None],
        ["Limit fully used", "Wage budget", 3510.2, None, 3510.08, None],
        ["Café", "Café Markt", 50, 0, "at its most", [4]],
        ["Café", "Café Süd", 50, 0, "at its most", [3]],
        ["Café", "Café West", 50, 0, "at its most", [5]],
        ["Café", "Café Hafen", 40, 0, "at its most", [2]],
        ["Café", "Café Nord", 10.34, 0, None, [1]]], r["rows"]
    assert r["text"] == ("The best plan of “Hours to hand out” gives 50 Hours to Café Markt, 50 to Café Süd, 50 to Café West, and some to other Cafés."
                         " Hours given: 200.34 (now 0). Fully used: Wage budget."
                         " After rounding, the plan is slightly over: Wage budget (3,510.2 of 3,510.08)."
                         " At their most Hours: Café Markt, Café Süd, Café West and others."
                         " Proposed as 5 changes for a person to approve; nothing is changed until then.")
    assert grounded(r)
    mine = waiting()[r["proposal"]["batch"]]
    assert sorted((c["row"]["cafe_id"], c["new"]) for c in mine["changes"]) == [(1, "10.34"), (2, "40.00"), (3, "50.00"), (4, "50.00"), (5, "50.00")]
    with pytest.raises(abi.ActionError) as e:  # DECIMAL(10,0): whole numbers
        call("save_optimizer", optimizer=ROUND | {"id": "whole", "decide": ROUND["decide"] | {"write": "hours_0dp"}})
    assert e.value.status == 422 and [(x["file"], x["path"]) for x in e.value.body["errors"]] == [("optimizers/whole.json", "$.decide.integer")]
    assert call("save_optimizer", optimizer=ROUND | {"id": "whole", "decide": ROUND["decide"] | {"write": "hours_0dp", "integer": True}}) == {"id": "whole"}
    w = run("whole")
    sure("delete_optimizer", project=P, optimizer="whole")
    assert objects(w) == {"Café Markt": 50, "Café Süd": 50, "Café West": 50, "Café Hafen": 40, "Café Nord": 10} and w["rows"][-1] == [
        "Limit", "Wage budget", 3500, None, 3510.08, None] and "After rounding" not in w["text"]


def test_opt_ranges_and_an_objects_own_bounds(opt):
    """A limit with a least and a most: the plan says which end it reaches; in a conflict only the bound that is in the
    way is named, with its figure in a row. An object whose own least is above its own most is named, with both."""
    r = run("range")
    assert r["rows"][0] == ["Total", "Revenue", 4000, 1400, None, None] and ["Limit fully used", "Total hours", 100, None, 100, None] in r["rows"]
    no = run("range_no")
    assert no["outcome"] == "infeasible" and no["rows"] == [
        ["Limit in conflict", "Hours per Café: at most its Max hours", None, None, None, None],
        ["Limit in conflict", "Total hours (at least 250)", None, None, 250, None]]
    assert no["text"] == ("No plan fits: these limits can't all hold: Hours per Café: at most its Max hours; Total hours (at least 250)."
                          " Relax one of them, then run it again.")
    b = run("bounds")
    assert b["outcome"] == "infeasible" and b["text"] == (
        "No plan fits: Café “Café Nord” needs at least 45 Hours and can take at most 40. Change one of the two, then run it again.")
    assert b["rows"] == [["Limit in conflict", "Hours for Café Nord: at least", None, None, 45, [1]],
                         ["Limit in conflict", "Hours for Café Nord: at most", None, None, 40, [1]]]
    assert "proposal" not in no and "proposal" not in b and cells() == NOW


def test_opt_every_run_leaves_at_most_one_proposal(opt):
    """One rule for an Editor's runs. A run refused before anything is worked out withdraws nothing, and the run whose
    proposal is still open is never pruned; a run that finds no plan withdraws the earlier proposal and says so."""
    def flip(o: dict) -> dict:
        call("save_optimizer", optimizer=o | {"id": "flip", "name": "Flip"})
        return run("flip")

    try:
        first = flip(HOURS)
        batch = first["proposal"]["batch"]
        late = flip(SPLIT)  # M10: the time ran out with no plan: nothing was learned, so nothing is withdrawn
        assert late["outcome"] == "timeout" and batch in waiting() and "withdrawn" not in late["text"]
        for _ in range(6):  # refused: Café West has no tips
            assert flip(BY_TIPS)["status"] == "failed"
        assert batch in waiting() and registry("count", optimizer="flip")["n"] == 6, "five refusals kept, and the run whose proposal is open"
        none = flip(TIGHT)
        assert none["outcome"] == "infeasible" and none["text"] == (
            "No plan fits: these limits can't all hold: Total hours (at most 100); Staffed regions (at least 60, for each Region)."
            " Relax one of them, then run it again. The proposal an earlier run of this plan left open is withdrawn.")
        assert batch not in waiting() and cells() == NOW
        again = flip(HOURS)  # a plan again: one proposal, the new one
        assert set(waiting()) == {again["proposal"]["batch"]} and "withdrawn" not in again["text"]
    finally:
        sure("delete_optimizer", project=P, optimizer="flip")


# a run that fails after its proposal was stored (the lake commits before the registry does)
ORPHAN = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, flows, optimize, security
a = json.loads(sys.stdin.read())
with db.connect() as c:
    u = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
def away(*x, **k):
    raise RuntimeError("the registry is away")
optimize._withdraw = away
flows.IN_FLOW.set(True)  # worked out in this process, as a flow's step is
try:
    actions.run("run_optimizer", {"project": a["project"], "optimizer": "orphan"}, u, "rest")
    print(json.dumps("ran"))
except Exception as e:
    print(json.dumps(type(e).__name__))
"""


def test_opt_a_failed_run_leaves_no_proposal(opt):
    """M10 (M12's security LOW): a run that fails after its plan was stored as a proposal takes that proposal back, so
    none outlives the run that knows it; the run reads failed."""
    call("save_optimizer", optimizer=HOURS | {"id": "orphan", "name": "Orphan"})
    try:
        was = set(waiting())
        r = engine(ORPHAN, timeout=300, stdin=json.dumps({"project": P}))
        assert r.returncode == 0 and json.loads(r.stdout.splitlines()[-1]) == "RuntimeError", r.stderr[-2000:]
        h = call("edit_history", table="cafes", limit=1000)
        at = h["columns"].index
        mine = {x[at("status")] for x in h["entries"] if x[at("comment")] == "The best plan of “Orphan”"}
        assert mine == {"rejected"} and set(waiting()) == was, "it was proposed, and taken back"
        assert call("optimizers", optimizer="orphan")["status"] == "failed"
    finally:
        sure("delete_optimizer", project=P, optimizer="orphan")


def test_opt_delete_is_refused_while_a_flow_runs_it(opt):
    """M10: a plan a flow runs is not deleted, and the person is told which flow before they are asked anything."""
    call("save_optimizer", optimizer=HOURS | {"id": "kept", "name": "Kept plan"})
    sure("save_flow", project=P, flow={"id": "plans_weekly", "name": "Weekly plan", "enabled": False, "trigger": {"type": "manual"},
                                       "steps": [{"id": "plan", "type": "action", "action": "run_optimizer", "input": {"optimizer": "kept"}}]})
    try:
        with pytest.raises(abi.ActionError) as e:
            call("delete_optimizer", optimizer="kept")
        assert e.value.status == 409 and "confirm_token" not in e.value.body, e.value.body
        assert e.value.body["message"] == "“Kept plan” is run by the flow “Weekly plan”. Take that step out of the flow, or delete the flow, first."
    finally:
        sure("delete_flow", project=P, flow="plans_weekly")
    with pytest.raises(abi.ActionError) as e:  # free now: asked, then deleted
        call("delete_optimizer", optimizer="kept")
    assert e.value.status == 409 and e.value.body["confirm_token"] and e.value.body["message"].startswith("Destructive: deletes the plan definition 'kept'")
    sure("delete_optimizer", project=P, optimizer="kept")
    assert "kept" not in [o["id"] for o in call("optimizers")["optimizers"]]


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
            ["until", f"{card}.querySelector('.msg').textContent === 'Done.'"], ["until", f"{card}.querySelectorAll('.plan .btn').length === 1"],
            ["get", "text", f"{card}.innerText"], ["get", "wide", wide],
            ["get", "names", f"[...{card}.querySelectorAll('.oopen')].map(b => b.textContent)"],
            ["get", "buttons", f"[...{card}.querySelectorAll('.btn')].map(b => b.textContent)"], ["shot", str(tmp_path / "plans-1440.png")],
            ["get", "go", f"{card}.querySelector('.oopen').click()"], ["wait", "main .okv"], ["text", "opened", ".pghead h1"]])
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
        # once the person has decided the proposal, there is nothing left to review: the button goes
        for b in list(waiting().values()):
            call("reject_edit", table=b["table"], batch=b["batch"])
        after = drive(owner, tmp_path, [["show", "plans"], ["wait", "main .card .mtext"],
                                        ["get", "buttons", "api('edit_proposals',{}).then(() => new Promise(z => setTimeout(z, 500))).then(() => [...document.querySelectorAll('main .card .btn')].map(b => b.textContent))"]])
        assert "Review the changes" not in after["buttons"] and "Find the best plan" in after["buttons"]
    finally:
        srv.shutdown()
        for k in set(app.HUBS) - hubs:
            app.HUBS.pop(k, None)
