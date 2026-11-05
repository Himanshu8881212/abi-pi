#!/usr/bin/env python3
"""The agent team, live: one job bigger than a few steps, given to the Lead as a person would, and what the team did.

    .venv/bin/python bench/team_live.py [--model provider/model] [--fallback p/m,...]

The job needs all nine specialists (harness/team.json): load a raw Brewline export (data-engineer), model it with
revenue and order measures (analytics-engineer), a managers' report (analyst), a three-month revenue forecast
(ml-engineer), a Monday 07:00 flow that emails the report, saved switched off (automation-engineer), a custom agent
for café managers with an evaluation set (agent-builder), a review (reviewer), the work committed with release notes
(release-ops) and a board pack (board-pack). Recorded (one JSON line, also in logs/team-<stamp>.json): the Lead's plan
(harness/team.json's agents per task), the subagent calls in order, each handoff and its checks from the team journal,
the steps waiting for the person's yes, and whether the result is right: the model Ready with revenue by region equal
to the generator's known answers to the cent, the report's three visuals, the flow saved and switched off. Nothing is
confirmed for the agent (a step that needs a yes waits, as it would for the person). The project goes at the end."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests"), str(ROOT / "bench")]
import abi  # noqa: E402
import bench  # noqa: E402 (its data, checks and known answers)
from conftest import drop  # noqa: E402
from pi_driver import ask  # noqa: E402


def team_files(p: str) -> tuple:
    d = abi.WORKSPACES / "demo" / "_team" / p
    plan = json.loads((d / "plan.json").read_text()) if (d / "plan.json").exists() else None
    journal = [json.loads(x) for x in (d / "journal.jsonl").read_text().splitlines() if x.strip()] if (d / "journal.jsonl").exists() else []
    pending = json.loads((d / "pending.json").read_text()) if (d / "pending.json").exists() else []
    return plan, journal, pending


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default=os.environ.get("ABI_PI_LIVE_MODEL"))
    ap.add_argument("--fallback", default=os.environ.get("ABI_PI_LIVE_FALLBACK"))
    a = ap.parse_args(argv)
    os.environ.update({k: v for k, v in (("ABI_PI_LIVE_MODEL", a.model), ("ABI_PI_LIVE_FALLBACK", a.fallback)) if v})
    stamp = time.strftime("%m%d%H%M%S")
    p, out = f"bench_team_{stamp}", ROOT / "scratch" / f"bench_team_{stamp}"
    c = bench.data(out)
    want = bench.totals(c["known"])
    job = (f"Load the file {c['flat']} into a new ABI project {p} in workspace demo. Then, as a team: build the model"
           " with measures for revenue and for the number of orders; make a managers' report with a revenue KPI, revenue"
           " by region and revenue by month; forecast revenue for the next three months; save a flow that emails that"
           " report every Monday at 07:00 to managers@example.com (saved switched off: I'll turn it on myself); build a"
           " custom agent that answers café managers' questions about their revenue, with an evaluation set; have all of"
           " it reviewed; commit the work with release notes; and prepare a board pack with a one-page summary and"
           " commentary for the quarterly review. Tell me when it's done and what waits for my yes.")
    nine = ["data-engineer", "analytics-engineer", "analyst", "ml-engineer", "automation-engineer", "agent-builder",
            "reviewer", "release-ops", "board-pack"]
    rec = {"bench": "team", "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "project": p, "model": a.model}
    try:
        r = ask(job, timeout=5400)
        plan, journal, pending = team_files(p)
        subs = [(t[1] or {}) for t in r.called("subagent")]
        reports = [x for x in (abi.call("list_reports", project=p).get("reports") or [])] if abi.folder(p) else []
        flows = (abi.call("list_flows", project=p).get("flows") or []) if abi.folder(p) else []
        ml = (abi.call("ml_models", project=p).get("models") or []) if abi.folder(p) else []
        agents = (abi.call("list_agents", project=p).get("agents") or []) if abi.folder(p) else []
        commits = len(abi.call("history", project=p).get("commits") or []) if abi.folder(p) else 0
        packs = abi.folder(p).parent / "_app" / p / "packs" if abi.folder(p) else None
        rec |= {
            "seconds": round(r.seconds, 1), "turns": r.usage.get("turns"), "tools": len(r.tools),
            "tool_errors": sum(1 for t in r.tools if t[2]), "masked": bench.masked(r.answer), "model_used": r.model,
            "plan": [{k: t.get(k) for k in ("id", "agent", "area", "status", "attempts")} for t in (plan or {}).get("tasks", [])],
            "delegated": [s.get("agent") for s in subs],
            "specialists_missing": [n for n in nine if n not in {s.get("agent") for s in subs}],
            "handoffs": [{k: j.get(k) for k in ("agent", "task_id", "status", "ok", "check", "passed") if k in j} for j in journal],
            "pending": pending, "confirms_asked": [t for t, _ in r.confirms],
            "right": {"model_ready_and_exact": bench.loaded(p, want),
                      "report_visuals": [len(pg.get("visuals", [])) for rp in reports for pg in rp.get("pages", [])],
                      "flows": [{"id": f.get("id"), "enabled": f.get("enabled")} for f in flows],
                      "ml_models": len(ml), "custom_agents": len(agents), "commits": commits,
                      "board_packs": len(list(packs.iterdir())) if packs and packs.exists() else 0},
            "answer": r.answer[:1500],
        }
    finally:
        drop([p])
    print(json.dumps(rec), flush=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / f"team-{stamp}.json").write_text(json.dumps(rec, indent=1))
    return rec


if __name__ == "__main__":
    main()
