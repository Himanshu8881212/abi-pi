"""M3.3 pages that don't queue (needs the engine at :8780, no LLM): page data computed once for the people who see the
same rows and columns, never handed to anyone else; the engine's pooled Postgres connections come back clean; a slow
query stops at ABI_QUERY_TIMEOUT; ABI_MAX_AGENTS chat agents at once, the next question waits its turn."""
import http.client
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import pytest
from conftest import drop, parquet

import abi
import app
from pi_driver import ROOT

P = "pi_pages_test"
OWNER = Path(os.environ.get("ABI_ACCOUNTS", ROOT / "data/accounts.txt")).expanduser()  # the owner's password, as creds.py reads it
PY = ROOT / "engine/backend/.venv/bin/python"
REGIONS = ("North", "South", "East")
ROWS = [(i, REGIONS[i % 3], ("web", "shop")[i % 2], i) for i in range(1, 61)]  # order_id, region, channel, amount


def sure(action: str, **args):
    """An action on what this module made: ABI's confirmation answered here."""
    try:
        return abi.call(action, **args)
    except abi.ActionError as e:
        if e.status != 409 or "confirm_token" not in e.body:  # a 409 refusal (in use, conflict) is no question
            raise
        return abi.call(action, confirm=e.body["confirm_token"], **args)


def engine(code: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Python in the engine's own environment (scripts/engine.sh env), with `env` on top."""
    e = subprocess.run(["sh", "-c", "scripts/engine.sh env"], cwd=ROOT, capture_output=True, text=True).stdout
    return subprocess.run(["sh", "-c", f"{e}\nexec {PY} -c \"$0\" \"$@\"", code, *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, env={**os.environ, **(env or {})})


@pytest.fixture(scope="module")
def people(tmp_path_factory):
    """P with a report by region and by channel, four Viewers (a and a2 see North, b South, c every region; the channel
    is hidden from all of them) and an Editor d, each signed in to an app started here. Gone at the end."""
    if not OWNER.exists():
        pytest.skip("no accounts file")
    d = tmp_path_factory.mktemp("pages")
    (d / "orders.csv").write_text("order_id,region,channel,amount\n" + "".join(f"{i},{r},{c},{a}\n" for i, r, c, a in ROWS))
    if P in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([P])
    abi.load("demo", P, parquet(d / "orders.csv"))
    m = next(x["id"] for x in abi.call("semantic_model", project=P, all=True)["measures"] if x["expression"].upper() == "SUM(ORDERS.AMOUNT)")
    q = lambda dim: {"measures": [m], "dimensions": [dim]}  # noqa: E731
    abi.call("save_report", project=P, report={"id": "split", "name": "Split", "pages": [{"id": "p", "name": "P", "visuals": [
        {"id": "by_region", "type": "bar", "query": q("orders.region")}, {"id": "by_channel", "type": "bar", "query": q("orders.channel")}]}]})
    stamp, made = time.strftime("%H%M%S"), {}
    s = app.serve(P, 0, open_browser=False, signin=True)
    try:
        for who, role in (("a", "viewer"), ("a2", "viewer"), ("b", "viewer"), ("c", "viewer"), ("d", "editor")):
            name, pw = f"pi_pages_{who}_{stamp}", secrets.token_urlsafe(16)
            abi.call("create_user", name=name, password=pw)
            made[who] = (name, pw)
            abi.call("set_role", project=P, user=name, role=role)
        ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}
        uid = {w: ids[n] for w, (n, _) in made.items()}
        values = {"a": ["North"], "a2": ["North"], "b": ["South"], "c": list(REGIONS)}
        sure("set_rls_rule", project=P, id="region", table="orders", column="region", users={uid[w]: v for w, v in values.items()})
        sure("set_column_rule", project=P, id="channel", table="orders", columns=["channel"])
        port = s.server_address[1]
        owner = Browser(port, "admin", OWNER)
        yield {"port": port, "owner": owner, "people": {w: Browser(port, *made[w]) for w in made}, "uid": uid, "measure": m}
    finally:
        s.shutdown()
        for name, _ in made.values():
            sure("delete_user", name=name)
        drop([P])


class Browser:
    """A person signed in to the app: their session cookie, and the page key once a page opened."""

    def __init__(self, port: int, name: str, pw, project: str = P):
        if isinstance(pw, Path):  # the owner: data/accounts.txt, read as scripts/creds.py does (never printed)
            pw = next(x.split()[-1] for x in pw.read_text().splitlines() if re.match(rf"\s*{re.escape(name)}\b", x))
        self.port, self.cookie, self.key, self.project = port, "", "", project
        st, _, hd = self.http("POST", "/signin", urllib.parse.urlencode({"name": name, "password": pw}),
                              {"Content-Type": "application/x-www-form-urlencoded"})
        assert st == 303, f"{name} can't sign in"
        self.cookie = hd["Set-Cookie"].split(";")[0]

    def http(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            c.request(method, path, body, {"Cookie": self.cookie, **(headers or {})})
            r = c.getresponse()
            return r.status, r.read().decode(), r.headers
        finally:
            c.close()

    def page(self) -> dict:
        """The page as this person opens it: its data (window.APP), computed as them."""
        st, b, _ = self.http("GET", f"/?p={self.project}")
        assert st == 200, st
        self.key = self.key or re.search(r"KEY='([^']+)'", b)[1]
        return json.loads(re.search(r"window.APP=(\{.*?\});\s*const tip", b, re.S)[1].replace("<\\/", "</"))

    def post(self, path, body) -> dict:
        return json.loads(self.http("POST", path, json.dumps(body), {"Content-Type": "application/json", "X-Abi-App": self.key,
                                                                    "X-Abi-Client": "pages", "X-Abi-Project": self.project})[1])


def seen(page: dict) -> dict:
    """{visual: {member: figure}} of the split report, or {visual: "error"} where ABI refused it."""
    vs = next(r for r in page["reports"] if r["id"] == "split")["pages"][0]["visuals"]
    return {v["id"]: {r[0]: r[1] for r in v["data"]["rows"]} if "data" in v else "error" for v in vs}


def total(region: str) -> int:
    return sum(a for _, r, _, a in ROWS if r == region)


def test_each_person_sees_their_own_rows_and_columns_from_a_shared_page_after_a_commit(people, monkeypatch):
    """The critical one: after a commit, people with different row rules, one with every region and a hidden column,
    and an Editor open the same report at once. Each sees exactly their own rows and columns. The restricted ones share
    the rest of the page (one computation for four) and the two with the same rule their rows; a rule changed behind
    the app's back shows at once, never a stale entry."""
    owner, them = people["owner"], people["people"]
    owner.page()
    gen = app._GEN.get(P, 0)
    done = owner.post("/api/commit", {"project": P, "message": "pages test: roles and rules"})
    assert "result" in done, done
    for _ in range(300):  # the commit's refresh (changed) moved P's version on
        if app._GEN.get(P, 0) > gen:
            break
        time.sleep(0.1)
    assert app._GEN.get(P, 0) > gen, "the commit made the page data new"
    monkeypatch.setattr(app, "changed", lambda p: None)  # the app's 30 s watch: no refresh in the middle of the count
    pages, rows = [], []
    real_page, real_rows = app.snapshot, app.own
    monkeypatch.setattr(app, "snapshot", lambda dest, p, visuals=True: pages.append(visuals) or real_page(dest, p, visuals))
    monkeypatch.setattr(app, "own", lambda p, base, rules: rows.append(rules) or real_rows(p, base, rules))
    gate, got = threading.Barrier(len(them)), {}

    def one(who):
        gate.wait()
        got[who] = seen(them[who].page())
    ts = [threading.Thread(target=one, args=(w,)) for w in them]
    [t.start() for t in ts]
    [t.join(120) for t in ts]
    everyone = {r: total(r) for r in REGIONS}
    assert got["a"] == got["a2"] == {"by_region": {"North": total("North")}, "by_channel": "error"}, got
    assert got["b"] == {"by_region": {"South": total("South")}, "by_channel": "error"}, got
    assert got["c"] == {"by_region": everyone, "by_channel": "error"}, "every row, but never the hidden column"
    assert got["d"] == {"by_region": everyone, "by_channel": {c: sum(a for _, _, x, a in ROWS if x == c) for c in ("web", "shop")}}, got
    assert sorted(pages) == [False, True], f"one page for the restricted (no rows), one for the Editor: {pages}"
    assert sorted(tuple(r["rules"][0]["values"]) for r in rows) == sorted([("North",), ("South",), REGIONS]), f"a and a2 share: {rows}"
    # a's rule now says South (not through the app, so nothing told it): a's next page is South's, b's entry
    sure("set_rls_rule", project=P, id="region", table="orders", column="region",
         users={people["uid"][w]: v for w, v in {"a": ["South"], "a2": ["North"], "b": ["South"], "c": list(REGIONS)}.items()})
    assert seen(them["a"].page())["by_region"] == {"South": total("South")}
    assert seen(them["a2"].page())["by_region"] == {"North": total("North")}
    assert len(pages) == 2 and len(rows) == 3, "both from entries already computed for the same rows"


def test_one_computation_per_key_at_once_and_none_kept_across_a_change(monkeypatch):
    """Single flight: people alike wait for one computation, other keys compute at the same time (up to 4); the
    restricted share the page and wait for their rows only with people of the same rules; an error reaches every waiter
    and leaves nothing behind; data computed while the person's rules changed is theirs alone."""
    monkeypatch.setattr(app, "_SNAP", {})
    monkeypatch.setattr(app, "_FLIGHT", {})
    monkeypatch.setattr(app.abi, "folder", lambda p: Path("."))
    me = threading.local()
    seen_by = {"a": ("viewer", {"restricted": True, "values": ["a"]}), "b": ("viewer", {"restricted": True, "values": ["b"]}),
               "c": ("editor", None), "d": ("owner", None)}  # a1, a2 and a3 have the same rules
    monkeypatch.setattr(app, "seen_as", lambda p: seen_by[me.who[0]])
    calls, fail = [], []

    def slow(kind, out):
        calls.append(kind)
        time.sleep(0.3)
        if fail:
            raise OSError("ABI went away")
        return out | {"n": len(calls)}
    monkeypatch.setattr(app, "snapshot", lambda dest, p, visuals=True: slow("page" if visuals else "base", {}))
    monkeypatch.setattr(app, "own", lambda p, base, rules: slow("rows", {"rules": rules}))

    def burst(whos):
        gate, out = threading.Barrier(len(whos)), {}

        def one(w):
            me.who = w
            gate.wait()
            try:
                out[w] = app.cached("x", w)
            except OSError as e:
                out[w] = e
        ts = [threading.Thread(target=one, args=(w,)) for w in whos]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        return out
    t0 = time.perf_counter()
    out = burst(["a1", "a2", "a3", "b1", "c1", "d1"])
    took = time.perf_counter() - t0
    assert took < 1.1 and sorted(calls) == ["base", "page", "page", "rows", "rows"], (took, calls)  # in turn: 1.5 s
    assert out["a1"] is out["a2"] is out["a3"] and out["a1"]["rules"]["values"] == ["a"] and out["b1"]["rules"]["values"] == ["b"]
    fail.append(1)
    seen_by["e"] = ("viewer", None)
    out = burst(["e1", "e2"])
    assert len(calls) == 6 and all(isinstance(v, OSError) for v in out.values()) and not app._FLIGHT, out
    fail.clear()
    me.who = "e1"
    assert app.cached("x", "e1") == {"n": 7}, "an error is never kept"
    seq = iter([("viewer", None), ("editor", None)])  # the person's role before and after the computation differ
    monkeypatch.setattr(app, "seen_as", lambda p: next(seq))
    monkeypatch.setattr(app, "_GEN", {"x": 5})
    assert app.cached("x", "f1") == {"n": 8} and ("x", 5, "viewer", None) not in app._SNAP, "computed across a change: not kept"


def test_a_change_to_one_project_keeps_the_others_pages(monkeypatch):
    monkeypatch.setattr(app.abi, "sync", lambda p: {})
    monkeypatch.setattr(app, "_SNAP", {("a", 0, "owner", None): {}, ("a_pub", 0, "viewer", "r"): {}, ("b", 0, "owner", None): {}})
    monkeypatch.setattr(app, "_GEN", {})
    app.changed("a")
    for _ in range(50):
        if app._GEN:
            break
        time.sleep(0.1)
    with app._SNAP_LOCK:  # the refresh moves the versions and drops the entries in one step
        assert app._GEN == {"a": 1, "a_pub": 1} and set(app._SNAP) == {("b", 0, "owner", None)}


def test_a_pooled_connection_comes_back_clean_after_a_non_owners_action(people):
    """One pooled connection (ABI_PG_POOL=1), used by a borrower that leaves a role, a search_path, a setting for the
    person and a temporary table behind, then by a Viewer's action: that action and the next borrower get the same
    connection with no transaction open, the default role and nothing of the earlier context."""
    a = people["uid"]["a2"]
    r = engine("""
import json, sys
import abi.app  # every action registered
from abi import actions, db, security
db._pooled().wait(10)  # its one connection open (before, a first call takes a connection of its own)
with db.connect() as c:
    pid = c.info.backend_pid
    c.execute("SET ROLE pg_read_all_data")
    c.execute("SET search_path = pg_catalog")
    c.execute("SELECT set_config('abi.person', 'someone', false)")
    c.execute("CREATE TEMP TABLE theirs (x int)")
viewer = security.User(int(sys.argv[1]), 'a2', False)
seen = actions.run('rls_rules', {'project': sys.argv[2]}, viewer, 'rest')
with db.connect() as c:
    print(json.dumps({'same': c.info.backend_pid == pid, 'tx': c.info.transaction_status.name, 'seen': seen,
                      **c.execute("SELECT current_user = session_user AS own, current_setting('role') AS role,"
                                  " current_setting('search_path') AS path, current_setting('abi.person', true) AS person,"
                                  " to_regclass('pg_temp.theirs') IS NULL AS no_temp").fetchone()}))
""", str(a), P, env={"ABI_PG_POOL": "1"})
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["same"] and out["tx"] == "IDLE" and out["own"] and out["role"] == "none", out
    assert out["path"] == '"$user", public' and not out["person"] and out["no_temp"], out
    assert out["seen"]["restricted"] and out["seen"]["rules"] == [{"table": "orders", "column": "region", "values": ["North"]}], out


def test_a_restricted_persons_reads_at_once_build_their_views_once(people):
    """A page asks a restricted Viewer's visuals at once: their row-level security views are built once for them, the
    other reads wait for it (each made its own: six cold visuals at once took 1.96 s, one after another 1.10 s), and
    every read gets their rows."""
    r = engine("""
import json, sys, threading
import abi.app  # every action registered
from abi import semantic
from abi.semantic import rls
built, real = [], rls.conditions
rls.conditions = lambda *a: built.append(1) or real(*a)
q, out, gate = {"measures": [sys.argv[2]], "dimensions": ["orders.region"]}, [], threading.Barrier(6)
def one():
    gate.wait()
    out.append(semantic.query(sys.argv[1], q, int(sys.argv[3]))["rows"])
ts = [threading.Thread(target=one) for _ in range(6)]
[t.start() for t in ts]
[t.join() for t in ts]
print(json.dumps({"built": len(built), "rows": out}))
""", P, people["measure"], str(people["uid"]["a2"]))
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["built"] == 1 and out["rows"] == [[["North", total("North")]]] * 6, out


def test_a_slow_query_is_stopped_at_the_timeout(people):
    """ABI_QUERY_TIMEOUT: semantic_query and a page's visual_data stop a read still running then (DuckDB's interrupt)
    and answer a plain 400, not a thread held for good."""
    r = engine("""
import json, sys, time
import abi.app  # every action registered
from abi import actions, db, security, semantic
with db.connect() as c:
    owner = security.User(**c.execute("SELECT id, name, workspace_owner FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone())
slow = "SELECT count(*) FROM range(1000000000000) t(i) WHERE i % 7 = 3"
semantic.plan = lambda project, q, *a, **k: (q, slow, ["n"], [], {})  # a read that would run for minutes
out = [semantic.TIMEOUT]
for name, args in (('semantic_query', {'measures': [sys.argv[2]]}), ('visual_data', {'query': {'measures': [sys.argv[2]]}})):
    t = time.perf_counter()
    try:
        actions.run(name, {'project': sys.argv[1], **args}, owner, 'rest')
        out.append(None)
    except actions.ActionFailed as e:
        out.append([e.status, str(e), round(time.perf_counter() - t, 1)])
print(json.dumps(out))
""", P, people["measure"], env={"ABI_QUERY_TIMEOUT": "1"})
    assert r.returncode == 0, r.stderr[-3000:]
    limit, *runs = json.loads(r.stdout.strip().splitlines()[-1])
    assert limit == 1.0
    for status, message, seconds in runs:
        assert status == 400 and "longer than the 1 s limit" in message and seconds < 10, runs


STANDIN = """#!/usr/bin/env python3
import json, os, sys, time
d, me = os.environ["PI_STANDIN"], os.environ["ABI_APP_CLIENT"].replace("/", "_")
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") in ("prompt", "follow_up"):
        open(os.path.join(d, me + ".asked"), "a").write(msg["message"] + "\\n")
        while not os.path.exists(os.path.join(d, me + ".done")):
            time.sleep(0.05)
        os.remove(os.path.join(d, me + ".done"))
        out = {"role": "assistant", "content": [{"type": "text", "text": "Done."}], "provider": "standin", "model": "pi"}
        print(json.dumps({"type": "message_end", "message": out}), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
"""


def standins(tmp_path, monkeypatch) -> None:
    """Chats whose agent is STANDIN, eight at most at once."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/pi").write_text(STANDIN)
    (tmp_path / "bin/pi").chmod(0o755)
    (tmp_path / "ws/demo/proj/.git").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PI_STANDIN", str(tmp_path))
    monkeypatch.setenv("ABI_PI_WORKSPACES", str(tmp_path / "ws"))  # Settings: none
    monkeypatch.setenv("ABI_PI_LLM_MODEL", "standin")  # 1.2: ABI names no model, so the deployment's starts the agents
    monkeypatch.setattr(app.abi, "WORKSPACES", tmp_path / "ws")
    monkeypatch.setattr(app.abi, "ROOT", tmp_path)  # Pi's log and the usage lines go here
    monkeypatch.setattr(app, "HUBS", {})
    monkeypatch.setattr(app, "WAITING", [])
    monkeypatch.setattr(app, "MAX_AGENTS", 8)


def hubs_of(users: list[str], notes: dict) -> list:
    hubs = [app.Hub("proj", "http://127.0.0.1:9", "k", f"c{i}", user=u, token="t") for i, u in enumerate(users)]
    for h in hubs:
        app.HUBS[h.hid] = h
        notes[h.hid] = []
        h.emit = lambda e, here=True, h=h: notes[h.hid].append(e)
    return hubs


def until(cond, what):
    end = time.time() + 20
    while not cond():
        assert time.time() < end, what
        time.sleep(0.05)


def test_the_ninth_chat_waits_and_starts_when_one_ends(tmp_path, monkeypatch):
    """ABI_MAX_AGENTS (8): eight chats' agents run; the ninth question waits with a note in its chat and starts when an
    answer ends, in the slot of the agent idle longest; Stop takes a waiting question back."""
    standins(tmp_path, monkeypatch)
    notes: dict = {}
    hubs = hubs_of([f"user{i}" for i in range(10)], notes)
    running = lambda: [h for h in hubs if h.pi and h.pi.poll() is None]  # noqa: E731
    asked = lambda h: (tmp_path / f"{h.hid.replace('/', '_')}.asked")  # noqa: E731
    try:
        for i, h in enumerate(hubs[:9]):
            h.chat(f"question {i}", "home")
        until(lambda: all(asked(h).exists() for h in hubs[:8]), "eight agents got their questions")
        assert len(running()) == 8 and hubs[8].pi is None and [w[0] for w in app.WAITING] == [hubs[8]]
        assert any(e["type"] == "note" and e.get("chat") and "agents are answering" in e["text"] for e in notes[hubs[8].hid])
        (tmp_path / f"{hubs[3].hid.replace('/', '_')}.done").touch()  # chat 3's answer ends
        until(lambda: asked(hubs[8]).exists(), "the ninth question starts once an answer ended")
        assert asked(hubs[8]).read_text().endswith("question 8\n") and hubs[3].pi is None and len(running()) == 8
        assert any(e["type"] == "answer" and e["text"] == "Done." for e in notes[hubs[3].hid]), "chat 3 got its answer first"
        hubs[9].chat("question 9", "home")  # every agent busy again: it waits, and Stop takes it back
        assert [w[0] for w in app.WAITING] == [hubs[9]] and hubs[9].unwait() and not app.WAITING
    finally:
        for h in hubs:
            if h.pi:
                h.pi.kill()


def test_one_person_runs_half_the_agents_however_many_tabs_they_open(tmp_path, monkeypatch):
    """M3 review: a hub is a person and a browser's own ID, so one person with 8 tabs held all 8 agents and everyone
    else waited. A person runs at most half (4 of 8): their fifth question waits though slots are free, someone else's
    starts at once, and when one of theirs answers, their waiting question takes that agent's slot."""
    standins(tmp_path, monkeypatch)
    notes: dict = {}
    hubs = hubs_of(["ann"] * 5 + ["bob"], notes)
    asked = lambda h: (tmp_path / f"{h.hid.replace('/', '_')}.asked")  # noqa: E731
    try:
        for i, h in enumerate(hubs[:5]):
            h.chat(f"question {i}", "home")
        until(lambda: all(asked(h).exists() for h in hubs[:4]), "four of ann's agents got their questions")
        assert hubs[4].pi is None and [w[0] for w in app.WAITING] == [hubs[4]]
        hubs[5].chat("bob's question", "home")
        until(lambda: asked(hubs[5]).exists(), "bob's starts at once")
        (tmp_path / f"{hubs[1].hid.replace('/', '_')}.done").touch()  # one of ann's answers ends
        until(lambda: asked(hubs[4]).exists(), "ann's fifth starts in her own agent's slot")
        assert hubs[1].pi is None and not app.WAITING
    finally:
        for h in hubs:
            if h.pi:
                h.pi.kill()


def test_a_page_has_no_raw_security_rules(tmp_path, monkeypatch):
    """M3 review: every person's page data had sections.Security, the raw security/rls.json naming every person's values
    (a restricted Viewer saw who else sees what), shared by all restricted people. The Security page draws ABI's
    rls_rules, answered as the person, and nothing else."""
    (tmp_path / "security").mkdir()
    (tmp_path / "security/rls.json").write_text(json.dumps({"rules": [{"table": "cafes", "column": "region",
                                                                        "users": {"7": ["North"], "9": ["South"]}}]}))
    monkeypatch.setattr(app.abi, "folder", lambda p: tmp_path / "ws" / p)
    out = app._snapshot(tmp_path, "proj", None, lambda a: None, None)
    assert "Security" not in out["sections"] and "South" not in json.dumps(out), out["sections"].keys()


# ---------- 1.2.6 M7: object pages, the object-type graph and a report's form card, in the engine's Chromium ----------

PO = "pi_pages_objs"
VISIT = {"id": "cafe_visit", "name": "Café visit", "enabled": False, "trigger": {"type": "manual"}, "steps": [
    {"id": "look", "type": "action", "action": "get_object", "input": {"type": "cafe", "key": "{{ data.trigger.key }}"}}]}
CARD = {"id": "flag", "type": "form", "title": "Flag a café for a visit", "form": {"action": "flag_visit"}, "layout": {"x": 6, "y": 7, "w": 6, "h": 6}}
REPORT = {"id": "cafes", "name": "Cafés", "pages": [{"id": "main", "name": "Main", "visuals": [
    {"id": "flagged", "type": "kpi", "title": "Flagged cafés", "query": {"measures": ["flagged"]}, "layout": {"x": 0, "y": 0, "w": 4, "h": 2}},
    {"id": "by_name", "type": "bar", "title": "Revenue by café", "layout": {"x": 0, "y": 2, "w": 6, "h": 5},
     "query": {"measures": ["revenue"], "dimensions": ["cafes.cafe_name"], "order_by": [{"field": "revenue", "desc": True}], "limit": 8}},
    {"id": "by_key", "type": "table", "title": "Orders by café", "layout": {"x": 6, "y": 2, "w": 6, "h": 5},
     "query": {"measures": ["orders_rows"], "dimensions": ["cafes.cafe_id"], "order_by": [{"field": "cafes.cafe_id"}], "limit": 8}},
    {"id": "by_region", "type": "bar", "title": "Revenue by region", "query": {"measures": ["revenue"], "dimensions": ["cafes.region"]},
     "layout": {"x": 0, "y": 7, "w": 6, "h": 4}}, CARD]}]}
IDS = re.compile(r"ontology|object type|many-to-many|\blink\b|cafe_id|cafe_name|cafe_contacts|opened_on|visit_reason|flag_visit|order_cafe", re.I)


def ontology(bad_format: str) -> dict:
    """Cafés (with a manager and a visit reason kept in an input table), customers and orders; three action types on
    cafés: one confirmed, with a check and a flow; one plain; one for Editors."""
    reason = {"type": "object", "required": ["reason"], "additionalProperties": False, "properties": {"reason": {
        "type": "string", "minLength": 1, "maxLength": 100, "title": "Reason", "description": "Why this café needs a visit."}}}
    return {
        "objects": [
            {"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name", "description": "One Brewline shop.",
             "properties": [{"column": "city", "meaning": "city"}, {"column": "region", "meaning": "category"},
                            {"column": "opened_on", "name": "Opened", "meaning": "date"}, {"column": "seats", "meaning": "quantity"},
                            {"column": "format", "meaning": "category"}, {"column": "cafe_contacts.manager", "name": "Manager", "meaning": "person"},
                            {"column": "cafe_contacts.visit_reason", "name": "Visit reason", "meaning": "text"}],
             "measures": ["revenue", "orders_rows"]},
            {"id": "customer", "table": "customers", "name": "Customer", "plural": "Customers", "title": "customer_id",
             "properties": [{"column": "segment", "meaning": "category"}, {"column": "home_country", "name": "Home country", "meaning": "country"}],
             "measures": ["revenue"]},
            {"id": "order", "table": "orders", "name": "Order", "plural": "Orders", "title": "order_id",
             "properties": [{"column": "revenue", "meaning": "money"}, {"column": "quantity", "meaning": "quantity"}]}],
        "links": [
            {"id": "order_cafe", "from": "order", "to": "cafe", "name": "café", "inverse": "orders", "via": ["orders-cafe_id"]},
            {"id": "order_customer", "from": "order", "to": "customer", "name": "customer", "inverse": "orders", "via": ["orders-customer_id"]},
            {"id": "visited", "from": "customer", "to": "cafe", "name": "visited", "inverse": "customers who visited",
             "via": ["orders-customer_id", "orders-cafe_id"]}],
        "actions": [
            {"id": "flag_visit", "object": "cafe", "name": "Flag for a visit", "description": "Asks the regional team to visit this café.",
             "role": "viewer", "confirm": True, "inputs": reason,
             "checks": [{"value": "{{ data.object.format }}", "op": "!=", "to": bad_format, "message": "These cafés are visited by another team."}],
             "writes": {"table": "cafe_contacts", "set": {"visit_reason": "{{ data.inputs.reason }}"}}, "flow": "cafe_visit"},
            {"id": "note", "object": "cafe", "name": "Note the manager", "role": "viewer",
             "inputs": {"type": "object", "required": ["who"], "properties": {"who": {"type": "string", "maxLength": 100}}},
             "writes": {"table": "cafe_contacts", "set": {"manager": "{{ data.inputs.who }}"}}},
            {"id": "close", "object": "cafe", "name": "Close the visit", "writes": {"table": "cafe_contacts", "set": {"visit_reason": "closed"}}}]}


@pytest.fixture(scope="module")
def objs(tmp_path_factory):
    """PO: Brewline with the ontology above, a North-region Viewer who never sees cafés' seats and an Editor, each signed
    in to an app started here (no published copy: Viewer reads the project), and the report REPORT. `good` is a North
    café with a contact, `fresh` and `other` two without, `bad` one the check stops, `south` one the Viewer can't see."""
    if not OWNER.exists():
        pytest.skip("no accounts file")
    from test_ontology_nobreak import brew  # not at top: it imports this module
    brew(PO, tmp_path_factory.mktemp("objs"))
    stamp, made = time.strftime("%H%M%S"), {}
    s = app.serve(PO, 0, open_browser=False, signin=True)
    try:
        for who in ("viewer", "editor"):
            made[who] = (f"pi_objs_{who}_{stamp}", secrets.token_urlsafe(16))
            abi.call("create_user", name=made[who][0], password=made[who][1])
            abi.call("set_role", project=PO, user=made[who][0], role=who)
        ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=PO)["accounts"]}
        sure("set_rls_rule", project=PO, id="region", table="cafes", column="region", users={ids[made["viewer"][0]]: ["North"]})
        sure("set_column_rule", project=PO, id="seats", table="cafes", columns=["seats"])
        sql = lambda q: abi.call("sql_query", project=PO, sql=q)["rows"]  # noqa: E731
        [(bad, fmt)] = sql("SELECT cafe_id, format FROM cafes WHERE region = 'North' ORDER BY cafe_id LIMIT 1")
        good, fresh, other = [x[0] for x in sql(f"SELECT cafe_id FROM cafes WHERE region = 'North' AND format <> '{fmt}' ORDER BY cafe_id LIMIT 3")]
        south = sql("SELECT min(cafe_id) FROM cafes WHERE region = 'South'")[0][0]
        sure("create_input_table", project=PO, table="cafe_contacts", key=["cafe_id"],
             columns=[{"name": "cafe_id", "type": "VARCHAR"}, {"name": "manager", "type": "VARCHAR"}, {"name": "visit_reason", "type": "VARCHAR"}])
        abi.call("add_relationship", project=PO, **{"from": "cafe_contacts.cafe_id", "to": "cafes.cafe_id"})
        abi.call("edit_cells", project=PO, table="cafe_contacts", add=[{"cafe_id": good, "manager": "Ada Example", "visit_reason": "first"}])
        abi.call("set_measure", project=PO, id="flagged", name="Flagged cafés", expression="COUNT(cafe_contacts.visit_reason)")
        sure("save_flow", project=PO, flow=VISIT)
        sure("save_ontology", project=PO, ontology=ontology(fmt))
        abi.call("save_report", project=PO, report=REPORT)
        port = s.server_address[1]
        yield {"port": port, "owner": Browser(port, "admin", OWNER, PO), **{w: Browser(port, *made[w], PO) for w in made},
               "editor_name": made["editor"][0], "good": good, "fresh": fresh, "other": other, "bad": bad, "south": south, "sql": sql}
    finally:
        s.shutdown()
        for name, _ in made.values():
            sure("delete_user", name=name)
        drop([PO])


DRIVE = r"""
import json, sys
from playwright.sync_api import sync_playwright
a, out, errors = json.load(open(sys.argv[1])), {}, []
with sync_playwright() as p:
    b, phone = p.chromium.launch(args=["--disable-dev-shm-usage"]), a["width"] < 600
    ctx = b.new_context(locale="en-US", viewport={"width": a["width"], "height": 844 if phone else 900}, is_mobile=phone, has_touch=phone)
    ctx.add_cookies([{"name": a["cookie"].split("=", 1)[0], "value": a["cookie"].split("=", 1)[1], "url": a["url"]}])
    pg = ctx.new_page(); pg.set_default_timeout(30000)
    pg.on("pageerror", lambda e: errors.append(str(e)[:300]))
    pg.goto(a["url"]); pg.wait_for_selector("aside button.it", state="attached")
    for op, *x in a["steps"]:
        try:
            if op == "show": pg.evaluate("k => show(k)", x[0])
            elif op == "click": pg.locator(x[0]).first.click()
            elif op == "fire": pg.locator(x[0]).first.dispatch_event("click")  # a chart's mark, whatever is drawn over it
            elif op == "fill": pg.fill(x[0], x[1])
            elif op == "wait": pg.wait_for_selector(x[0], state=x[1] if len(x) > 1 else "visible")
            elif op == "until": pg.wait_for_function(x[0])
            elif op == "text": out[x[0]] = pg.locator(x[1]).first.inner_text()
            elif op == "all": out[x[0]] = pg.locator(x[1]).all_inner_texts()
            elif op == "get": out[x[0]] = pg.evaluate(x[1])
            elif op == "shot": pg.screenshot(path=x[0], full_page=len(x) < 2)
        except Exception as e:  # which step, and what the page showed then
            raise SystemExit(f"step {[op, *x]}: {str(e)[:400]}\nerrors {errors}\nmain: {pg.inner_text('main')[:1500]}")
    b.close()
print(json.dumps({"errors": errors, **out}))
"""


def drive(who: Browser, tmp_path, steps: list, width: int = 1440) -> dict:
    """The app as this signed-in person in headless Chromium (their session cookie), these steps run in order: what
    they read off the page, and the page's script errors (none allowed)."""
    from test_app import _chromium  # not at top: test_app starts its own app
    f = tmp_path / f"steps-{time.monotonic_ns()}.json"
    f.write_text(json.dumps({"url": f"http://127.0.0.1:{who.port}/?p={who.project}", "cookie": who.cookie, "width": width, "steps": steps}))
    out = _chromium(DRIVE, str(f))
    assert out.pop("errors") == [], "a script error on the page"
    return out


def okey(type_: str, key: str) -> str:
    """An object page's key in the app, as its own okey() makes it."""
    return f"obj:{type_}:" + urllib.parse.quote(json.dumps([key]), safe="")


KV = "!!document.querySelector('main .kv')"
VISIT_REASON = "api('get_object',{type:'cafe',key:[%s]}).then(o=>o.rows[0][o.columns.indexOf('cafe_contacts.visit_reason')])"


def test_object_page_renders(objs, tmp_path):
    """Data Model > Objects: a type's list (title, named properties, the type's measures; search, category filters,
    pages); a row opens the object's page: the title, its properties, its measures filtered to it, a section per
    connection whose neighbours open their own pages, and its history. Business names only, never an id; at a phone's
    width nothing overflows."""
    good = objs["good"]
    o = abi.call("get_object", project=PO, type="cafe", key=[good])
    was = dict(zip(o["columns"], o["rows"][0]))
    names = {m["id"]: m["name"] for m in abi.call("semantic_model", project=PO, all=True)["measures"]}
    out = drive(objs["owner"], tmp_path, [
        ["get", "nav", "[...document.querySelectorAll('aside button.it')].map(b => b.dataset.k)"],
        ["show", "obj:cafe"], ["wait", "main .pick tr[data-r]"], ["all", "heads", "main .pick th"],
        ["get", "tips", "[...document.querySelectorAll('main .pick th')].map(t => t.title).join('')"],
        ["all", "chips", ".pghead .seg .chip"], ["all", "pager", "main .pager button"],
        ["until", "[...document.querySelectorAll('main .obar select')].every(s => s.options.length > 1)"],
        ["get", "facets", "[...document.querySelectorAll('main .obar select')].map(s => [s.getAttribute('aria-label'), [...s.options].map(o => o.text)])"],
        ["get", "rows", "document.querySelectorAll('main .pick tr[data-r]').length"],
        ["click", "main .pager button:last-child"], ["until", "document.querySelector('main .pager .file')?.textContent === '51 to 100'"],
        ["fill", "main .obar input", was["cafe_name"]], ["until", "document.querySelectorAll('main .pick tr[data-r]').length === 1"],
        ["text", "row", "main .pick tr[data-r]"], ["click", "main .pick tr[data-r]"], ["until", KV],
        ["until", "document.querySelectorAll('main .cards > .card.wide .pick table').length === 2"], ["wait", "main .cards > .card:last-child :is(table, p.empty)"],
        ["text", "h1", "main .pghead h1"], ["text", "eyebrow", "main .pghead .eyebrow"], ["get", "cur", "CUR"],
        ["all", "cards", "main .cards > .card > h3"], ["text", "kv", "main .kv"], ["all", "kpis", "main .kpis .kpi"], ["text", "main", "main"],
        ["get", "wide", "[...document.querySelectorAll('main .cards > .card.wide')].map(c => [c.querySelector('h3').innerText, c.querySelectorAll('tr[data-r]').length])"],
        ["click", "main .cards > .card.wide tr[data-r]"], ["until", KV + " && document.querySelector('main .pghead .eyebrow').textContent !== 'Café'"],
        ["text", "eyebrow2", "main .pghead .eyebrow"], ["text", "main2", "main"],
        ["get", "total", "plainErr(\"Measure 'revenue' follows the many-to-many link 'visited', and totals across it are refused: its members overlap, so they don't add up; ask without totals.\")"],
        ["get", "stale", "plainErr(\"No action type 'flag_visit' on object type 'cafe' in project 'pi_pages_objs': `ontology` lists them.\")"]])
    assert "objects" in out["nav"] and out["nav"].index("objects") == out["nav"].index("model") + 1, "Objects sits in Data Model"
    assert out["chips"] == ["Overview", "Cafés", "Customers", "Orders"] and out["rows"] == 50 and out["pager"] == ["Previous", "Next"], out
    assert out["heads"] == ["Cafe name", "City", "Region", "Opened", "Seats", "Format", names["revenue"], names["orders_rows"]], out["heads"]
    assert out["tips"] == "", "a header's tooltip never shows a column id"
    assert [f[0] for f in out["facets"]] == ["Region", "Format"] and {"North", "South"} < set(out["facets"][0][1]), out["facets"]
    assert out["row"].split("\t")[:3] == [was["cafe_name"], was["city"], was["region"]], out["row"]
    assert out["h1"] == was["cafe_name"] and out["eyebrow"].lower() == "café" and out["cur"] == okey("cafe", good), out
    assert [c.split("\n")[0] for c in out["cards"]] == ["Properties", "Measures", "Orders", "Customers who visited", "History"], out["cards"]
    for label, value in (("City", was["city"]), ("Region", was["region"]), ("Manager", "Ada Example"), ("Visit reason", "first"), ("Seats", str(was["seats"]))):
        assert f"{label}\n{value}" in out["kv"], (label, out["kv"])
    assert [k.split("\n")[-1] for k in out["kpis"]] == [names["revenue"], names["orders_rows"]], out["kpis"]
    assert all(n > 0 for _, n in out["wide"][:2]), f"each connection lists its neighbours: {out['wide']}"
    assert out["eyebrow2"].lower() == "order", "a neighbour opens its own page"
    for text in (out["main"], out["main2"], out["total"], out["stale"]):
        assert not IDS.search(text), f"an id or a model word on an object page: {IDS.search(text)[0]}"
    assert "shown per member instead" in out["total"] and names["revenue"] in out["total"] and "pi_pages_objs" not in out["stale"], out["total"]
    # a phone (always Viewer): the list, an object's page and the overview as a list, nothing wider than the screen
    wide = "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    phone = drive(objs["owner"], tmp_path, [
        ["show", "obj:cafe"], ["wait", "main .pick tr[data-r]"], ["get", "list", wide],
        ["show", okey("cafe", good)], ["until", KV], ["wait", "main .cards > .card.wide table"], ["wait", "main .cards > .card:last-child .item"],
        ["get", "page", wide], ["text", "h1", "main .pghead h1"],
        ["get", "acts", "document.querySelectorAll('main .pghead .bar.top .btn, main .aform').length"],
        ["show", "objects"], ["wait", "main .mlist2 .card"], ["get", "home", wide], ["all", "types", "main .mlist2 .card h3"]], width=390)
    assert phone["list"] <= 1 and phone["page"] <= 1 and phone["home"] <= 1 and phone["h1"] == was["cafe_name"] and phone["acts"] == 0, phone
    assert [t.split("\n")[0] for t in phone["types"]] == ["Cafés", "Customers", "Orders"], phone["types"]


def test_viewer_sees_only_visible(objs, tmp_path):
    """Every screen reads as the signed-in person: a restricted Viewer lists only their rows, never a hidden column; an
    object hidden from them reads exactly like one that doesn't exist; Viewer mode shows no action and no form, and the
    server refuses a run."""
    v, good, south = objs["viewer"], objs["good"], objs["south"]
    north = {r[0] for r in objs["sql"]("SELECT cafe_id FROM cafes WHERE region = 'North'")}
    cafe = next(t for t in v.page()["ontology"]["objects"] if t["id"] == "cafe")
    assert "seats" not in [p["column"] for p in cafe["properties"]], "the hidden column isn't even named"
    got = v.post("/api/list_objects", {"project": PO, "type": "cafe", "limit": 1000})["result"]
    assert {r[got["columns"].index("cafe_id")] for r in got["rows"]} == north and "seats" not in got["columns"], got["columns"]
    owner = objs["owner"]
    owner.page()
    assert len(owner.post("/api/list_objects", {"project": PO, "type": "cafe", "limit": 1000})["result"]["rows"]) > len(north)
    hidden = v.post("/api/get_object", {"project": PO, "type": "cafe", "key": [south]})
    missing = v.post("/api/get_object", {"project": PO, "type": "cafe", "key": ["CF-NONE"]})
    assert "result" not in hidden and hidden["error"].replace(south, "CF-NONE") == missing["error"], (hidden, missing)
    ran = v.post("/api/run_object_action", {"project": PO, "type": "cafe", "key": [good], "action": "note", "inputs": {"who": "Nobody"}})
    assert "Viewer mode is read-only" in ran["error"] and objs["sql"](f"SELECT manager FROM cafe_contacts WHERE cafe_id = '{good}'") == [["Ada Example"]], ran
    out = drive(v, tmp_path, [
        ["show", "obj:cafe"], ["wait", "main .pick tr[data-r]"], ["all", "heads", "main .pick th"],
        ["get", "regions", "[...new Set([...document.querySelectorAll('main .pick tr[data-r]')].map(tr => tr.cells[2].textContent))]"],
        ["get", "rows", "document.querySelectorAll('main .pick tr[data-r]').length"],
        ["show", okey("cafe", south)], ["wait", "main .emptyx"], ["text", "hidden", "main .emptyx"],
        ["get", "acts", "document.querySelectorAll('main .pghead .bar.top .btn').length"],
        ["show", okey("cafe", "CF-NONE")], ["wait", "main .emptyx"], ["text", "missing", "main .emptyx"],
        ["show", okey("cafe", good)], ["until", KV], ["text", "kv", "main .kv"], ["text", "h1", "main .pghead h1"],
        ["get", "acts2", "document.querySelectorAll('main .pghead .bar.top .btn, main .aform').length"],
        ["show", "rep:cafes"], ["wait", "main .grid .card"], ["get", "forms", "document.querySelectorAll('main .aform').length"]])
    assert "Seats" not in out["heads"] and out["regions"] == ["North"] and out["rows"] == min(50, len(north)), out
    assert out["hidden"] == out["missing"] and "isn't available" in out["hidden"] and "Open cafés" in out["hidden"], out["hidden"]
    assert "Seats" not in out["kv"] and "Ada Example" in out["kv"] and out["acts"] == out["acts2"] == out["forms"] == 0, out


def test_action_form_confirms(objs, tmp_path):
    """An object's actions are buttons (one main action, the others under More). A button opens a form of exactly the
    action's inputs, the input's description as its hint. An empty required field stops in the form; a filled form asks
    ABI, whose confirmation is the person's click: nothing is written before the yes, nothing after a no; after the yes
    the page shows the new value and the run in the history. A check that doesn't hold shows its message in the form.
    The page never holds the confirm token."""
    e, fresh, bad = objs["editor"], objs["fresh"], objs["bad"]
    reason = VISIT_REASON % json.dumps(fresh)
    out = drive(e, tmp_path, [
        ["show", okey("cafe", fresh)], ["until", KV], ["all", "main_action", "main .pghead .bar.top > .btn"],
        ["get", "more", "[...document.querySelectorAll('main .pghead .bar.top .more .menu button')].map(b => b.textContent)"],
        ["click", "main .pghead .bar.top > .btn"], ["wait", "main .aform"], ["all", "labels", "main .aform .fld > label"],
        ["all", "hints", "main .aform .fld > .file"], ["get", "fields", "document.querySelectorAll('main .aform :is(input, select, textarea)').length"],
        ["get", "primary", "document.querySelectorAll('main .btn.pri').length"],
        ["click", "main .aform .acts .btn.pri"], ["wait", "main .aform .msg.bad"], ["text", "empty", "main .aform .msg"],
        ["get", "asked0", "document.querySelector('#confirm').open"],
        ["fill", "main .aform .fld input", "needs a look"], ["click", "main .aform .acts .btn.pri"], ["wait", "#confirm[open]"],
        ["text", "title", "#confirm h3"], ["text", "lead", "#c-msg"], ["text", "preview", "#c-prev"], ["text", "yes", "#c-yes"],
        ["get", "raw", "document.querySelector('#c-abi').hidden"], ["get", "before", reason],
        ["click", "#c-no"], ["wait", "main .aform .msg.bad"], ["text", "declined", "main .aform .msg"], ["get", "after_no", reason],
        ["click", "main .aform .acts .btn.pri"], ["wait", "#confirm[open]"], ["click", "#c-yes"], ["wait", "body > .toast"], ["text", "toast", "body > .toast"],
        ["until", "[...document.querySelectorAll('main .kv span')].some(s => s.textContent === 'needs a look')"], ["get", "after_yes", reason],
        ["wait", "main .cards > .card:last-child table"], ["text", "history", "main .cards > .card:last-child"],
        ["show", okey("cafe", bad)], ["until", KV], ["click", "main .pghead .bar.top > .btn"], ["fill", "main .aform .fld input", "anything"],
        ["click", "main .aform .acts .btn.pri"], ["wait", "main .aform .msg.bad"], ["text", "check", "main .aform .msg"],
        ["get", "asked1", "document.querySelector('#confirm').open"]])
    assert out["main_action"] == ["Flag for a visit"] and out["more"] == ["Note the manager", "Close the visit"], out
    assert out["labels"] == ["Reason"] and out["hints"] == ["Why this café needs a visit."] and out["fields"] == 1 and out["primary"] == 1, out
    assert out["empty"].startswith("Reason:") and out["asked0"] is False, "an empty required field stops in the form, with no request"
    name = objs["sql"](f"SELECT cafe_name FROM cafes WHERE cafe_id = '{fresh}'")[0][0]
    assert out["title"] == "Flag for a visit?" and out["yes"] == "Flag for a visit" and name in out["lead"] and out["raw"] is True, out
    assert "Reason: needs a look" in out["preview"] and "Visit reason: (empty) → needs a look" in out["preview"] and "Café visit" in out["preview"], out["preview"]
    assert not IDS.search(out["title"] + out["lead"] + out["preview"]), "the confirmation is in the person's words"
    assert out["before"] is None and out["after_no"] is None and "declined" in out["declined"], "nothing runs before the yes, or after a no"
    assert out["after_yes"] == "needs a look" and "Saved" in out["toast"] and "Café visit" in out["toast"], out
    assert "Flag for a visit" in out["history"] and objs["editor_name"] in out["history"] and "Visit reason changed" in out["history"], out["history"]
    assert out["check"] == "These cafés are visited by another team." and out["asked1"] is False, out
    assert objs["sql"](f"SELECT count(*) FROM cafe_contacts WHERE cafe_id = '{bad}'") == [[0]]
    # the app's own answer to the form's request: ABI's question without its token; unanswered, nothing is written
    e.page()
    asked = e.post("/api/run_object_action", {"project": PO, "type": "cafe", "key": [fresh], "action": "flag_visit", "inputs": {"reason": "again"}})
    assert set(asked) == {"confirm"} and set(asked["confirm"]) == {"id", "message", "preview"} and "token" not in json.dumps(asked), asked
    assert objs["sql"](f"SELECT visit_reason FROM cafe_contacts WHERE cafe_id = '{fresh}'") == [["needs a look"]]


def test_object_graph_has_every_link(objs, people, tmp_path):
    """The object-type graph: every type a box, every link a line with its name and cardinality, on the canvas and in
    the page's offline HTML (render.diagram). A project without an ontology has neither, and no Objects page."""
    o = abi.call("ontology", project=PO)
    said = sorted(f"{ln['name']} · {ln['cardinality'].replace('-', ' ')}" for ln in o["links"])
    assert len(said) == 3 and {ln["cardinality"] for ln in o["links"]} == {"many-to-one", "many-to-many"}, o["links"]
    html = objs["owner"].page()["sections"]["Objects"]
    for ln in o["links"]:
        assert f">{ln['name']} · {ln['cardinality']}</text>" in html, ln
    assert all(f">{t['name']}</text>" in html for t in o["objects"]) and html.count("<rect") == len(o["objects"]), html[:300]
    out = drive(objs["owner"], tmp_path, [
        ["show", "objects"], ["wait", "main .canvas .tbl"], ["until", "document.querySelectorAll('main .canvas text.lbl').length > 0"],
        ["get", "labels", "[...document.querySelectorAll('main .canvas text.lbl')].map(t => t.textContent)"], ["all", "boxes", "main .canvas .tbl .th > span"], ["text", "heading", "main h4.sub"],
        ["get", "paths", "document.querySelectorAll('main .canvas path.ln').length"],
        ["click", "main .canvas .tbl .badge"], ["wait", "main .pick tr[data-r]"], ["get", "cur", "CUR"]])
    assert sorted(out["labels"]) == said and out["paths"] == 3 and sorted(out["boxes"]) == ["Cafés", "Customers", "Orders"], out
    assert out["heading"] == "Objects and connections" and out["cur"].startswith("obj:"), out
    # no ontology: the page data, the sidebar and the pages are as before
    page = people["owner"].page()
    assert page["ontology"]["objects"] == [] and "Objects" not in page["sections"], page["ontology"]
    none = drive(people["owner"], tmp_path, [
        ["get", "nav", "[...document.querySelectorAll('aside button.it')].map(b => b.dataset.k)"],
        ["show", "obj:cafe"], ["get", "cur", "CUR"], ["get", "keys", "Object.keys(PAGES).filter(k => k.startsWith('obj'))"]])
    assert "objects" not in none["nav"] and none["cur"] == "home" and none["keys"] == [], none


def test_report_click_opens_object_page(objs, tmp_path):
    """In a report, a mark or a row that is an object (the visual groups by a type's title or key) opens that object's
    page; any other member still filters the report."""
    top = abi.call("visual_data", project=PO, report="cafes", page="main", visual="by_name")["rows"][0][0]
    first = abi.call("visual_data", project=PO, report="cafes", page="main", visual="by_key")["rows"][0][0]
    name = objs["sql"](f"SELECT cafe_name FROM cafes WHERE cafe_id = '{first}'")[0][0]
    out = drive(objs["owner"], tmp_path, [
        ["show", "rep:cafes"], ["wait", "main [data-lk=by_name] [data-r='0']", "attached"], ["fire", "main [data-lk=by_name] [data-r='0']"],
        ["until", KV], ["text", "by_title", "main .pghead h1"], ["get", "cur", "CUR"],
        ["show", "rep:cafes"], ["wait", "main [data-lk=by_key] tr[data-r]"], ["click", "main [data-lk=by_key] tr[data-r]"],
        ["until", KV], ["text", "by_key", "main .pghead h1"], ["get", "cur2", "CUR"],
        ["show", "rep:cafes"], ["wait", "main [data-lk=by_region] [data-r='0']", "attached"], ["fire", "main [data-lk=by_region] [data-r='0']"],
        ["wait", "main .fchip"], ["get", "cur3", "CUR"], ["all", "filters", "main .fchip b"]])
    assert out["by_title"] == top and out["cur"].startswith("obj:cafe:"), out
    assert out["by_key"] == name and out["cur2"] == okey("cafe", first), out
    assert out["cur3"] == "rep:cafes" and out["filters"] == ["Region"], "a member that is no object filters, as before"


def test_report_form_card_runs_action(objs, tmp_path):
    """A form card is a visual of type `form` in the report's JSON, saved through save_report like any other (a wrong one
    is refused naming the file and the field), so it is in Git and in the project export; where forms aren't drawn it
    reads as its action's name; the published copy gets none. On the report page it shows the action's inputs and an
    object picker; submit runs the action through ABI's confirmation and the page's visuals read their rows again."""
    import io
    import zipfile

    import transfer
    e, fresh = objs["editor"], objs["other"]
    e.page()
    visits = {"id": "visits", "name": "Visits", "pages": [{"id": "main", "name": "Main", "visuals": [REPORT["pages"][0]["visuals"][0], CARD]}]}
    with_card = lambda card: e.post("/api/save_report", {"project": PO, "report": visits | {"pages": [{  # noqa: E731
        "id": "main", "name": "Main", "visuals": [visits["pages"][0]["visuals"][0], card]}]}})
    wrong = with_card(CARD | {"form": {"action": "flag_visits"}})
    assert "reports/visits.json $.pages[0].visuals[1].form.action: Unknown action type 'flag_visits'." in wrong["error"] and "flag_visit," in wrong["error"], wrong
    for card, field in ((CARD | {"form": {"act": "flag_visit"}}, "$.pages[0].visuals[1].form: "), ({k: v for k, v in CARD.items() if k != "form"}, "`form`"),
                        (CARD | {"query": {"measures": ["revenue"]}}, "`query`")):
        refused = with_card(card)
        assert "reports/visits.json $.pages[0].visuals[1]" in refused["error"] and field in refused["error"], refused
    assert with_card(CARD)["result"]["pages"][0]["visuals"] == ["flagged", "flag"]
    assert abi.call("visual_data", project=PO, report="visits", page="main")["visuals"][1] == {
        "id": "flag", "type": "text", "title": CARD["title"], "text": "Flag for a visit"}
    with pytest.raises(abi.ActionError) as gone:  # an action type a form card uses can't go
        sure("delete_ontology_item", project=PO, kind="action", id="flag_visit")
    assert gone.value.status == 409 and "still used" in gone.value.body["message"], gone.value.body
    assert any(x["file"] == "reports/cafes.json" and x["path"].endswith(".form.action") for x in gone.value.body["errors"]), gone.value.body
    abi.call("commit", project=PO, message="pages test: a form card")
    z = zipfile.ZipFile(io.BytesIO(transfer.export(PO)))
    bundle = {n.removeprefix("abi/"): json.loads(z.read(n)) if n.endswith(".json") else z.read(n).decode() for n in z.namelist() if n.startswith("abi/")}
    assert CARD in bundle["reports/visits.json"]["pages"][0]["visuals"], "the card is in the project's files, so in Git and the export"
    copy = {k: v for k, v in bundle.items() if not k.startswith("flows/")}  # what Publish sends: no flows
    with pytest.raises(abi.ActionError) as flow:
        abi.call("import_project", project=PO, bundle=copy, dry_run=True)
    assert flow.value.status == 422 and "cafe_visit" in json.dumps(flow.value.body), "an action type's flow isn't in the copy"
    app.read_only(copy)
    assert "actions" not in copy["model/ontology.json"] and all(v.get("type") != "form" for r, c in copy.items() if r.startswith("reports/")
                                                                for p in c["pages"] for v in p["visuals"])
    abi.call("import_project", project=PO, bundle=copy, dry_run=True)  # the read-only copy passes ABI's import checks
    # the docs page's example is what the schemas take (docs/objects.md)
    doc = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", (ROOT / "docs/objects.md").read_text(), re.S)]
    assert [d["$schema"].rsplit("/", 1)[-1] for d in doc] == ["ontology.json", "report.json"]
    abi.call("import_project", project=PO, dry_run=True, bundle=bundle | {"model/ontology.json": doc[0], f"reports/{doc[1]['id']}.json": doc[1]} | {
        r: c | {"pages": [p | {"visuals": [v for v in p["visuals"] if v.get("type") != "form"]} for p in c["pages"]]}
        for r, c in bundle.items() if r.startswith("reports/")})
    gen = app._GEN.get(PO, 0)
    app.changed(PO)  # the commit above was made behind the app's back: its pages read the project again
    for _ in range(300):
        if app._GEN.get(PO, 0) > gen:
            break
        time.sleep(0.1)
    cafe = objs["sql"](f"SELECT cafe_name FROM cafes WHERE cafe_id = '{fresh}'")[0][0]
    kpi = "document.querySelector('main [data-lk=flagged] .kpi .v').textContent"
    out = drive(e, tmp_path, [
        ["show", "rep:cafes"], ["wait", "main .grid [data-lk=flag].aform"], ["wait", "main [data-lk=flagged] .kpi .v"], ["get", "kpi", kpi],
        ["text", "title", "main [data-lk=flag] h3"], ["all", "labels", "main [data-lk=flag] .fld > label"],
        ["click", "main [data-lk=flag] .acts .btn.pri"], ["wait", "main [data-lk=flag] .msg.bad"], ["text", "unpicked", "main [data-lk=flag] .msg"],
        ["fill", "main [data-lk=flag] .opick input", cafe], ["until", "document.querySelectorAll('main [data-lk=flag] .opick .hits button').length === 1"], ["all", "matches", "main [data-lk=flag] .opick .hits button"],
        ["click", "main [data-lk=flag] .opick .hits button"], ["fill", "main [data-lk=flag] .fld > input", "from the report"],
        ["click", "main [data-lk=flag] .acts .btn.pri"], ["wait", "#confirm[open]"], ["text", "lead", "#c-msg"], ["get", "before", VISIT_REASON % json.dumps(fresh)],
        ["click", "#c-yes"], ["wait", "body > .toast"], ["text", "toast", "body > .toast"], ["get", "kpi2", kpi], ["get", "cur", "CUR"],
        ["get", "after", VISIT_REASON % json.dumps(fresh)]])
    assert out["title"].startswith(CARD["title"]) and out["labels"] == ["Café", "Reason"] and "Pick the café first" in out["unpicked"], out
    assert out["matches"] == [cafe] and cafe in out["lead"] and out["before"] is None, out
    assert out["after"] == "from the report" and "Saved" in out["toast"] and out["cur"] == "rep:cafes", out
    assert int(out["kpi2"]) == int(out["kpi"]) + 1, f"the page's visuals read their rows again: {out['kpi']} → {out['kpi2']}"
