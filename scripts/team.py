#!/usr/bin/env python3
"""The agent team's rules, in one place (harness/team.json): the Lead's plan, the delegation guard, each specialist's
briefing, the handoff (journal + post-checks against ABI) and the confirmations specialists can't ask for.

Files per project, next to the views (outside ABI's folder, git-ignored): workspaces/<ws>/_team/<project>/
  plan.json        the Lead's plan: tasks {id, agent, area, task, depends_on} with status and attempts
  brief.md         goal, conventions, open questions (the Lead keeps it)
  journal.jsonl    every handoff and every check result, in order
  pending.json     steps a specialist couldn't run because they need the person's yes

    team.py validate|plan <project>            (plan JSON on stdin)
    team.py guard <project> <agent>            (task text on stdin) -> {"ok", "reason", "task"}
    team.py brief <project> <agent> <task_id>
    team.py handoff <project> <agent>          (handoff JSON on stdin) -> checks and status
    team.py pending <project> [done <id>]
    team.py status <project>
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import abi  # noqa: E402

TEAM = json.loads((abi.ROOT / "harness" / "team.json").read_text())
AGENTS, PHASES = TEAM["agents"], TEAM["phases"]
TASK_REF = re.compile(r"\bPlan task ([A-Za-z0-9_-]+)")
MAX_ATTEMPTS = 3
KINDS = {"table", "measure", "glossary", "relationship", "report", "dataset", "ml", "flow", "tool", "agent", "commit", "doc", "pack", "other"}


def tdir(project: str) -> Path:
    dest = abi.folder(project)
    if not dest:
        raise SystemExit(f"No folder for project '{project}'.")
    d = dest.parent / "_team" / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read(project: str, name: str, default):
    f = tdir(project) / name
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return default


def write(project: str, name: str, value) -> None:
    (tdir(project) / name).write_text(json.dumps(value, indent=1))


def log(project: str, entry: dict) -> None:
    with (tdir(project) / "journal.jsonl").open("a") as f:
        f.write(json.dumps({"t": now(), **entry}) + "\n")


def journal(project: str, n: int = 200) -> list[dict]:
    f = tdir(project) / "journal.jsonl"
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()] if f.exists() else []
    return rows[-n:]


# ---------- the plan ----------

def validate(plan: dict) -> list[str]:
    """A plan is a list of tasks. Each goes to the agent that owns its area; work of a later phase depends (directly
    or through others) on every task of an earlier phase, so the order is explicit and checkable."""
    errs, tasks = [], plan.get("tasks") or []
    if not tasks:
        return ["The plan has no tasks."]
    ids = [t.get("id") for t in tasks]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids):
        errs.append("Task ids must be unique words (letters, digits, - and _).")
    by = {t.get("id"): t for t in tasks}
    for t in tasks:
        a = AGENTS.get(t.get("agent"))
        if not a:
            errs.append(f"{t.get('id')}: unknown agent '{t.get('agent')}' (team: {', '.join(AGENTS)}).")
            continue
        if t.get("area") not in a["areas"]:
            owner = next((n for n, x in AGENTS.items() if t.get("area") in x["areas"]), None)
            errs.append(f"{t['id']}: area '{t.get('area')}' belongs to {owner or 'no agent'}, not {t['agent']} ({', '.join(a['areas'])}).")
        if not (t.get("task") or "").strip():
            errs.append(f"{t['id']}: describe the task.")
        for d in t.get("depends_on") or []:
            if d not in by:
                errs.append(f"{t['id']}: depends on unknown task '{d}'.")
    if errs:
        return errs

    def deps(i: str, seen=None) -> set:
        seen = seen if seen is not None else set()
        for d in by[i].get("depends_on") or []:
            if d not in seen:
                seen.add(d)
                deps(d, seen)
        return seen
    phase = {t["id"]: PHASES.index(AGENTS[t["agent"]]["phase"]) for t in tasks}
    for t in tasks:
        if t["id"] in deps(t["id"]):
            errs.append(f"{t['id']}: depends on itself through others.")
            continue
        missing = [o["id"] for o in tasks if phase[o["id"]] < phase[t["id"]] and o["id"] not in deps(t["id"])]
        if missing:
            errs.append(f"{t['id']} ({AGENTS[t['agent']]['phase']}) must come after {', '.join(missing)}: add them to depends_on.")
    return errs


def save_plan(project: str, plan: dict) -> dict:
    errs = validate(plan)
    if errs:
        return {"ok": False, "errors": errs}
    old = {t["id"]: t for t in (read(project, "plan.json", {}) or {}).get("tasks", [])}
    for t in plan["tasks"]:  # a revised plan keeps what already passed
        prev = old.get(t["id"])
        t.update({"status": prev["status"], "attempts": prev.get("attempts", 0)} if prev and prev.get("agent") == t["agent"] else
                 {"status": "pending", "attempts": 0})
    plan = {"project": project, "goal": plan.get("goal", ""), "tasks": plan["tasks"], "updated": now()}
    write(project, "plan.json", plan)
    log(project, {"kind": "plan", "agent": "lead", "tasks": [{k: t[k] for k in ("id", "agent", "area")} for t in plan["tasks"]]})
    return {"ok": True, "tasks": [{k: t.get(k) for k in ("id", "agent", "area", "status", "depends_on")} for t in plan["tasks"]]}


def set_status(project: str, task_id: str, status: str) -> None:
    plan = read(project, "plan.json", {})
    for t in plan.get("tasks", []):
        if t["id"] == task_id:
            t["status"] = status
    write(project, "plan.json", plan)


# ---------- the delegation guard ----------

def guard(project: str, agent: str, text: str) -> dict:
    """A delegation is allowed only for a task of the plan, to its agent, once what it depends on has passed its
    checks, while no other running task works on the same area; at most MAX_ATTEMPTS tries."""
    plan = read(project, "plan.json", None)
    if not plan:
        return {"ok": False, "reason": "No plan yet: make one with abi_plan before delegating."}
    m = TASK_REF.search(text or "")
    if not m:
        return {"ok": False, "reason": "Start the task with 'Plan task <id>:' (an id from your plan)."}
    by = {t["id"]: t for t in plan["tasks"]}
    t = by.get(m.group(1))
    if not t:
        return {"ok": False, "reason": f"Plan task {m.group(1)} is not in the plan."}
    if t["agent"] != agent:
        return {"ok": False, "reason": f"Plan task {t['id']} belongs to {t['agent']}, not {agent}."}
    if t["status"] == "passed":
        return {"ok": False, "reason": f"Plan task {t['id']} already passed its checks."}
    if t["status"] == "running":
        return {"ok": False, "reason": f"Plan task {t['id']} is already running."}
    waiting = [d for d in t.get("depends_on") or [] if by[d]["status"] != "passed"]
    if waiting:
        return {"ok": False, "reason": f"Plan task {t['id']} waits for {', '.join(waiting)} to pass its checks."}
    clash = [o["id"] for o in plan["tasks"] if o["status"] == "running" and o["area"] == t["area"]]
    if clash:
        return {"ok": False, "reason": f"{', '.join(clash)} is already working on {t['area']}."}
    if t.get("attempts", 0) >= MAX_ATTEMPTS:
        return {"ok": False, "reason": f"Plan task {t['id']} failed {MAX_ATTEMPTS} times: ask the person."}
    t["status"], t["attempts"] = "running", t.get("attempts", 0) + 1
    write(project, "plan.json", plan)
    log(project, {"kind": "delegate", "agent": agent, "task": t["id"], "attempt": t["attempts"]})
    return {"ok": True, "task": t["id"]}


# ---------- the briefing a specialist starts with ----------

def brief(project: str, agent: str, task_id: str) -> str:
    plan = read(project, "plan.json", {})
    by = {t["id"]: t for t in plan.get("tasks", [])}
    t, a = by.get(task_id), AGENTS.get(agent, {})
    lines = [f"Team briefing for {agent} on project {project}. You are one of the Lead's specialists: {a.get('description', '')}"]
    f = tdir(project) / "brief.md"
    if f.exists():
        lines += ["", "Project brief (from the Lead):", f.read_text().strip()]
    if plan.get("goal"):
        lines += ["", f"Goal of the plan: {plan['goal']}"]
    if t:
        lines += ["", f"Your task {t['id']} ({t['area']}): {t['task']}",
                  f"When you finish, the harness checks: {', '.join(a.get('checks', []))}. Report each object you made in the handoff's 'done' list with its kind and ID."]
    related = {d for d in (t or {}).get("depends_on") or []} | {o["id"] for o in plan.get("tasks", []) if t and o["area"] == t["area"] and o["id"] != t["id"]}
    hand = [j for j in journal(project) if j.get("kind") == "handoff" and j.get("task") in related]
    if hand:
        lines += ["", "What the team did before you (handoffs your task builds on):"]
        for j in hand[-6:]:
            h = j.get("handoff", {})
            lines.append(f"- {j['task']} by {j['agent']} ({j.get('result')}): {h.get('summary', '')}")
            for k in ("decisions", "assumptions", "open_issues"):
                if h.get(k):
                    lines.append(f"  {k}: " + "; ".join(map(str, h[k]))[:600])
            if h.get("next"):
                lines.append(f"  for the next agent: {h['next'][:600]}")
            if h.get("done"):
                lines.append("  objects: " + ", ".join(f"{d.get('kind')} {d.get('id')}" for d in h["done"])[:600])
    try:
        import vault  # noqa: PLC0415
        docs = vault.listing(project)
        if docs:
            lines += ["", "The vault (abi_vault search/read): " + "; ".join(f"{d['name']} ({', '.join(d['sections'][:8])})" for d in docs)]
    except SystemExit:
        pass
    lines += ["", "Rules for specialists: you can't ask the person anything. A step that needs their yes: don't retry it; put it in",
              "pending_confirmations (action, args, why) and go on with the rest. Finish with one abi_handoff call."]
    return "\n".join(lines)


# ---------- handoff: validate, journal, post-checks ----------

def _rows(action: str, **kw):
    try:
        return abi.call(action, **kw)
    except abi.ActionError as e:
        return {"error": str(e.body.get("message") if isinstance(e.body, dict) else e.body)}


def check(project: str, name: str, done: list[dict], h: dict) -> dict:
    """Deterministic checks against ABI, per role: the work the handoff claims is there and works."""
    ids = lambda kind: [d["id"] for d in done if d.get("kind") == kind and d.get("id")]  # noqa: E731
    bad: list[str] = []
    if name == "tables_exist":
        q, sm = _rows("data_quality", project=project), _rows("semantic_model", project=project)
        have = {r[q["columns"].index("table")]: r[q["columns"].index("rows")] for r in q.get("rows", [])} if "rows" in q else {}
        cols = (sm.get("columns") or {}) if isinstance(sm, dict) else {}
        for tid in ids("table"):
            if tid not in have and tid not in cols:
                bad.append(f"table {tid} isn't in the project")
            elif have.get(tid) == 0:
                bad.append(f"table {tid} has no rows")
        if not ids("table"):
            bad.append("list every table this task covers in done, also the ones you reused unchanged: {kind: 'table', id, what: 'reused'}; the harness checks each exists and has rows")
    elif name == "model_ready":
        r = _rows("readiness", project=project)
        if not r.get("ready"):
            bad += [f"readiness: {m}" for m in (r.get("missing") or [r.get("error", "not ready")])][:8]
    elif name == "measures_work":
        for m in ids("measure"):
            r = _rows("semantic_query", project=project, measures=[m], limit=1)
            if "error" in r and "date dimension" in r["error"]:  # time intelligence (PREVIOUS_YEAR, YTD…) needs a date grain
                r = _rows("semantic_query", project=project, measures=[m], dimensions=["calendar.year", "calendar.month"], limit=1)
            if "error" in r:
                bad.append(f"measure {m}: {r['error'][:200]}")
    elif name == "reports_work":
        reps = {r["id"]: r for r in (_rows("list_reports", project=project).get("reports") or [])}
        for rid in ids("report"):
            if rid not in reps:
                bad.append(f"report {rid} doesn't exist")
                continue
            for p in reps[rid].get("pages", []):
                for v in p.get("visuals", [])[:16]:
                    if v.get("type") == "text":
                        continue
                    r = _rows("visual_data", project=project, report=rid, page=p["id"], visual=v["id"])
                    if "error" in r:
                        bad.append(f"report {rid} visual {v['id']}: {r['error'][:160]}")
        if not ids("report") and not h.get("checks"):
            bad.append("the handoff lists no report and no analysis checks")
    elif name == "ml_trained":
        models = {m["id"]: m for m in (_rows("ml_models", project=project).get("models") or [])}
        for mid in ids("ml"):
            run = (models.get(mid) or {}).get("latest_run") or {}
            if mid not in models:
                bad.append(f"ML model {mid} doesn't exist")
            elif run.get("status") != "ok":
                bad.append(f"ML model {mid}: latest run {run.get('kind', 'none')} is {run.get('status', 'missing')}")
    elif name == "flows_saved_off":
        flows = {f["id"]: f for f in (_rows("list_flows", project=project).get("flows") or [])}
        for fid in ids("flow"):
            if fid not in flows:
                bad.append(f"flow {fid} doesn't exist")
            elif flows[fid].get("enabled"):
                bad.append(f"flow {fid} is enabled: save it off; the person turns it on")
    elif name == "agents_tested":
        for tid in ids("tool"):
            r = _rows("test_tool", project=project, tool=tid)
            if not r.get("passed"):
                bad.append(f"tool {tid}: tests don't pass ({r.get('error', 'see test_tool')[:120]})")
        for aid in ids("agent"):
            r = _rows("test_agent", project=project, agent=aid)
            if "error" in r or not r.get("passed", all(c.get("passed") for c in r.get("cases", [{}]))):
                bad.append(f"agent {aid}: evaluation set doesn't pass ({str(r.get('error', ''))[:120]})")
    elif name == "review_findings":
        if not h.get("checks"):
            bad.append("a review lists what it checked (checks: [{what, result}])")
    elif name == "pack_ready":
        import pack  # noqa: PLC0415 (sibling script)
        packs = {m["id"]: m for m in pack.listing(project)}
        for pid in ids("pack"):
            m = packs.get(pid)
            if not m:
                bad.append(f"pack {pid} wasn't made (abi_pack)")
                continue
            for it in m["items"]:
                if it["status"] == "ok" and not (pack.home(project) / pid / it["file"]).exists():
                    bad.append(f"pack {pid}: {it['file']} is missing")
            if not any(it["status"] == "ok" for it in m["items"]):
                bad.append(f"pack {pid}: nothing could be rendered")
        if not ids("pack"):
            bad.append("the handoff lists no pack (done: [{kind: 'pack', id}])")
    elif name == "committed":
        r = _rows("changes", project=project)
        if r.get("files"):
            bad.append(f"{len(r['files'])} change(s) not committed yet")
    return {"check": name, "passed": not bad, "problems": bad}


def handoff(project: str, agent: str, h: dict) -> dict:
    errs = []
    for k in ("task_id", "status", "summary"):
        if not h.get(k):
            errs.append(f"'{k}' is required")
    if h.get("status") not in ("done", "partial", "blocked"):
        errs.append("status is done, partial or blocked")
    for d in h.get("done") or []:
        if not isinstance(d, dict) or d.get("kind") not in KINDS or not d.get("id"):
            errs.append(f"each done item is {{kind, id, what}} with kind one of {', '.join(sorted(KINDS))}")
            break
    plan = read(project, "plan.json", {})
    t = next((x for x in plan.get("tasks", []) if x["id"] == h.get("task_id")), None)
    if not t:
        errs.append(f"task {h.get('task_id')} is not in the plan")
    elif t["agent"] != agent:
        errs.append(f"task {t['id']} belongs to {t['agent']}")
    if errs:
        return {"accepted": False, "errors": errs}
    pend = read(project, "pending.json", [])
    for p in h.get("pending_confirmations") or []:
        name, args = str(p.get("action") or "").removeprefix("mcp__abi__"), p.get("args")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not isinstance(args or {}, dict):  # the Lead sends it to ABI by this name
            return {"accepted": False, "errors": [f"pending_confirmations: '{name[:60]}' is not the name of an ABI action with its arguments"]}
        pend.append({"id": f"p{len(pend) + 1}", "task": t["id"], "agent": agent, "action": name,
                     "args": {k: v for k, v in (args or {}).items() if k != "project"}, "why": p.get("why", ""), "status": "waiting", "t": now()})
    write(project, "pending.json", pend)
    results = [check(project, c, h.get("done") or [], h) for c in AGENTS[agent]["checks"]]
    checks_ok = all(r["passed"] for r in results)
    waits = checks_ok and h["status"] == "partial" and bool(h.get("pending_confirmations"))
    ok = h["status"] == "done" and checks_ok
    result = "passed" if ok else "waiting" if waits else ("failed" if h["status"] == "done" else h["status"])
    set_status(project, t["id"], "passed" if ok else "waiting" if waits else "failed")
    log(project, {"kind": "handoff", "agent": agent, "task": t["id"], "result": result, "handoff": h, "checks": results})
    return {"accepted": True, "task": t["id"], "result": result, "checks": results,
            "next": "Passed: the Lead can start what depends on it." if ok else
                    "Checks passed; waiting only for the person's yes on the pending steps (the Lead runs them with abi_pending)." if waits else
                    "Not passed: fix the problems listed (or say why you can't) and hand off again."}


def pending(project: str) -> list[dict]:
    return [p for p in read(project, "pending.json", []) if p["status"] == "waiting"]


def pending_done(project: str, pid: str, outcome: str) -> None:
    pend = read(project, "pending.json", [])
    for p in pend:
        if p["id"] == pid:
            p["status"], p["outcome"] = "done" if outcome == "ok" else outcome, outcome
    write(project, "pending.json", pend)
    log(project, {"kind": "confirmation", "agent": "lead", "pending": pid, "outcome": outcome})
    task = next((p["task"] for p in pend if p["id"] == pid), None)
    mine = [p for p in pend if p["task"] == task]
    if task and mine and all(p["status"] == "done" for p in mine):  # every step it waited for ran: the task passes
        plan = read(project, "plan.json", {})
        if any(t["id"] == task and t["status"] in ("waiting", "failed") for t in plan.get("tasks", [])):
            set_status(project, task, "passed")
            log(project, {"kind": "passed_after_yes", "agent": "lead", "task": task})


def status(project: str) -> dict:
    plan = read(project, "plan.json", {})
    return {"goal": plan.get("goal"), "tasks": [{k: t.get(k) for k in ("id", "agent", "area", "status", "attempts", "depends_on")} for t in plan.get("tasks", [])],
            "pending": pending(project), "journal": journal(project, 60),
            "brief": (tdir(project) / "brief.md").read_text() if (tdir(project) / "brief.md").exists() else ""}


def main(argv: list[str]) -> None:
    cmd, project, *rest = argv
    stdin = lambda: sys.stdin.read()  # noqa: E731
    if cmd == "validate":
        out = {"errors": validate(json.loads(stdin()))}
    elif cmd == "plan":
        out = save_plan(project, json.loads(stdin()))
    elif cmd == "guard":
        out = guard(project, rest[0], stdin())
    elif cmd == "brief":
        print(brief(project, rest[0], rest[1]))
        return
    elif cmd == "handoff":
        out = handoff(project, rest[0], json.loads(stdin()))
    elif cmd == "pending":
        if rest[:1] == ["done"]:
            pending_done(project, rest[1], rest[2] if len(rest) > 2 else "ok")
        out = pending(project)
    elif cmd == "release":  # a task whose child exited without a handoff goes back to failed (the Lead may retry)
        plan = read(project, "plan.json", {})
        for t in plan.get("tasks", []):
            if t["id"] == rest[0] and t["status"] == "running":
                t["status"] = "failed"
                log(project, {"kind": "no_handoff", "agent": t["agent"], "task": t["id"]})
        write(project, "plan.json", plan)
        out = {"ok": True}
    elif cmd == "brief-set":
        (tdir(project) / "brief.md").write_text(stdin().strip() + "\n")
        log(project, {"kind": "brief", "agent": "lead"})
        out = {"ok": True}
    elif cmd == "whereis":  # a specialist's task id -> its project (the running task of that agent)
        agent, task_id = rest[0], rest[1]
        out = {}
        for plan_file in sorted((abi.WORKSPACES).glob("*/_team/*/plan.json")):
            try:
                plan = json.loads(plan_file.read_text())
            except ValueError:
                continue
            if any(t["id"] == task_id and t["agent"] == agent and t["status"] == "running" for t in plan.get("tasks", [])):
                out = {"project": plan_file.parent.name}
        print(json.dumps(out))
        return
    elif cmd == "status":
        out = status(project)
    else:
        raise SystemExit(__doc__)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
