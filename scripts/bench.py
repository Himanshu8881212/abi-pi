#!/usr/bin/env python3
"""Benchmark the harness on fixed tasks: wall time, turns, tool calls, tokens, grounding flags, pass/fail.

    .venv/bin/python scripts/bench.py [--runs 2] [--variant name=--thinking,low ...] [--tasks ask,why,report,load]

Writes bench/<stamp>.json and appends a summary table to bench/RESULTS.md."""
import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "scripts")]
import abi  # noqa: E402
from pi_driver import ask  # noqa: E402


def t_ask(extra, env):
    r = ask("In project yt: which 3 countries have the most views gained (measure gained)? Give the figures.", extra=extra, env=env)
    rows = [t[4] for t in r.called("semantic_query")]
    ok = bool(rows) and all(c in r.answer for c in ("US", "GB", "IN")) and "[not verified]" not in r.answer
    return r, ok


def t_why(extra, env):
    r = ask("In project yt: why did views gained drop in the last month of data compared with the month before?", extra=extra, env=env)
    # masked figures are the check working; pass = answered from queries, not withheld, at most one figure masked
    return r, bool(r.called("semantic_query")) and r.answer.count("[not verified]") <= 1 and "won't state it" not in r.answer


def t_report(extra, env):
    rid = f"bench_{int(time.time())}"
    r = ask(f"In project pi_smoke build a report with id {rid}: KPIs of views and likes and a line chart of views by "
            "calendar.day. Then sync the folder and tell me the path of its HTML view.", extra=extra, env=env)
    ok = rid in json.dumps(abi.call("list_reports", project="pi_smoke")) and (ROOT / f"workspaces/demo/_views/pi_smoke/reports/{rid}.html").exists()
    return r, ok


def t_load(extra, env):
    from test_scenarios import scenario1
    c = scenario1(f"b_{time.strftime('%m%d%H%M%S')}")
    return c["run"], c["ready"] and c["abi"] == [tuple(x) for x in c["hand"]] and len(c["answer_has"]) >= 3


TASKS = {"ask": t_ask, "why": t_why, "report": t_report, "load": t_load}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--tasks", default="ask,why,report")
    ap.add_argument("--variant", action="append", default=[], help="name=arg,arg (extra pi args) or name=ENV:K=V")
    a = ap.parse_args()
    variants = {"baseline": ([], {})}
    for v in a.variant:
        name, _, spec = v.partition("=")
        if spec.startswith("ENV:"):
            k, _, val = spec[4:].partition("=")
            variants[name] = ([], {k: val})
        else:
            variants[name] = ([x for x in spec.split(",") if x], {})
    out = []
    for vname, (extra, env) in variants.items():
        for task in a.tasks.split(","):
            for i in range(a.runs):
                r, ok = TASKS[task](extra, env)
                rec = {"variant": vname, "task": task, "run": i, "ok": ok, "seconds": r.seconds, "turns": r.usage["turns"],
                       "tools": len(r.tools), "tool_errors": sum(1 for t in r.tools if t[2]), "input": r.usage["input"],
                       "cached": r.usage["cacheRead"], "output": r.usage["output"],
                       "masked": "[not verified]" in r.answer, "tool_names": [t[0] for t in r.tools]}
                out.append(rec)
                print(json.dumps({k: v for k, v in rec.items() if k != "tool_names"}), flush=True)
    (ROOT / "bench").mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (ROOT / "bench" / f"{stamp}.json").write_text(json.dumps(out, indent=1))
    lines = [f"\n## {stamp} (runs={a.runs})\n", "| variant | task | pass | median s | turns | tools | tool errors | input tok | cached tok | output tok |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for v in variants:
        for task in a.tasks.split(","):
            rs = [x for x in out if x["variant"] == v and x["task"] == task]
            med = lambda k: statistics.median(x[k] for x in rs)  # noqa: E731
            lines.append(f"| {v} | {task} | {sum(x['ok'] for x in rs)}/{len(rs)} | {med('seconds')} | {med('turns')} | {med('tools')} |"
                         f" {med('tool_errors')} | {med('input')} | {med('cached')} | {med('output')} |")
    with open(ROOT / "bench" / "RESULTS.md", "a") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
