#!/usr/bin/env python3
"""Write .pi/agents/<name>.md for the specialists of harness/team.json: each gets only its ABI tool groups (from
harness/areas.json), its skills, the team tools (abi_handoff, abi_vault) and no subagent tool, so it can't delegate.
The subagent extension passes the list as --tools and ABI_PI_ALLOWED, so abi_tools can't widen it."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AREAS = json.loads((ROOT / "harness/areas.json").read_text())
TEAM = json.loads((ROOT / "harness/team.json").read_text())
COMMON = ["read", "ls", "abi_tools", "abi_sync", "abi_prefs", "abi_vault", "abi_handoff", "abi_show"]
READS = ["semantic_model", "semantic_query", "visual_data", "dataset_data", "data_quality", "readiness", "lineage", "docs",
         "how_calculated", "list_reports", "list_flows", "flow_runs", "ml_models", "list_tools", "list_agents", "changes",
         "history", "diff", "rls_rules", "preview_steps", "test_tool", "test_agent"]
EXTRA = {"analyst": ["run_agent", "list_agents", "agent_runs", "optimizers"], "release-ops": ["data_quality", "flow_runs", "refresh"],
         "automation-engineer": ["optimizers"]}  # 1.2.6 M12: they read plans (the analyst explains one, a flow runs one); only the ml-engineer makes them
HARNESS = {"data-engineer": ["abi_load", "abi_project"], "release-ops": ["abi_cli"], "board-pack": ["abi_pack"],
           "analyst": ["abi_explore", "abi_monitor"], "automation-engineer": ["abi_monitor"]}  # the app's Explore and Monitor


def main() -> None:
    out = ROOT / ".pi/agents"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.md"):
        old.unlink()  # the team file is the only source
    for name, a in TEAM["agents"].items():
        tools = COMMON + HARNESS.get(name, []) + [f"mcp__abi__{t}" for t in AREAS["core"]]
        tools += [f"mcp__abi__{t}" for g in a["groups"] for t in AREAS["areas"][g]["tools"]] if not a.get("read_only") else []
        tools += [f"mcp__abi__{t}" for t in READS + EXTRA.get(name, [])]
        tools = [t for t in dict.fromkeys(tools) if t != "mcp__abi__remember" or name == "release-ops"]
        skills = ", ".join(a["skills"])
        (out / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: {json.dumps(a['description'])}\ntools: {', '.join(tools)}\n---\n\n"
            f"You are the team's {name}. {a['description']}\n"
            f"Your areas: {', '.join(a['areas'])}. Your ABI tool groups: {', '.join(a['groups']) or 'reads only'} (call abi_tools with them\n"
            f"first to activate them and read their notes). Read these skills before the first step: {skills}.\n\n"
            "You work for the Lead, who planned this task; your briefing (in your context) says what the team did before you.\n"
            "Rules: data only through ABI, changes only through actions, every number from a tool result's rows. Counts only with the items named right after them (\"3 visuals: A, B and C\"); a group you\nsummarise (\"the other cities\") gets no number, only its rows' figures. You can't ask\n"
            "the person anything: when a step needs their yes, don't retry it, list it in pending_confirmations and go on.\n"
            + ("You change nothing: read, check and report findings.\n" if a.get("read_only") else "")
            + "Finish with exactly one abi_handoff call: task_id, status (done/partial/blocked), summary, done (every object\n"
              "you made or changed: {kind, id, what}), decisions, assumptions, checks you ran with figures from the rows,\n"
              "open_issues, pending_confirmations, next (what the next agent needs to know). The harness checks your work\n"
              "against ABI; if a check fails, fix it and hand off again.\n")
        print(name, len(tools))


if __name__ == "__main__":
    main()
