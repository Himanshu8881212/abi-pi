"""app.html and its local server (needs the engine at :8780, no LLM): the build, and every guard on the server."""
import contextlib
import json
import subprocess
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from conftest import parquet

import abi
import app
from pi_driver import ROOT

P = "pi_app_test"
HUB = None  # the server's hub: the agent key lives there (the harness gets it through the env)


@pytest.fixture(scope="module")
def srv():
    global HUB
    if not abi.folder(P):
        drop = ROOT / "scratch" / P
        drop.mkdir(parents=True, exist_ok=True)
        (drop / "orders.csv").write_text("order_id,order_date,region,amount\n" + "".join(  # an id: `amount` is no key
            f"{d * 2 + i},2026-05-{d:02d},{r},{d * 3 + i}\n" for d in range(1, 29) for i, r in enumerate(["North", "South"])))
        abi.load("demo", P, parquet(drop / "orders.csv"))
    s = app.serve(P, 0, open_browser=False)
    port = s.server_address[1]
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
    HUB = s.hub
    yield port, re.search(r"KEY='([^']+)'", page).group(1), page
    s.shutdown()


def post(port, path, body, key, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                 {"X-Abi-App": key, "Content-Type": "application/json", "Host": host or f"127.0.0.1:{port}"})
    try:
        return 200, json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_served_page_is_live_and_queries_go_through_abi(srv):
    port, key, page = srv
    assert not key.startswith("__")
    m = json.loads(re.search(r"window.APP=(\{.*?\});\s*const tip", page, re.S).group(1).replace("<\\/", "</"))["measures"][0]["id"]
    code, r = post(port, "/api/semantic_query", {"project": P, "measures": [m], "dimensions": ["orders.region"]}, key)
    assert code == 200 and len(r["result"]["rows"]) == 2


@pytest.mark.parametrize("path,body,key,host", [
    ("/api/list_flows", {"project": P}, "wrong", None),                 # no key: another site can't call it
    ("/api/list_flows", {"project": P}, None, "evil.example:1"),        # DNS rebinding
    ("/api/set_role", {"project": P, "user": "staff", "role": "owner"}, None, None),  # access stays in ABI's app
    ("/api/set_secret", {"project": P}, None, None),
    ("/api/list_flows", {"project": "yt"}, None, None),                 # only this project
])
def test_guards_refuse(srv, path, body, key, host):
    port, k, _ = srv
    code, _ = post(port, path, body, key or k, host)
    assert code == 403


def test_confirmation_stays_on_the_server_and_needs_the_persons_click(srv):
    port, key, _ = srv
    assert post(port, "/api/set_measure", {"project": P, "id": "app_probe", "name": "Probe", "expression": "SUM(orders.amount)"}, key)[1].get("result")
    code, r = post(port, "/api/delete_measure", {"project": P, "id": "app_probe"}, key)
    assert "confirm" in r and "Destructive" in r["confirm"]["message"]
    assert "token" not in json.dumps(r).lower(), "the page never sees ABI's confirm token"
    assert post(port, "/confirm/" + r["confirm"]["id"], {}, key)[1].get("result")
    assert post(port, "/confirm/" + r["confirm"]["id"], {}, key)[0] == 410, "a confirmation is redeemed once"
    assert "app_probe" not in json.dumps(abi.call("semantic_model", project=P)["measures"])


def test_rejected_change_shows_abis_fix(srv):
    port, key, _ = srv
    _, r = post(port, "/api/set_measure", {"project": P, "id": "app_bad", "name": "Bad", "expression": "SUM(orders.nope)"}, key)
    assert "nothing changed" in r["error"] and "Use one of" in r["error"]


def test_agent_steers_the_page_only_with_its_key(srv):
    port, key, _ = srv
    show = {"project": P, "page": "measures", "item": "amount", "note": "look here", "query": None, "chart": None}
    req = lambda k: urllib.request.Request(f"http://127.0.0.1:{port}/agent/show", json.dumps(show).encode(),  # noqa: E731
                                           {"X-Abi-Agent": k, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req(key))  # the page key is not the agent key
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/events?k=wrong")
    stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/events?k={key}", timeout=10)
    assert b'"busy"' in stream.readline()
    urllib.request.urlopen(req(HUB.agent_key))
    for _ in range(20):
        line = stream.readline()
        if line.startswith(b"data:"):
            ev = json.loads(line[5:])
            if ev["type"] == "show":
                break
    assert ev == {"type": "show", "page": "measures", "report": None, "filters": None, "item": "amount", "note": "look here", "query": None, "chart": None}


def test_the_chat_agent_opens_a_project_it_just_made(srv):
    """A project the agent built in the chat opens in that person's app (the page reloads on it, and the server checks
    their access there); before, abi_show found no app for the new project and told the person to run a command."""
    port, key, _ = srv
    other = next((p["project"] for p in abi.call("list_projects")["projects"] if p["project"] != P), None)
    if not other:
        pytest.skip("needs a second project")
    stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/events?k={key}&c=open1&m=0", timeout=10)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/agent/show", json.dumps({"project": other, "page": "report", "report": "overview"}).encode(),
                                 {"X-Abi-Agent": HUB.agent_key, "X-Abi-Client": "open1", "Content-Type": "application/json"})
    urllib.request.urlopen(req)
    for _ in range(20):
        line = stream.readline()
        if line.startswith(b"data:") and (ev := json.loads(line[5:]))["type"] == "open":
            break
    assert ev == {"type": "open", "project": other, "page": "rep:overview"}


@pytest.mark.live
def test_chat_in_the_page_answers_and_steers_it(srv):
    port, key, _ = srv
    stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/events?k={key}", timeout=300)
    post(port, "/chat", {"message": "Which region has the highest total amount? Show me the Overview report filtered to it.",
                         "page": "home"}, key)
    seen, until = {}, time.time() + 300
    while not ("answer" in seen and "show" in seen) and time.time() < until:
        line = stream.readline()
        if line.startswith(b"data:"):
            ev = json.loads(line[5:])
            if ev["type"] != "show" or ev.get("filters") or "show" not in seen:
                seen[ev["type"]] = ev  # the last show with filters wins
    assert "show" in seen and "answer" in seen, f"within 5 min: {sorted(seen)}"
    assert seen["show"]["page"] == "report" and seen["show"]["filters"], seen["show"]
    top = max(abi.call("semantic_query", project=P, measures=[m["id"] for m in abi.call("semantic_model", project=P)["measures"]
                                                              if m["expression"].startswith("SUM(orders.amount")][:1], dimensions=["orders.region"])["rows"],
              key=lambda r: r[1])
    assert top[0] in seen["answer"]["text"] and "[not verified]" not in seen["answer"]["text"]


def test_viewer_mode_is_read_only_on_the_server(srv):
    port, key, _ = srv
    assert post(port, "/mode", {"mode": "viewer"}, key)[0] == 200
    try:
        code, r = post(port, "/api/set_measure", {"project": P, "id": "v_probe", "name": "V", "expression": "SUM(orders.amount)"}, key)
        assert code == 403 and "Viewer" in r["error"]
        assert post(port, "/api/run_flow", {"project": P, "flow": "x"}, key)[0] == 403
        assert post(port, "/vault/add", {"name": "x.md", "data": ""}, key)[0] == 403
        assert post(port, "/team/answer", {"id": "p1", "decision": "approve"}, key)[0] == 403, "no approvals in Viewer"
        assert post(port, "/transfer/export", {}, key)[0] == 403, "no export or import in Viewer"
        assert post(port, "/layouts", {"page": "home", "layout": {}}, key)[0] == 403, "no layout changes in Viewer"
        code, r = post(port, "/api/semantic_model", {"project": P}, key)
        assert code == 200 and "result" in r, "reads still work"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/agent/mode", b"{}", {"X-Abi-Agent": HUB.agent_key, "X-Abi-Client": "default"})
        assert json.loads(urllib.request.urlopen(req).read())["mode"] == "viewer", "the harness reads the same switch"
    finally:
        post(port, "/mode", {"mode": "developer"}, key)


def test_an_agent_only_reaches_its_own_browser(srv):
    port, key, _ = srv
    assert app.HUBS[HUB.hid] is HUB, "the chat agent is told the id its hub is stored under"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/agent/mode", b"{}", {"X-Abi-Agent": HUB.agent_key, "X-Abi-Client": "someone/else"})
    assert json.loads(urllib.request.urlopen(req).read())["mode"] == "viewer", "an unknown browser fails closed, never another's Developer"


def test_bad_input_gets_an_answer_not_a_dropped_connection(srv):
    port, key, _ = srv
    for path, raw in (("/api/semantic_model", b"{not json"), ("/edits/rows", b'{"table":"x","offset":"abc"}')):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", raw, {"X-Abi-App": key, "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400 and "error" in json.loads(e.value.read())


def test_a_confirmation_belongs_to_the_browser_that_asked(srv):
    port, key, _ = srv
    _, r = post(port, "/api/set_measure", {"project": P, "id": "c_probe", "name": "C", "expression": "SUM(orders.amount)"}, key)
    if "confirm" not in r:
        pytest.skip("ABI asked no confirmation for this change")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/confirm/{r['confirm']['id']}", b"{}",
                                 {"X-Abi-App": key, "Content-Type": "application/json", "X-Abi-Client": "another-browser"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 410


def test_switcher_lists_workspaces_and_their_projects(srv):
    port, key, _ = srv
    _, r = post(port, "/projects", {}, key)
    ws = r["result"]["workspaces"]
    assert r["result"]["current"] == P and P in [p["id"] for p in ws["demo"]]
    assert not any(p["id"].endswith("_pub") for ps in ws.values() for p in ps), "published copies aren't projects to pick"


def test_team_answer_only_for_a_waiting_step(srv):
    port, key, _ = srv
    assert post(port, "/team/answer", {"id": "no_such_step", "decision": "approve"}, key)[0] == 410


def test_vault_add_search_remove(srv):
    import base64
    port, key, _ = srv
    doc = "# Net revenue\n\nNet revenue is gross amount minus refunds and discounts.\n\n# Region\n\nNorth covers Hamburg and Berlin."
    _, r = post(port, "/vault/add", {"name": "definitions.md", "data": base64.b64encode(doc.encode()).decode()}, key)
    assert r["result"]["chunks"] == 2
    _, r = post(port, "/vault/search", {"query": "how is net revenue defined", "k": 3}, key)
    assert r["result"][0]["section"] == "Net revenue" and "refunds" in r["result"][0]["text"]
    _, r = post(port, "/vault/add", {"name": "evil.exe", "data": ""}, key)
    assert "use .md" in r["error"]
    post(port, "/vault/rm", {"name": "definitions.md"}, key)
    assert post(port, "/vault/list", {}, key)[1]["result"] == []


def test_modes_are_per_browser_and_a_phone_stays_viewer(srv):
    port, key, _ = srv
    def p(path, body, cid, mobile="0"):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                   {"X-Abi-App": key, "Content-Type": "application/json", "X-Abi-Client": cid, "X-Abi-Mobile": mobile})
        try:
            return urllib.request.urlopen(r).status, json.loads(urllib.request.urlopen(r).read() if False else b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
    assert p("/mode", {"mode": "developer"}, "phone1", "1")[0] == 200
    assert p("/api/set_measure", {"project": P, "id": "m_probe", "name": "M", "expression": "SUM(orders.amount)"}, "phone1", "1")[0] == 403
    p("/mode", {"mode": "developer"}, "laptop1")
    assert app.HUBS["laptop1"].mode == "developer" and app.HUBS["phone1"].mode == "viewer"
    p("/mode", {"mode": "viewer"}, "laptop1", "1")  # a window made narrow, then wide again: the phone layout ends with it
    p("/mode", {"mode": "viewer"}, "laptop1", "0")
    assert app.HUBS["laptop1"].mobile is False


def _account(name):
    """A test account's password, read like scripts/creds.py does (never printed)."""
    acc = Path(os.environ.get("ABI_ACCOUNTS", Path(__file__).resolve().parent.parent / "data/accounts.txt")).expanduser()
    line = next((x for x in acc.read_text().splitlines() if re.match(rf"\s*{re.escape(name)}\b", x)), None) if acc.exists() else None
    return line.split()[-1] if line else None


def test_people_sign_in_with_their_abi_account_and_get_only_their_access():
    import http.client
    if not _account("himanshu"):
        pytest.skip("no accounts file")
    s = app.serve(P, 0, open_browser=False, signin=True)
    port = s.server_address[1]
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "Sign in with your ABI account" in page and "__ABI_APP_KEY__" not in page, "no app, no key before sign-in"

        def sign(name, pw):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request("POST", "/signin", urllib.parse.urlencode({"name": name, "password": pw}), {"Content-Type": "application/x-www-form-urlencoded"})
            r = c.getresponse()
            return r.status, r.getheader("Set-Cookie") or "", r.read().decode()
        # a wrong password on a name nobody has: the same refusal, and the owner (the app's own account) never
        # collects failures toward ABI's 10-per-15-minutes lock from repeated runs
        st, cookie, body = sign("nobody-here", "wrong-" + "x" * 12)
        assert st == 200 and "Wrong name or password" in body and not cookie
        st, cookie, _ = sign("himanshu", _account("himanshu"))
        assert st == 303 and "abi_u=" in cookie and "HttpOnly" in cookie
        sid = cookie.split(";")[0]
        page = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Cookie": sid})).read().decode()
        key = re.search(r"KEY='([^']+)'", page).group(1)
        assert '"name": "himanshu"' in page and '"admin": true' in page
        req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/users", b"{}", {"Cookie": sid, "X-Abi-App": key, "Content-Type": "application/json"})
        users = json.loads(urllib.request.urlopen(req).read())["result"]["users"]
        assert any(u["name"] == "himanshu" for u in users)
        tok = next(x.token for x in s.hubs.values() if x.user == "himanshu")
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request("POST", "/signout", "", {"Cookie": sid})
        assert c.getresponse().status == 303
        page = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Cookie": sid})).read().decode()
        assert "Sign in with your ABI account" in page, "signed out: the old cookie opens nothing"
        with pytest.raises(abi.ActionError) as e2, abi.as_user(tok):
            abi.call("list_projects")
        assert e2.value.status == 401, "sign-out ends the ABI session too"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/semantic_model", json.dumps({"project": P}).encode(), {"X-Abi-App": key, "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403, "the page key alone is not enough: a session is"
        other = next((n for n in ("lena", "ken", "ivy", "omar") if _account(n)), None)
        if other:  # an account with no role in any project on this machine sees none, and no admin page
            st, cookie, _ = sign(other, _account(other))
            page = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Cookie": cookie.split(";")[0]})).read().decode()
            assert "You have no project yet" in page and "window.APP" not in page
    finally:
        s.shutdown()


def test_an_owner_adds_a_person_without_email_and_they_change_their_own_password():
    """No mail server needed: the owner adds an account with a starting password; its owner changes it in the app."""
    import http.client
    import secrets as _s
    if not _account("himanshu"):
        pytest.skip("no accounts file")
    s = app.serve(P, 0, open_browser=False, signin=True)
    port, name = s.server_address[1], "probe_" + _s.token_hex(4)
    pw1, pw2 = _s.token_urlsafe(12), _s.token_urlsafe(12)

    def sign(n, pw):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request("POST", "/signin", urllib.parse.urlencode({"name": n, "password": pw}), {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        sid = (r.getheader("Set-Cookie") or "").split(";")[0]
        if not sid:
            return None, None
        page = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Cookie": sid})).read().decode()
        return sid, (re.search(r"KEY='([^']+)'", page) or [None, None])[1]

    def post(sid, key, path, body):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(), {"Cookie": sid, "X-Abi-App": key or "", "Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(r).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        osid, okey = sign("himanshu", _account("himanshu"))
        code, r = post(osid, okey, "/admin/adduser", {"name": name, "password": "short"})
        assert code == 400 and "short" not in json.dumps(r), "a short password is refused here, never echoed"
        code, r = post(osid, okey, "/admin/adduser", {"name": name, "password": pw1})
        assert code == 200 and "result" in r, r
        sid, key = sign(name, pw1)
        assert sid, "the new person signs in with the starting password"
        code, r = post(sid, key, "/admin/adduser", {"name": name + "x", "password": pw1})
        assert code == 403, "only owners add people"
        code, r = post(sid, key, "/password", {"current": "wrong-" + pw1, "password": pw2})
        assert "error" in r and pw2 not in json.dumps(r)
        code, r = post(sid, key, "/password", {"current": pw1, "password": pw2})
        assert code == 200 and "result" in r, r
        assert sign(name, pw1)[0] is None and sign(name, pw2)[0], "the old password is gone, the new one works"
    finally:
        with abi.as_user(abi.login("himanshu", _account("himanshu"))[1]["token"]):
            with contextlib.suppress(abi.ActionError):
                try:
                    abi.call("delete_user", name=name)
                except abi.ActionError as e:
                    if e.status == 409 and (e.body or {}).get("confirm_token"):
                        abi.call("delete_user", confirm=e.body["confirm_token"], name=name)
        s.shutdown()


def test_no_html_template_puts_text_in_unescaped():
    """Measure names, titles and errors come from projects (anyone with Editor): into HTML only through E()."""
    import render
    src = app.JS + render.JS
    bad = [m.group(0)[:80] for m in re.finditer(r"(?:innerHTML|outerHTML|insertAdjacentHTML\([^,]+,)\s*=?\s*`[^`]*", src)
           if re.search(r"\$\{(?!E\()", m.group(0))]
    assert not bad, bad


def test_chats_are_each_persons_own():
    a, b = app.Hub(P, "", "k", "c1", user="ann"), app.Hub(P, "", "k", "c1", user="bob")
    assert a.chats_dir != b.chats_dir and a.chats_dir.name == "ann" and b.chats_dir.name == "bob"
    assert app.Hub(P, "", "k", "c1").chats_dir.name == "chats", "one person on this machine: as before"
    assert app.Hub(P, "", "k", "c1", user="../../x").chats_dir.parent.name == "chats", "a name can't climb out"


def test_pages_cannot_be_framed_or_sniffed(srv):
    port, _, _ = srv
    h = urllib.request.urlopen(f"http://127.0.0.1:{port}/").headers
    assert h["X-Frame-Options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"


def test_every_change_belongs_to_a_section_the_admin_can_close():
    assert app.section_of("/api/set_measure") == "Data Model" and app.section_of("/api/add_data") == "Data"
    assert app.section_of("/api/save_flow") == "Automate" and app.section_of("/api/save_report") == "Visualizations"
    assert app.section_of("/api/commit") == "Govern" and app.section_of("/edits/rows") == "Data"
    assert app.section_of("/api/semantic_query") is None, "reads serve every section"
    unmapped = sorted(a for a in app.WRITES if app.section_of(f"/api/{a}") is None)
    assert not unmapped, unmapped


def test_one_person_mode_is_never_open_to_the_network():
    with pytest.raises(SystemExit, match="sign-in"):
        app.main(["pi_app_test", "0", "--single", "--open"])


def test_a_restricted_person_gets_the_team_status_without_its_journal():
    t = {"tasks": [{"id": "a", "status": "passed"}], "pending": [], "journal": [{"kind": "handoff", "handoff": {"summary": "revenue 37.3m"}}], "brief": "x"}
    assert app.team_view(t, {"reports": ["r1"]}) == {"tasks": t["tasks"], "pending": [], "journal": [], "brief": ""}
    assert app.team_view(t, {}) == t


def test_idle_chat_agents_and_old_sessions_are_swept():
    class Proc:
        def poll(self):
            return None
    busy, idle, fresh = (app.Hub(P, "", "k", c) for c in "abc")
    for h, last, b in ((busy, 0, True), (idle, 0, False), (fresh, 10_000, False)):
        h.pi, h.busy, h.last = Proc(), b, last
    assert app.idle([busy, idle, fresh], now=10_000 + 60, limit=1800) == [idle]
    people = {"s1": {"t": 0, "token": "x"}, "s2": {"t": 50_000, "token": "y"}}
    assert app.stale_sessions(people, now=50_000, ttl=12 * 3600) == ["s1"]


def test_settings_take_a_key_and_never_give_it_back(srv):
    port, key, _ = srv
    prov = {"id": "e2e-prov", "name": "E2E", "api": "openai-completions", "base_url": "http://127.0.0.1:9/v1", "models": ["m1"]}
    try:
        code, r = post(port, "/admin/settings/provider", {"provider": prov, "key": "sk-e2e-secret-4321"}, key)
        assert code == 200 and "sk-e2e-secret" not in json.dumps(r) and any(p["key_ends"] == "4321" for p in r["result"]["providers"])
        _, r = post(port, "/admin/settings", {}, key)
        assert "sk-e2e-secret" not in json.dumps(r)
        code, r = post(port, "/admin/settings/provider", {"provider": {**prov, "api": "nope"}}, key)
        assert code == 400 and "error" in r
        _, r = post(port, "/admin/settings/test", {"id": "e2e-prov"}, key)
        assert r["result"]["ok"] is False and "sk-e2e" not in json.dumps(r), "an unreachable provider says so, without the key"
        post(port, "/mode", {"mode": "viewer"}, key)
        assert post(port, "/admin/settings", {}, key)[0] == 403
    finally:
        post(port, "/mode", {"mode": "developer"}, key)
        post(port, "/admin/settings/remove", {"id": "e2e-prov"}, key)


def test_an_agent_that_dies_gives_an_answer_not_silence():
    import io
    h = app.Hub(P, "", "k", "dies")
    events = []
    h.emit = events.append
    h.busy, h.pi = True, type("Proc", (), {"stdout": io.StringIO("")})()
    h.read()
    assert not h.busy and any(e.get("type") == "answer" and "stopped" in e["text"] for e in events)


def test_a_silent_model_is_reported_after_two_minutes():
    h = app.Hub(P, "", "k", "slow")
    h.busy, h.heard = True, 0
    assert app.silent([h], now=121) == [h] and app.silent([h], now=60) == []
    h.busy = False
    assert app.silent([h], now=500) == []


def test_abi_down_answers_503_not_a_hung_request(srv, monkeypatch):
    port, key, _ = srv
    monkeypatch.setattr(abi, "URL", "http://127.0.0.1:9")  # nothing listens there: ABI is down
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/readiness", json.dumps({"project": P}).encode(),
                                 {"X-Abi-App": key, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=30)
    assert e.value.code == 503 and "ABI" in json.loads(e.value.read())["error"]


def test_odd_numbers_and_missing_vault_documents_get_answers(srv):
    port, key, _ = srv
    code, r = post(port, "/edits/rows", {"table": "orders", "limit": 1e309}, key)
    assert code == 400 and "error" in r
    code, r = post(port, "/vault/read", {"name": "../../no-such.md"}, key)
    assert code in (400, 404) and "error" in r


def test_report_pdf_is_landscape_a4_so_charts_are_not_cut_off(srv):
    """The engine draws charts for a 1280 px viewport; a portrait Letter sheet cut their right side off (REPORT #39)."""
    s = abi.call("report_snapshot", project=P, report="overview", format="pdf", pages=["overview"])
    pdf = abi.artifact(s["snapshot"])
    w, h = map(float, re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)", pdf).groups())
    assert (int(w), int(h)) == (842, 595), (w, h)  # A4 landscape in points (Chrome: 842.88 x 595.92)


def test_the_apps_own_settings_have_a_git_history(srv):
    """people.json, analyses.json: JSON like the project's files, and versioned like them (REPORT #46)."""
    port, key, _ = srv
    for n in (1, 2):
        assert post(port, "/analyses", {"analyses": [{"name": f"a{n}", "measures": [], "dimensions": []}]}, key)[0] == 200
    d = app.app_dir(P)
    log = subprocess.run(["git", "-C", str(d), "log", "--format=%s", "--", "analyses.json"], capture_output=True, text=True).stdout
    assert log.count("analyses.json by") >= 2, log
    assert subprocess.run(["git", "-C", str(d), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


def test_a_model_that_refuses_is_told_not_silent():
    """A provider out of quota (429) ended the run with no answer: the chat just stopped (REPORT #50)."""
    import types
    err = {"role": "assistant", "content": [], "stopReason": "error",
           "errorMessage": 'cliproxy API error (429): {"code":"model_cooldown","message":"All credentials are cooling down"}'}
    lines = [json.dumps({"type": "message_end", "message": err}), json.dumps({"type": "agent_settled", "aborted": False})]
    h = app.Hub.__new__(app.Hub)
    out = []
    h.pi, h.busy, h.emit, h.chat_note = types.SimpleNamespace(stdout=iter(l + "\n" for l in lines)), True, out.append, lambda *a: None
    h.read()
    answers = [e["text"] for e in out if e["type"] == "answer"]
    assert answers and "429" in answers[0] and "Settings" in answers[0], out
    assert "credentials" not in answers[0], "the provider's own text stays in the log"


def test_chat_answers_draw_markdown_tables_safely():
    """The agent answers with markdown tables; the chat showed them as raw pipes. Cells stay escaped."""
    import shutil
    if not shutil.which("node"):
        pytest.skip("no node")
    src = re.search(r"const md=t=>.*?\}\);", app.JS, re.S).group(0)
    out = subprocess.run(["node", "-e", src + "\nprocess.stdout.write(md(process.argv[1]))",
                          "Top:\n| Category | Views |\n|---|---:|\n| <b>Music</b> | 1,200 |\n| **Gaming** | 900 |\nDone."],
                         capture_output=True, text=True, check=True).stdout
    assert "<table class=mdt><tr><th>Category</th><th>Views</th></tr>" in out and "<td class=n>1,200</td>" in out
    assert "&lt;b&gt;Music&lt;/b&gt;" in out and "<td><b>Gaming</b></td>" in out and "|---" not in out


def test_every_page_draws_in_a_real_browser(srv):
    """Each sidebar page in headless Chromium (the engine's, as report exports use): no script error, nothing blank.
    A page script that loses a definition (KIND) left Agents, Tools, Flows and ML blank while every other test passed."""
    py = ROOT / "engine/backend/.venv/bin/python"
    if not py.exists():
        pytest.skip("no engine venv with Playwright")
    port, _, _ = srv
    code = """
import json, sys
from playwright.sync_api import sync_playwright
url, out = sys.argv[1], {"errors": [], "blank": [], "pages": 0}
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: out["errors"].append(str(e)[:200]))
    pg.goto(url); pg.wait_for_selector("aside button.it")
    for k in pg.eval_on_selector_all("aside button.it", "bs => bs.map(b => b.dataset.k)"):
        pg.click(f"aside button.it[data-k='{k}']"); pg.wait_for_timeout(300); out["pages"] += 1
        if len(pg.inner_text("main").strip()) < 10: out["blank"].append(k)
    for want in ("viewer", "developer"):  # the switch's own buttons (a stray comment once swallowed their wiring)
        pg.click("#m-view" if want == "viewer" else "#m-dev"); pg.wait_for_timeout(800)
        out[want] = pg.evaluate("document.body.dataset.mode")
    b.close()
print(json.dumps(out))
"""
    import os
    env = {**os.environ, "HOME": os.environ.get("ENGINE_HOME", str(ROOT / "data/home"))} if (ROOT / "data/home").exists() else None
    r = subprocess.run([str(py), "-c", code, f"http://127.0.0.1:{port}/"], capture_output=True, text=True, timeout=300, env=env)
    if r.returncode and "Executable doesn't exist" in r.stderr:
        pytest.skip("Playwright's Chromium isn't installed here")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["pages"] >= 25 and out["errors"] == [] and out["blank"] == [], out
    assert out["viewer"] == "viewer" and out["developer"] == "developer", out


def test_page_layouts_are_a_draft_until_publish(srv):
    """Layout edits land in layouts.json "draft"; Publish copies them to "published", which Viewer reads."""
    port, key, _ = srv
    lay = {"0": {"Needs attention#0": {"o": 1, "w": 6, "h": 200}}}
    assert post(port, "/layouts", {"page": "x" * 201, "layout": lay}, key)[0] == 400
    code, r = post(port, "/layouts", {"page": "home", "layout": lay}, key)
    assert code == 200 and r["result"]["home"] == lay
    f = app.app_dir(P) / "layouts.json"
    assert json.loads(f.read_text()).get("published", {}).get("home") != lay, "Viewer does not see a draft"
    app.publish_layouts(P)
    assert json.loads(f.read_text())["published"]["home"] == lay
    code, r = post(port, "/layouts", {"page": "home", "layout": None}, key)
    assert code == 200 and "home" not in r["result"], "Reset drops the page's layout"


def test_parquet_is_the_only_input(tmp_path):
    """ABI-PI takes .parquet files and S3 folders of Parquet, nothing else: no CSV, Excel, JSON, local paths or databases."""
    for args in ({"location": "s3://b/f/*.csv"}, {"location": "/data/x.parquet"}, {"location": "s3://b/f/*.parquet", "format": "csv"},
                 {"location": "postgres://h/db"}, {}):
        with pytest.raises(abi.ActionError, match="S3 folder of Parquet"):
            abi.call("add_data", project="p", table="t", **args)
    abi.parquet_only("add_data", {"location": "s3://b/f/*.parquet", "format": "parquet"})
    (tmp_path / "x.csv").write_text("a\n1\n")
    with pytest.raises(abi.ActionError, match="S3 folder of Parquet"):
        abi.upload("p", "t", tmp_path / "x.csv")


def test_the_agent_makes_monitor_checks_and_explore_analyses(srv):
    """abi_monitor and abi_explore: the app's own Monitor checks and Explore analyses, through the agent's channel."""
    port, _, _ = srv

    def agent(route, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{route}", json.dumps({"project": P, **body}).encode(),
                                     {"X-Abi-Agent": HUB.agent_key, "Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    week = {"name": "Revenue down on last week", "kind": "measure", "measure": "revenue", "op": "<", "against": "prev", "value": -15,
            "period": "calendar.week"}
    code, r = agent("/agent/monitor", {"action": "save", "check": week})
    assert code == 200 and any(c["id"] == "revenue_down_on_last_week" and c["period"] == "calendar.week" for c in r["result"])
    code, r = agent("/agent/monitor", {"action": "save", "check": {"name": "x", "kind": "measure", "measure": "revenue", "op": "<", "against": "measure"}})
    assert code == 400 and "other" in r["error"], "a target check names the other measure"
    assert all(c["id"] != "revenue_down_on_last_week" for c in agent("/agent/monitor", {"action": "delete", "id": "revenue_down_on_last_week"})[1]["result"])
    code, r = agent("/agent/analysis", {"action": "save", "name": "Revenue by region", "query": {"measures": ["revenue"], "dimensions": ["orders.region"]}, "chart": "bar"})
    assert code == 200 and r["result"][0]["name"] == "Revenue by region" and r["result"][0]["q"]["dimensions"] == ["orders.region"]
    assert agent("/agent/analysis", {"action": "save", "name": "", "query": {"measures": []}})[0] == 400
    req = urllib.request.Request(f"http://127.0.0.1:{port}/agent/monitor", b"{}", {"X-Abi-Agent": "wrong"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req)


def test_a_chat_running_in_one_window_is_followed_in_another(srv):
    """Release run 30 Oct: a chat running in one window showed nothing live in another; typing there would start a second
    agent on the same conversation. Now the other window follows its steps, and waits for its turn."""
    a, b = (app.Hub(P, "http://x", "k", f"w{i}", False, "pat", None) for i in (1, 2))
    app.HUBS.update({"pat/w1": a, "pat/w2": b})
    try:
        a.chat_id = b.chat_id = "20261030-000000-abcd"
        qb: list = []
        b.clients.append(qb)
        a.emit({"type": "step", "tool": "semantic_query", "state": "ok"})
        a.emit({"type": "show", "page": "home"})
        assert [json.loads(x[5:])["type"] for x in qb] == ["step"], "steps follow; page moves stay in their window"
        a.busy = True
        b.chat("hello", "home")
        assert b.pi is None and "another window" in json.loads(qb[-1][5:])["text"], "no second agent on a running chat"
    finally:
        app.HUBS.pop("pat/w1", None), app.HUBS.pop("pat/w2", None)


def test_a_new_chat_is_not_answered_by_the_old_agent_ending(srv):
    """Release run 31 Oct: New chat stops the idle agent; its reader, ending, saw the window busy with the next message
    and answered it "The agent stopped before it could answer"."""
    import subprocess as sp
    import threading
    h = app.Hub(P, "http://x", "k", "w9", False, "pat", None)
    q: list = []
    h.clients.append(q)
    h.pi = sp.Popen(["sleep", "0.3"], stdout=sp.PIPE, text=True)
    t = threading.Thread(target=h.read)
    t.start()
    h.pi, h.busy = sp.Popen(["sleep", "5"], stdout=sp.PIPE, text=True), True  # the next message's agent
    try:
        t.join(5)
        assert h.busy and not [x for x in q if b'"answer"' in x], "the old agent's end says nothing about the new turn"
    finally:
        h.pi.kill()


def test_a_big_upload_arrives_in_pieces(srv, tmp_path):
    """A file over the 64 MB request limit can't come in one request: the page sends pieces, the last one loads it."""
    import base64
    import duckdb
    port, key, _ = srv
    f = tmp_path / "pieces.parquet"
    duckdb.sql(f"COPY (SELECT range AS id, range % 7 AS k FROM range(5000)) TO '{f}' (FORMAT parquet)")
    raw, uid = f.read_bytes(), "ab" * 16
    half = len(raw) // 2
    code, r = post(port, "/upload", {"table": "pieces_probe", "name": f.name, "id": uid, "data": base64.b64encode(raw[:half]).decode(), "last": False}, key)
    assert code == 200 and r["result"]["received"] == half
    code, r = post(port, "/upload", {"table": "pieces_probe", "name": f.name, "id": uid, "data": base64.b64encode(raw[half:]).decode(), "last": True}, key)
    assert code == 200 and r["result"].get("snapshot"), r  # the joined pieces are a valid Parquet file: it loaded
    assert post(port, "/upload", {"table": "x", "name": "a.parquet", "id": "../../etc", "data": ""}, key)[0] == 400


def test_s3_keys_become_secrets_bound_to_the_bucket_host_and_never_come_back(srv):
    port, key, _ = srv
    assert post(port, "/s3key", {"table": "s3_probe", "location": "s3://bucket/x/*.parquet", "key_id": "AKIAEXAMPLE"}, key)[0] == 400
    code, r = post(port, "/s3key", {"table": "s3_probe", "location": "s3://probe-bucket/x/*.parquet", "region": "eu-west-1",
                                    "key_id": "AKIAEXAMPLEPROBE", "secret": "probe-secret-value-123"}, key)
    try:
        assert code == 200 and r["result"] == {"key_id": "${secret:S3_S3_PROBE_KEY_ID}", "secret": "${secret:S3_S3_PROBE_SECRET}"}, r
        listed = json.dumps(abi.call("list_secrets", project=P))
        assert "S3_S3_PROBE_SECRET" in listed and "probe-secret-value-123" not in listed
    finally:
        for n in ("S3_S3_PROBE_KEY_ID", "S3_S3_PROBE_SECRET"):
            try:
                abi.call("delete_secret", name=n)
            except abi.ActionError as e:
                if e.status == 409 and (e.body or {}).get("confirm_token"):
                    abi.call("delete_secret", confirm=e.body["confirm_token"], name=n)
