"""Custom agent definitions. PRD §7.2 AG-1, §3 readiness, DECISIONS D-3, D-4.

An agent is `agents/<id>.json` (schemas/v1/agent.json) with its evaluation set `agents/<id>.eval.json`
(schemas/v1/eval.json). `save_agent` always saves a draft owned by the caller; creating one needs a Ready model
(`require_ready`, also the gate of test, publish and run). Imports accept agents on an unready model: they stay
inert until it is Ready. An agent may hold only custom tools, `semantic_query`, `report_snapshot` and D-3's
action tools.
"""


from pydantic import Field

from abi import storage
from abi.actions import ActionFailed, action
from abi.project import ID, SCHEMA, git, imports
from abi.project.bundle import bundle, loads
from abi.security import Role, User
from abi.semantic import lineage, model, planner
from abi.tools import AGENT_ACTION_TOOLS, saved

BUILTIN = ("semantic_query", "report_snapshot", *AGENT_ACTION_TOOLS)  # every tool an agent may hold besides custom tools
ASK = "ask_agent"  # AGENT-AS-TOOL: the tool offered for the agent's `agents`


def require_ready(project: str) -> None:
    """§3: custom agents are enabled only once the semantic model is Ready; refused with what is missing."""
    if gaps := model.report(project)["missing"]:
        raise ActionFailed(409, "Custom agents need a Ready semantic model. Missing: " + " ".join(gaps), missing=gaps)


def _tables(q: dict) -> set[str]:
    return {c.split(".")[0] for c in [*q.get("dimensions", []), *(f["column"] for f in q.get("filters", []))]}


def scope(a: dict, files: dict, g: dict) -> tuple[set[str], set[str]]:
    """(measures, tables) the agent may read: its scope plus what its scoped datasets read (their measures with every
    measure those use in MEL, and their dimension and filter tables). `g` = lineage.graph(files)."""
    s = a["scope"]
    ds = s.get("datasets", [])
    qs = [d["query"] for d in (files.get("model/datasets.json") or {}).get("datasets", []) if d["id"] in ds]
    up = lineage.reach(g, {f"dataset:{d}" for d in ds}, "upstream")
    return set(s.get("measures", [])) | {n[8:] for n in up if n.startswith("measure:")}, set(s.get("tables", [])).union(*map(_tables, qs))


def _bridged(q: dict, files: dict) -> set[str]:
    """1.2.6 M5: the tables a query reads across a link (named, or a default one): both sides of each route's hops from
    its first one against a relationship's direction on (`planner.route`). None without links: as before 1.2.6."""
    m, out = files.get("model/model.json") or {}, set()
    try:
        links = planner.links(m, files.get("model/ontology.json"), q.get("links"))
    except planner.PlanError:  # the query is refused anyway
        return out
    for f in lineage._facts(files, q.get("measures", [])) if links else ():
        for t in _tables(q) - {f}:
            try:
                hops = planner.route(m, f, t, links=links)[0]
            except planner.PlanError:
                continue
            i = next((i for i, (_, along) in enumerate(hops) if not along), len(hops))
            out |= {r[s]["table"] for r, _ in hops[i:] for s in ("from", "to")}
    return out


def outside(q: dict, measures: set[str], tables: set[str], g: dict, files: dict | None = None) -> list[str]:
    """D-24: what a semantic query reads outside a scope: its measures and every measure they use in MEL (the lineage
    walk), then its dimension and filter tables, and (`files`, 1.2.6 M5) the tables it reads across a link. A
    measure's own columns come with it (§7.2: agent → tools → semantic queries → measures → tables)."""
    ms = set(q.get("measures", []))
    ms |= {n[8:] for n in lineage.reach(g, {f"measure:{m}" for m in ms}, "upstream") if n.startswith("measure:")}
    return sorted(ms - measures) + sorted((_tables(q) | (_bridged(q, files) if files else set())) - tables)


def tools_outside(a: dict, files: dict) -> dict[str, list[str]]:
    """D-24: {custom tool: what its query reads outside the agent's scope}, for the agent's tools that do."""
    g = lineage.graph(files)
    measures, tables = scope(a, files, g)
    return {t: out for t in a["tools"] if (tool := files.get(f"tools/{t}.json"))
            and (out := outside(tool.get("query") or {}, measures, tables, g, files))}


def references(rel: str, a: dict, files: dict, old: dict) -> list[dict]:
    """Import checks of one agent file (called by imports._references after its schema passed); `old` = the repo's
    current files. D-24: an agent saved or imported with a custom tool reading outside its scope is refused; when only
    a tool or measure changes, the AG-4 re-check (runs.retest) disables it instead."""
    err, ids = imports._err, imports._id
    m = files.get("model/model.json")
    known = {
        "tables": m and {t["table"] for t in m.get("tables", [])},  # None: no model to check against
        "measures": {x["id"] for x in (files.get(imports.MEASURES) or {}).get("measures", [])},
        "datasets": {x["id"] for x in (files.get("model/datasets.json") or {}).get("datasets", [])},
        "glossary": {x["id"] for x in (files.get("model/glossary.json") or {}).get("terms", [])},
    }
    errs = [err(rel, f"$.scope.{k}[{i}]", f"Unknown {k.rstrip('s')} '{x}'.", f"Use one of: {', '.join(sorted(known[k])) or 'none yet'}.")
            for k, xs in a["scope"].items() for i, x in enumerate(xs) if known[k] is not None and x not in known[k]]
    custom = {ids(r) for r in files if imports._kind(r) == "tool"}
    errs += [err(rel, f"$.tools[{i}]", f"'{t}' is not a tool this agent may hold.",
                 f"Use a custom tool ({', '.join(sorted(custom)) or 'none yet'}) or one of: {', '.join(BUILTIN)} (D-3).")
             for i, t in enumerate(a["tools"]) if t not in custom and t not in BUILTIN]
    others = {i for r in files if imports._kind(r) == "agent" and "." not in (i := ids(r))} - {a["id"]}  # AGENT-AS-TOOL
    errs += [err(rel, f"$.agents[{i}]", f"'{x}' is not another agent of this project.", f"Use one of: {', '.join(sorted(others)) or 'none yet'}.")
             for i, x in enumerate(a.get("agents", [])) if x not in others]
    if a.get("agents") and ASK in a["tools"]:
        errs.append(err(rel, "$.tools", f"A custom tool named '{ASK}' clashes with asking agents.", "Rename the tool."))
    if a != old.get(rel):
        bad = tools_outside(a, files)
        errs += [err(rel, f"$.tools[{i}]", f"Tool '{t}' reads outside the agent's knowledge scope: {', '.join(bad[t])}.",
                     f"Add {', '.join(bad[t])} to the scope, or remove the tool.") for i, t in enumerate(a["tools"]) if t in bad]
    if not a["grounding"] and any(c["type"] != "in_app" for c in a["channels"]):  # §7.2: required for external channels
        errs.append(err(rel, "$.grounding", "Grounding can't be off for an agent with channels other than in_app.",
                        "Set grounding to true, or keep only in_app channels."))
    want = f"agents/{a['id']}.eval.json"
    if a["evaluation"] != want or want not in files:
        errs.append(err(rel, "$.evaluation", f"The evaluation set must be the file {want}.", f"Set evaluation to '{want}' and add that file."))
    return errs


class AgentRef(storage.ProjectRef):
    agent: str = Field(pattern=ID, description="agent ID (file name in agents/)")


class SaveAgent(storage.ProjectRef):
    agent: dict = Field(description=(
        "The agent (schemas/v1/agent.json): {id, name, purpose, instructions, scope: {tables?, measures?, datasets?, glossary?}"
        " (IDs from semantic_model and the glossary), tools: [custom tool IDs, semantic_query, report_snapshot, send_email,"
        " post_message, refresh, propose_edit], agents?: [other published agent IDs it may ask, as you], mode: tools_only (default) | exploratory, model: auto (default) or a"
        " provider alias from list_llm_providers (small, large, …), effort? (reasoning effort: none, minimal, low, medium,"
        " high, xhigh, max; sent only where the model lists it), grounding (default true; required"
        " for channels other than in_app), channels: [{type: in_app|email|slack|teams|api|mcp, address?, channel?, reply? (teams only, required: the"
        " settings.delivery Teams channel name its answers go to)}]}."
        " version, status, owner, sharing and evaluation are set for you."))
    cases: list[dict] | None = Field(None, description=(
        "Evaluation set (schemas/v1/eval.json cases): [{question, expect: {contains: [values the answer must contain],"
        " tools?: [tools it must call]}}]; omit to keep the current one"))


@action(Role.editor, definition=True)
def save_agent(conn, user: User, args: SaveAgent) -> dict:
    """Creates or replaces a custom agent (agents/<id>.json) and its evaluation set (agents/<id>.eval.json) as a draft; you become its owner (its retests run and are billed as you); an existing one keeps its sharing. Creating one needs a Ready semantic model. Checked like an import (schema, scope IDs, allowed tools, grounding); rejected with file, JSON path and fix if invalid."""
    a = args.agent
    i = imports.object_id(a, "An agent")
    rel, erel = f"agents/{i}.json", f"agents/{i}.eval.json"
    files = bundle(git.path(args.project))
    old = files.get(rel)
    if old is None:
        require_ready(args.project)
    a = {"scope": {}, "tools": [], "mode": "tools_only", "grounding": True, "channels": [], **a, "$schema": SCHEMA + "agent.json",
         "status": "draft", "version": (old or {}).get("version", 0), "owner": str(user.id),  # FL-6 rule of imports.apply
         "sharing": (old or {}).get("sharing", "project"), "evaluation": erel}  # SH-1: sharing as stored
    ev = {"$schema": SCHEMA + "eval.json", "cases": args.cases} if args.cases is not None else \
        files.get(erel) or {"$schema": SCHEMA + "eval.json", "cases": []}
    imports.check(conn, args.project, files | {rel: a, erel: ev})  # PC-3
    imports.write(args.project, {rel: a, erel: ev})
    return saved(a)


@action(Role.editor, definition=True, confirms=True)
def delete_agent(conn, user: User, args: AgentRef) -> dict:
    """Deletes a custom agent and its evaluation set."""
    imports.remove(args.project, "agent", args.agent)
    (git.path(args.project) / "agents" / f"{args.agent}.eval.json").unlink(missing_ok=True)
    return {}


@action(Role.viewer)
def list_agents(conn, user: User, args: storage.ProjectRef) -> dict:
    """The project's custom agents, each with its definition, its evaluation set (`cases`), and `disabled` with the evaluation failure when AG-4 disabled it."""
    folder = git.path(args.project) / "agents"
    ps = sorted(p for p in folder.glob("*.json") if not p.name.endswith(".eval.json")) if folder.is_dir() else []
    state = {r["agent"]: r["error"] for r in conn.execute("SELECT agent, error FROM agent_state WHERE project = %s", [args.project])}
    agents = [(a := loads(p.read_text())) | {"disabled": bool(state.get(a["id"])), "error": state.get(a["id"]),
                                             "cases": loads(e.read_text())["cases"] if (e := p.with_name(p.stem + ".eval.json")).exists() else []}
              for p in ps]
    return {"agents": agents, "columns": ["status", "version", "disabled"],  # D-22: server-set; D-114: never id/name
            "rows": [[a["status"], a["version"], a["disabled"]] for a in agents]}
