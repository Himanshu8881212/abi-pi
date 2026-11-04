"""Results reach people (1.2 M1.4), on this machine's engine and worker, no LLM: a report emailed on a schedule set on
its page (a flow: the schedule and ABI's send_email with every page as one PDF, through the account mail server), Monitor
checks that alert with no browser open (a data_condition flow that posts to the inbox and emails if asked; the engine
computes the check, the previous-period change too, so an alert's figures are its rows), and the Inbox, where each
person sees only what they may. Mail goes only to example.test addresses at a local SMTP sink (tests/smtp_sink.py)."""
import email
import email.policy
import http.client
import json
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import zlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import drop, parquet
from test_app import _chromium

import abi
import app
import settings

ROOT = abi.ROOT
P = "pi_results_test"
TZ = "Asia/Kolkata"  # half an hour off any whole-hour zone: the schedule is read in the person's zone, not the server's


def projects() -> set:
    return {x["project"] for x in abi.call("list_projects")["projects"]}


def sure(action: str, **args) -> dict:
    """An action on what this test made (its accounts, rule, projects): ABI's confirmation is answered here."""
    try:
        return abi.call(action, **args)
    except abi.ActionError as e:
        if e.status != 409 or not (e.body or {}).get("confirm_token"):
            raise
        return abi.call(action, confirm=e.body["confirm_token"], **args)


def tick(*flows: str) -> None:
    """The worker's minute tick for these data-condition flows, now (flows._data_condition, in the engine's own Python):
    a false→true starts a run, which the running worker executes."""
    code = ("import sys\nfrom datetime import UTC, datetime\nfrom abi import flows\n"
            "for f in sys.argv[2:]:\n    flows._data_condition(sys.argv[1], flows._load(sys.argv[1], f), datetime.now(UTC))")
    r = subprocess.run(["sh", "-c", 'eval "$(scripts/engine.sh env)"; cd engine/backend; exec .venv/bin/python -c "$0" "$@"', code, P, *flows],
                       cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]


def until(fn, seconds: float, what: str):
    end = time.time() + seconds
    while not (got := fn()) and time.time() < end:
        time.sleep(1)
    assert got, what
    return got


def cells(data: dict) -> list:
    """An inbox message's attached rows in its columns' order (Postgres keeps them as JSON objects)."""
    return [[r[c] for c in data["columns"]] for r in data["rows"]]


def mails(box) -> list:
    """The sink's messages, parsed as a mail app reads them."""
    return [email.message_from_string(x, policy=email.policy.default) for x in box.read_text().split("=====\n")[:-1]] if box.exists() else []


def pdf_text(pdf: bytes) -> str:
    """The text Chromium's PDF draws, spaces dropped: each font's ToUnicode map applied to the strings shown in it."""
    objs = {int(m[1]): m[2] for m in re.finditer(rb"(\d+) 0 obj(.*?)endobj", pdf, re.S)}

    def stream(o: bytes) -> bytes:
        m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", o, re.S)
        return b"" if not m else zlib.decompress(m[1]) if b"/FlateDecode" in o[:m.start()] else m[1]

    def cmap(n: int) -> dict:
        s, out = stream(objs[n]).decode("latin-1"), {}
        for blk in re.findall(r"beginbfchar(.*?)endbfchar", s, re.S):
            out |= {int(a, 16): bytes.fromhex(b).decode("utf-16-be") for a, b in re.findall(r"<(\w+)>\s*<(\w+)>", blk)}
        for blk in re.findall(r"beginbfrange(.*?)endbfrange", s, re.S):
            for a, b, c in re.findall(r"<(\w+)>\s*<(\w+)>\s*<(\w+)>", blk):
                out |= {k: chr(int(c, 16) + k - int(a, 16)) for k in range(int(a, 16), int(b, 16) + 1)}
        return out
    fonts = {name: (cmap(int(m[1])), 4 if b"Identity-H" in objs[int(n)] else 2) for o in objs.values()
             for name, n in re.findall(rb"/(F\d+) (\d+) 0 R", o) if (m := re.search(rb"/ToUnicode (\d+) 0 R", objs.get(int(n), b"")))}
    out = []
    for o in objs.values():
        if b"/Type /Page" in o[:20] and (m := re.search(rb"/Contents (\d+) 0 R", o)):
            font = ({}, 2)
            for t in re.finditer(rb"/(F\d+) [\d.]+ Tf|<([0-9A-Fa-f]*)>", stream(objs[int(m[1])])):
                if t[1]:
                    font = fonts.get(t[1], ({}, 2))
                else:
                    h, w = t[2].decode(), font[1]
                    out.append("".join(font[0].get(int(h[i:i + w], 16), "") for i in range(0, len(h), w)))
    return "".join(out).replace(" ", "")


class Person:
    """A signed-in browser of the app, as the page talks to it (its key, its hub, its project)."""

    def __init__(self, port: int, name: str, pw: str):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request("POST", "/signin", urllib.parse.urlencode({"name": name, "password": pw}), {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        self.port, self.name, self.sid = port, name, (r.getheader("Set-Cookie") or "").split(";")[0]
        assert self.sid.startswith("abi_u="), f"{name} signs in"
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request("GET", f"/?p={P}", headers={"Cookie": self.sid})
        self.key = re.search(r"KEY='([^']+)'", c.getresponse().read().decode())[1]

    def post(self, path: str, body: dict) -> dict:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        c.request("POST", path, json.dumps(body), {"Cookie": self.sid, "X-Abi-App": self.key, "Content-Type": "application/json",
                                                   "X-Abi-Client": self.name, "X-Abi-Project": P})
        r = c.getresponse()
        return {"status": r.status, **json.loads(r.read() or b"{}")}

    def yes(self, r: dict) -> dict:
        """The person's Confirm in ABI's dialog (the token stays in the app)."""
        assert "confirm" in r, r
        return self.post(f"/confirm/{r['confirm']['id']}", {})


@pytest.fixture(scope="module")
def w(tmp_path_factory):
    """A project of April and May orders, Ann (Editor) and Bob (Viewer of region North only), the account mail server
    pointed at a local sink, and the app with sign-in."""
    tmp = tmp_path_factory.mktemp("results")
    drop([p for p in (P, app.pub_id(P)) if p in projects()])  # an interrupted run's
    lines, n = [], 0
    for month, k, days in ((4, 4, 30), (5, 3, 28)):  # April 3750 (North 1860), May 2464 (North 1218)
        for d in range(1, days + 1):
            for i, region in enumerate(("North", "South")):
                n += 1
                lines.append(f"{n},2026-{month:02d}-{d:02d},{region},{k * d + i}\n")
    (tmp / "orders.csv").write_text("order_id,order_date,region,amount\n" + "".join(lines))
    abi.load("demo", P, parquet(tmp / "orders.csv"))
    ann, bob = (f"res_{x}_{secrets.token_hex(3)}" for x in ("ann", "bob"))
    pw = {x: secrets.token_urlsafe(16) for x in (ann, bob)}
    ids = {x: abi.call("create_user", name=x, password=pw[x])["id"] for x in (ann, bob)}
    abi.call("set_role", project=P, user=ann, role="editor")
    abi.call("set_role", project=P, user=bob, role="viewer")
    sure("set_rls_rule", project=P, id="north", table="orders", column="region", users={str(ids[bob]): ["North"]})
    with socket.socket() as s0:
        s0.bind(("127.0.0.1", 0))
        port = s0.getsockname()[1]
    box = tmp / "mailbox.txt"
    sink = subprocess.Popen([sys.executable, ROOT / "tests/smtp_sink.py", box, str(port)])
    before = settings.load().get("mail")  # the app's note of the account mail server (never its password)
    abi.call("set_account_mail", smtp={"host": "127.0.0.1", "port": port, "from": "abi@example.test", "security": "none"})
    srv = app.serve(P, 0, open_browser=False, signin=True)
    try:
        yield {"ann": Person(srv.server_address[1], ann, pw[ann]), "bob": Person(srv.server_address[1], bob, pw[bob]), "box": box,
               "tok": {x: abi.login(x, pw[x])[1]["token"] for x in (ann, bob)}, "srv": srv, "ids": ids}
    finally:
        srv.shutdown()
        sink.terminate()
        abi.call("set_account_mail", smtp=before and {**before, **({"password": "${secret:SMTP_PASSWORD}"} if before.get("username") else {})})
        drop([p for p in (P, app.pub_id(P)) if p in projects()])
        for x in (ann, bob):
            sure("delete_user", name=x)


def test_email_this_report_is_a_schedule_and_the_first_send_to_a_domain_asks_once(w):
    """The report page's Email this report saves flow email_<report>: a schedule in the person's time zone and ABI's
    send_email with every page as one PDF. Saving asks ABI's confirmation once: it runs unattended, and its recipients'
    domain is new to the project; saving again to that domain asks nothing (the run itself can't confirm)."""
    ann = w["ann"]
    now = datetime.now(ZoneInfo(TZ))
    fire = (now + timedelta(seconds=75)).replace(second=0, microsecond=0)  # 15 to 75 s away: saved well before it
    body = {"report": "overview", "to": ["cfo@example.test"], "days": [fire.isoweekday() % 7], "time": f"{fire:%H:%M}", "timezone": TZ}
    r = ann.post("/report/email", body)
    assert "confirm" in r and "email:example.test" in r["confirm"]["message"] and "unattended" in r["confirm"]["message"], r
    assert not (abi.ABI_PROJECTS / P / "flows/email_overview.json").exists(), "nothing saved before the person's yes"
    assert ann.yes(r).get("result") == "saved"
    r = ann.post("/report/email", body | {"to": ["cfo@example.test", "ops@example.test"]})
    assert r.get("result") == "saved", f"the same domain again asks nothing: {r}"
    f = json.loads((abi.ABI_PROJECTS / P / "flows/email_overview.json").read_text())
    assert f["trigger"] == {"type": "schedule", "cron": f"{fire.minute} {fire.hour} * * {fire.isoweekday() % 7}", "timezone": TZ}
    assert f["enabled"] and f["owner"] == str(w["ids"][ann.name]), "runs as Ann, with her access"
    step = f["steps"][0]["input"]
    assert step["to"] == ["cfo@example.test", "ops@example.test"] and step["report"] == {"report": "overview", "pages": "all", "format": "pdf"}
    assert ann.post("/report/email", body | {"to": ["not an address"]})["status"] == 400
    assert w["bob"].post("/report/email", body)["status"] == 403, "a Viewer sets no report email"
    w["fire"] = fire


def test_a_monitor_check_alerts_once_when_it_starts_to_hold_with_no_browser_open(w):
    """Saving a measure check saves its flow (after ABI's confirmation): a data_condition trigger that posts the check's
    rows to the inbox and emails them if asked. The worker's tick (advanced here, as no browser is open) starts one run
    when the check goes from false to true, and none while it stays true."""
    ann, check = w["ann"], {"name": "A region above 3500", "kind": "measure", "measure": "amount", "op": ">", "against": "value",
                            "value": 3500, "by": "orders.region"}
    r = ann.post("/monitor", {"action": "save", "check": check, "email": "ann@alerts.example.test"})
    assert "email:alerts.example.test" in r["confirm"]["message"], r
    r = ann.yes(r)
    assert [c["id"] for c in r["result"]] == ["a_region_above_3500"] and r["result"][0]["condition"]["query"]["dimensions"] == ["orders.region"]
    fid = "monitor_a_region_above_3500"
    c = http.client.HTTPConnection("127.0.0.1", w["srv"].server_address[1], timeout=60)
    c.request("POST", "/agent/monitor", json.dumps({"project": P, "action": "delete", "id": "a_region_above_3500"}),
              {"X-Abi-Agent": next(h for h in w["srv"].hubs.values() if h.user == ann.name).agent_key, "Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 400 and "Monitor page" in json.loads(r.read())["error"], "the agent can't leave its flow alerting on its own"
    runs = lambda: abi.call("flow_runs", project=P, flow=fid)["runs"]  # noqa: E731
    tick(fid)
    assert runs() == [], "North 3078 and South 3136: no region above 3500, no run"
    # the data changes (Ann's write-back): +500 on a North row and -500 on a South one, May's total and the report's KPIs unchanged
    ed = ann.post("/api/edit_cells", {"project": P, "table": "orders", "edits": [{"key": [61], "column": "amount", "value": 503},
                                                                                {"key": [62], "column": "amount", "value": -496}]})
    assert "result" in ed, ed
    tick(fid)
    tick(fid)  # still true: nothing new
    assert len(runs()) == 1, runs()
    msgs = until(lambda: [m for m in ann.post("/inbox", {})["result"]["messages"] if m["text"].startswith("Monitor: A region above 3500")],
                 120, "the alert reaches Ann's inbox")
    want = abi.call("semantic_query", project=P, measures=["amount"], dimensions=["orders.region"])
    assert len(msgs) == 1 and msgs[0]["text"] == "Monitor: A region above 3500. It alerts when Amount for any region is above 3500."
    assert cells(msgs[0]["data"]) == [list(x) for x in want["rows"]] == [["North", 3578], ["South", 2636]]
    mail = until(lambda: [m for m in mails(w["box"]) if m["Subject"] == "Monitor: A region above 3500"], 60, "the alert is emailed")
    assert len(mail) == 1 and mail[0]["To"] == "ann@alerts.example.test"
    att = {a.get_filename(): a.get_content() for a in mail[0].iter_attachments()}
    head, *body = [x.split(",") for x in att["data.csv"].splitlines()]
    assert head == ["orders.region", "amount"] and [[r, float(v)] for r, v in body] == [["North", 3578], ["South", 2636]], "the check's rows"
    assert len(runs()) == 1


def test_the_previous_period_change_is_computed_by_the_engine(w):
    """A check against the previous period: ABI's check_condition (what the Monitor page and the flow ask) returns the
    latest month, its total, the month before, its total and the % change, equal to the totals by month; its alert
    carries that row, so the figures in it are tool rows, never the browser's arithmetic."""
    ann = w["ann"]
    check = {"name": "Amount down on last month", "kind": "measure", "measure": "amount", "op": "<", "against": "prev", "value": -10,
             "period": "calendar.month"}
    r = ann.yes(ann.post("/monitor", {"action": "save", "check": check}))
    cond = next(c["condition"] for c in r["result"] if c["id"] == "amount_down_on_last_month")
    assert cond == {"query": {"measures": ["amount"], "filters": [], "dimensions": []}, "measure": "amount", "op": "<", "value": -10.0,
                    "change": True, "period": "calendar.month", "skip_latest": False}
    got = ann.post("/api/check_condition", {"project": P, **cond})["result"]
    by = {tuple(x[:2]): x[2] for x in abi.call("semantic_query", project=P, measures=["amount"], dimensions=["calendar.year", "calendar.month"])["rows"]}
    may, april = by[(2026, 5)], by[(2026, 4)]
    assert (may, april) == (2464, 3750)
    assert got["columns"] == ["calendar.year", "calendar.month", "amount", "previous_year", "previous_month", "previous_amount", "change_pct"]
    assert got["rows"] == [[2026, 5, may, 2026, 4, april, round((may - april) / abs(april) * 100, 4)]] and got["holds"]
    tick("monitor_amount_down_on_last_month")
    msg = until(lambda: [m for m in ann.post("/inbox", {})["result"]["messages"] if m["text"].startswith("Monitor: Amount down")], 120,
                "the change alert reaches the inbox")[0]
    assert msg["text"] == "Monitor: Amount down on last month. It alerts when the change of Amount from one month to the next is below -10%."
    assert cells(msg["data"]) == got["rows"]
    w["prev"] = cond


def test_the_inbox_shows_each_person_only_what_they_may_see(w):
    """The Inbox reads ABI's inbox and alerts feed as the person: Ann's test run of the report email is hers alone (the
    workspace owner doesn't see it either), Bob, a Viewer restricted to region North, gets only his own copy of each
    alert, with his rows, and a failed flow's alert is read or not per person. The admin's sections apply: with
    Visualizations closed to him, his Inbox is refused."""
    ann, bob = w["ann"], w["bob"]
    with abi.as_user(w["tok"][ann.name]):
        abi.call("test_task", project=P, flow="email_overview")
    mine = until(lambda: [m for m in ann.post("/inbox", {})["result"]["messages"] if m["text"].startswith("Test run: email to")], 120,
                 "Ann's test run reaches her inbox")
    assert "cfo@example.test" in mine[0]["text"] and mine[0]["snapshot"].endswith(".pdf")
    owner = abi.call("inbox", project=P)["messages"]
    assert {m["text"].split(".")[0] for m in owner} == {"Monitor: A region above 3500", "Monitor: Amount down on last month"}, \
        "the owner sees the posts, not Ann's test run"
    got = bob.post("/inbox", {})["result"]["messages"]
    assert sorted(m["text"].split(".")[0] for m in got) == ["Monitor: A region above 3500", "Monitor: Amount down on last month"]
    with abi.as_user(w["tok"][bob.name]):
        north = abi.call("semantic_query", project=P, measures=["amount"], dimensions=["orders.region"])["rows"]
        change = abi.call("check_condition", project=P, **w["prev"])["rows"]
    rows = {m["text"].split(".")[0]: cells(m["data"]) for m in got}
    assert rows["Monitor: A region above 3500"] == [list(x) for x in north] == [["North", 3578]], "his copy has his rows only"
    assert rows["Monitor: Amount down on last month"] == change and change[0][2] == 1718, "May North 1218 + 500"
    # a failed flow is an alert (ABI's feed): new for each person until they read it, its data-derived text hidden from Bob
    broken = {"$schema": app.FLOW_SCHEMA, "id": "broken", "name": "Broken", "enabled": False, "trigger": {"type": "manual"},
              "steps": [{"id": "load", "type": "refresh", "table": "nope"}]}
    assert "result" in ann.post("/api/save_flow", {"project": P, "flow": broken})
    assert "result" in ann.post("/api/run_flow", {"project": P, "flow": "broken"})
    alert = until(lambda: [a for a in ann.post("/inbox", {})["result"]["alerts"] if a["ref"] == "broken"], 120, "the failure is an alert")[0]
    assert "No table 'nope'" in alert["message"] and not alert["read"]
    assert next(a for a in ann.post("/inbox", {"read": [alert["id"]]})["result"]["alerts"] if a["id"] == alert["id"])["read"]
    theirs = next(a for a in bob.post("/inbox", {})["result"]["alerts"] if a["id"] == alert["id"])
    assert not theirs["read"] and "nope" not in theirs["message"] and theirs["message"].endswith("[hidden by row-level security]"), theirs
    app.app_save(P, "people.json", {"users": {bob.name: {"sections": ["Data"]}}})
    try:
        r = bob.post("/inbox", {})
        assert r["status"] == 403 and "Visualizations" in r["error"]
    finally:
        app.app_save(P, "people.json", {"users": {}})


def test_a_report_emails_itself_on_schedule_with_the_figures_of_visual_data(w):
    """Within two minutes of its time the scheduled run sends the report, every page as one A4 landscape PDF, through
    the account mail server; the PDF's figures are visual_data's for the flow's owner (here the KPIs)."""
    ann, fire = w["ann"], w["fire"]
    mail = until(lambda: [m for m in mails(w["box"]) if m["Subject"] == "Overview"],
                 max(5, (fire - datetime.now(ZoneInfo(TZ))).total_seconds()) + 120, "the scheduled report arrives")
    assert len(mail) == 1 and mail[0]["To"] == "cfo@example.test, ops@example.test" and mail[0]["From"] == "abi@example.test"
    pdfs = [a.get_content() for a in mail[0].iter_attachments() if a.get_content_type() == "application/pdf"]
    assert len(pdfs) == 1 and pdfs[0].startswith(b"%PDF")
    w_, h_ = map(float, re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)", pdfs[0]).groups())
    assert (int(w_), int(h_)) == (842, 595), "A4 landscape"
    text = pdf_text(pdfs[0])
    with abi.as_user(w["tok"][ann.name]):
        for v in ("kpi-orders_rows", "kpi-amount"):
            d = abi.call("visual_data", project=P, report="overview", page="overview", visual=v)
            (m, name), = d["measures"].items()
            f = d["format"][m]
            assert not f["compact"] and f["decimals"] == 0, f
            assert name.replace(" ", "") + f"{d['rows'][0][0]:,.0f}" in text, (name, d["rows"], text[:400])
    run = abi.call("flow_runs", project=P, flow="email_overview")["runs"]
    assert [x["id"].rsplit("/", 1)[1] for x in run if not x["id"].rsplit("/", 1)[1].startswith("test-")] == [
        f"{fire.astimezone(UTC):%Y-%m-%dT%H:%MZ}"], "one run, for that minute"


def test_publish_leaves_the_flows_out_of_the_published_copy(w):
    """Viewer's published copy gets no flows: a schedule there would send every email twice. They also blocked the
    workspace owner's Publish, as ABI's import takes a changed flow only from its owner and Ann saved these (her Monitor
    checks and report email). A copy that 1.1 published with Ann's flows switched on (they ran twice) loses them."""
    abi.call("commit", project=P, message="Results reach people")
    pub, bundle, _ = app.publish_bundle(P)
    assert "reports/overview.json" in bundle and not [rel for rel in bundle if rel.startswith("flows/")]
    sure("import_project", project=pub, bundle=bundle)  # the workspace owner's Publish
    ann, theirs = w["ann"].name, ("flows/email_overview.json", "flows/monitor_a_region_above_3500.json")
    abi.call("set_role", project=pub, user=ann, role="editor")
    with abi.as_user(w["tok"][ann]):  # the copy as 1.1's Publish left it: Ann's flows, switched on
        sure("import_project", project=pub, bundle=bundle | {rel: json.loads((abi.ABI_PROJECTS / P / rel).read_text()) for rel in theirs})
    assert sorted(f["id"] for f in abi.call("list_flows", project=pub)["flows"]) == ["email_overview", "monitor_a_region_above_3500"]
    sure("import_project", project=pub, bundle=bundle)  # the owner's next Publish
    assert abi.call("list_flows", project=pub)["flows"] == [], "they run once, in the project"
    assert json.loads((abi.ABI_PROJECTS / P / "flows/email_overview.json").read_text())["enabled"], "the project's own stay on"


def test_monitor_inbox_and_email_this_report_draw_in_a_real_browser(w):
    """Ann's pages in headless Chromium: Monitor shows ABI's verdicts and figures and where alerts go with the page
    closed, the Inbox lists her alerts and test run with their rows, the report's Email this report opens as saved.
    A new change-on-last-period check compares completed periods unless unticked: with daily loads the latest one is
    still filling, and its change on a whole period would alert at the start of every period."""
    ann = w["ann"]
    out = _chromium(r"""
import json, sys
from playwright.sync_api import sync_playwright
base, sid, p = sys.argv[1:4]
out, errors = {}, []
with sync_playwright() as pw:
    b = pw.chromium.launch(); ctx = b.new_context(locale="en-US", viewport={"width": 1440, "height": 900})
    ctx.add_cookies([{"name": "abi_u", "value": sid, "url": base}])
    pg = ctx.new_page(); pg.on("pageerror", lambda e: errors.append(str(e)[:200]))
    pg.goto(f"{base}/?p={p}#monitor")
    pg.wait_for_function("() => [...document.querySelectorAll('main .card p')].filter(x => /^(OK|Alert) · /.test(x.textContent)).length >= 2")
    out["monitor"] = pg.eval_on_selector_all("main .card", "cs => cs.map(c => c.innerText)")
    pg.evaluate("location.hash = 'inbox'"); pg.wait_for_selector("main .hit")
    out["inbox"] = pg.inner_text("main"); out["badge"] = pg.inner_text("aside button.it[data-k='inbox']")
    pg.evaluate("location.hash = 'rep:overview'"); pg.click("button:has-text('Email this report')"); pg.wait_for_selector("text=Save schedule")
    out["to"] = pg.eval_on_selector("input[placeholder^='name@company.com, ']", "e => e.value")
    pg.evaluate("location.hash = 'monitor'"); pg.click("button:has-text('New monitor')")
    pg.select_option(".monform select:has(option[value=prev])", "prev"); out["completed"] = pg.is_checked(".monform label.chk input")
    pg.fill(".monform input[placeholder^='e.g. Views']", "Down on last month"); pg.fill(".monform input[type=number]", "-10")
    with pg.expect_request(lambda r: r.url.endswith("/monitor") and r.method == "POST") as rq:
        pg.click(".monform button:has-text('Create')")  # ABI asks before the flow is saved: left unanswered here
    out["sent"] = json.loads(rq.value.post_data)["check"]
    b.close()
print(json.dumps({"out": out, "errors": errors}))
""", f"http://127.0.0.1:{w['srv'].server_address[1]}", ann.sid.split("=", 1)[1], P)
    assert out["errors"] == [], out["errors"]
    mon = "\n".join(out["out"]["monitor"])
    assert "Alert · 1 of 2 region is above 3,500: North" in mon, mon
    assert "Alert · Amount: 2,464 in month 5 of 2026, -34.29% on month 4 of 2026 (3,750) · alerts when the change is below -10%" in mon, mon
    assert "With this page closed: on, alerts go to the Inbox and ann@alerts.example.test" in mon, mon
    inbox = out["out"]["inbox"]
    assert "Monitor: A region above 3500" in inbox and "3,578" in inbox and "Test run: email to" in inbox, inbox
    assert out["out"]["to"] == "cfo@example.test, ops@example.test" and re.search(r"\d", out["out"]["badge"]), out["out"]
    assert out["out"]["completed"] and out["out"]["sent"]["against"] == "prev" and out["out"]["sent"]["skip_latest"] is True, out["out"]


def test_a_check_made_again_or_edited_alerts_when_it_holds(w):
    """Monitor names a check's flow after the check, so a check deleted and made again, or edited, kept the old one's
    state ("holds", "fired today", its run IDs): the new check, which holds, never alerted. A new trigger starts as
    never checked; the same check saved again (its email changed) changes nothing."""
    ann, fid = w["ann"], "monitor_a_region_above_3500"
    check = {"name": "A region above 3500", "kind": "measure", "measure": "amount", "op": ">", "against": "value", "value": 3500,
             "by": "orders.region"}
    runs = lambda: len(abi.call("flow_runs", project=P, flow=fid)["runs"])  # noqa: E731
    save = lambda b: (lambda r: ann.yes(r) if "confirm" in r else r)(ann.post("/monitor", b))  # noqa: E731 (Ann's yes when ABI asks)
    assert runs() == 1
    assert "result" in save({"action": "delete", "id": "a_region_above_3500"})
    assert "result" in save({"action": "save", "check": check})  # made again: North 3578 holds
    tick(fid)
    assert runs() == 2, "the new check alerts"
    assert "result" in save({"action": "save", "check": check, "email": "ann@alerts.example.test"})  # the same trigger
    tick(fid)
    assert runs() == 2
    assert "result" in save({"action": "save", "check": check | {"value": 3000}})  # edited: still holds
    tick(fid)
    tick(fid)
    assert runs() == 3, "the edited check alerts once"
