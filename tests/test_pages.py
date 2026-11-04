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
        if e.status != 409:
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
        owner = Browser(port, "himanshu", OWNER)
        yield {"port": port, "owner": owner, "people": {w: Browser(port, *made[w]) for w in made}, "uid": uid, "measure": m}
    finally:
        s.shutdown()
        for name, _ in made.values():
            sure("delete_user", name=name)
        drop([P])


class Browser:
    """A person signed in to the app: their session cookie, and the page key once a page opened."""

    def __init__(self, port: int, name: str, pw):
        if isinstance(pw, Path):  # the owner: data/accounts.txt, read as scripts/creds.py does (never printed)
            pw = next(x.split()[-1] for x in pw.read_text().splitlines() if re.match(rf"\s*{re.escape(name)}\b", x))
        self.port, self.cookie, self.key = port, "", ""
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
        st, b, _ = self.http("GET", f"/?p={P}")
        assert st == 200, st
        self.key = self.key or re.search(r"KEY='([^']+)'", b)[1]
        return json.loads(re.search(r"window.APP=(\{.*?\});\s*const tip", b, re.S)[1].replace("<\\/", "</"))

    def post(self, path, body) -> dict:
        return json.loads(self.http("POST", path, json.dumps(body), {"Content-Type": "application/json", "X-Abi-App": self.key,
                                                                    "X-Abi-Client": "pages", "X-Abi-Project": P})[1])


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
    monkeypatch.setenv("ABI_PI_WORKSPACES", str(tmp_path / "ws"))  # Settings: none, so no model flags
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
