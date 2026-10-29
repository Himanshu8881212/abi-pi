#!/usr/bin/env python3
"""Write .pi/agents/<name>.md: the harness subagents, each with only its tool groups (from harness/areas.json).
The subagent extension passes the list as --tools and ABI_PI_ALLOWED, so abi_tools can't widen it."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AREAS = json.loads((ROOT / "harness/areas.json").read_text())
MODEL = "cliproxy/gpt-6.1-sol"
COMMON = ["read", "ls", "abi_tools", "abi_sync", "abi_prefs", "abi_vault", "abi_show"]
AGENTS = {
    "data-engineer": (["data", "model"], ["abi_load", "abi_project"],
                      "Loads, profiles, cleans and models data in ABI until the model is Ready.",
                      "Read the load-data, medallion, profile-clean, build-model and practice-data-engineering skills. Grain first; profile before modelling; reconcile each layer."),
    "analyst": (["model", "reports", "docs"], [], "Writes measures, builds reports and answers data questions from ABI.",
                "Read the practice-analyst, measures and build-report skills. Definitions from the vault (abi_vault); segment before you conclude. Ask ABI's own custom agents with run_agent when one fits."),
    "ml": (["ml", "model"], [], "Builds datasets, trains and scores ML models (forecasts, clusters, anomalies) in ABI.",
           "Read the forecast-ml and practice-analyst skills. Accuracy and baseline only from ml_models rows."),
    "automation": (["flows", "tools"], [], "Creates flows, schedules, alerts and sends in ABI.",
                   "Read the flows-alerts and practice-automation skills. Flows off by default; numbers ride in a step's data, never in its text."),
}
EXTRA = {"analyst": ["run_agent", "list_agents", "agent_runs"]}


def main() -> None:
    out = ROOT / ".pi/agents"
    out.mkdir(parents=True, exist_ok=True)
    for name, (areas, harness, desc, how) in AGENTS.items():
        tools = COMMON + harness + [f"mcp__abi__{t}" for t in AREAS["core"]]
        tools += [f"mcp__abi__{t}" for a in areas for t in AREAS["areas"][a]["tools"]] + [f"mcp__abi__{t}" for t in EXTRA.get(name, [])]
        tools = list(dict.fromkeys(tools))
        (out / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\ntools: {', '.join(tools)}\nmodel: {MODEL}\n---\n\n"
            f"You are ABI's {name} subagent. {desc} Your ABI tools are this agent's groups ({', '.join(areas)}): call\n"
            f"abi_tools with them first to activate them and read their notes. {how}\n\n"
            "Rules: data only through ABI (semantic_query), changes only through actions, every number you report from a\n"
            "tool result's rows. You can't get the person's confirmation: when an action needs it, stop and report what\n"
            "is waiting so the main agent asks them. End with a short report: what you did (IDs), the figures from the\n"
            "tool rows, and what is left.\n")
        print(name, len(tools))


if __name__ == "__main__":
    main()
