#!/usr/bin/env python3
"""Map every ABI action to its tool group, exactly as ABI's `agent.area()` does (longest module prefix wins), by reading
ABI's source statically (no import, nothing written there). Writes harness/areas.json.

    python3 scripts/gen_areas.py [~/Work/Working/ABI/backend]
"""
import json
import re
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "~/Work/Working/ABI/backend").expanduser()
OUT = Path(__file__).resolve().parent.parent / "harness" / "areas.json"

# copied from abi/agent/__init__.py (CORE, AREAS: description and module prefixes)
CORE = ["list_projects", "semantic_model", "semantic_query", "how_calculated", "lineage", "docs", "readiness",
        "detect_model", "data_quality", "list_reports", "list_tools", "list_agents", "list_flows", "ml_models",
        "remember", "changes"]
AREAS = {
    "data": ("load, connect, transform, refresh and delete sources and tables",
             ("abi.ingest", "abi.storage", "abi.semantic.detect", "abi.semantic.preview_steps", "abi.tools.builtin.refresh")),
    "model": ("semantic model: tables, keys, relationships, measures, glossary, datasets, date/rates tables, row-level rules",
              ("abi.semantic", "abi.project.imports")),
    "reports": ("reports, visuals, snapshots, exports, sharing, share links and embeds",
                ("abi.reports", "abi.project.sharing", "abi.tools.builtin.report_snapshot", "abi.tools.builtin.export_visual")),
    "writeback": ("write-back: edit cells and totals, input tables, proposals and approvals, edit history, sync to sources",
                  ("abi.writeback",)),
    "tools": ("custom tools: save, test, publish, run", ("abi.tools",)),
    "agents": ("custom agents (save, test, publish, run) and project memory", ("abi.agents", "abi.agent")),
    "flows": ("flows and tasks, schedules, conditions and subscriptions, email/message sends, inbox, inbound messages",
              ("abi.flows", "abi.semantic.subscriptions", "abi.tools.builtin")),
    "ml": ("ML models: forecast, classify, predict, cluster, anomalies; train, score, pre-fill", ("abi.ml",)),
    "docs": ("documentation: generate, draft and review descriptions", ("abi.docs",)),
    "project": ("projects, project JSON import, commits, undo, revert, history, diffs, remotes, push/pull, environments, locale",
                ("abi.project", "abi.project.imports.import_project")),
    "admin": ("users, roles, tenants, sign-in providers, LLM providers, usage, secrets list", ("abi.security", "abi.agent.llm")),
}
PREFIX = {p: a for a, (_, ps) in AREAS.items() for p in ps}
# ABI's question keyword stems per area (agent.AREAS third field), matched with \b(...) case-insensitively (D-71)
WORDS = {
    "data": ("load|upload|file|csv|parquet|excel|json|source|connect|database|postgres|mysql|clickhouse|s3|step|clean|"
             "transform|dedup|rename|column|refresh|reload|table|import|fresh|quality|restore"),
    "model": ("model|measure|relationship|join|glossary|term|dataset|date table|calendar|currency|rate|key|row-level|rls|"
              "define|definition|formula|kpi|metric|margin|build"),
    "reports": "report|page|visual|chart|dashboard|export|download|share|sharing|embed|link|pdf|png|snapshot|show me",
    "writeback": ("edit|write-back|writeback|write back|budget|plan|input table|proposal|propose|approve|adjust|raise|"
                  "lower|increase|decrease|change the value|sync"),
    "tools": "tool",
    "agents": "agent|assistant|bot|eval|memory|remember|forget|convention|notification",
    "flows": ("flow|task|schedul|every|daily|weekly|monthly|hourly|monday|tuesday|wednesday|thursday|friday|morning|email|"
              "e-mail|mail|send|post(?!gres)|message|inbox|slack|teams|webhook|alert|subscri|notify|condition|trigger|run"),
    "ml": ("forecast|predict|churn|classif|cluster|segment|anomal|unusual|outlier|train|score|machine learning|ml|"
           "next (year|quarter|month|week)|future"),
    "docs": "doc|describ|description|explain|what does|meaning",
    "project": ("project|commit|undo|revert|history|diff|change|branch|push|pull|git|remote|environment|production|"
                "promote|locale|time zone|rename"),
    "admin": "user|role|permission|access|invite|tenant|provider|llm|sso|oidc|sign-in|usage|secret|admin|owner",
}
DEF = re.compile(r"^@action\([^\n]*\n(?:@[^\n]*\n)*def (\w+)\(", re.M)


def area(module: str, name: str) -> str | None:
    parts = f"{module}.{name}".split(".")
    return next((PREFIX[p] for i in range(len(parts), 0, -1) if (p := ".".join(parts[:i])) in PREFIX), None)


def main() -> None:
    groups: dict[str, list[str]] = {a: [] for a in AREAS}
    other = []
    for f in sorted((SRC / "abi").rglob("*.py")):
        rel = f.relative_to(SRC).with_suffix("")
        module = ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)
        for name in DEF.findall(f.read_text()):
            if name in CORE:
                continue
            (groups[a] if (a := area(module, name)) else other).append(name)
    out = {"core": CORE, "unclassified": sorted(other),
           "areas": {a: {"about": AREAS[a][0], "words": WORDS[a], "tools": sorted(t)} for a, t in groups.items()}}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    n = len(CORE) + len(other) + sum(map(len, groups.values()))
    print(f"{n} actions -> {OUT}")
    assert n > 150 and "semantic_query" in CORE and "train_model" in out["areas"]["ml"]["tools"]


if __name__ == "__main__":
    main()
