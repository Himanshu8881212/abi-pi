"""app.html and its local server (needs ABI at :8770, no LLM): the build, and every guard on the server."""
import json
import re
import urllib.error
import urllib.request

import pytest

import abi
import app
from pi_driver import ROOT

P = "pi_app_test"


@pytest.fixture(scope="module")
def srv():
    if not abi.folder(P):
        drop = ROOT / "scratch" / P
        drop.mkdir(parents=True, exist_ok=True)
        (drop / "orders.csv").write_text("order_date,region,amount\n" + "".join(
            f"2026-05-{d:02d},{r},{d * 3 + i}\n" for d in range(1, 29) for i, r in enumerate(["North", "South"])))
        abi.load("demo", P, [str(drop / "orders.csv")])
    s = app.serve(P, 0, open_browser=False)
    port = s.server_address[1]
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
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
