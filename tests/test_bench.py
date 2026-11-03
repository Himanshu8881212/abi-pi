"""bench/bench.py without a model: a stand-in for Pi does each task's work through ABI's actions, right in the first run
and wrong in the second, so every check passes once and fails once against ABI and the known answers (needs the engine)."""
import json
import re
import sys

import abi
from pi_driver import ROOT, Run

sys.path.insert(0, str(ROOT / "bench"))
import bench  # noqa: E402


def figures(p: str, dim: str, **q) -> str:
    rows = abi.call("semantic_query", project=p, measures=["revenue"], dimensions=[dim], **q)["rows"]
    return "; ".join(f"{k}: {v:,.2f}" for k, v in rows)


def report(p: str, rid: str, right: bool) -> None:
    v = lambda i, typ, dims, x: {"id": i, "type": typ, "query": {"measures": ["revenue"], "dimensions": dims},  # noqa: E731
                                 "layout": {"x": x, "y": 0, "w": 4, "h": 4}}
    visuals = [v("kpi", "kpi", [], 0), v("bar", "bar", ["cafes.region" if right else "cafes.city"], 4)]
    abi.call("save_report", project=p, report={"id": rid, "name": "Managers", "pages": [
        {"id": "p1", "name": "Overview", "visuals": visuals + [v("line", "line", ["calendar.month"], 8)] * right}]})


def pi(prompt: str) -> Run:
    """The first run of a task does it as asked; the second answers for every year, masks a figure, leaves the line
    out or loads nothing."""
    p = re.search(r"project (\w+)", prompt)[1]
    kind = "load" if prompt.startswith("Load") else next(k for k in ("why", "report", "2025") if k in prompt)  # only load's has a path
    again = kind in pi.seen
    pi.seen.append(kind)
    if kind == "why":
        answer = f"Revenue fell by {'[not verified]' if again else 'a seventh'} in December 2025, mostly in North."
    elif kind == "2025":
        answer = figures(p, "cafes.region", **({} if again else {"filters": [{"column": "calendar.year", "op": "=", "value": 2025}]}))
    elif kind == "report":
        rid = re.search(r"id (\w+)", prompt)[1]
        report(p, rid, right=not again)
        answer = f"Built report {rid}."
    elif again:
        answer = "I couldn't load it."
    else:
        abi.load("demo", p, [re.search(r"file (\S+)", prompt)[1]])
        answer = figures(p, "brewline.region")
    return Run(prompt, answer=answer, tools=[("mcp__abi__semantic_query", {}, False, 5, "")], model="test/stand-in")


def test_every_task_passes_on_the_right_work_and_fails_on_the_wrong(tmp_path, monkeypatch):
    pi.seen = []
    monkeypatch.setattr(bench, "ask", pi)
    monkeypatch.setattr(bench, "RESULTS", tmp_path / "RESULTS.md")
    monkeypatch.setattr(bench, "RUNS", tmp_path)
    monkeypatch.setenv("ABI_PI_LIVE_MODEL", "test/stand-in")  # the bench sets both for Pi: put them back after
    monkeypatch.delenv("ABI_PI_LIVE_FALLBACK", raising=False)
    bench.main(["--runs", "2"])
    (log,) = tmp_path.glob("bench-*.jsonl")
    runs = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(x["task"], x["ok"]) for x in runs] == [(t, ok) for t in ("ask", "why", "report", "load") for ok in (True, False)]
    assert [x["masked"] for x in runs if x["task"] == "why"] == [0, 1]
    table = (tmp_path / "RESULTS.md").read_text()
    assert ": test/stand-in (runs=2; answered by test/stand-in in 8 of 8)" in table, table
    assert all(f"| {t} | 1/2 |" in table for t in ("ask", "why", "report", "load")), table
    made = "bench_" + log.stem.removeprefix("bench-")
    assert not [x for x in abi.call("list_projects")["projects"] if x["project"].startswith(made)]  # the shared one and the loaded one
    assert not (ROOT / "scratch" / made).exists() and not list(abi.WORKSPACES.glob(f"*/{made}*"))
