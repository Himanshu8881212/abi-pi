"""app.html and its local server (needs ABI at :8770, no LLM): the build, and every guard on the server."""
import json
import re
import time
import urllib.error
import urllib.request

import pytest

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
        (drop / "orders.csv").write_text("order_date,region,amount\n" + "".join(
            f"2026-05-{d:02d},{r},{d * 3 + i}\n" for d in range(1, 29) for i, r in enumerate(["North", "South"])))
        abi.load("demo", P, [str(drop / "orders.csv")])
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


def test_one_file_in_the_project_folder_never_committed_or_sent(srv):
    dest = abi.folder(P)
    html = (dest / "app.html").read_text()
    assert app.KEY_SLOT in html, "the file on disk holds no key: opened from disk it is a snapshot"
    for k in ("reports", "measures", "flows", "ml", "tools", "agents", "glossary", "datasets", "docs", "history", "rls"):
        assert f'"{k}":' in html, k
    assert abi.git(dest, "status", "--porcelain").strip() == "", "app.html is git-excluded"
    assert "app.html" in (dest / ".git/info/exclude").read_text()


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
                                                              if "amount" in m["expression"]][:1], dimensions=["orders.region"])["rows"],
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
        code, r = post(port, "/api/semantic_model", {"project": P}, key)
        assert code == 200 and "result" in r, "reads still work"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/agent/mode", b"{}", {"X-Abi-Agent": HUB.agent_key, "X-Abi-Client": "default"})
        assert json.loads(urllib.request.urlopen(req).read())["mode"] == "viewer", "the harness reads the same switch"
    finally:
        post(port, "/mode", {"mode": "developer"}, key)


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


def test_vpn_mode_asks_for_the_passphrase_first(srv):
    import secrets
    import urllib.parse
    pw = secrets.token_urlsafe(16)  # a test passphrase, made here and never printed
    s = app.serve(P, 0, open_browser=False, remote=pw)
    port = s.server_address[1]
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "Passphrase" in page and "window.APP" not in page, "nothing of the project before the passphrase"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/semantic_model", b"{}", {"X-Abi-App": "x"}))
        wrong = urllib.request.urlopen(f"http://127.0.0.1:{port}/login", urllib.parse.urlencode({"p": "nope"}).encode()).read().decode()
        assert "Wrong passphrase" in wrong

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(f"http://127.0.0.1:{port}/login", urllib.parse.urlencode({"p": pw}).encode())
        except urllib.error.HTTPError as e:
            r = e
        assert r.code == 303 and "HttpOnly" in r.headers["Set-Cookie"]
        cookie = r.headers["Set-Cookie"].split(";")[0]
        page = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Cookie": cookie})).read().decode()
        assert "window.APP" in page
    finally:
        s.shutdown()
