"""1.2.6 M8 (needs the engine and its worker at :8780, no LLM): flows and monitors per object, on Brewline. A data
condition with `per` is checked for each object of a type: an object whose row starts to hold gets its own run, with
the object as {{ data.trigger.* }}; its state is one row per (flow, object); at most 50 start per check and the rest
are reported once. `recipients` finds the people of an object in its data, but only project members who see the object
themselves are ever sent to, each with their own rows; anyone else is skipped and traced, and learns nothing. The first
send to an email domain still waits for a person's confirmation. Mail goes only to example.test addresses at a local
SMTP sink (tests/smtp_sink.py)."""
import csv
import email
import email.policy
import io
import json
import re
import secrets
import socket
import subprocess
import sys
import time

import pytest
from conftest import drop
from test_engine import engine
from test_ontology_nobreak import brew
from test_pages import sure

import abi
import settings

ROOT = abi.ROOT
P = "pi_object_flows"
MARK = "[hidden by row-level security]"
COND = {"query": {"measures": ["alarm"]}, "measure": "alarm", "op": ">", "value": 0}
ROWS = {"measures": ["alarm"], "dimensions": ["cafes.cafe_name"]}  # what an alert attaches: filtered to its café by ABI
HINT = "A run can't confirm it: send one email to that domain (or one post to that channel) yourself and confirm it; the next run then sends."
ODD = 'it\'s 100% "odd" {{ data.trigger.key }}'  # a key and title of any characters, a template among them

# the worker's minute tick for these flows, now or `ahead` minutes on (flows._data_condition, in the engine's own Python);
# the flows stay switched off, so only this ticks them. Prints the real cap and each flow's state rows.
TICK = r"""
import json, sys
from datetime import UTC, datetime, timedelta
from abi import db, flows
a = json.loads(sys.stdin.read())
cap, flows.PER_TICK = flows.PER_TICK, a.get("cap") or flows.PER_TICK
if a.get("slow"):  # this tick holds the flow's lock for a while after it started a run
    import time
    start = flows.start
    flows.start = lambda *x, **k: (start(*x, **k), time.sleep(a["slow"]))[0]
for f in a["flows"]:
    flows._data_condition(a["project"], flows._load(a["project"], f), datetime.now(UTC) + timedelta(minutes=a["ahead"]))
with db.connect() as c:
    state = {r["object"]: r["holds"] for r in c.execute(
        "SELECT object, holds FROM flow_conditions WHERE project = %s AND flow = %s", [a["project"], a["state"]])}
    feed = [r["message"] for r in c.execute("SELECT message FROM notifications WHERE project = %s AND message LIKE %s ORDER BY id DESC LIMIT 1",
                                            [a["project"], "%data condition%"])]
print(json.dumps({"cap": cap, "state": state, "feed": feed}))
"""

# the worker's whole minute tick (flows.tick), as if the projects on disk were all but one
WHOLE = r"""
import json, sys
from datetime import UTC, datetime
from abi import flows
a = json.loads(sys.stdin.read())
real = flows.git.projects
flows.git.projects = lambda: [p for p in real() if p != a["without"]]
flows.tick(datetime.now(UTC))
"""

# a database as schema 6 left it (flow_conditions keyed by project and flow, one row of state), upgraded by this code
UPGRADE = r"""
import json, os
os.environ["ABI_PG_POOL"] = "0"  # every connect() reads PGDATABASE as set below
import psycopg
from abi import db
D = "abi_upgrade_m8"
admin = psycopg.connect(autocommit=True)
admin.execute(f"DROP DATABASE IF EXISTS {D} WITH (FORCE)")
admin.execute(f"CREATE DATABASE {D}")
with psycopg.connect(dbname=D) as c:
    c.execute(db.STATE)
    c.execute("INSERT INTO schema_state (app_version, app_schema) VALUES ('1.2.5', 6)")
    c.execute("CREATE TABLE flow_conditions (project text NOT NULL, flow text NOT NULL, holds boolean NOT NULL, fired timestamptz,"
              " PRIMARY KEY (project, flow)); ALTER TABLE flow_conditions ADD COLUMN since timestamptz")
    c.execute("INSERT INTO flow_conditions VALUES ('p', 'low_stock', true, now(), NULL)")
os.environ["PGDATABASE"] = D
db.init()
db.init()  # a second start changes nothing
with psycopg.connect(dbname=D) as c:
    out = {"rows": c.execute("SELECT project, flow, holds, fired IS NOT NULL, object FROM flow_conditions").fetchall(),
           "key": c.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'flow_conditions_pkey'").fetchone()[0],
           "schema": c.execute("SELECT app_schema FROM schema_state").fetchone()[0], "number": db.SCHEMA_NUMBER}
admin.execute(f"DROP DATABASE {D} WITH (FORCE)")
print(json.dumps(out))
"""


def sql(q: str) -> list:
    return abi.call("sql_query", project=P, sql=q)["rows"]


def tick(fid: str, ahead: int = 0, cap: int | None = None, check: bool = True, slow: int = 0) -> dict:
    """One check of flow `fid`; returns the cap as shipped and the flow's state: {object: holds}, '' the flow's own row."""
    r = engine(TICK, timeout=600, stdin=json.dumps({"project": P, "flows": [fid] if check else [], "state": fid, "ahead": ahead, "cap": cap, "slow": slow}))
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert not [m for m in out["feed"] if "can't be checked" in m], out["feed"]
    return out


def runs(flow: str, n: int) -> list:
    """The flow's runs, oldest first, once there are `n` and each has ended."""
    end = time.monotonic() + 300
    while True:
        got = abi.call("flow_runs", project=P, flow=flow, limit=200)["runs"][::-1]
        if len(got) >= n and all(r["status"] not in ("queued", "running") for r in got):
            assert len(got) == n, [(r["id"], r["status"], r["error"]) for r in got]
            return got
        assert time.monotonic() < end, [(r["id"], r["status"], r["error"]) for r in got]
        time.sleep(1)


def inbox(token: str | None = None) -> list:
    """A person's Inbox messages of this project (the workspace owner's without a token), oldest first."""
    if token is None:
        return abi.call("inbox", project=P)["messages"][::-1]
    with abi.as_user(token):
        return abi.call("inbox", project=P)["messages"][::-1]


def mails(w) -> list:
    return [email.message_from_string(x, policy=email.policy.default) for x in w["box"].read_text().split("=====\n")[:-1]] if w["box"].exists() else []


def alarms(w, *cafes: str) -> None:
    """Exactly these cafés raise the alarm (an edit of the input table, as a person makes it)."""
    now = dict(sql("SELECT cafe_id, alarm FROM cafe_contacts"))
    if edits := [{"key": [c], "column": "alarm", "value": int(c in cafes)} for c in now if now[c] != int(c in cafes)]:
        abi.call("edit_cells", project=P, table="cafe_contacts", edits=edits)


def flow(fid: str, steps: list, **trigger) -> dict:
    return {"id": fid, "name": fid.replace("_", " ").capitalize(), "enabled": False,
            "trigger": {"type": "data_condition", **COND, "per": "cafe", "cooldown_minutes": 60, **trigger}, "steps": steps}


POST = {"id": "post", "type": "action", "action": "post_message", "input": {
    "channel": "inbox", "text": "Alarm at {{ data.trigger.title }} ({{ data.trigger.object.seats }} seats)", "data": ROWS}}
MAIL = {"id": "mail", "type": "action", "action": "send_email", "when": {"value": "{{ data.trigger.recipients }}", "op": "!=", "to": []}, "input": {
    "to": "{{ data.trigger.recipients }}", "subject": "Alarm at {{ data.trigger.title }}", "body": "Its figures are below.", "data": ROWS}}


@pytest.fixture(scope="module")
def w(tmp_path_factory):
    """P: Brewline with the input table cafe_contacts (a café's manager and its alarm), the measure `alarm`, the object
    types café (its manager an email property), contact (linked to its café) and site (an input table keyed by two
    columns), a row rule (the Viewer `north` sees North cafés) and a column rule (no Viewer sees seats). Accounts with
    an email: north (Viewer), editor and late (Editors), outsider (no role here). Cafés: a (North, managed by north),
    b (North, by outsider), c (North, by an address with no account), s (South, by north, who can't see it), e (South, by
    the editor's name), l (South, by late), n (South, nobody). The account mail server is a local sink."""
    tmp = tmp_path_factory.mktemp("object_flows")
    brew(P, tmp)
    stamp, made, tok, mail, pws = secrets.token_hex(3), {}, {}, {}, {}
    dom = f"m8-{stamp}.example.test"
    before = settings.load().get("mail")
    sink = None
    try:
        for who, role in (("north", "viewer"), ("editor", "editor"), ("late", "editor"), ("outsider", None)):
            made[who], pw, mail[who] = f"pi_of_{who}_{stamp}", secrets.token_urlsafe(16), f"{who}@{'late-' if who == 'late' else ''}{dom}"
            uid = abi.call("create_user", name=made[who], password=pw)["id"]
            abi.call("set_user_email", name=made[who], email=mail[who])
            if role:
                abi.call("set_role", project=P, user=made[who], role=role)
            tok[who], pws[who] = abi.login(made[who], pw)[1]["token"], pw
            if who == "north":
                sure("set_rls_rule", project=P, id="region", table="cafes", column="region", users={str(uid): ["North"]})
        sure("set_column_rule", project=P, id="seats", table="cafes", columns=["seats"])
        a, b, c = (x[0] for x in sql("SELECT cafe_id FROM cafes WHERE region = 'North' ORDER BY cafe_id LIMIT 3"))
        s, e, l, n = (x[0] for x in sql("SELECT cafe_id FROM cafes WHERE region = 'South' ORDER BY cafe_id LIMIT 4"))
        sure("create_input_table", project=P, table="cafe_contacts", key=["cafe_id"],
             columns=[{"name": "cafe_id", "type": "VARCHAR"}, {"name": "manager", "type": "VARCHAR"}, {"name": "alarm", "type": "INTEGER"}])
        abi.call("add_relationship", project=P, **{"from": "cafe_contacts.cafe_id", "to": "cafes.cafe_id"})
        who = {a: mail["north"], b: mail["outsider"], c: f"nobody@{dom}", s: mail["north"].upper(), e: made["editor"], l: mail["late"], n: None}
        abi.call("edit_cells", project=P, table="cafe_contacts", add=[{"cafe_id": k, "manager": v, "alarm": 0} for k, v in who.items()])
        sure("create_input_table", project=P, table="sites", key=["region", "code"],
             columns=[{"name": "region", "type": "VARCHAR"}, {"name": "code", "type": "VARCHAR"}, {"name": "alarm", "type": "INTEGER"}])
        abi.call("edit_cells", project=P, table="sites", add=[{"region": r, "code": k, "alarm": 0} for r, k in (("North", "a/b"), ("North", ODD), ("Süd", "ü ñ"))])
        abi.call("set_measure", project=P, id="alarm", name="Alarm", expression="SUM(cafe_contacts.alarm)")
        abi.call("set_measure", project=P, id="site_alarm", name="Site alarm", expression="SUM(sites.alarm)")
        sure("save_ontology", project=P, ontology={
            "objects": [
                {"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name", "measures": ["alarm"],
                 "properties": [{"column": "cafe_contacts.manager", "name": "Manager", "meaning": "email"}, {"column": "city", "meaning": "city"}]},
                {"id": "contact", "table": "cafe_contacts", "name": "Contact", "plural": "Contacts", "title": "manager",
                 "properties": [{"column": "manager", "meaning": "person"}]},
                {"id": "site", "table": "sites", "name": "Site", "plural": "Sites", "title": "code"}],
            "links": [{"id": "contact_cafe", "from": "contact", "to": "cafe", "name": "café", "inverse": "contact", "via": ["cafe_contacts-cafe_id"]}]})
        with socket.socket() as s0:
            s0.bind(("127.0.0.1", 0))
            port = s0.getsockname()[1]
        box = tmp / "mailbox.txt"
        sink = subprocess.Popen([sys.executable, ROOT / "tests/smtp_sink.py", box, str(port)])
        for _ in range(100):  # until it listens
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        abi.call("set_account_mail", smtp={"host": "127.0.0.1", "port": port, "from": "abi@example.test", "security": "none"})
        sure("send_email", project=P, to=[f"setup@{dom}"], subject="Setup", body="This project may mail this domain.")  # FL-6, by a person
        names = dict(sql("SELECT cafe_id, cafe_name FROM cafes"))
        yield {"tok": tok, "mail": mail, "made": made, "pw": pws, "dom": dom, "box": box, "names": names,
               "a": a, "b": b, "c": c, "s": s, "e": e, "l": l, "n": n}
    finally:
        if sink:
            sink.terminate()
        abi.call("set_account_mail", smtp=before and {**before, **({"password": "${secret:SMTP_PASSWORD}"} if before.get("username") else {})})
        try:
            drop([P])
        finally:
            for name in made.values():
                sure("delete_user", name=name)


def test_one_run_per_object_edge(w):
    """Each café whose row starts to hold gets one run, with that café; none while it holds, none for an edge inside
    the cooldown, one again after it. The post carries the café (so its title opens the café's page) and only that
    café's rows; a Viewer gets the alerts of the cafés they see, in their own words and rows, and nothing of the others."""
    a, s, names = w["a"], w["s"], w["names"]
    sure("save_flow", project=P, flow=flow("cafe_alarm", [POST]))
    alarms(w)
    assert tick("cafe_alarm")["state"] == {"": False} and runs("cafe_alarm", 0) == [], "nothing holds: no run"
    alarms(w, a, s)
    st = tick("cafe_alarm")["state"]
    assert st == {"": False, json.dumps([a]): True, json.dumps([s]): True}, st
    got = runs("cafe_alarm", 2)
    assert {r["status"] for r in got} == {"ok"} and sorted(r["object"]["key"] for r in got) == sorted([[a], [s]]), got
    assert all(r["object"]["type"] == "cafe" for r in got), "Editors and Owners see which object a run was for"
    tick("cafe_alarm")
    assert len(runs("cafe_alarm", 2)) == 2, "still holding: no new run"
    alarms(w, s)
    tick("cafe_alarm")
    alarms(w, a, s)
    tick("cafe_alarm")
    assert len(runs("cafe_alarm", 2)) == 2, "an edge inside the cooldown is dropped, as a plain condition's"
    alarms(w, s)
    tick("cafe_alarm", ahead=61)
    alarms(w, a, s)
    tick("cafe_alarm", ahead=62)
    got = runs("cafe_alarm", 3)
    assert got[2]["object"]["key"] == [a], "false, then true again after the cooldown: one more run, for that café only"
    # the owner (unrestricted): one post per run, the café with it, its rows only
    mine, seats = [m for m in inbox() if m["text"].startswith("Alarm at ")], dict(sql("SELECT cafe_id, seats FROM cafes"))
    assert sorted(m["text"] for m in mine) == sorted(f"Alarm at {names[k]} ({seats[k]} seats)" for k in (a, s, a))
    for m in mine:
        o = m["data"]["object"]
        assert o["type"] == "cafe" and m["text"].startswith(f"Alarm at {o['title']} ") and o["title"] == names[o["key"][0]]
        assert m["data"]["rows"] == [{"cafes.cafe_name": o["title"], "alarm": 1}], "filtered to its café"
    # the Viewer of North: café a's alerts only, the hidden column masked in their copy, their own rows; nothing of café s
    theirs = [m for m in inbox(w["tok"]["north"]) if m["text"].startswith("Alarm at ")]
    assert [m["text"] for m in theirs] == [f"Alarm at {names[a]} ({MARK} seats)"] * 2, theirs
    assert all(m["data"]["object"] == {"type": "cafe", "key": [a], "title": names[a]} and m["data"]["rows"] == [{"cafes.cafe_name": names[a], "alarm": 1}]
               for m in theirs)
    assert names[s] not in json.dumps(theirs) and s not in json.dumps(theirs)
    with abi.as_user(w["tok"]["north"]):
        seen = abi.call("flow_runs", project=P, flow="cafe_alarm")["runs"]
        last = abi.call("list_flows", project=P)["last_runs"]
    assert seen == [] and "cafe_alarm" not in last, "a restricted Viewer's run history doesn't say that some café met the condition"
    # a per-object alert has no object until its condition finds one
    for act in ("run_flow", "test_task"):
        with pytest.raises(abi.ActionError) as e:
            abi.call(act, project=P, flow="cafe_alarm")
        assert e.value.status == 409 and "can't be run by hand" in e.value.body["message"], e.value.body


def test_cap_reports_rest(w):
    """At most 50 objects start a run in one check (3 here, set for this tick). The others count as alerted and are
    reported once, with their rows, to the flow's owner alone, who read them; nothing starts or is said again."""
    cafes = sorted(w[k] for k in "abcse")
    sure("save_flow", project=P, flow=flow("cap_alarm", [POST | {"input": POST["input"] | {"text": "Capped {{ data.trigger.title }}"}}]))
    alarms(w)
    tick("cap_alarm")
    alarms(w, *cafes)
    out = tick("cap_alarm", cap=3)
    assert out["cap"] == 50, "the cap as shipped"
    assert out["state"] == {"": False} | {json.dumps([k]): True for k in cafes}, "the rest count as alerted: none is retried"
    got = runs("cap_alarm", 3)
    assert sorted(r["object"]["key"][0] for r in got) == cafes[:3]
    said = [m for m in inbox() if "started to meet its condition" in m["text"]]
    assert len(said) == 1 and said[0]["text"] == ("Flow Cap alarm: 5 cafés started to meet its condition at once. 3 of them got an alert."
                                                  " The other 2 are listed here and get one when they stop and start again."), said
    assert said[0]["data"]["rows"] == [{"cafes.cafe_id": k, "alarm": 1} for k in cafes[3:]], "the rest, by their rows"
    for who in ("editor", "north"):
        assert not [m for m in inbox(w["tok"][who]) if "started to meet" in m["text"]], f"{who} is not told what the owner read"
    assert tick("cap_alarm", cap=3)["state"] == out["state"]
    assert len(runs("cap_alarm", 3)) == 3 and len([m for m in inbox() if "started to meet its condition" in m["text"]]) == 1, "said once, not retried"
    assert sorted(m["text"] for m in inbox() if m["text"].startswith("Capped ")) == sorted(f"Capped {w['names'][k]}" for k in cafes[:3])


def test_state_per_object(w):
    """State is one row per (flow, object that holds): a key of two columns and of any characters is one object, its
    run ID safe; a title that looks like a template stays text. A row goes when its object no longer holds or is gone,
    all of a flow's go with a new trigger and with the flow. A schema 6 database's state row is kept as the flow's own."""
    post = {"id": "post", "type": "action", "action": "post_message", "input": {
        "channel": "inbox", "text": "Site {{ data.trigger.title }}", "data": {"measures": ["site_alarm"], "dimensions": ["sites.code"]}}}
    f = {"id": "site_alarm", "name": "Site alarm", "enabled": False, "steps": [post], "trigger": {
        "type": "data_condition", "query": {"measures": ["site_alarm"]}, "measure": "site_alarm", "op": ">", "value": 0, "per": "site", "cooldown_minutes": 60}}
    sure("save_flow", project=P, flow=f)
    odd, plain, own = ["North", ODD], ["North", "a/b"], {"": False}
    abi.call("edit_cells", project=P, table="sites", edits=[{"key": k, "column": "alarm", "value": 1} for k in (odd, plain)])
    st = tick("site_alarm")["state"]
    assert st == own | {json.dumps(odd): True, json.dumps(plain): True}, st
    got = runs("site_alarm", 2)
    assert {r["status"] for r in got} == {"ok"} and sorted(r["object"]["key"] for r in got) == sorted([odd, plain])
    assert all(re.fullmatch(rf"{P}/site_alarm/condition-[0-9a-f]{{16}}-[0-9TZ:-]{{20}}", r["id"]) for r in got), [r["id"] for r in got]
    posts = {m["text"]: m["data"] for m in inbox() if m["text"].startswith("Site ")}
    assert set(posts) == {f"Site {ODD}", "Site a/b"}, "a title's {{ … }} is text, never filled"
    assert posts[f"Site {ODD}"]["rows"] == [{"sites.code": ODD, "site_alarm": 1}] and posts[f"Site {ODD}"]["object"]["key"] == odd
    abi.call("edit_cells", project=P, table="sites", edits=[{"key": plain, "column": "alarm", "value": 0}])
    st = tick("site_alarm")["state"]
    assert st == own | {json.dumps(odd): True}, "a row only while its object holds"
    abi.call("edit_cells", project=P, table="sites", edits=[{"key": plain, "column": "alarm", "value": 1}])
    assert tick("site_alarm")["state"] == st | {json.dumps(plain): True} and len(runs("site_alarm", 2)) == 2, "its cooldown is read from its last run"
    abi.call("edit_cells", project=P, table="sites", delete=[odd, plain])  # objects that disappear
    st = tick("site_alarm")["state"]
    assert st == own, f"no row outlives its object: {st}"
    abi.call("edit_cells", project=P, table="sites", edits=[{"key": ["Süd", "ü ñ"], "column": "alarm", "value": 1}])
    assert len(tick("site_alarm")["state"]) == 2 and len(runs("site_alarm", 3)) == 3
    sure("save_flow", project=P, flow=f | {"trigger": f["trigger"] | {"value": 0.5}})
    assert tick("site_alarm", check=False)["state"] == own, "a new trigger starts over: only the flow's own row is left"
    assert len(tick("site_alarm")["state"]) == 2 and len(runs("site_alarm", 4)) == 4, "the new check alerts again, the old one's cooldown gone"
    sure("delete_flow", project=P, flow="site_alarm")
    end = time.monotonic() + 200  # the worker's own tick, within a minute
    while len(tick("site_alarm", check=False)["state"]) > 1 and time.monotonic() < end:
        time.sleep(5)
    assert tick("site_alarm", check=False)["state"] == own, "a deleted flow's per-object rows go at the worker's next tick"
    r = engine(UPGRADE, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.splitlines()[-1])
    assert out["rows"] == [["p", "low_stock", True, True, ""]] and out["key"] == "PRIMARY KEY (project, flow, object)", out
    assert out["schema"] == out["number"] == 7


def step(run: dict, sid: str) -> dict:
    return next(s for s in run["steps"] if s["step"] == sid)


def test_recipient_must_see_object(w):
    """`recipients` reads who a café's alert is for from its data, on the café or over a link. A member is sent to only
    when they see that café themselves: north gets café a's alert (Inbox and mail, their own rows), nothing of café s,
    which names them too; that run ends as stopped, its trace saying who was left out."""
    a, s, e, names, north = w["a"], w["s"], w["e"], w["names"], w["mail"]["north"]
    was = len(inbox(w["tok"]["north"]))
    for fid, rc in (("manager_alarm", {"property": "cafe_contacts.manager"}), ("linked_alarm", {"link": "contact_cafe", "property": "manager"})):
        sure("save_flow", project=P, flow=flow(fid, [
            POST | {"input": POST["input"] | {"text": fid + ": {{ data.trigger.title }}, {{ data.trigger.object.seats }} seats"}},
            MAIL | {"input": MAIL["input"] | {"subject": fid + ": {{ data.trigger.title }}"}}], recipients=rc))
        alarms(w)
        tick(fid)
        alarms(w, a, s, e)
        tick(fid)
        by = {r["object"]["key"][0]: r for r in runs(fid, 3)}
        assert by[a]["status"] == "ok" and step(by[a], "_recipients")["output"] == {"to": [north], "skipped": []}
        assert step(by[a], "mail")["output"] == {"sent": True, "to": [north], "skipped": []}
        assert by[s]["status"] == "stopped" and "no project member who sees this Café" in by[s]["error"], by[s]
        assert step(by[s], "_recipients")["output"] == {"to": [], "skipped": [{"value": north.upper(), "why": "can't see this Café"}]}
        assert [x["step"] for x in by[s]["steps"]] == ["_recipients"], "nothing ran, nothing was sent"
        assert by[e]["status"] == "ok" and step(by[e], "_recipients")["output"]["to"] == [w["mail"]["editor"]], "a person's account name finds them too"
    theirs = [m for m in inbox(w["tok"]["north"])[was:] if m["text"].startswith(("manager_alarm", "linked_alarm"))]
    assert [m["text"] for m in theirs] == [f"{f}: {names[a]}, {MARK} seats" for f in ("manager_alarm", "linked_alarm")], theirs
    assert all(m["data"]["rows"] == [{"cafes.cafe_name": names[a], "alarm": 1}] and m["data"]["object"]["key"] == [a] for m in theirs)
    assert names[s] not in json.dumps(theirs) and names[e] not in json.dumps(theirs), "nothing of a café they can't see, or of another's alert"
    ed = [m for m in inbox(w["tok"]["editor"]) if m["text"].startswith(("manager_alarm", "linked_alarm"))]
    seats = sql(f"SELECT seats FROM cafes WHERE cafe_id = '{e}'")[0][0]
    assert sorted(m["text"] for m in ed) == sorted(f"{f}: {names[e]}, {seats} seats" for f in ("manager_alarm", "linked_alarm")), "the editor's own café only"
    assert not [m for m in inbox() if m["text"].startswith(("manager_alarm", "linked_alarm"))], "the owner is not a recipient: no post"
    got = [m for m in mails(w) if m["Subject"].startswith(("manager_alarm", "linked_alarm"))]
    assert sorted((m["To"], m["Subject"]) for m in got) == sorted(
        (to, f"{f}: {names[k]}") for f in ("manager_alarm", "linked_alarm") for to, k in ((north, a), (w["mail"]["editor"], e))), [(m["To"], m["Subject"]) for m in got]
    for m in got:
        rows = list(csv.reader(io.StringIO(next(x.get_content() for x in m.iter_attachments() if x.get_filename() == "data.csv"))))
        assert rows[0] == ["cafes.cafe_name", "alarm"] and [r[0] for r in rows[1:]] == [m["Subject"].split(": ", 1)[1]], "its café's row only"
    assert names[s] not in json.dumps([str(m) for m in mails(w)]), "no mail names café s"


def test_non_member_skipped(w):
    """Only project members are ever sent to: an address in the data with an account but no role here, or with no
    account, gets nothing and is traced; so does a literal address of a non-member in a per-object alert's own step."""
    b, c, n, names, out = w["b"], w["c"], w["n"], w["names"], w["mail"]["outsider"]
    sure("save_flow", project=P, flow=flow("members_alarm", [POST, MAIL | {"input": MAIL["input"] | {"subject": "Members: {{ data.trigger.title }}"}}],
                                           recipients={"property": "cafe_contacts.manager"}))
    alarms(w)
    tick("members_alarm")
    alarms(w, b, c, n)
    tick("members_alarm")
    by = {r["object"]["key"][0]: r for r in runs("members_alarm", 3)}
    assert {r["status"] for r in by.values()} == {"stopped"}, [(r["status"], r["error"]) for r in by.values()]
    assert step(by[b], "_recipients")["output"] == {"to": [], "skipped": [{"value": out, "why": "not a member of this project"}]}
    assert step(by[c], "_recipients")["output"] == {"to": [], "skipped": [{"value": f"nobody@{w['dom']}", "why": "not a member of this project"}]}
    assert step(by[n], "_recipients")["output"] == {"to": [], "skipped": []}, "nobody named"
    # a literal address: confirmed for the project when the flow is saved (FL-6), but mailed only if it is a member's who sees the café
    lit = MAIL | {"input": MAIL["input"] | {"to": [w["mail"]["editor"], out, f"cfo@{w['dom']}"], "subject": "Literal: {{ data.trigger.title }}"}}
    sure("save_flow", project=P, flow=flow("literal_alarm", [{k: v for k, v in lit.items() if k != "when"}]))
    alarms(w)
    tick("literal_alarm")
    alarms(w, b)
    tick("literal_alarm")
    [r] = runs("literal_alarm", 1)
    assert r["status"] == "ok" and step(r, "mail")["output"] == {"sent": True, "to": [w["mail"]["editor"]], "skipped": [out, f"cfo@{w['dom']}"]}, r
    got = [m for m in mails(w) if m["Subject"].startswith(("Members: ", "Literal: "))]
    assert [(m["To"], m["Subject"]) for m in got] == [(w["mail"]["editor"], f"Literal: {names[b]}")], [(m["To"], m["Subject"]) for m in got]
    assert out not in json.dumps([m["To"] for m in mails(w)]) and f"nobody@{w['dom']}" not in json.dumps([m["To"] for m in mails(w)])


def test_first_send_still_confirms(w):
    """A recipient's mail domain the project never sent to still waits for a person: the run's send fails as needing the
    confirmation and nothing leaves; once a person has confirmed that domain by a send of their own, the next alert goes."""
    l, late, names = w["l"], w["mail"]["late"], w["names"]
    sure("save_flow", project=P, flow=flow("late_alarm", [MAIL | {"input": MAIL["input"] | {"subject": "Late: {{ data.trigger.title }}"}}],
                                           recipients={"property": "cafe_contacts.manager"}))
    alarms(w)
    tick("late_alarm")
    alarms(w, l)
    tick("late_alarm")
    [r] = runs("late_alarm", 1)
    assert r["status"] == "failed" and step(r, "_recipients")["output"] == {"to": [late], "skipped": []}
    assert step(r, "mail")["error"] == f"needs confirmation: First send from project '{P}' to email:{late.split('@')[1]}. {HINT}", r
    assert not [m for m in mails(w) if m["To"] == late], "nothing left before a person's yes"
    with pytest.raises(abi.ActionError) as e:
        abi.call("send_email", project=P, to=[late], subject="Hello", body="A first mail, sent by a person.")
    assert e.value.status == 409 and f"email:{late.split('@')[1]}" in e.value.body["message"]
    abi.call("send_email", confirm=e.value.body["confirm_token"], project=P, to=[late], subject="Hello", body="A first mail, sent by a person.")
    alarms(w)
    tick("late_alarm", ahead=61)
    alarms(w, l)
    tick("late_alarm", ahead=62)
    got = runs("late_alarm", 2)
    assert got[1]["status"] == "ok" and step(got[1], "mail")["output"] == {"sent": True, "to": [late], "skipped": []}
    assert [(m["To"], m["Subject"]) for m in mails(w) if m["Subject"].startswith("Late: ")] == [(late, f"Late: {names[l]}")]


def refused(f: dict) -> list:
    with pytest.raises(abi.ActionError) as e:
        abi.call("save_flow", project=P, flow=f)
    assert e.value.status == 422, e.value.body
    return [(x["file"], x["path"], x["error"], x["fix"]) for x in e.value.body["errors"]]


def test_a_wrong_per_or_recipients_names_the_file_the_field_and_the_fix(w):
    """Saving is where a wrong `per` or `recipients` is caught, by file, field and fix; so is a step whose readers or
    rows can't be checked against the object (a channel, a tool's result, a report)."""
    rel = "flows/wrong.json"
    [(file, path, error, fix)] = refused(flow("wrong", [POST], per="shop"))
    assert (file, path, error) == (rel, "$.trigger.per", "Unknown object type 'shop'.") and "cafe, contact, site" in fix
    [(_, path, error, fix)] = refused(flow("wrong", [POST], recipients={"property": "city"}))
    assert path == "$.trigger.recipients.property" and "'city' is not an email or person property of object type 'cafe'" in error
    assert "cafe_contacts.manager" in fix and "meaning" in fix
    [(_, path, error, fix)] = refused(flow("wrong", [POST], recipients={"link": "visited", "property": "manager"}))
    assert path == "$.trigger.recipients.link" and "'visited' is not a link of object type 'cafe'" in error and "contact_cafe" in fix
    f = flow("wrong", [POST])
    [(_, path, error, fix)] = [x for x in refused(f | {"trigger": {k: v for k, v in f["trigger"].items() if k != "per"} | {"recipients": {"property": "manager"}}})
                               if x[1].startswith("$.trigger")]  # its step's {{ data.trigger }} has nothing to read either
    assert path == "$.trigger.recipients" and "no `per`" in error and "per:" in fix
    [(_, path, error, _)] = refused(f | {"trigger": f["trigger"] | {"query": {"measures": ["revenue"]}, "measure": "revenue", "per": "contact"}})
    assert path == "$.trigger.per" and error.startswith("The condition can't be checked for each Contact (cafe_contacts.cafe_id): ")
    [(_, path, error, _)] = refused(f | {"trigger": f["trigger"] | {"query": {"measures": ["revenue"]}, "measure": "revenue", "change": True,
                                                                     "period": "calendar.month"}})
    assert path == "$.trigger.per" and "can't be checked for each object" in error
    bad = [POST | {"input": {"channel": "ops", "text": "x"}}, {"id": "mail", "type": "action", "action": "send_email", "input": {
        "to": [f"x@{w['dom']}"], "subject": "x", "body": "x", "data": {"tool": "t"}, "report": {"report": "overview", "page": "main"}}}]
    assert [(p, fix.split(":")[0]) for _, p, e, fix in refused(flow("wrong", bad)) if "per-object alert" in e] == [
        ("$.steps[0].input.channel", "Post to channel inbox, or use a send_email step"),
        ("$.steps[1].input.data", "Attach a semantic query or a condition"), ("$.steps[1].input.report", "Leave `report` and `docs` out of a per-object alert; attach a semantic query as `data`.")]
    assert not (abi.ABI_PROJECTS / P / rel).exists()


def test_the_docs_example_of_an_alert_per_object_is_what_the_schemas_take(w):
    """docs/objects.md "An alert per object": its flow passes ABI's checks on this project when it is saved (the flow
    schema, the object type, the recipients property, the steps)."""
    [doc] = [json.loads(b) for b in re.findall(r"```jsonc\n(.*?)\n```", (ROOT / "docs/objects.md").read_text(), re.S)]
    assert doc["$schema"].endswith("/flow.json") and doc["trigger"]["per"] == "cafe" and doc["trigger"]["recipients"] == {"property": "cafe_contacts.manager"}
    sure("save_flow", project=P, flow=doc | {"enabled": False})  # off: this test's cafés get no alert from it
    assert json.loads((abi.ABI_PROJECTS / P / f"flows/{doc['id']}.json").read_text())["trigger"] == doc["trigger"]
    sure("delete_flow", project=P, flow=doc["id"])


def test_app_says_it_per_object(w, tmp_path):
    """The app, as the editor in a browser. New monitor offers "each café" under Check and then who it is for under Send
    to, in words, and says the check back in one sentence; saving it (after ABI's confirmation) makes ABI's per-object
    flow. Monitor and the flow's page say "For each café" and "Send to: the café's manager", never an id; the flow's
    page offers no Run now. The Inbox alert names its café and opens the café's page. Screenshots beside (tmp_path)."""
    import app
    from test_pages import OWNER, Browser, drive
    e, name, hubs = w["e"], w["names"][w["e"]], set(app.HUBS)
    alarms(w)
    srv = app.serve(P, 0, open_browser=False, signin=True)
    form = "[...document.querySelectorAll('main .monform label.l')].find(x => x.textContent === %s).nextElementSibling"
    pick = lambda label, value: f"(s => {{ s.value = {json.dumps(value)}; s.dispatchEvent(new Event('change', {{bubbles: true}}));" \
                                f" s.dispatchEvent(new Event('input', {{bubbles: true}})); return s.selectedOptions ? s.selectedOptions[0].text : s.value }})({form % json.dumps(label)})"  # noqa: E731
    card = "[...document.querySelectorAll('main .card')].find(c => c.querySelector('h3')?.textContent.includes('Alarm raised'))"
    try:
        ed = Browser(srv.server_address[1], w["made"]["editor"], w["pw"]["editor"], P)
        out = drive(ed, tmp_path, [
            ["show", "monitor"], ["click", "main button:has-text('New monitor')"], ["wait", "main .monform"], ["fill", "main .monform input.f", "Alarm raised"],
            ["get", "measure", pick("Measure", "alarm")], ["get", "op", pick("Alert when it", ">")], ["get", "value", pick("Value", "0")],
            ["get", "send_hidden", f"({form % json.dumps('Send to')}).hidden"],
            ["get", "check", pick("Check", "cafes.cafe_id")], ["get", "send_to", f"[...({form % json.dumps('Send to')}).options].map(o => o.text)"],
            ["get", "to", pick("Send to", json.dumps({"property": "cafe_contacts.manager"}, separators=(",", ":")))],
            ["text", "sum", "main .monform p.sum"], ["get", "hint", f"(e => e.hidden ? '' : e.textContent)(({form % json.dumps('Send to')}).nextElementSibling)"],
            ["shot", str(tmp_path / "monitor-form.png")],
            ["click", "main .monform button:has-text('Create')"], ["wait", "#confirm[open]"], ["click", "#c-yes"],
            ["until", f"({card})?.innerText.includes('For each café')"], ["until", f"({card})?.innerText.includes('No café where')"], ["get", "card", f"({card}).innerText"]])
        assert out["check"] == "each café, with an alert of its own" and out["send_hidden"] is True, out
        assert out["send_to"] == ["everyone who can see the café", "the café's manager", "the café's contact (manager)"], out["send_to"]
        assert out["sum"] == "For each café: alerts when Alarm is above 0. Send to: the café's manager, in the Inbox.", out["sum"]
        assert "For each café: ABI checks it every minute, also with this page closed. Send to: the café's manager, in the Inbox." in out["card"]
        assert "No café where Alarm is above 0" in out["card"], out["card"]
        assert out["hint"].startswith("People are found by their account's email or name."), out["hint"]  # round 2 (M2)
        f = json.loads((abi.ABI_PROJECTS / P / "flows/monitor_alarm_raised.json").read_text())
        assert f["trigger"]["per"] == "cafe" and f["trigger"]["recipients"] == {"property": "cafe_contacts.manager"} and f["enabled"]
        assert f["steps"][0]["input"]["text"] == "Monitor: Alarm raised: {{ data.trigger.title }}. It alerts for each café when Alarm is above 0."
        assert f["steps"][0]["input"]["data"]["query"]["dimensions"] == ["cafes.cafe_name"], "the alert's row names the café by its title"
        alarms(w, e)
        tick("monitor_alarm_raised")
        assert runs("monitor_alarm_raised", 1)[0]["status"] == "ok"
        text = f"Monitor: Alarm raised: {name}. It alerts for each café when Alarm is above 0."
        msg = f"[...document.querySelectorAll('main .item')].find(d => d.textContent.includes({json.dumps(text)}))"
        out = drive(ed, tmp_path, [
            ["show", "monitor"], ["until", f"({card})?.innerText.includes('Holds now')"], ["get", "card", f"({card}).innerText"], ["shot", str(tmp_path / "monitor.png")],
            ["get", "links", f"[...({card}).querySelectorAll('.mtext .oopen')].map(b => b.textContent)"],  # round 2 (M1): a name opens its page
            ["click", "main .mtext .oopen"], ["wait", "main .kv"], ["text", "opened", "main h1"],
            ["show", "flw:monitor_alarm_raised"], ["wait", "main .lastrun"], ["until", "document.querySelector('main .lastrun .oopen')"],
            ["get", "flow", "document.querySelector('main').innerText"], ["all", "buttons", "main .bar.top button"],
            ["shot", str(tmp_path / "flow.png")],
            ["show", "inbox"], ["until", f"!!({msg})"], ["get", "open", f"({msg}).querySelector('.oopen').textContent"],
            ["get", "figs", f"({msg}).querySelector('.figs').innerText"], ["shot", str(tmp_path / "inbox.png")],
            ["get", "_", f"({msg}).querySelector('.oopen').click()"], ["wait", "main .kv"], ["text", "title", "main h1"]])
        assert re.search(rf"1 of \d+ cafés: Alarm is above 0: {re.escape(name)}\n", out["card"]), out["card"]
        assert "For each café: when Alarm is above 0 · Send to: the café's manager" in out["flow"], out["flow"]
        assert "“Monitor: Alarm raised: (its name). It alerts for each café when Alarm is above 0.”" in out["flow"] and "for this café" in out["flow"]
        assert "Runs by itself, once for each café that starts to meet its condition." in out["flow"] and "Run now" not in out["buttons"], out["buttons"]
        assert not re.search(r"cafe_id|cafe_contacts|data\.trigger|monitor_alarm|\bper\b", out["flow"] + out["card"]), "no ids, columns or field names"
        assert out["open"] == f"Open {name}" and name in out["figs"] and out["title"] == name, out
        assert out["links"] == [name] and out["opened"] == name, "on the Monitor card, the café's name opens the café's page"
        sure("save_flow", project=P, flow=flow("shown_alarm", [POST | {"input": POST["input"] | {"text": "Shown {{ data.trigger.title }}"}}]))
        alarms(w)
        tick("shown_alarm")
        alarms(w, w["a"], w["b"], e)
        tick("shown_alarm", cap=1)
        runs("shown_alarm", 1)
        rest = [m for m in inbox() if m["text"].startswith("Flow Shown alarm: 3 cafés") and m["data"].get("objects")]
        assert len(rest) == 1 and OWNER.exists(), "the message of the two cafés past the cap, and the owner's sign-in"
        if rest and OWNER.exists():  # round 2 (M1): the owner's message of the objects past the cap, each by its name
            item = f"[...document.querySelectorAll('main .item')].find(d => d.textContent.includes({json.dumps(rest[-1]['text'])}))"
            seen = drive(Browser(srv.server_address[1], "admin", OWNER, P), tmp_path, [
                ["show", "inbox"], ["until", f"!!({item})"], ["get", "names", f"[...({item}).querySelectorAll('.oopen')].map(b => b.textContent)"],
                ["get", "heads", f"[...({item}).querySelectorAll('th')].map(t => t.textContent)"], ["shot", str(tmp_path / "inbox-rest.png")],
                ["get", "_", f"({item}).querySelector('.oopen').click()"], ["wait", "main .kv"], ["text", "title", "main h1"]])
            assert seen["names"] == [o["title"] for o in rest[-1]["data"]["objects"]] and seen["title"] == seen["names"][0], seen
            assert seen["heads"] == ["Café", "Alarm"], "its name and the figures, no key column"
            narrow = drive(Browser(srv.server_address[1], "admin", OWNER, P), tmp_path, [  # round 3: at a phone's width
                ["show", "inbox"], ["until", f"!!({item})"], ["shot", str(tmp_path / "inbox-rest-390.png")],
                ["get", "wide", "document.documentElement.scrollWidth > innerWidth + 1"]], width=390)
            assert narrow["wide"] is False, "nothing wider than the phone's screen"
            alarms(w, e)
            narrow = drive(ed, tmp_path, [["show", "monitor"], ["until", f"({card})?.innerText.includes('Holds now')"],
                                          ["shot", str(tmp_path / "monitor-390.png")], ["get", "links", f"[...({card}).querySelectorAll('.mtext .oopen')].map(b => b.textContent)"],
                                          ["get", "wide", "document.documentElement.scrollWidth > innerWidth + 1"]], width=390)
            assert narrow["links"] == [name] and narrow["wide"] is False
        assert all((tmp_path / f"{x}.png").stat().st_size > 10_000 for x in ("monitor-form", "monitor", "flow", "inbox"))
        ed.page()
        r = ed.post("/monitor", {"action": "delete", "id": "alarm_raised"})  # its flow goes with it, after ABI's confirmation
        assert ed.post(f"/confirm/{r['confirm']['id']}", {}).get("result") == [], r
        assert not (abi.ABI_PROJECTS / P / "flows/monitor_alarm_raised.json").exists()
    finally:
        srv.shutdown()
        for k in set(app.HUBS) - hubs:
            app.HUBS.pop(k, None)
        alarms(w)


def feed(token: str) -> list:
    """The project's alerts as this person's Inbox page gets them."""
    with abi.as_user(token):
        return [n["message"] for n in abi._open("GET", "/api/notifications")[1] if n.get("project") == P]


def test_the_owner_is_told_whom_an_alert_cannot_reach(w):
    """Round 2 (M2, L3, L5, L11): a recipient who isn't in the project, or nobody named, no longer ends silently: the
    flow's owner gets one Inbox message naming the cafés and people and what to do, said again only when it changes. A
    one-to-one property reads in a step as {{ data.trigger.object.<table>.<column> }}. With a cap, the rest are listed
    by key and name so each opens, and "got an alert" counts only the runs started. A failed run is told to the owner's
    Inbox, never to the project's feed, where a restricted Viewer would learn that some café met the condition."""
    a, b, n, e, names, out = w["a"], w["b"], w["n"], w["e"], w["names"], w["mail"]["outsider"]
    post = POST | {"input": POST["input"] | {"text": "Told: {{ data.trigger.title }}, managed by {{ data.trigger.object.cafe_contacts.manager }}"}}
    sure("save_flow", project=P, flow=flow("told_alarm", [post], recipients={"property": "cafe_contacts.manager"}))
    alarms(w)
    tick("told_alarm")
    alarms(w, a, b, n)
    tick("told_alarm")
    assert sorted(r["status"] for r in runs("told_alarm", 3)) == ["ok", "stopped", "stopped"]
    head = "Flow Told alarm couldn't reach everyone its alert is for. "
    told = [m["text"] for m in inbox() if m["text"].startswith(head)]
    part = {b: f"{names[b]}: {out} (not a member of this project)", n: f"{names[n]}: nobody is named"}
    assert told == [head + "; ".join(part[k] for k in sorted(part)) + ". People are found by their account's"
                    " email or name. Someone who isn't in this project: ask a workspace owner to add them under Users & access with that"
                    " email, or correct the value. Where nobody is named: fill in the value. They get the alert the next time the condition"
                    " starts to hold for that café."], told
    for who in ("editor", "north"):
        assert not [m for m in inbox(w["tok"][who]) if "couldn't reach" in m["text"]], f"{who} is not told"
    mine = [m["text"] for m in inbox(w["tok"]["north"]) if m["text"].startswith("Told: ")]
    assert mine == [f"Told: {names[a]}, managed by {w['mail']['north']}"], "a one-to-one property, as the recipient sees it"
    alarms(w)
    tick("told_alarm", ahead=61)
    alarms(w, a, b, n)
    tick("told_alarm", ahead=62)
    runs("told_alarm", 6)
    assert [m["text"] for m in inbox() if m["text"].startswith(head)] == told, "the same people again: not said twice"
    # under the cap: one run started (café a's), the others listed by key and name; a stopped one is not "an alert"
    sure("save_flow", project=P, flow=flow("few_alarm", [post], recipients={"property": "cafe_contacts.manager"}))
    alarms(w)
    tick("few_alarm")
    alarms(w, a, b, e)
    cafes = sorted([a, b, e])
    tick("few_alarm", cap=2)
    got = {r["object"]["key"][0]: r["status"] for r in runs("few_alarm", 2)}
    assert set(got) == set(cafes[:2]), got
    [rest] = [m for m in inbox() if m["text"].startswith("Flow Few alarm: 3 ")]
    started = sum(v == "ok" for v in got.values())
    assert f"{started} of them got an alert. The other 1 are listed here" in rest["text"], rest["text"]
    assert rest["data"]["objects"] == [{"key": [cafes[2]], "title": names[cafes[2]]}] and rest["data"]["type"] == "cafe"
    assert rest["data"]["keys"] == ["cafes.cafe_id"] and rest["data"]["rows"] == [{"cafes.cafe_id": cafes[2], "alarm": 1}]
    # a failed run: the owner's Inbox, not the feed
    sure("save_flow", project=P, flow=flow("fail_alarm", [{k: v for k, v in MAIL.items() if k != "when"} | {
        "input": MAIL["input"] | {"to": [f"x@unconfirmed-{w['dom']}"]}}]))
    r = engine("import sys\nfrom abi import db\nwith db.connect() as c:\n    c.execute('DELETE FROM send_targets WHERE project = %s AND target = %s',"
               " sys.argv[1:])".replace("sys.argv[1:]", json.dumps([P, f"email:unconfirmed-{w['dom']}"])))  # as if never confirmed
    assert r.returncode == 0, r.stderr[-2000:]
    alarms(w)
    tick("fail_alarm")
    alarms(w, a)
    tick("fail_alarm")
    [r] = runs("fail_alarm", 1)
    assert r["status"] == "failed" and r["error"].endswith("yourself and confirm it; the next run then sends."), r["error"]
    assert [m["text"] for m in inbox() if m["text"].startswith("Flow Fail alarm (owner ")][-1].endswith(r["error"])
    for who in ("north", "editor"):
        assert not [m for m in feed(w["tok"][who]) if "Fail alarm (owner" in m], f"nothing of it in {who}'s feed"
    assert not [m for m in feed(w["tok"]["north"]) if re.search(r"Flow (Told|Few|Fail|Late|Cafe|Cap|Manager|Linked|Members|Literal) alarm \(owner", m)], \
        "a restricted Viewer's feed never says that a per-object alert ran"


def test_overlapping_ticks_and_the_cleanup_keep_the_state(w):
    """Round 2 (L1, L2): a tick that waited for another's lock reads the state that tick wrote, so the cap holds and the
    rest are reported once; the worker's cleanup leaves a switched-off flow's rows and those of a project not listed
    in that tick."""
    cafes = sorted(w[k] for k in "abs")
    sure("save_flow", project=P, flow=flow("twice_alarm", [POST | {"input": POST["input"] | {"text": "Twice {{ data.trigger.title }}"}}]))
    alarms(w)
    tick("twice_alarm")
    alarms(w, *cafes)
    env = subprocess.run(["sh", "-c", "scripts/engine.sh env"], cwd=ROOT, capture_output=True, text=True).stdout
    first = subprocess.Popen(["sh", "-c", f"{env}\nexec {ROOT / 'engine/backend/.venv/bin/python'} -c \"$0\"", TICK], cwd=ROOT, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first.stdin.write(json.dumps({"project": P, "flows": ["twice_alarm"], "state": "twice_alarm", "ahead": 0, "cap": 1, "slow": 20}))
    first.stdin.close()
    end = time.monotonic() + 120  # its run is in, its lock held
    while not abi.call("flow_runs", project=P, flow="twice_alarm")["runs"]:
        assert time.monotonic() < end and first.poll() is None, first.stderr.read()[-2000:]
        time.sleep(0.5)
    state = tick("twice_alarm", cap=1)["state"]  # waits for the first one
    assert first.wait(120) == 0, first.stderr.read()[-2000:]
    assert state == {"": False} | {json.dumps([k]): True for k in cafes}
    assert [r["object"]["key"][0] for r in runs("twice_alarm", 1)] == cafes[:1], "one run: the second tick saw the first one's state"
    assert len([m for m in inbox() if m["text"].startswith("Flow Twice alarm: 3 ")]) == 1, "the rest are reported once"
    # the cleanup: the flow is switched off and stays; then its project isn't among those the tick lists
    for without in ("", P):
        r = engine(WHOLE, timeout=600, stdin=json.dumps({"without": without}))
        assert r.returncode == 0, r.stderr[-2000:]
        assert tick("twice_alarm", check=False)["state"] == state, f"rows kept (tick without {without or 'nothing'})"


def test_a_per_object_flow_runs_only_what_is_on_its_list(w):
    """Round 2 (M3, L6, L9): an allow-list at save, each refusal with file, field and fix: no status post (notify), no
    approval, tool step, run_flow or "answer changed" check, and no attached rows that can't be filtered to the object;
    and a published tool that sends is refused inside the run, before anything else of it."""
    said = lambda f: {p: (e, fix) for _, p, e, fix in refused(f)}  # noqa: E731
    steps = [{"id": "ok", "type": "approval", "message": "Send it?"}, {"id": "tool", "type": "tool", "tool": "lookup"},
             {"id": "again", "type": "action", "action": "run_flow", "input": {"flow": "cafe_alarm"}},
             {"id": "ask", "type": "agent", "agent": "helper", "prompt": "Why?"}, {"id": "new", "type": "condition", "changed": "ask"},
             POST | {"input": POST["input"] | {"data": {"measures": ["site_alarm"]}}}]
    got = said(flow("listed", steps) | {"notify": {"channel": "ops", "on": ["failed"]}})
    assert "Leave `notify` out" in got["$.notify"][1] and "aren't posted to a channel" in got["$.notify"][0]
    for i, what in ((0, "approval"), (1, "tool"), (2, "run_flow")):
        e, fix = got[f"$.steps[{i}]"]
        assert f"({what}) isn't one an alert for one object may run" in e and "Use only agent, condition, refresh, snapshot steps" in fix, (e, fix)
    assert "$.steps[3]" not in got or "may run" not in got["$.steps[3]"][0], "an agent step is on the list (its sends are checked when it runs)"
    assert not [p for p, (e, _) in got.items() if "may run" in e and p not in ("$.steps[0]", "$.steps[1]", "$.steps[2]")], "only those three are off the list"
    assert "compares with the run before it, which is another object's" in got["$.steps[4]"][0]
    e, fix = got["$.steps[5].input.data"]
    assert e.startswith("Step 'post' attaches rows that can't be filtered to one Café: ") and "table 'cafes'" in fix
    assert not (abi.ABI_PROJECTS / P / "flows/listed.json").exists()
    src = (ROOT / "engine/backend/abi/tools/__init__.py").read_text()
    assert src.index('if t.get("kind") == "api" and builtin.flows.OBJECT.get():') < src.index("if sends(t) and (test := builtin.flows.TEST_RUN.get()) is not None:")


GUARD = r"""
import json, sys
import abi.app  # noqa: F401
from abi import actions, db, flows, security
a = json.loads(sys.stdin.read())
with db.connect() as c:
    u = security.user(c, a["uid"])
flows.OBJECT.set({"type": "cafe", "key": ["x"], "title": "x", "object": {}})  # inside a per-object run
out = {}
for tool in a["tools"]:
    try:
        actions.run("run_tool", {"project": a["project"], "tool": tool, "inputs": {}}, u, "agent")
        out[tool] = "ran"
    except actions.ActionFailed as e:
        out[tool] = [e.status, str(e)]
print(json.dumps(out))
"""


def test_the_run_itself_refuses_what_the_save_check_refuses(w):
    """Round 3 (M1, M2, L1, L2, L4, L8): a step written into the flow's file behind the save check doesn't run: the run
    fails with the refusal, nothing is sent and the owner is told once, in their own Inbox. Inside a per-object run every
    published API tool is refused, a reading one too, before any request leaves (run_tool, as an agent's tool call
    reaches it; the bench has no model, so no agent step runs here). Failing runs and one that went well in one check
    are one message and no "works again". Recipients without an email address stop the run with the reason; a stopped
    run starts no cooldown; the owner is told again when the people it couldn't reach change; an object with a key too
    long to keep is left out and the owner told."""
    a, b, s, e, names = w["a"], w["b"], w["s"], w["e"], w["names"]
    path = abi.ABI_PROJECTS / P / "flows/behind_alarm.json"
    for n, (step, why) in enumerate((
            ({"id": "post", "type": "action", "action": "post_message", "input": {"channel": "ops", "text": "Behind {{ data.trigger.title }}"}},
             "step 'post' failed: An alert for one object reaches only project members who see it: channel 'ops' has readers that can't be"
             " checked. Post to channel inbox, or use send_email."),
            ({"id": "tool", "type": "tool", "tool": "hook"}, "step(s) tool aren't ones an alert for one object may run. Save the flow again with only"
             " these steps: refresh a table, ask an agent, export a report, check a measure, post to the Inbox, send an email."))):
        sure("save_flow", project=P, flow=flow("behind_alarm", [POST | {"input": POST["input"] | {"text": "Behind {{ data.trigger.title }}"}}]))
        f = json.loads(path.read_text())
        path.write_text(json.dumps(f | {"steps": [step]}))  # behind ABI's back: what no save lets through
        alarms(w)
        tick("behind_alarm", ahead=70 * n)
        alarms(w, a)
        tick("behind_alarm", ahead=70 * n + 1)
        r = runs("behind_alarm", n + 1)[-1]
        assert r["status"] == "failed" and r["error"] == why, r
        told = [m["text"] for m in inbox() if m["text"].startswith("Flow Behind alarm (owner ")]
        assert len(told) == n + 1 and told[-1].endswith(why), told
    assert not [m for m in inbox() + inbox(w["tok"]["north"]) if m["text"].startswith("Behind ")], "nothing was posted"
    # every published API tool, sending or reading, is refused inside the run, before a request
    tool = {"$schema": "https://schema.abi.invalid/v1/tool.json", "name": "Hook", "description": "Calls outside.", "version": 1, "status": "published",
            "kind": "api", "owner": f["owner"], "inputs": {"type": "object", "additionalProperties": False, "properties": {}},
            "rows": "/items", "outputs": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "string"}}}},
            "tests": [{"inputs": {}}], "permissions": {"role": "viewer"}}
    made = {"hook_post": "POST", "hook_get": "GET"}
    try:
        (abi.ABI_PROJECTS / P / "tools").mkdir(exist_ok=True)
        for tid, method in made.items():
            (abi.ABI_PROJECTS / P / f"tools/{tid}.json").write_text(json.dumps(tool | {"id": tid, "request": {
                "method": method, "url": "https://hooks.example.test/x"}}))
        r = engine(GUARD, timeout=120, stdin=json.dumps({"project": P, "uid": int(f["owner"]), "tools": list(made)}))
        assert r.returncode == 0, r.stderr[-2000:]
        out = json.loads(r.stdout.splitlines()[-1])
        for tid in made:
            assert out[tid] == [403, f"Tool '{tid}' calls an outside API; an alert for one object reaches only project members who see it."
                                     " Use post_message (channel inbox) or send_email."], out
    finally:
        for tid in made:
            (abi.ABI_PROJECTS / P / f"tools/{tid}.json").unlink(missing_ok=True)
        path.unlink()
    # two failing runs and one that went well in one check: one message, no "works again"
    mail = {k: v for k, v in MAIL.items() if k != "when"} | {"when": {"value": "{{ data.trigger.object.region }}", "op": "=", "to": "South"},
                                                             "input": MAIL["input"] | {"to": [f"x@mixed-{w['dom']}"]}}
    sure("save_flow", project=P, flow=flow("mixed_alarm", [mail]))
    r = engine("import sys\nfrom abi import db\nwith db.connect() as c:\n    c.execute('DELETE FROM send_targets WHERE project = %s AND target = %s',"
               " sys.argv[1:])".replace("sys.argv[1:]", json.dumps([P, f"email:mixed-{w['dom']}"])))  # as if never confirmed
    assert r.returncode == 0, r.stderr[-2000:]
    alarms(w)
    tick("mixed_alarm")
    alarms(w, a, s, e)
    tick("mixed_alarm")
    assert sorted(x["status"] for x in runs("mixed_alarm", 3)) == ["failed", "failed", "ok"]
    told = [m["text"] for m in inbox() if m["text"].startswith("Flow Mixed alarm (owner ")]
    assert len(told) == 1 and told[0].endswith(HINT) and not [m for m in inbox() if "Mixed alarm" in m["text"] and "works again" in m["text"]], told
    r = engine("import sys\nfrom abi import db\nwith db.connect() as c:\n    c.execute(\"UPDATE inbox SET at = at - interval '2 days' WHERE project = %s"
               " AND text LIKE 'Flow Mixed alarm (owner %%'\", sys.argv[1:])".replace("sys.argv[1:]", json.dumps([P])))  # round 4: two days on
    assert r.returncode == 0, r.stderr[-2000:]
    alarms(w)
    tick("mixed_alarm", ahead=61)
    alarms(w, s)
    tick("mixed_alarm", ahead=62)
    runs("mixed_alarm", 4)
    again = [m["text"] for m in inbox() if m["text"].startswith("Flow Mixed alarm (owner ")]
    assert again == told * 2, "the same failure more than a day later is told again"
    # the people named have no email address: the run stops at the email step, with the reason
    sure("save_flow", project=P, flow=flow("nomail_alarm", [{k: v for k, v in MAIL.items() if k != "when"}, POST],
                                           recipients={"property": "cafe_contacts.manager"}))
    abi.call("set_user_email", name=w["made"]["editor"], email=None)
    try:
        alarms(w)
        tick("nomail_alarm")
        alarms(w, e)
        tick("nomail_alarm")
        [r] = runs("nomail_alarm", 1)
    finally:
        abi.call("set_user_email", name=w["made"]["editor"], email=w["mail"]["editor"])
    assert r["status"] == "stopped" and r["error"] == ("stopped at step 'mail': the people this alert is for have no email address on their account,"
                                                       " so nothing was mailed."), r
    assert [x["step"] for x in r["steps"]] == ["_recipients", "mail"], "the post after it didn't run"
    # round 4: the owner is told that person has no email address; a run that posted before it stopped keeps its cooldown
    assert [m for m in inbox() if m["text"].startswith("Flow Nomail alarm couldn't reach") and f"{names[e]}: {w['made']['editor']} (has no email address"
            " on their account: add one under Users & access)" in m["text"]]
    sure("save_flow", project=P, flow=flow("posted_alarm", [POST | {"input": POST["input"] | {"text": "Posted {{ data.trigger.title }}"}},
                                                            {k: v for k, v in MAIL.items() if k != "when"}], recipients={"property": "cafe_contacts.manager"}))
    abi.call("set_user_email", name=w["made"]["editor"], email=None)
    try:
        for _ in (1, 2):  # two edges inside the cooldown
            alarms(w)
            tick("posted_alarm")
            alarms(w, e)
            tick("posted_alarm")
        [r] = runs("posted_alarm", 1)
    finally:
        abi.call("set_user_email", name=w["made"]["editor"], email=w["mail"]["editor"])
    assert r["status"] == "stopped" and [x["step"] for x in r["steps"]] == ["_recipients", "post", "mail"], r
    assert len([m for m in inbox(w["tok"]["editor"]) if m["text"].startswith("Posted ")]) == 1, "one post: the second edge is inside its cooldown"
    # a stopped run (nobody to reach) starts no cooldown; a changed list of people is said again
    sure("save_flow", project=P, flow=flow("again_alarm", [POST | {"input": POST["input"] | {"text": "Again {{ data.trigger.title }}"}}],
                                           recipients={"property": "cafe_contacts.manager"}))
    head = "Flow Again alarm couldn't reach everyone its alert is for. "
    for n in (1, 2):
        alarms(w)
        tick("again_alarm")
        alarms(w, b)
        tick("again_alarm")
        assert [x["status"] for x in runs("again_alarm", n)] == ["stopped"] * n, "no cooldown after a run that reached nobody"
    assert len([m for m in inbox() if m["text"].startswith(head)]) == 1, "the same person: said once"
    was = sql(f"SELECT manager FROM cafe_contacts WHERE cafe_id = '{b}'")[0][0]
    abi.call("edit_cells", project=P, table="cafe_contacts", edits=[{"key": [b], "column": "manager", "value": f"someone@else-{w['dom']}"}])
    try:
        alarms(w)
        tick("again_alarm")
        alarms(w, b)
        tick("again_alarm")
        runs("again_alarm", 3)
    finally:
        abi.call("edit_cells", project=P, table="cafe_contacts", edits=[{"key": [b], "column": "manager", "value": was}])
    said = [m["text"] for m in inbox() if m["text"].startswith(head)]
    assert len(said) == 2 and f"{names[b]}: someone@else-{w['dom']} (not a member of this project)" in said[1], said
    # a key too long to keep track of: no alert for it, the owner told what to do
    long = ["North", "k" * 2100]
    post = {"id": "post", "type": "action", "action": "post_message", "input": {"channel": "inbox", "text": "Long {{ data.trigger.key }}"}}
    sure("save_flow", project=P, flow={"id": "long_alarm", "name": "Long alarm", "enabled": False, "steps": [post], "trigger": {
        "type": "data_condition", "query": {"measures": ["site_alarm"]}, "measure": "site_alarm", "op": ">", "value": 0, "per": "site"}})
    abi.call("edit_cells", project=P, table="sites", add=[{"region": long[0], "code": long[1], "alarm": 1}])
    try:
        state = tick("long_alarm")["state"]
    finally:
        abi.call("edit_cells", project=P, table="sites", delete=[long])
    assert not [k for k in state if len(k) > 2000] and not [m for m in inbox() if m["text"].startswith("Long ") and long[1] in m["text"]]
    assert [m["text"] for m in inbox() if m["text"].startswith("Flow Long alarm can't alert for")] == [
        "Flow Long alarm can't alert for 1 of the sites: what identifies them is too long to keep track of. Shorten that value in the data, or use"
        " this alert for an object type with a shorter one."]


def test_a_pending_step_stays_waiting_when_the_person_is_in_viewer_mode():
    """M7's review, carried in: abi_pending stops at a confirmation that changed nothing because the app is in Viewer
    mode, and leaves that step waiting (it is not `done` as failed)."""
    src = (ROOT / ".pi/extensions/abi/index.ts").read_text()
    tool = src[src.index('name: "abi_pending"'):]
    tool = tool[:tool.index("pi.registerTool(")]
    stop = tool.index('if (String(r.status ?? "").startsWith("Viewer mode")) { out.push({ id: it.id, action: it.action, outcome: "waiting", detail: r }); break; }')
    assert tool.index("await confirmBridge(") < stop < tool.index('await runTeam(["pending", p.project, "done"')
    assert 'return { status: "Viewer mode: nothing changed.' in src


def test_a_restricted_recipient_gets_the_alert_of_the_period_the_flow_checked(w):
    """1.2.6 M9: a condition on the month before the latest (`period`, `skip_latest`), per café. Café a has visits in
    November only, café b in November and December: the flow's check, grouped by café, takes December as the latest
    month for every café and so checks November. The restricted recipient's own check, and the rows they get in the
    Inbox and by mail, are of that same November, not of café a's own months (its latest is November, the one before
    nothing: they would get no alert)."""
    a, b, north = w["a"], w["b"], w["mail"]["north"]
    sure("create_input_table", project=P, table="visits", key=["cafe_id", "day"],
         columns=[{"name": "cafe_id", "type": "VARCHAR"}, {"name": "day", "type": "DATE"}, {"name": "n", "type": "INTEGER"}])
    abi.call("add_relationship", project=P, **{"from": "visits.cafe_id", "to": "cafes.cafe_id"})
    abi.call("add_relationship", project=P, **{"from": "visits.day", "to": "calendar.day"})
    abi.call("edit_cells", project=P, table="visits", add=[{"cafe_id": a, "day": "2025-11-10", "n": 5}, {"cafe_id": b, "day": "2025-11-11", "n": 7},
                                                           {"cafe_id": b, "day": "2025-12-02", "n": 1}])
    abi.call("set_measure", project=P, id="visits_n", name="Visits", expression="SUM(visits.n)")
    cond = {"query": {"measures": ["visits_n"]}, "measure": "visits_n", "op": ">", "value": 0, "period": "calendar.month", "skip_latest": True}
    was, sent = len(inbox(w["tok"]["north"])), len(mails(w))
    sure("save_flow", project=P, flow=flow("visits_last_month", [
        POST | {"input": {"channel": "inbox", "text": "Visits at {{ data.trigger.title }} last month", "data": cond}},
        MAIL | {"input": MAIL["input"] | {"subject": "Visits at {{ data.trigger.title }} last month", "data": cond}},
        # round 4: a step that attaches the month's change (one total, no dimensions) still runs, filtered to its café
        POST | {"id": "trend", "input": {"channel": "inbox", "text": "Trend at {{ data.trigger.title }}", "data": {
            "query": {"measures": ["visits_n"]}, "measure": "visits_n", "op": ">", "value": -1000, "period": "calendar.month", "change": True}}}],
        **cond, recipients={"property": "cafe_contacts.manager"}))
    tick("visits_last_month")
    by = {r["object"]["key"][0]: r for r in runs("visits_last_month", 2)}
    assert set(by) == {a, b} and by[a]["status"] == "ok", by
    assert step(by[a], "post")["output"] == {"sent": True, "channel": "inbox"} and step(by[a], "mail")["output"]["to"] == [north], by[a]["steps"]
    assert step(by[a], "trend")["status"] == "ok" and step(by[a], "trend")["output"]["sent"] is True, step(by[a], "trend")
    november = [{"calendar.year": 2025, "calendar.month": 11, "visits_n": 5}]
    theirs = [m for m in inbox(w["tok"]["north"])[was:] if m["text"].startswith("Visits at ")]
    assert [m["text"] for m in theirs] == [f"Visits at {w['names'][a]} last month"] and theirs[0]["data"]["rows"] == november, theirs
    [mail] = [m for m in mails(w)[sent:] if m["Subject"].startswith("Visits at ")]
    rows = list(csv.reader(io.StringIO(next(x.get_content() for x in mail.iter_attachments() if x.get_filename() == "data.csv"))))
    assert mail["To"] == north and rows[0] == ["calendar.year", "calendar.month", "visits_n"] and [[float(v) for v in r] for r in rows[1:]] == [[2025, 11, 5]], rows
    sure("delete_flow", project=P, flow="visits_last_month")
