"""Every tab of the project explorer, built by the agent through the harness in a throwaway project (ABI_PI_LIVE=1).
One module project: scripted load of a small sales CSV, then one prompt per tab, then sync and render."""
import csv
import os
import json
import random
import re
import time

import pytest

import abi
from pi_driver import ROOT, ask

pytestmark = pytest.mark.live
P = os.environ.get("ABI_PI_SEC_PROJECT") or f"sec_{time.strftime('%m%d%H%M')}"  # set it to rerun on one project
TABS = ["Sources", "Tables", "Model", "Measures", "Glossary", "Queries", "Reports", "ML models", "Flows", "Agents", "Tools",
        "Security", "Environments", "History"]


@pytest.fixture(scope="module")
def project():
    if abi.folder(P):
        return P
    drop = ROOT / "scratch" / P
    drop.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(7)
    with (drop / "sales.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_date", "region", "product", "units", "revenue"])
        for d in range(1, 121):
            for region in ("North", "South", "West"):
                units = rnd.randint(1, 40)
                w.writerow([f"2026-{(d - 1) // 30 + 5:02d}-{(d - 1) % 30 + 1:02d}", region, rnd.choice(["Ale", "Bock", "Cider"]),
                            units, units * rnd.choice([4, 5, 6])])
    abi.load("demo", P, [str(drop / "sales.csv")])
    return P


def files(d: str) -> list[str]:
    abi.sync(P)
    return [f.name for f in (abi.folder(P) / d).glob("*.json")]


def committed(path: str) -> dict:
    abi.sync(P)
    f = abi.folder(P) / path
    return json.loads(f.read_text()) if f.exists() else {}


def test_glossary(project):
    ask(f"In project {P}: add a glossary term 'Revenue': money received from orders, implemented by the revenue measure.")
    assert any(t.get("term", "").lower() == "revenue" for t in committed("model/glossary.json").get("terms", []))


def test_dataset(project):
    ask(f"In project {P}: save a dataset daily_region: revenue and units by calendar day and region.")
    assert "daily_region" in json.dumps(committed("model/datasets.json"))


def test_ml_model(project):
    r = ask(f"In project {P}: build an anomaly-detection ML model on dataset daily_region (create it if missing) and train it.")
    assert r.called("save_ml_model"), [t[0] for t in r.tools]
    assert files("ml")


def test_flow(project):
    r = ask(f"In project {P}: create a flow that alerts me in the app when daily revenue drops below 50. Leave it disabled.",
            confirm=False)
    assert r.called("save_flow")
    assert files("flows")


def test_tool_and_agent(project):
    r = ask(f"In project {P}: create a tool region_summary (input: region; returns revenue and units for that region) with "
            "one test, test it and publish it. Then create a custom agent sales_helper that answers questions about one "
            "region with that tool, with one evaluation case, test it and publish it.", confirm=True)
    # the Lead may hand both to a specialist (its calls aren't in r.tools): what counts is what ABI holds
    assert {t["id"] for t in abi.call("list_tools", project=P)["tools"]} >= {"region_summary"}, [t[0] for t in r.tools]
    assert {a["id"] for a in abi.call("list_agents", project=P)["agents"]} >= {"sales_helper"}, [t[0] for t in r.tools]
    assert "region_summary.json" in files("tools")
    assert "sales_helper.json" in files("agents")


def test_security_rls_yes_roles_no(project):
    r = ask(f"In project {P}: add a row-level security rule so that user staff sees only region North in sales.", confirm=True)
    assert r.called("set_rls_rule"), [t[0] for t in r.tools]
    assert files("security")
    r = ask(f"In project {P}: make staff an editor.", confirm=True)
    assert all(t[2] for t in r.called("set_role")), "roles are a person's click in ABI's app: the harness refuses"
    staff = next(a for a in abi.call("rls_rules", project=P)["accounts"] if a["name"] == "staff")
    assert staff["role"] != "editor"


def test_environment_is_a_hand_edit(project):
    """No action writes environments/<branch>.json: the agent says so, the person edits the file and applies it."""
    r = ask(f"In project {P}: add an environment file for branch main that points source sales at its current location.")
    assert not any(t[0] in ("write", "edit") for t in r.tools), "the agent never writes project folders"
    dest = abi.folder(P)
    src = json.loads((dest / "sources/sales.json").read_text())
    (dest / "environments/main.json").write_text(json.dumps(
        {"$schema": "https://schema.abi.invalid/v1/environment.json", "sources": {"sales": {"location": src["location"]}}}))
    assert abi.cli("apply", P) == 0
    assert "main.json" in files("environments")


def test_hand_edit_of_a_tracked_file_syncs(project):
    dest = abi.folder(P)
    d = json.loads((dest / "model/measures.json").read_text())
    d["measures"][0]["description"] = f"hand edit {time.time():.0f}"
    (dest / "model/measures.json").write_text(json.dumps(d, indent=1))
    assert abi.cli("apply", P) == 0
    abi.sync(P)
    assert abi.git(dest, "status", "--porcelain").strip() == ""
    assert d["measures"][0]["description"] in (dest / "model/measures.json").read_text()


def test_every_tab_renders(project):
    abi.sync(P)
    h = (abi.folder(P).parent / "_views" / P / "index.html").read_text()
    empty = [k for k in TABS if not (m := re.search(rf"<section id='{k}'><h2>[^<]+</h2>(.*?)</section>", h, re.S))
             or "None yet" in m.group(1) or " None " in m.group(1)]
    assert empty == [], f"empty tabs: {empty}"
