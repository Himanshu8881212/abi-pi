"""bench/bench.py without a model: a stand-in for Pi does each task's work through ABI's actions, right in the first run
and wrong in the others, so every check passes once and fails on each kind of wrong work against ABI and the known
answers (needs the engine). The answer checks also run alone on answers that look right and aren't (no engine)."""
import json
import re
import sys

import abi
from pi_driver import ROOT, Run

sys.path.insert(0, str(ROOT / "bench"))
import bench  # noqa: E402

KNOWN = {"revenue_by_region_year": {"East": {"2025": 40774.45}, "North": {"2025": 36360.52}, "South": {"2025": 39285.46},
                                    "West": {"2025": 41718.55}},  # seed 42, scale 0.01 (bench/brewline.py)
         "why": {"revenue": {"2025-11": 14095.04, "2025-12": 12043.64}, "dimension": "region", "member": "North",
                 "member_revenue": {"2025-11": 3271.28, "2025-12": 1586.08}}}
RETAIL = {**KNOWN, "why": {"revenue": {"2025-11": 14059.04, "2025-12": 11303.05}, "dimension": "category", "member": "Retail",
                           "member_revenue": {"2025-11": 4719.6, "2025-12": 2145.7}}}  # brewline.py --plant retail


def test_answers_that_look_right_and_arent_fail():
    """M0 review: any figure anywhere passed ask, and any mention of North passed why."""
    assert bench.ask_ok("| Region | Revenue |\n|---|---|\n| West | 41,718.55 |\n| East | 40,774.45 |\n| South | 39,285.46 |\n"
                        "| North | 36,360.52 |", KNOWN)
    assert not bench.ask_ok("Revenue in 2025: West 36,360.52; East 39,285.46; South 40,774.45; North 41,718.55.", KNOWN)
    why = ("Revenue fell from 14,095.04 in November 2025 to 12,043.64 in December. Most of the fall is in the North region:"
           " it went from 3,271.28 to 1,586.08, while East, South and West dipped slightly.")
    assert bench.why_ok(why, KNOWN)
    assert not bench.why_ok(why.replace("the North region:", "the East region. North"), KNOWN), "the wrong driver"
    assert not bench.why_ok(why.replace("14,095.04", "13,100.00"), KNOWN), "the wrong months"
    assert not bench.why_ok(why.replace("1,586.08", "2,986.08"), KNOWN), "the driver's figures"
    assert not bench.why_ok("Revenue rose from October to November 2025, driven by the East region. North was flat (3,271.28)."
                            " Region | Nov: East 3,673.46, North 3,271.28, South 3,542.79, West 3,607.51", KNOWN)
    assert not bench.clean(bench.WITHHELD) and not bench.clean("Revenue was [not verified].") and bench.clean(why)
    # --plant retail: the driver is a product category, so a region (any region) named as the driver fails
    retail = ("Revenue fell from 14,059.04 in November 2025 to 11,303.05 in December. Most of the fall is in Retail: it went"
              " from 4,719.60 to 2,145.70, across every region, while Coffee, Bakery, Food and Tea dipped slightly.")
    assert bench.why_ok(retail, RETAIL) and not bench.why_ok(retail, KNOWN)
    assert not bench.why_ok(retail.replace("in Retail:", "in the North region. Retail"), RETAIL), "a region as the driver"
    assert not bench.why_ok(retail.replace("in Retail:", "in Coffee. Retail"), RETAIL), "the wrong category"
    footer = "\n\n_Grounding: 2 figure(s) or name(s) no tool result backs were masked._"
    assert bench.masked("from 2025-01-01 to [not verified]" + footer) == 2 and bench.masked("[not verified] and [not verified]") == 2


def figures(p: str, dim: str, **q) -> list:
    return abi.call("semantic_query", project=p, measures=["revenue"], dimensions=[dim], **q)["rows"]


def month(p: str, m: int) -> dict:
    return dict(figures(p, "cafes.region", filters=[{"column": "calendar.year", "op": "=", "value": 2025},
                                                    {"column": "calendar.month", "op": "=", "value": m}]))


def report(p: str, rid: str, line: list) -> None:
    v = lambda i, typ, dims, x: {"id": i, "type": typ, "query": {"measures": ["revenue"], "dimensions": dims},  # noqa: E731
                                 "layout": {"x": x, "y": 0, "w": 4, "h": 4}}
    abi.call("save_report", project=p, report={"id": rid, "name": "Managers", "pages": [
        {"id": "p1", "name": "Overview", "visuals": [v("kpi", "kpi", [], 0), v("bar", "bar", ["cafes.region"], 4), v("line", "line", line, 8)]}]})


def pi(prompt: str) -> Run:
    """The first run of a task does it as asked. Then ask puts the figures under the wrong regions, then changes the
    project while it answers; why names the wrong region as the driver; report draws the line on the month number
    alone, then has its answer withheld; load loads nothing."""
    p = re.search(r"project (\w+)", prompt)[1]
    kind = "load" if prompt.startswith("Load") else next(k for k in ("why", "report", "2025") if k in prompt)  # only load's has a path
    k = pi.seen.count(kind)
    pi.seen.append(kind)
    if kind == "2025":
        rows = figures(p, "cafes.region", filters=[{"column": "calendar.year", "op": "=", "value": 2025}])
        names = [r[0] for r in rows][1:] + [rows[0][0]] if k == 1 else [r[0] for r in rows]
        answer = "; ".join(f"{n}: {v:,.2f}" for n, (_, v) in zip(names, rows))
        if k == 2:
            abi.call("set_measure", project=p, id="bench_probe", name="Probe", expression="SUM(orders.revenue)")
    elif kind == "why":
        nov, dec = month(p, 11), month(p, 12)
        top, calm = (f(nov, key=lambda r: abs(dec[r] - nov[r])) for f in (max, min))
        answer = (f"Revenue fell from {sum(nov.values()):,.2f} in November 2025 to {sum(dec.values()):,.2f} in December. Most of"
                  f" the fall is in {calm if k else top}. {top} went from {nov[top]:,.2f} to {dec[top]:,.2f}.")
    elif kind == "report":
        rid = re.search(r"id (\w+)", prompt)[1]
        report(p, rid, ["calendar.month"] if k == 1 else ["calendar.year", "calendar.month"])
        answer = bench.WITHHELD if k == 2 else f"Built report {rid}."
    elif k:
        answer = "I couldn't load it."
    else:
        abi.load("demo", p, [re.search(r"file (\S+)", prompt)[1]])
        answer = "; ".join(f"{n}: {v:,.2f}" for n, v in figures(p, "brewline.region"))
    return Run(prompt, answer=answer, tools=[("mcp__abi__semantic_query", {}, False, 5, "")], model="test/stand-in")


def test_every_task_passes_on_the_right_work_and_fails_on_the_wrong(tmp_path, monkeypatch):
    pi.seen = []
    monkeypatch.setattr(bench, "ask", pi)
    monkeypatch.setattr(bench, "RESULTS", tmp_path / "RESULTS.md")
    monkeypatch.setattr(bench, "RUNS", tmp_path)
    monkeypatch.setenv("ABI_PI_LIVE_MODEL", "test/stand-in")  # the bench sets both for Pi: put them back after
    monkeypatch.delenv("ABI_PI_LIVE_FALLBACK", raising=False)
    bench.main(["--runs", "ask=3,why=2,report=3,load=2"])
    (log,) = tmp_path.glob("bench-*.jsonl")
    runs = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(x["task"], x["ok"]) for x in runs] == [(t, i == 0) for t, n in (("ask", 3), ("why", 2), ("report", 3), ("load", 2)) for i in range(n)]
    assert [x["withheld"] for x in runs if x["task"] == "report"] == [False, False, True]
    assert all(len(x["load"]) == 2 and x["masked"] == 0 for x in runs)
    table = (tmp_path / "RESULTS.md").read_text()
    assert ": test/stand-in (runs: ask 3, why 2, report 3, load 2; answered by test/stand-in in 10 of 10; load average" in table, table
    assert all(f"| {t} | 1/{n} | {m} |" in table for t, n, m in (("ask", 3, 0), ("why", 2, 0), ("report", 3, 1), ("load", 2, 0))), table
    made = "bench_" + log.stem.removeprefix("bench-")
    assert not [x for x in abi.call("list_projects")["projects"] if x["project"].startswith(made)]  # the shared one and the loaded one
    assert not (ROOT / "scratch" / made).exists() and not list(abi.WORKSPACES.glob(f"*/{made}*"))
