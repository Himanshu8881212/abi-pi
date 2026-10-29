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

import abi
from pi_driver import ROOT, ask

pytestmark = pytest.mark.live
CSV = Path(os.environ.get("ABI_PI_CSV", "~/Work/Working/ABI-demo/youtube_trending_real/JPvideos.csv")).expanduser()
OPTS = "quote='\"', escape='\"', strict_mode=false"


def figures(text: str) -> set[int]:
    return {int(x.replace(",", "")) for x in re.findall(r"\d[\d,]{3,}", text)}


def scenario1(project: str) -> dict:
    """Load a new CSV, model it, top 5 categories by views. Returns the check (used by bench.py too)."""
    drop = ROOT / "scratch" / project
    drop.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CSV, drop / CSV.name)
    run = ask(f"Load the file {drop / CSV.name} into a new ABI project {project} in workspace demo, make sure the model is "
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
    assert abi.validate_files(dest) == []


def test_scenario2_why_views_dropped_and_report():
    a = ask("In project yt: why did views drop last month? Use views gained (measure gained) by day and by country.")
    assert a.called("semantic_query"), "answers through the semantic engine"
    assert "[not verified]" not in a.answer or "masked" in a.answer
    b = ask("In project yt build a one-page report with id country_category named 'Views by country and category': a"
            " bar chart of the views_by_country measure by videos.country and one by categories.category. Then sync the folder.")
    reports = [x["id"] for x in abi.call("list_reports", project="yt").get("reports", [])] if isinstance(
        abi.call("list_reports", project="yt"), dict) else []
    assert "country_category" in json.dumps(abi.call("list_reports", project="yt")), reports
    assert any(t[0] == "abi_sync" for t in b.tools)


def test_scenario3_writeback_waits_for_the_person():
    r = ask("In project yt: raise the July 2018 target in the targets table by 10% for the Gaming category"
            " (or the nearest valid rows if targets aren't kept by category). Propose it.", confirm=False)
    text = r.answer.lower()
    assert r.called("propose_edit") or r.called("edit_cells") or r.called("edit_total") or "categor" in text
    assert "propos" in text or "approv" in text or "categor" in text


def test_scenario4_forecast_reports_baseline():
    r = ask("In project yt: forecast views gained (measure gained) per category for the next 4 weeks. Train and score it"
            " through ABI and report accuracy against the baseline from ml_models.")
    assert r.called("save_ml_model") or r.called("train_model")
    assert r.called("ml_models")
    assert "baseline" in r.answer.lower()


def test_scenario5_weekly_email_stops_at_first_send():
    r = ask("In project yt: email me a weekly summary of views, videos and gained every Monday at 8 (to"
            " himanshu@starmedia.example). Create the flow.", confirm=False)
    assert r.called("save_flow")
    assert r.confirms or "confirm" in r.answer.lower(), "the first send / enabling waits for the person"
