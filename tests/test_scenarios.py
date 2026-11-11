"""DEMO.md scenarios 1-5, driven through Pi like a person at the terminal (ABI_PI_LIVE=1)."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import duckdb
import pytest
from conftest import parquet

import abi
from pi_driver import ROOT, ask

pytestmark = pytest.mark.live
CSV = Path(os.environ.get("ABI_PI_CSV", "~/Work/Working/ABI-demo/youtube_trending_real/JPvideos.csv")).expanduser()
OPTS = "quote='\"', escape='\"', strict_mode=false"


def figures(text: str) -> set[int]:
    return {int(x.replace(",", "")) for x in re.findall(r"\d[\d,]{3,}", text)}


def scenario1(project: str) -> dict:
    """Load a new Parquet file (the CSV, converted: ABI-PI takes Parquet), model it, top 5 categories by views."""
    drop = ROOT / "scratch" / project
    drop.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CSV, drop / CSV.name)
    pq = drop / (CSV.stem + ".parquet")
    duckdb.sql(f"COPY (SELECT * FROM read_csv('{drop / CSV.name}', {OPTS})) TO '{pq}' (FORMAT parquet)")
    run = ask(f"Load the file {pq} into a new ABI project {project} in workspace demo, make sure the model is "
              "Ready, and tell me the top 5 categories (category_id) by total views (the sum of the views column) with their figures.")
    hand = duckdb.sql(f"SELECT category_id, SUM(views) v FROM read_csv('{drop / CSV.name}', {OPTS}) GROUP BY 1 "
                      "ORDER BY v DESC NULLS LAST LIMIT 5").fetchall()
    ready = abi.call("readiness", project=project)
    sm = abi.call("semantic_model", project=project)
    m = next(x["id"] for x in sm["measures"] if re.fullmatch(r"SUM\(\w+\.views\)", x["expression"].replace(" ", "")))
    table = next(t for t, cols in sm["columns"].items() if "category_id" in cols)
    got = abi.call("semantic_query", project=project, measures=[m], dimensions=[f"{table}.category_id"],
                   order_by=[{"field": m, "desc": True}], limit=6)["rows"]
    got = [tuple(r) for r in got if r[0] is not None][:5]
    return {"run": run, "hand": hand, "abi": got, "ready": ready["ready"], "table": table,
            "answer_has": [v for _, v in hand if v in figures(run.answer) or f"{v:,}" in run.answer]}


def test_scenario1_load_model_top5():
    project = f"s1_{time.strftime('%m%d%H%M')}"
    r = scenario1(project)
    assert r["ready"], "model must end Ready"
    assert r["abi"] == [tuple(x) for x in r["hand"]], "ABI's figures equal the hand-written DuckDB query"
    assert len(r["answer_has"]) >= 3, f"answer states the top figures: {r['run'].answer}"
    dest = abi.folder(project)
    for f in (f"sources/{r['table']}.json", f"tables/{r['table']}.json", "model/model.json", "model/measures.json"):
        assert (dest / f).exists(), f
    abi.sync(project)
    head = subprocess.run(["git", "-C", str(abi.ABI_PROJECTS / project), "ls-tree", "-r", "--name-only", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.split()
    for f in head:  # the folder holds exactly ABI's committed bundle
        theirs = subprocess.run(["git", "-C", str(abi.ABI_PROJECTS / project), "show", f"HEAD:{f}"], capture_output=True,
                                text=True, check=True).stdout
        assert (dest / f).read_text() == theirs, f


@pytest.fixture(scope="module")
def yt():
    """Scenarios 2-5 on their own project: the trending videos (the same CSV as scenario 1) and a keyed targets table
    with a few rows, as a person would set it up. Built once; reused while it exists."""
    if not abi.folder("yt"):
        drop = ROOT / "scratch" / "yt"
        drop.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CSV, drop / CSV.name)
        abi.load("demo", "yt", parquet(drop / CSV.name))
        abi.call("create_input_table", project="yt", table="targets", key=["category_id", "month"],
                 columns=[{"name": "category_id", "type": "BIGINT"}, {"name": "month", "type": "VARCHAR"}, {"name": "target", "type": "BIGINT"}])
        abi.call("edit_cells", project="yt", table="targets",
                 add=[{"category_id": c, "month": "2018-07", "target": v} for c, v in ((20, 5_000_000), (24, 9_000_000), (10, 7_000_000))])
        abi.call("set_measure", project="yt", id="target", name="Target", expression="SUM(targets.target)")  # reads the current value
        abi.sync("yt")
    return "yt"


def test_scenario2_why_views_dropped_and_report(yt):
    a = ask("In project yt: which categories (category_id) lost the most views from one month to the next in the last"
            " two months of data? Use total views by month and category.")
    assert a.called("semantic_query") or a.called("explain_change"), "answers through the semantic engine"
    assert "[not verified]" not in a.answer, f"a masked figure fails the answer: {a.answer}"
    b = ask("In project yt build a one-page report with id category_views named 'Views by category': a bar chart of total"
            " views by category_id and a line chart of total views by day. Then sync the folder.")
    assert "category_views" in json.dumps(abi.call("list_reports", project="yt")), [t[0] for t in b.tools]
    assert any(t[0] == "abi_sync" for t in b.tools)


def test_scenario3_writeback_waits_for_the_person(yt):
    r = ask("In project yt: raise the July 2018 target (month 2018-07) in the targets table by 10% for category_id 20."
            " Propose it.", confirm=False)
    text = r.answer.lower()
    assert r.called("propose_edit") or r.called("edit_cells") or r.called("edit_total"), [t[0] for t in r.tools]
    assert "propos" in text or "approv" in text
    rows = abi._open("GET", "/api/projects/yt/tables/targets/rows?offset=0&limit=50")[1]
    assert "5000000" in json.dumps(rows), "nothing changed without the person's yes"


def test_scenario4_forecast_reports_baseline(yt):
    r = ask("In project yt: forecast total views per day for the next 4 weeks. Train and score it through ABI and report"
            " accuracy against the baseline from ml_models.")
    assert r.called("save_ml_model") or r.called("train_model") or abi.call("ml_models", project="yt").get("models")
    assert r.called("ml_models")
    assert "baseline" in r.answer.lower()


def test_scenario5_weekly_email_stops_at_first_send(yt):
    r = ask("In project yt: email me a weekly summary of total views and videos every Monday at 8 (to"
            " someone@example.invalid). Create the flow.", confirm=False)
    flows = abi.call("list_flows", project="yt").get("flows", [])
    assert r.called("save_flow") or flows, [t[0] for t in r.tools]
    assert not any(f.get("enabled") for f in flows), "the flow waits for the person: never enabled by the agent"
    assert "[not verified]" not in r.answer, r.answer


@pytest.fixture(scope="module")
def cafes(tmp_path_factory):
    """Brewline with an ontology (cafés, their orders), as test_ontology builds it."""
    from conftest import drop
    from test_ontology_nobreak import brew
    from test_pages import sure
    p = "pi_objects_live"
    brew(p, tmp_path_factory.mktemp("objects"))
    sure("save_ontology", project=p, ontology={
        "objects": [{"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name", "measures": ["revenue"]},
                    {"id": "order", "table": "orders", "name": "Order", "plural": "Orders", "title": "order_id"}],
        "links": [{"id": "order_cafe", "from": "order", "to": "cafe", "name": "café", "inverse": "orders", "via": ["orders-cafe_id"]}]})
    yield p
    drop([p])


def test_object_question_uses_get_object(cafes):
    """1.2.6 M6 #22: a question about one thing is answered with the object reads (the digest's `objects` line and
    SYSTEM.md point there), and its figures are the object's own rows."""
    name, key, seats = abi.call("sql_query", project=cafes, sql="SELECT cafe_name, cafe_id, seats FROM cafes ORDER BY cafe_id LIMIT 1")["rows"][0]
    r = ask(f"In project {cafes}: tell me about the café {name}: its city, how many seats it has and its revenue.")
    assert r.called("get_object") or r.called("list_objects"), [t[0] for t in r.tools]
    revenue = abi.call("get_object", project=cafes, type="cafe", key=[key])
    assert "[not verified]" not in r.answer and str(seats) in r.answer, r.answer
    assert f"{float(revenue['rows'][0][revenue['columns'].index('revenue')]):,.2f}" in r.answer, r.answer
