"""End to end, stress and edge cases on the throwaway project demo/pi_app_test: every change goes through the app's
own routes (as the page sends them), is checked in ABI, and is undone. ABI's confirmations are never confirmed here:
a step that asks for one is checked for its dialog and left."""
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

import abi
import app
from test_app import P, post, srv  # noqa: F401 (the fixture: one app server for this module)


def api(port, key, action, **args):
    return post(port, f"/api/{action}", {"project": P, **args}, key)


def model(port, key):
    return api(port, key, "semantic_model")[1]["result"]


def test_measure_glossary_query_report_round_trip(srv):  # noqa: F811
    port, key, _ = srv
    code, r = api(port, key, "set_measure", id="e2e_total", name="E2E · total ünïcødé", expression="SUM(orders.amount)")
    assert code == 200 and "result" in r, r
    assert any(m["id"] == "e2e_total" for m in model(port, key)["measures"])
    code, r = api(port, key, "semantic_query", measures=["e2e_total"], dimensions=["orders.region"])
    assert code == 200 and r["result"]["rows"], r
    code, r = api(port, key, "set_glossary_term", term="E2E term", definition="A test term.", measures=["e2e_total"])
    assert code == 200 and "result" in r, r
    code, r = api(port, key, "save_dataset", id="e2e_q", name="E2E query", query={"measures": ["e2e_total"], "dimensions": ["orders.region"]})
    assert code == 200 and "result" in r, r
    code, r = api(port, key, "dataset_data", dataset="e2e_q")
    assert code == 200 and r["result"]["rows"], r
    rep = {"id": "e2e_rep", "name": "E2E report", "pages": [{"id": "p1", "name": "One", "visuals": [
        {"id": "v1", "type": "bar", "title": "<b>total</b> & <script>", "query": {"measures": ["e2e_total"], "dimensions": ["orders.region"]},
         "layout": {"x": 0, "y": 0, "w": 6, "h": 4}}]}]}
    code, r = api(port, key, "save_report", report=rep)
    assert code == 200 and "result" in r, r
    code, r = api(port, key, "visual_data", report="e2e_rep", page="p1", visual="v1")
    assert code == 200 and r["result"]["rows"], r
    # undo it all, newest first; anything that asks ABI's confirmation is left for a person
    term = next(g for g in model(port, key).get("glossary", []) if g.get("term") == "E2E term")
    for action, args in (("delete_report", {"report": "e2e_rep"}), ("delete_dataset", {"id": "e2e_q"}),
                         ("delete_glossary_term", {"id": term["id"]}), ("delete_measure", {"id": "e2e_total"})):
        code, r = api(port, key, action, **args)
        assert code == 200 and ("result" in r or "confirm" in r), (action, r)
        assert "confirm_token" not in json.dumps(r), "the confirm token never reaches the page"


def test_abis_refusal_names_the_field(srv):  # noqa: F811
    port, key, _ = srv
    code, r = api(port, key, "delete_report")
    assert "report" in r["error"] and "None" not in r["error"], r


def test_bad_measures_and_odd_input_get_abis_reason_not_a_crash(srv):  # noqa: F811
    port, key, _ = srv
    for args in ({"id": "e2e_bad", "name": "x", "expression": "SUM(nope.missing)"}, {"id": "Bad Id!", "name": "x", "expression": "1"},
                 {"id": "e2e_long", "name": "n" * 5000, "expression": "SUM(orders.amount)"}, {"id": "e2e_null", "name": None, "expression": None}):
        code, r = api(port, key, "set_measure", **args)
        assert code in (200, 400) and ("error" in r or "result" in r), (args, code, r)
        if "result" in r:
            api(port, key, "delete_measure", id=args["id"])


def test_exports_render_and_the_zip_comes_back_unchanged(srv):  # noqa: F811
    port, key, _ = srv
    rep = model(port, key) and api(port, key, "list_reports")[1]["result"]["reports"][0]
    page, vis = rep["pages"][0]["id"], next(v["id"] for v in rep["pages"][0]["visuals"] if v.get("type") != "text")
    req = lambda path, body: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
        f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(), {"X-Abi-App": key, "Content-Type": "application/json"}), timeout=300)
    assert req("/report/export", {"report": rep["id"], "page": page, "format": "pdf"}).read()[:5] == b"%PDF-"
    assert req("/data/export", {"report": rep["id"], "page": page, "visual": vis, "format": "csv"}).read()
    z = req("/transfer/export", {}).read()
    assert zipfile.ZipFile(io.BytesIO(z)).read("manifest.json")
    import base64
    r = json.loads(req("/transfer/import", {"data": base64.b64encode(z).decode()}).read())
    assert "uncommitted" in r and isinstance(r["result"]["changes"], list), r
    assert r["result"]["changes"] == [] or r["uncommitted"], "changes against the export are only the work not committed yet"


def test_commit_and_undo_work_and_leave_nothing(srv):  # noqa: F811
    port, key, _ = srv
    api(port, key, "set_measure", id="e2e_c", name="C", expression="SUM(orders.amount)")
    code, r = api(port, key, "changes")
    assert code == 200 and r["result"]["files"], r
    code, r = api(port, key, "undo")
    assert code == 200 and ("result" in r or "confirm" in r), r
    if "result" in r:
        assert not any(m["id"] == "e2e_c" for m in model(port, key)["measures"])


def test_many_browsers_at_once_all_get_answers(srv):  # noqa: F811
    port, key, _ = srv
    codes, errs = [], []

    def hit(i):
        try:
            rq = urllib.request.Request(f"http://127.0.0.1:{port}/api/readiness", json.dumps({"project": P}).encode(),
                                        {"X-Abi-App": key, "Content-Type": "application/json", "X-Abi-Client": f"stress{i % 7}", "X-Abi-Project": P})
            codes.append(urllib.request.urlopen(rq, timeout=120).status)
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e)[:120])
    ts = [threading.Thread(target=hit, args=(i,)) for i in range(40)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and codes.count(200) == 40, errs[:3]


def test_rapid_mode_flips_and_project_switches_stay_consistent(srv):  # noqa: F811
    port, key, _ = srv
    for i in range(10):
        post(port, "/mode", {"mode": "viewer" if i % 2 else "developer"}, key)
    post(port, "/mode", {"mode": "developer"}, key)
    code, r = api(port, key, "set_measure", id="e2e_m", name="M", expression="SUM(orders.amount)")
    assert code == 200, r
    api(port, key, "delete_measure", id="e2e_m")
    rq = urllib.request.Request(f"http://127.0.0.1:{port}/api/readiness", json.dumps({"project": P}).encode(),
                                {"X-Abi-App": key, "Content-Type": "application/json", "X-Abi-Project": "no_such_project"})
    assert urllib.request.urlopen(rq).status == 200, "an unknown project in the header keeps the browser's own"


def test_a_huge_body_is_refused_cleanly(srv):  # noqa: F811
    port, key, _ = srv
    rq = urllib.request.Request(f"http://127.0.0.1:{port}/api/readiness", b'{"project": "' + b"x" * (65 << 20) + b'"}',
                                {"X-Abi-App": key, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(rq, timeout=120)
    assert e.value.code in (400, 413)


def test_the_agent_tools_know_every_page():
    src = (app.abi.ROOT / ".pi/extensions/abi/index.ts").read_text()
    pages = set(__import__("re").search(r"const PAGES = \[([^\]]+)\]", src).group(1).replace('"', "").replace(" ", "").split(","))
    nav = {"home", "chat", "sources", "staged", "tables", "edits", "model", "measures", "glossary", "datasets", "reports", "monitor", "explore",
           "flows", "ml", "tools", "agents", "vault", "docs", "security", "environments", "history"}
    assert nav <= pages | {"chat", "edits"}, sorted(nav - pages)
