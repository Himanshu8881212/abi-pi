"""Living docs + Generate Documentation. PRD §8 DC-1..DC-5, DC-8, two layers.

Structural layer: built at call time from the project files, Postgres, Git and the semantic engine (row counts,
profiles and worked example values come from `semantic.execute`/`semantic.query`, never from an LLM: principle 7).
Explanatory layer: the `description` fields of project.json, model tables, measures, datasets and reports. Missing ones are
drafted by the Documenter skill (`agent.document`); docs/descriptions.json marks each text agent-written or
human-reviewed and keeps a hash of the object's definition, so a changed definition flags it "may be outdated".

A package is {"generated", "projects": [{"project", "name", "sections": [{"id", "title", "items"}], "review"}],
"files": {path: markdown}}. SECTIONS holds one builder per §8 section; empty sections are left out. Tools, agents,
flows and ML models (10-13) also read their runtime state from Postgres (tool_state, agent_state, flow_runs, ml_runs):
run history is not in the repository (§6.6).
Secrets (DC-4): source files are read for their connection type, format and refresh mode only; delivery settings for
hosts and the names of the secrets they reference; flow action steps for their action, recipients and subject only.
"""

import base64
import hashlib
import json
import math
import re
import time
import zipfile
from datetime import UTC, datetime
from html import escape
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import BaseModel, Field
from pygit2.enums import SortMode

from abi import actions, agent, db, ml, semantic, storage, transform, writeback
from abi.actions import ACTIONS, ActionFailed, action
from abi.agent import relevant
from abi.agents import studio
from abi.ingest import quality
from abi.ml import unsupervised
from abi.project import SCHEMA, bundle, git, imports
from abi.project.bundle import dumps
from abi.security import MEMBERS, Role, User, credentials, role_in, tenants
from abi.semantic import lineage, model, ontology
from abi.tools import builtin

MARKS = "docs/descriptions.json"


def _h(v) -> str:
    return hashlib.sha256(dumps(v).encode()).hexdigest()


# --- explanatory layer -------------------------------------------------------------------------------------------

def _explained(project: str, files: dict) -> dict[str, tuple[str | None, object]]:
    """{object: (description or None, definition)} for every object with an explanatory text."""
    out = {f"project:{project}": (files["project.json"].get("description"), files["project.json"]["name"])}
    for t, i in model.load(project)["tables"].items():
        out[f"table:{t}"] = (i["description"], [i["kind"], i["key"], (files.get(f"tables/{t}.json") or {}).get("steps")])
    for m in (files.get("model/measures.json") or {}).get("measures", []):
        out[f"measure:{m['id']}"] = (m.get("description"), m["expression"])
    for d in (files.get("model/datasets.json") or {}).get("datasets", []):
        out[f"dataset:{d['id']}"] = (d.get("description"), d["query"])
    for rel, r in files.items():
        if rel.startswith(("reports/", "ml/")):
            out[f"{rel.split('/')[0].removesuffix('s')}:{r['id']}"] = (r.get("description"), {k: v for k, v in r.items() if k != "description"})
    return out


def _marks(project: str) -> dict:
    p = git.path(project) / MARKS
    return json.loads(p.read_text())["descriptions"] if p.exists() else {}


def _explanation(obj: str, texts: dict, marks: dict) -> dict:
    """{"text", "by": agent | human | None (missing), "outdated"}. Text without a valid mark was written by a person."""
    text, definition = texts[obj]
    m = marks.get(obj)
    if text is None:
        return {"text": None, "by": None, "outdated": False}
    if m and m["text"] == _h(text):
        return {"text": text, "by": m["by"], "outdated": m["definition"] != _h(definition)}
    return {"text": text, "by": "human", "outdated": False}


def mark(project: str, objects: dict[str, str]) -> None:
    """Record {object: "agent" | "human"} for the objects' current texts and definitions (a definition-action write)."""
    texts, marks = _explained(project, bundle.bundle(git.path(project))), _marks(project)
    for obj, by in objects.items():
        text, definition = texts[obj]
        marks[obj] = {"by": by, "text": _h(text), "definition": _h(definition)}
    imports.write(project, {MARKS: {"$schema": SCHEMA + "descriptions.json", "descriptions": marks}})


def _set_text(project: str, obj: str, text: str) -> None:
    kind, oid = obj.split(":", 1)
    if rel := {"project": "project.json", "report": f"reports/{oid}.json", "ml": f"ml/{oid}.json"}.get(kind):
        imports.write(project, {rel: {**json.loads((git.path(project) / rel).read_text()), "description": text}})
    elif kind == "table":
        m = model.load(project)
        m["tables"][oid]["description"] = text
        model.save(project, m)
    else:
        file, key = {"measure": ("measures.json", "measures"), "dataset": ("datasets.json", "datasets")}[kind]
        doc = model._read(project, file)
        doc[key] = [{**x, "description": text} if x["id"] == oid else x for x in doc[key]]
        model._write(project, file, doc)


def notify_outdated(project: str) -> None:
    """§5 proactive help: post once when a marked description's definition changed, once when that clears (D-10)."""
    # ponytail: one feed lookup per marked object on every definition change; keep a last-state table if projects get big
    try:
        texts, marks = _explained(project, bundle.bundle(git.path(project))), _marks(project)
    except (ValueError, KeyError, OSError):  # unreadable files: the import checks and readiness report those
        return
    for obj in sorted(marks.keys() & texts.keys()):
        e = _explanation(obj, texts, marks)
        agent.notify_change("docs", project, f"The description of {obj} ",
                            "may be outdated: its definition changed since it was written." if e["outdated"] else None,
                            "is current again.")


# --- structural layer: one builder per §8 section ---------------------------------------------------------------

def _item(c, obj: str | None, title: str, facts=(), table=None, code=None, diagram=None) -> dict:
    # `cond and value` facts: a false cond ({}, False, "") drops the fact; 0 stays (0 is not False by identity)
    return {"object": obj, "title": title, "facts": [[k, str(v)] for k, v in facts if v is not False and v not in (None, "", [], {})],
            "table": table, "code": code, "diagram": diagram,
            "explanation": _explanation(obj, c.texts, c.marks) if obj in c.texts else None}


def _fig(c, what: str, v):
    """GATE-9-fix-8.5 (D-22): a server-made figure shown in the docs, also returned as a row of the result so an agent
    quoting it grounds; `what` is a fixed label, never an author-written name (D-114)."""
    if v not in (None, "") and not (type(v) is float and not math.isfinite(v)):  # inf/nan: no JSON number
        c.figures.append([what, v])
    return v


def _read(c, sql: str) -> list[tuple]:
    """Data reads go through the semantic engine (principle 4), logged as the generating user (OB-1)."""
    return semantic.execute(c.project, sql, {"docs": c.project}, c.user.id)


_names = ", ".join


def _ups(c, node: str) -> list[str]:
    return sorted(c.graph.get(node, {}))


def _downs(c, node: str) -> list[str]:
    return lineage.reach(c.graph, {node}, "downstream")


def _start(c):
    counts = {k: sum(n.startswith(k + ":") for n in c.graph) for k in ("source", "table", "measure", "dataset", "term")}
    counts |= {"tool": len(c.tools), "custom agent": len(c.agents), "flow": len(c.flows)}
    first = [f"Readiness: {'Ready' if not c.gaps else f'not Ready, {_fig(c, 'gaps', len(c.gaps))} gap(s) under Open issues'}",
             f"Fact tables: {_names(sorted(c.facts)) or 'none yet'}",
             f"Date table: {(c.model['date_table'] or {}).get('table', 'none')}",
             f"Measures: {_names(sorted(n.split(':', 1)[1] for n in c.graph if n.startswith('measure:'))) or 'none yet'}",
             "Every number comes from the semantic engine; ask the Main Agent 'what does X mean' or 'what breaks if I change Y'."]
    return [_item(c, f"project:{c.project}", c.name, [
        ("Owners", _names(c.owners) or "workspace owners only"),
        ("Objects", ", ".join(f"{_fig(c, k + ' count', n)} {k}(s)" for k, n in counts.items() if n)),
        ("Five things to know first", " | ".join(f"{i}. {x}" for i, x in enumerate(first, 1))),
        ("Where to look", ("Data sources, Tables and transformations, Semantic model, Measures, Glossary, Tools, Agents,"
                           " Agent tasks and flows, Lineage map, Open issues: the sections below; the Data tab opens each"
                           " table, model and change history.")),
    ])]


def _architecture(c):
    edges = [(u, n) for n, ups in c.graph.items() for u in ups if n.split(":")[0] in ("table", "measure", "dataset", "tool", "agent", "flow")]
    lines = ["flowchart LR"] + [f'  {_mid(u)}["{u}"] --> {_mid(n)}["{n}"]' for u, n in sorted(edges)]
    lines += [f'  {_mid(n)}["{n}"] --> agent["Main Agent (semantic_query)"]'
              for n in sorted(c.graph) if n.startswith("measure:") and not any(u == n for u, _ in edges)]
    return [_item(c, None, "How this project fits together", [
        ("Flow", ("sources → ingestion (IN-*) → lake raw tables → transform steps → model tables (DuckLake) →"
                  " semantic engine (model, measures, datasets) → Main Agent, UI, REST API")),
        ("Engine", "DuckDB over DuckLake; every read goes through the semantic engine (principle 4)"),
        ("Agents", "Main Agent: asks the semantic engine with semantic_query; changes go through the action API"),
        ("Custom agents, tasks and flows", (c.agents or c.flows) and (
            "custom agents read only through their tools (agent → tools → semantic queries → measures → tables); flows and"
            " agent tasks run their steps (refresh, tool, agent, snapshot, action) in the worker as their owner")),
    ], diagram="\n".join(lines))]


def _mid(node: str) -> str:
    return re.sub(r"\W", "_", node)


def _sources(c):
    refreshed = {m.removeprefix("refresh "): t for m, t in semantic.execute(  # lake metadata, no rows: a system read (SE-6)
        c.project, "SELECT commit_message, max(epoch(snapshot_time)) FROM lake.snapshots()"
                   " WHERE starts_with(commit_message, 'refresh ') GROUP BY 1", {"docs": c.project})}
    out = []
    for rel, s in sorted(c.files.items()):
        if not (m := re.fullmatch(r"sources/([^/]+)\.json", rel)):
            continue
        loc = s.get("location", "")
        kind = "S3" if loc.startswith("s3://") else "HTTP(S)" if loc.startswith(("http://", "https://")) else "Server file"
        a = s.get("api")
        if a:  # API-SRC.2: host and path of the definition as written (secret names at most), never its query
            kind = f"API {re.sub(r'[?#].*', '', loc.split('://', 1)[-1])}"
        tables = [n.split(":", 1)[1] for n, ups in c.graph.items() if f"source:{m[1]}" in ups]
        last = max((refreshed[t] for t in tables if t in refreshed), default=None)
        pages = a and a.get("pagination")
        out.append(_item(c, f"source:{m[1]}", m[1], [
            ("Connection", kind), ("Format", a and a.get("format", "json") or s.get("format") or "from the file extension"),
            ("Pagination", pages and f"{pages['kind'].replace('_', ' ')}, at most {pages.get('max_pages', 100)} pages"),
            ("Incremental", a and a.get("incremental") and f"past the largest {a['incremental']['field']}"),
            ("Refresh", a and a.get("every_hours") and f"every {a['every_hours']} h"
             or {"on_change": "on change (the worker checks every minute)"}.get(s.get("refresh"), s.get("refresh"))),
            ("Last refresh", last and _fig(c, "last refresh", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last)))),
            ("Freshness", last and f"{_fig(c, 'hours old', round((c.now - last) / 3600, 1))} hours old"),
            ("Loads into", ", ".join(f"{t} ({_fig(c, 'rows', c.rows[t])} rows)" for t in tables if t in c.rows)),
        ]))
    return out


def _step(s) -> str:
    fields = s.model_dump(mode="json", exclude={"type"}, exclude_none=True)
    return f"{s.type.replace('_', ' ')}: " + "; ".join(f"{k} {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items())


def _profile(c, t: str) -> list[tuple]:
    """Type, approx. distinct and null share per column. Not SUMMARIZE: its std/avg overflow on ±inf or ~1e200 DOUBLEs
    and fail the whole docs build (AGENT-CONTEXT-fix-2)."""
    hid = semantic.rls.hidden(c.project, c.user.id).get(t, set())  # D-110: a restricted Viewer's view has no such columns
    cols = [(k, v) for k, v in c.cols[t].items() if k.lower() not in hid]
    if not cols:
        return []
    aggs = ", ".join(f"approx_count_distinct({q}), CAST(100.0 * (count(*) - count({q})) / nullif(count(*), 0) AS DECIMAL(9,2))"
                     for q in (storage._q(k) for k, _ in cols))
    (r,) = _read(c, f"SELECT {aggs} FROM {storage._q(t)}")
    return [(k, v, r[2 * i], r[2 * i + 1]) for i, (k, v) in enumerate(cols)]


def _tables(c):
    out = []
    for t in sorted(c.cols):
        steps = c.steps.get(t) or transform.validate([{"type": "source", "source": t}])
        info = c.model["tables"].get(t)
        profile = _profile(c, t) if c.values else [(k, v, "", "") for k, v in c.cols[t].items()]
        # ponytail: no min/max in profiles: they are data values and docs may be committed to Git (PC-2: no data there)
        out.append(_item(c, f"table:{t}", t, [
            ("Kind", "staging (hidden, not in the semantic model)" if info and info.get("hidden")
             else info and info["kind"] or "not in the semantic model"),
            ("Key", info and info["key"] and ", ".join(info["key"])),
            ("Rows", _fig(c, "rows", c.rows[t]) if t in c.rows else HIDDEN),
            ("Reads from", _names(_ups(c, f"table:{t}"))),
            ("Used by", _names(_downs(c, f"table:{t}"))),
            ("Steps", " | ".join(f"{i}. {_step(s)}" for i, s in enumerate(steps, 1))),
        ], table={"columns": ["Column", "Type", "Distinct (approx.)", "Nulls %"], "rows": [  # the column name is author-written
            [str(col), *(_fig(c, w, str(v)) for w, v in zip(("type", "distinct", "nulls %"), r))] for col, *r in profile]},
            code=transform.compile(t, steps)))
    return out


def _model(c):
    m = model.visible(c.model)  # MODEL-HIDE: staging tables stay in the data sections (tables, write-back), not here
    if not m["tables"]:
        return []
    er = ["erDiagram"] + [f"  {_mid(r['to']['table'])} ||--o{{ {_mid(r['from']['table'])} : \"{r['id']}\""
                          for r in m["relationships"]]
    er += [f"  {_mid(t)} {{\n" + "".join(f"    {_mid(dt)} {_mid(col)}\n" for col, dt in c.cols.get(t, {}).items()) + "  }"
           for t in m["tables"]]
    rel = lambda r: f"{r['from']['table']}.{', '.join(r['from']['columns'])} → {r['to']['table']}.{', '.join(r['to']['columns'])}"
    out = [_item(c, None, "Model", [
        ("Facts", _names(sorted(c.facts))),
        ("Dimensions", _names(sorted(t for t, i in m["tables"].items() if i["kind"] == "dimension"))),
        ("Entities (keyed tables)", _names(f"{t} ({', '.join(i['key'])})" for t, i in sorted(m["tables"].items()) if i["key"])),
        ("Relationships", " | ".join(f"{rel(r)} (many-to-one)" for r in m["relationships"])),
        ("Date table", m["date_table"] and f"{m['date_table']['table']}.{m['date_table']['column']}"),
    ], diagram="\n".join(er))]
    for d in (c.files.get("model/datasets.json") or {}).get("datasets", []):
        out.append(_item(c, f"dataset:{d['id']}", d["name"], [("ID", d["id"]), ("Used by", _names(_downs(c, f"dataset:{d['id']}")))],
                         code=json.dumps(d["query"], indent=2)))
    return out


def _history(project: str) -> dict[str, list[dict]]:
    """Committed changes per measure ID, oldest first (§8 item 6 owner and change history)."""
    def ms(commit):
        try:
            return {m["id"]: m for m in json.loads(commit.tree["model/measures.json"].data)["measures"]}
        except KeyError:
            return {}

    out: dict = {}
    r = git.repo(project)  # no git.locked: save_docs runs inside git.change, which holds it; commits are immutable
    for commit in r.walk(r.head.target, SortMode.TOPOLOGICAL | SortMode.TIME | SortMode.REVERSE):
        now, before = ms(commit), ms(commit.parents[0]) if commit.parents else {}
        for mid in sorted(now.keys() | before.keys()):
            if now.get(mid) != before.get(mid):
                out.setdefault(mid, []).append({"author": commit.author.name, "time": commit.author.time,
                                                "message": commit.message.split("\n")[0]})
    return out


def example(c, mid: str) -> str:
    """A worked example value, computed by semantic_query (principle 7); time intelligence needs a date dimension (D-8)."""
    if not c.values:
        return HIDDEN
    q = {"measures": [mid]}
    dt = c.model["date_table"]
    if dt and "year" in c.cols.get(dt["table"], {}):
        q |= {"dimensions": [f"{dt['table']}.year"], "order_by": [{"field": f"{dt['table']}.year", "desc": True}], "limit": 1}
    try:
        rows = semantic.query(c.project, q, c.user.id)["rows"]
    except ActionFailed as e:
        return f"no example: {e}"
    if not rows:
        return "no rows"
    *dims, value = rows[0]
    # DOUBLE sums carry binary noise even at 15 digits (97583.1400000001): 12 significant digits, whole units always
    # kept; inf and nan shown as they are
    if type(value) is float and math.isfinite(value):
        value = float(f"{value:.12g}") if abs(value) < 1e12 else round(value)
    return (f"{_fig(c, 'worked example', value)}" + (f" for year {_fig(c, 'year', dims[0])}" if dims else " (all rows)")
            + f" — semantic_query {json.dumps(q)}")


def _measures(c):
    ms = (c.files.get("model/measures.json") or {}).get("measures", [])
    history = _history(c.project) if ms else {}
    out = []
    for m in ms:
        node, h = f"measure:{m['id']}", history.get(m["id"], [])
        out.append(_item(c, node, m["name"], [
            ("ID", m["id"]), ("Depends on", _names(_ups(c, node))), ("Used by", _names(_downs(c, node))),
            ("Worked example", example(c, m["id"])),
            ("Owner", h[0]["author"] if h else "not committed yet"),
            ("Change history", " | ".join(f"{_fig(c, 'changed', time.strftime('%Y-%m-%d', time.gmtime(x['time'])))} {x['author']}: {x['message']}" for x in h)),
            ("Tests", f"{_fig(c, 'tests', len(m.get('tests', [])))} (run by the readiness check)"),
        ], code=m["expression"]))
    return out


def _glossary(c):
    return [_item(c, f"term:{t['id']}", t["term"], [
        ("Definition", t["definition"]), ("Measures", _names(f"measure:{x}" for x in t.get("measures", []))),
        ("Columns", _names(f"{x['table']}.{x['column']}" for x in t.get("columns", []))),
        ("Object types", _names(t.get("objects", []))), ("Links", _names(t.get("links", []))),
    ]) for t in model.masked(c.project, (c.files.get("model/glossary.json") or {}).get("terms", []), c.hide)
        + ontology.terms(c.files.get(ontology.FILE) or {}, c.hide)]  # 1.2.6 M6: the ontology's descriptions are definitions too


def _reports(c):
    out = []
    for rel, r in sorted(c.files.items()):
        if not rel.startswith("reports/"):
            continue
        rows = [[p["name"], v.get("title") or v["id"], v.get("type", "auto"),
                 _names(f"measure:{m}" for m in v.get("query", {}).get("measures", [])),
                 _names(v.get("query", {}).get("dimensions", [])),
                 _names(f"{f['column']} {f['op']} {json.dumps(f['value'])}" for f in
                        [*r.get("filters", []), *p.get("filters", []), *v.get("query", {}).get("filters", [])])]
                for p in r["pages"] for v in p["visuals"]]
        out.append(_item(c, f"report:{r['id']}", r["name"], [
            ("Pages", _names(p["name"] for p in r["pages"])),
            ("Audience", "everyone with a role in the project (Security)"),
            ("Report filters", _names(f"{f['column']} {f['op']} {json.dumps(f['value'])}" for f in r.get("filters", []))),
        ], table={"columns": ["Page", "Visual", "Type", "Measures", "By", "Filters"], "rows": rows}))
    return out


def _edits(c) -> dict[str, list]:
    """{table: [active, proposed, undone, rejected, last edit, conflicts, orphaned, synced]} per edit log (WB-3, WB-6, WB-10),
    counted through the semantic engine (principle 4); flags come from `writeback.flags_sql`, as the review feed's."""
    cur, out = storage.lake(c.project).cursor(), {}
    counts = ", ".join(f"count(*) FILTER (status = '{s}')" for s in ("active", "proposed", "undone", "rejected", "synced"))
    for t in sorted(writeback._logs(cur)):
        [(*n, synced, last)] = _read(c, f"SELECT {counts}, strftime(max(coalesce(reviewed_at, \"timestamp\")), '%Y-%m-%d %H:%M UTC')"
                                f" FROM lake.edits.{storage._q(t)}")
        [flags] = _read(c, f"SELECT count(*) FILTER (flag = 'conflict'), count(*) FILTER (flag = 'orphaned')"
                           f" FROM ({writeback.flags_sql(cur, t)})")
        out[t] = [*n, last or "", *flags, synced]
    return out


def _writeback(c):
    inputs = sorted(rel[7:-5] for rel, f in c.files.items() if rel.startswith("tables/") and f.get("steps") == [])
    synced = sorted(rel[8:-5] for rel, f in c.files.items() if rel.startswith("sources/") and (f.get("database") or {}).get("sync") is True)
    if not (c.edits or inputs or synced):
        return []
    keyed = {t: i["key"] for t, i in c.model["tables"].items() if i["key"] and t != model.CALENDAR}  # D-21
    with db.connect() as conn:
        editors = [r["name"] for r in conn.execute((
            "SELECT u.name FROM project_roles r JOIN users u ON u.id = r.user_id WHERE r.project = %s AND r.role IN"
            " ('editor', 'owner') UNION SELECT name FROM users WHERE workspace_owner ORDER BY 1"), [c.project])]
    return [_item(c, None, "Write-back", [
        ("Editable tables (keyed)", _names(f"{t} ({', '.join(k)})" for t, k in sorted(keyed.items()))),
        ("Read-only tables (no key)", _names(sorted(t for t in c.cols if t not in keyed and t != model.CALENDAR))),
        ("Read-only tables (derived)", f"{model.CALENDAR} (the generated date table)" if model.CALENDAR in c.cols else "none"),
        ("Input tables", _names(inputs) or "none"),
        ("Who can edit", f"Editors and Owners (SC-2): {_names(editors)}"),
        ("Approval rules", ("an edit by a person applies at once; an edit by an agent is stored as a proposal with old and"
                            " new values and applies only when an Editor approves it (WB-9); rejected ones never apply")),
        ("Source sync", _names(synced) and (
            f"{_names(synced)}: an Owner writes the applied edits back to the source table (sync_edits, WB-10) after"
            " confirming a preview; one transaction, and a source value changed since an edit is reported per row and"
            " nothing is written; synced edits stay in the history as 'synced' and the table reloads from the source")
         or "none (a database source opts in with \"sync\": true)"),
    ], table={"columns": ["Table", "Active", "Proposed", "Undone", "Rejected", "Last edit", "Conflicts", "Orphaned", "Synced"],
              "rows": [[t, *(_fig(c, w, str(v)) for w, v in zip(("active", "proposed", "undone", "rejected", "last edit",
                                                                   "conflicts", "orphaned", "synced"), n))] for t, n in c.edits.items()]})]


def _when(t) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if t else ""


def _route(c, a: dict) -> str:
    """§8 item 11: agent → tools → semantic queries → measures → tables, per tool that reads the model (lineage)."""
    measures, tables = studio.scope(a, c.files, c.graph)
    out = []
    for t in [*a["tools"], *(["semantic_query"] if a["mode"] == "exploratory" and "semantic_query" not in a["tools"] else [])]:
        start = {f"tool:{t}"} if f"tools/{t}.json" in c.files else \
            {f"measure:{m}" for m in measures} | {f"table:{x}" for x in tables} if t == "semantic_query" else None
        if start:
            nodes = sorted(start | set(lineage.reach(c.graph, start, "upstream")))
            out.append(f"{t} → semantic queries → measures {_names(n[8:] for n in nodes if n.startswith('measure:')) or 'none'}"
                       f" → tables {_names(n[6:] for n in nodes if n.startswith('table:')) or 'none'}")
    return " | ".join(out)


def _tools(c):
    out = []
    for tid, t in c.tools.items():
        state, req = c.tool_state.get(tid, False), set(t["inputs"].get("required", []))  # False: never tested
        out.append(_item(c, f"tool:{tid}", t["name"], [
            ("ID", tid), ("Purpose", t["description"]),
            ("Status", f"published, version {_fig(c, 'version', t['version'])}" + ("; disabled: its tests fail (TL-4)" if state else "")
             if t["status"] == "published" else "draft: only published tools run"),
            ("Owner", _who(c, t["owner"])), ("Minimum role", t["permissions"]["role"]),
            ("Inputs", _names(f"{n} ({p['type']}" + (", required" if n in req else "") +
                              (f", default {json.dumps(p['default'])}" if "default" in p else "") + ")"
                              for n, p in t["inputs"]["properties"].items()) or "none"),
            ("Output columns", _names(t["outputs"].get("items", {}).get("properties", {}))),
            ("Implementation", f"one HTTPS request (below) to the outside host {urlsplit(t['request']['url']).hostname}; its rows come"
                               " from the response" if t.get("kind") == "api" else
                               "one semantic query (below) run by the semantic engine as the caller; every number comes from it"),
            ("Reads", _names(_ups(c, f"tool:{tid}"))),
            ("Tests", f"{_fig(c, 'tests', len(t['tests']))}; " + ("not run since the last change" if state is False
                                                 else f"failing: {state}" if state else "passing on the current data")),
            ("Used by", _names(n for n in _downs(c, f"tool:{tid}") if n.startswith(("agent:", "flow:"))) or "no agent or flow"),
        ], code=json.dumps({k: t[k] for k in ("inputs", "query", "request", "rows", "outputs") if k in t}, indent=2)))
    out.append(_item(c, None, "Built-in tools (TL-2)", [("Use", "every custom agent may hold them (D-3); flows run them as steps")],
                     table={"columns": ["Tool", "Minimum role", "What it does"], "rows": [
                         [n, ACTIONS[n].role.name, (ACTIONS[n].fn.__doc__ or "").split(". ")[0]] for n in builtin.BUILTIN_TOOLS]}))
    return out


def _agents(c):
    names = [f["function"]["name"] for f in agent.tools()]
    out = [_item(c, None, "Main Agent", [
        ("Purpose", "Answers questions, builds and changes projects for the person chatting with it (§5)"),
        ("Instructions", " | ".join(agent.INSTRUCTIONS)),
        ("Knowledge scope", "every project the person has a role in, within that role"),
        ("Tools", f"the {_fig(c, 'actions', len(names))} actions of the action API, except those that take a secret (SC-4): {_names(names)}"),
        ("How it reaches the semantic model", "Main Agent → semantic_query and published tools → semantic engine → measures → tables"),
        ("Mode", "exploratory: any semantic query the person may run"),
        ("Grounding", "on: an answer with numbers is checked against this chat's tool results, retried once, then a template answer"),
        ("Channels", "in-app chat"), ("Owners", "workspace owners"),
    ])]
    for aid, a in c.agents.items():
        state, s = c.agent_state.get(aid, False), a["scope"]
        cases = len((c.files.get(a["evaluation"]) or {}).get("cases", []))
        out.append(_item(c, f"agent:{aid}", a["name"], [
            ("ID", aid), ("Purpose", a["purpose"]), ("Instructions", a["instructions"]),
            ("Status", f"published, version {_fig(c, 'version', a['version'])}" + ("; disabled: its evaluation fails (AG-4)" if state else "")
             if a["status"] == "published" else "draft: only editors can test it"),
            ("Knowledge scope", "; ".join(f"{k} {_names(v)}" for k, v in s.items() if v) or "nothing"),
            ("Tools", _names(a["tools"])), ("How it reaches the semantic model", _route(c, a)),
            ("Mode", a["mode"].replace("_", " ")), ("Grounding", "on" if a["grounding"] else "off (in-app only; always on in flows)"),
            ("Channels", _names(" ".join(x for x in (ch["type"], ch.get("address"), ch.get("channel")) if x) for ch in a["channels"])),
            ("Owner", _who(c, a["owner"])),
            ("Evaluation", f"{_fig(c, 'cases', cases)} case(s); " + ("not run since the last change" if state is False
                                                   else f"failing: {state}" if state else "every case passes")),
        ]))
    return out


def _recipients(s: dict) -> list[tuple[str, str]]:
    """(kind, recipient) a flow step sends to: an action step's `to` addresses or `channel`."""
    p = (s.get("input") or {}) if s["type"] == "action" else {}
    to = p.get("to") if isinstance(p.get("to"), list) else []
    return [("email", x) for x in to] + ([("channel", p["channel"])] if isinstance(p.get("channel"), str) else [])


def _flow_step(s: dict) -> str:
    """One step without its free-form input (DC-4: recipients and subject only)."""
    if s["type"] == "action":
        p = s.get("input") or {}
        return f"action {s['action']}" + "".join(f" {k} {json.dumps(p[k])}" for k in ("to", "channel", "subject", "table") if k in p) \
            + (" with this documentation attached (docs.zip)" if p.get("docs") is True else "") + _graph(s)
    return {"refresh": lambda: f"refresh {s['table']}", "tool": lambda: f"tool:{s['tool']} {json.dumps(s.get('inputs', {}))}",
            "agent": lambda: f"agent:{s['agent']} asked {json.dumps(s['prompt'])}"
            + (f", answers one of {', '.join(s['choices'])}" if s.get("choices") else ""),
            "snapshot": lambda: f"snapshot of report:{s['report']} " + (f"page {s['page']}" if "page" in s else "all pages" if s["pages"] == "all"
                                                                         else "pages " + ", ".join(s["pages"])) + (f" visual {s['visual']}" if s.get("visual") else ""),
            "condition": lambda: f"only if the data behind {s['changed']} changed" if "changed" in s
            else f"only if {s['measure']} {s['op']} {s['value']} in {json.dumps(s['query'])}",
            "approval": lambda: f"wait for approval: {json.dumps(s['message'])}",
            "train": lambda: f"train ml:{s['model']}", "score": lambda: f"score ml:{s['model']}",
            }[s["type"]]() + _graph(s)


def _graph(s: dict) -> str:
    """FLOW-DAG: what a step waits for and when it runs (nothing for a step after the one listed before it)."""
    w = s.get("when")
    return ("" if "after" not in s else f" (after {', '.join(s['after'])})" if s["after"] else " (at the trigger)") \
        + (f" when {w['value']} {w['op']} {json.dumps(w['to'])}" if w else "")


def _delivery(c):
    d = c.files["project.json"].get("settings", {}).get("delivery", {})
    refs = lambda v: _names(f"secret {n}" for n in re.findall(credentials.REF, v or ""))  # DC-4: names, never values
    smtp = d.get("smtp")
    return [_item(c, None, "Delivery", [
        ("Email server", smtp and f"{smtp['host']}:{smtp['port']}, from {smtp['from']}, {smtp['security']}"
                                  + (f", sign-in {smtp['username']} with {refs(smtp['password'])}" if smtp.get("username") else "")
                                  or "none: send_email needs settings.delivery.smtp in project.json"),
        ("First sends", "the first send to a new email domain or channel asks a person to confirm (FL-6); task test runs go to the owner's inbox (AT-4)"),
    ], table={"columns": ["Channel", "Type", "Host", "Credentials"], "rows": [[builtin.INBOX, "in-app inbox", "", ""]] + [
        [n, ch["type"], urlsplit(ch["url"]).hostname or "", refs(ch["url"])]
        for n, ch in sorted(d.get("channels", {}).items())]})]


SERVER_RUN = re.compile(r"(manual|test|webhook|message|action)-[0-9a-f]{32}|(condition-after-)?\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z"
                        r"|condition-first|(snapshot|writeback)-\d+")


def _run(c, key: str) -> str:
    """A run key as a row only when the server made it all: `api-<key>` holds the caller's key and `quality-<table>-…` a
    table name, whose digits must not ground (D-114)."""
    return _fig(c, "run", key) if SERVER_RUN.fullmatch(key) else key


def _flows(c):
    out = []
    for fid, f in c.flows.items():
        t, runs, fails = f["trigger"], c.runs.get(fid, []), c.failures.get(fid, [])
        nxt = t["type"] == "schedule" and f["enabled"] and croniter(t["cron"], datetime.now(ZoneInfo(t["timezone"]))).get_next(datetime)
        out.append(_item(c, f"flow:{fid}", f["name"], [
            ("ID", fid), ("Kind", "agent task (AT-3)" if any(s["type"] == "agent" for s in f["steps"]) else "flow"),
            ("Description", f.get("description")),
            ("Trigger", {"manual": "manual (Run now)", "schedule": f"schedule {t.get('cron')} ({t.get('timezone')})",
                         "after_refresh": f"after a refresh of {_names(t.get('tables', []))}",
                         "data_condition": f"when {t.get('measure')} {t.get('op')} {t.get('value')} starts to hold in"
                                           f" {json.dumps(t.get('query'))} (checked every minute, at most once per"
                                           f" {t.get('cooldown_minutes', 1440)} minutes)",
                         "writeback": f"after an applied, approved or undone edit of {_names(t.get('tables', []))}",
                         "webhook": "per signed call to its webhook (FL-2, SC-8)",
                         "data_quality": f"once per new data-quality problem (IN-8) of {_names(t['tables']) if t.get('tables') else 'any table'}",
                         "message": f"per {t.get('channel')} message to {t.get('address') or t.get('channel_id')} from a linked sender"}[t["type"]]),
            ("Enabled", "yes" if f["enabled"] else "no: only a manual run starts it"),
            ("Next scheduled run", nxt and _fig(c, "next run", _when(nxt))),
            ("Runs as", "the message's sender (§11 #12)" if t["type"] == "message"
             else f"{_who(c, f['owner'])} (owner, FL-6)"),
            ("Steps", " | ".join(f"{i}. {s['id']}: {_flow_step(s)}" for i, s in enumerate(f["steps"], 1))),
            ("Recipients", _names(sorted({r for s in f["steps"] for _, r in _recipients(s)}))),
            ("Failure history", f"{_fig(c, 'failed runs', c.failed.get(fid, 0))} failed run(s)" +
             "".join(f" | {_fig(c, 'failed', _when(r['created']))}: {r['error']}" for r in fails)),
        ], table={"columns": ["Last runs", "Status", "Started", "Finished", "Error"],
                  "rows": [[_run(c, r["id"].rsplit("/", 1)[1]), r["status"], _fig(c, "started", _when(r["created"])),
                            _fig(c, "finished", _when(r["finished"])), r["error"] or ""] for r in runs]}))
    return out + _delivery(c) if out or c.files["project.json"].get("settings", {}).get("delivery") else out


def _drift(c, s: dict | None) -> str:
    d = (s or {}).get("drift") or {}
    if not s:
        return "not scored yet"
    if d.get("drifted") is None:
        return f"unknown (last scoring {s['status']}; drift needs a training run with reference data)"
    if not d["drifted"]:
        return f"no drift at the last scoring ({_fig(c, 'last scoring', _when(s['created']))})"
    return (f"drift in {_names(d['drifted'])} at the last scoring ({_fig(c, 'last scoring', _when(s['created']))}): retrain it. "
            + _names(f"{k} {_fig(c, 'drift', v)}" for k, v in [*(d.get("psi") or {}).items(), *((k, d.get(k)) for k in ("recent_mae", "backtest_mae"))] if v is not None))


def _search(c, t, x) -> str:
    """ML-ZOO.2 (DC-1): the search a training run did and the setting it kept; a setting is a figure only when code made
    it, as in ml.trial_rows (D-114: values the model file lists are its author's)."""
    own = t["method"] in ((x["spec"] or {}).get("space") or {})
    tried = " and ".join(f"{_fig(c, 'trials', v['trials'])} settings of {ml.LABEL[k]}"
                         + (f" ({_fig(c, 'trials skipped', v['trials skipped'])} skipped: time budget)" if "trials skipped" in v else "")
                         for k, v in t["candidates"].items() if "trials" in v)
    kept = ", ".join(f"{k} {v if own else _fig(c, 'setting', v)}" for k, v in x["params"].items()) or "its defaults"
    return (f"{x['spec']['search']['strategy']} search on {_fig(c, 'search folds', t['holdout'].get('search folds'))} folds of"
            f" the training part: {tried}; kept {ml.LABEL[t['method']]} with {kept}")


def _ml(c):
    """§8 item 13 from the registry (ML-4) and lineage; numbers come from the stored runs (principle 7)."""
    out = []
    for mid, m in c.ml.items():
        t, s, node = c.ml_runs.get((mid, "train")), c.ml_runs.get((mid, "score")), f"ml:{mid}"
        ok = t and t["status"] == "ok"
        out.append(_item(c, node, m["name"], [
            ("ID", mid), ("Task", m["task"] + (f", {m['horizon']} period(s) ahead" if m.get("horizon") else "")),
            ("Method", ok and t["method"] or m.get("method", "auto")),
            ("Training dataset", f"dataset:{m['dataset']}" + (ok and f", version: repository commit {_fig(c, 'commit', t['commit'][:12])},"
                                                                    f" lake snapshot {_fig(c, 'snapshot', t['snapshot'])}" or "")),
            ("Target", m.get("target")), ("Time", m.get("time")), ("Groups (k)", m.get("k") or m["task"] == "clustering" and "auto: 2 to 10 (k-means by silhouette, Gaussian mixture by BIC)"),
            ("Anomaly rule", m["task"] == "anomaly" and unsupervised.RULES[
                ok and t["method"] or m.get("method", "auto")]),
            ("Output", m["task"] == "clustering" and f"dimension ml_{mid}.cluster, measure {mid[:57]}_rows" or m["task"] == "anomaly"
             and f"dimensions ml_{mid}.anomaly, measures {mid[:57]}_rows and {mid[:57]}_score (largest anomaly score)"),
            ("Group sizes", ok and not c.hide and t["groups"] and "; ".join(f"group {_fig(c, 'group', g['cluster'])}: {_fig(c, 'rows', g['rows']):,} rows" for g in t["groups"])),
            ("Features", "its own history per series" if m["task"] == "forecast" else _names(m.get("features") or ["every other column of the dataset"])),
            ("Left out (leakage suspects)", ok and _names(t["excluded"] or [])),
            ("Accuracy vs baseline", t["summary"] if ok else "not trained yet" if not t else f"last training {t['status']}"),
            ("Search", ok and not c.hide and (x := c.search.get(t["run"])) and _search(c, t, x)),
            ("Fixed setting", m.get("params") and ", ".join(f"{k} {v}" for p in m["params"].values() for k, v in p.items())),
            ("Last training", t and f"{_fig(c, 'last training', _when(t['created']))} by {_who(c, t['user_id'])}: {t['status']}"),
            # the predictions table and what reads it; a flow that only trains or scores the model reads the model, not them
            ("Where predictions are used", (p := f"table:ml_{mid}") in c.graph and _names([p, *_downs(c, p)])
             or "nowhere yet: score it (score_model)"),
            ("Drift status", HIDDEN if c.hide and s else _drift(c, s)),
        ], table=ok and not c.hide and {"columns": ["Metric", "Model", "Baseline"], "rows": [
            # metric names are code-made (ML-4), some with a figure: `silhouette k=7` (GATE-9-fix-8.7)
            [_fig(c, "metric name", k), str(_fig(c, "metric", v)), str(_fig(c, "baseline", t["baseline"].get(k, "")))]
            for k, v in t["metrics"].items()]} or None))
    return out


ROLES = ("SELECT u.name, r.role FROM project_roles r JOIN users u ON u.id = r.user_id WHERE r.project = %s"
         " UNION SELECT name, 'owner (workspace)' FROM users WHERE workspace_owner ORDER BY 1")


def _security(c):
    with db.connect() as conn:
        rows = conn.execute(ROLES, [c.project]).fetchall()
    return [_item(c, None, "Roles", [("Rule", ("Viewer: view and ask; Editor: change definitions and data; Owner: also roles and"
                                               " secrets (SC-2). Every action is checked and audit-logged (SC-6)."))],
                  table={"columns": ["User", "Role"], "rows": [[r["name"], r["role"]] for r in rows]})] + _rls(c) + _external(c)


def _who(c, uid) -> str:
    """A user's name, or for one who isn't a member a letter label (D-106), never the ID."""
    uid = str(uid)
    return c.users.get(uid) or c.others.setdefault(uid, semantic.rls.outsider(len(c.others)))


def _rls(c):
    """§8 item 14 row-level rules (SC-3): who each rule covers, by user name; the values are row data, never listed."""
    f = c.files.get("security/rls.json", {})
    rs, cs = f.get("rules", []), f.get("columns", [])
    return (rs and [_item(c, None, "Row-level rules", [("Rule", (
        "Viewers listed in a rule see only rows of its table whose column holds their values; a Viewer not listed sees no"
        " rows of it, and facts related to a filtered table are filtered too. Owners and Editors read every row (D-42)."))],
        table={"columns": ["Rule", "Table", "Column", "Viewers"], "rows": [
            [r["id"], r["table"], r["column"], _names(sorted(_who(c, u) for u in sorted(r["users"], key=lambda u: (len(u), u))))] for r in rs]})]
    ) + (cs and [_item(c, None, "Column rules", [("Rule", (
        "Viewers never see the values of these columns: no reads, exports or measures over them, and tables built from"
        " the table need a column rule of their own. Owners and Editors read every column (D-110)."))],
        table={"columns": ["Rule", "Table", "Hidden columns"], "rows": [[r["id"], r["table"], _names(r["columns"])] for r in cs]})])


def _external(c):
    """§8 item 14: everyone outside the project that flows and custom agents send to."""
    used: dict = {}
    for fid, f in c.flows.items():
        for s in f["steps"]:
            for kind, r in _recipients(s):
                used.setdefault((r, kind), set()).add(f"flow:{fid}")
    for aid, a in c.agents.items():
        for ch in a["channels"]:
            if r := ch.get("address") or ch.get("channel"):
                used.setdefault((r, ch["type"]), set()).add(f"agent:{aid}")
    rows = [[r, kind, _names(sorted(by))] for (r, kind), by in sorted(used.items()) if r != builtin.INBOX]
    return [_item(c, None, "External recipients", table={"columns": ["Recipient", "Type", "Used by"], "rows": rows})] if rows else []


def _lineage(c):
    edges = sorted((u, n, ", ".join(sorted(v))) for n, ups in c.graph.items() for u, v in ups.items())
    if not edges:
        return []
    return [_item(c, None, "Source to every consumer", table={"columns": ["From", "To", "Via"], "rows": [list(e) for e in edges]},
                  diagram="\n".join(["flowchart LR"] + [f'  {_mid(u)}["{u}"] --> {_mid(n)}["{n}"]' for u, n, _ in edges]))]


def _runbook(c):
    return [_item(c, None, "Operations", [
        ("Schedules", ("File sources refresh on change: the worker checks every minute (IN-6); snapshots older than"
                       f" {_fig(c, 'retention days', storage.retention_days()):g} days expire daily at 03:17 (D-12).")),
        ("A refresh fails", ("The notification feed shows the error once. Fix the file or the source, and the next check"
                             " reloads it; the previous data stays live until a load succeeds.")),
        ("Restore data", "list_snapshots (Editor) lists the snapshot ids; restore_snapshot (Owner, with confirmation) restores any snapshot inside the retention window."),
        ("Restore definitions", "The Changes tab: undo uncommitted changes, or revert to any committed version."),
        ("A flow fails", c.flows and ("The run stops at the failed step and the feed says so once (FL-5); a failure a retry may"
                                      " fix is retried by the queue, resuming after the steps that worked, so nothing is sent twice"
                                      " (FL-4). Read the step trace in the run history, fix the cause, then run it again.")),
        ("Flow schedules", c.flows and "Scheduled flows fire from the worker's tick every minute; after an outage only the latest missed fire runs."),
        ("A tool fails", c.tools and ("After a model or tool change its tests run again; a failing tool is disabled and the feed names"
                                      " its owner (TL-4). Fix the tool or the model; it is enabled again once its tests pass.")),
        ("An agent fails", c.agents and ("Its evaluation set runs again after a model, tool or agent change; a failing agent is"
                                         " disabled (AG-4) until the set passes again.")),
    ])]


def _changelog(c):
    r = git.repo(c.project)
    commits = list(r.walk(r.head.target, SortMode.TOPOLOGICAL | SortMode.TIME))[:20]
    return [_item(c, None, "Recent changes", table={"columns": ["Time (UTC)", "By", "Change"], "rows": [
        [_fig(c, "changed", time.strftime("%Y-%m-%d %H:%M", time.gmtime(x.author.time))),
         x.author.name + (" (agent)" if x.author.email.startswith("agent-") else ""), x.message.split("\n")[0]]
        for x in commits]})]


def _issues(c):
    rows = _issue_rows(c)
    return [_item(c, None, "Open issues", table={"columns": ["Issue", "Detail"], "rows": rows})] if rows else []


def _issue_rows(c) -> list[list[str]]:
    rows = [["Readiness gap", g] for g in c.gaps]
    rows += [[what, f"{t}: {n[i]} edit(s)"] for t, n in c.edits.items()
             for i, what in [(5, "Conflicting edits (source value changed, still applied)"), (6, "Orphaned edits (row or column gone)")] if n[i]]
    rows += [["Disabled tool (failing tests)", f"tool:{t}: {e}"] for t, e in sorted(c.tool_state.items()) if e and t in c.tools]
    rows += [["Disabled agent (failing evaluation)", f"agent:{a}: {e}"] for a, e in sorted(c.agent_state.items()) if e and a in c.agents]
    rows += [["Flow's last run failed", f"flow:{f}: {r[0]['error']}"] for f, r in sorted(c.runs.items()) if f in c.flows and r[0]["status"] == "failed"]
    for obj in sorted(c.texts):
        e = _explanation(obj, c.texts, c.marks)
        if e["by"] is None:
            rows.append(["Missing description", obj])
        elif e["by"] == "agent":
            rows.append(["Unreviewed description", obj])
        if e["outdated"]:
            rows.append(["Outdated description", obj])
    return rows


QUESTION = {
    "Readiness gap": "How should this readiness gap be fixed: {}?",
    "Missing description": "What does {} mean? It has no description yet.",
    "Unreviewed description": "Is the agent-written description of {} correct?",
    "Outdated description": "The definition of {} changed after its description was written: does the description still hold?",
}


def _handover(c):
    """DC-7 (E-03.md decision 7): an onboarding checklist and questions for the previous owner, from metadata and tool
    state only (principle 7); each checklist line only when it applies; data-derived text hidden under RLS (SE-6)."""
    with db.connect() as conn:
        roles = conn.execute(ROLES, [c.project]).fetchall()
        refresh = [r for r in conn.execute("SELECT DISTINCT ON (ref) ref, message FROM notifications WHERE project = %s"
                                           " AND kind = 'refresh' AND ref IS NOT NULL ORDER BY ref, id DESC", [c.project])
                   if f"sources/{r['ref']}.json" in c.files and not r["message"].endswith("works again.")]
        problems = [r for r in quality._rows(conn, c.project) if r[-1]]
    secrets = sorted({n for f in c.files.values() for n in re.findall(credentials.REF, dumps(f))})  # DC-4: names only
    others = [f"{'task' if any(s['type'] == 'agent' for s in f['steps']) else 'flow'} {fid} (owner {_who(c, f['owner'])})"
              for fid, f in c.flows.items() if f["owner"] != str(c.user.id)]  # FL-6: runs use the owner's role
    steps = [
        roles and ["Transfer roles", ("Make sure someone who stays holds the Owner role before the previous owner leaves."
                                      f" Roles now: {'; '.join(f'{r['name']}: {r['role']}' for r in roles)}.")],
        secrets and ["Re-own secrets", (f"The project uses secrets {_names(secrets)} (names only; values are never shown)."
                                        " An Owner rotates them with set_secret.")],
        others and ["Take over flows and tasks", "They run as their owner (FL-6); open and save each to own it: " + _names(others)],
        *[["Fix failing tool", f"tool:{t}: {e}"] for t, e in sorted(c.tool_state.items()) if e and t in c.tools],
        *[["Fix source refresh", f"{r['ref']}: {HIDDEN if c.hide else r['message']}"] for r in refresh],
        *[["Check data quality", f"{r[0]}: {HIDDEN if c.hide else '; '.join(r[-1])}"] for r in problems],
    ]
    asked = [[QUESTION[k].format(d) if k in QUESTION else f"{k}: {d}. What causes it, and how is it resolved?"]
             for k, d in _issue_rows(c)]
    return [_item(c, None, "Onboarding checklist", table={"columns": ["Step", "Detail"], "rows": [s for s in steps if s]}),
            _item(c, None, "Questions to ask the previous owner",
                  [("Questions", not asked and "none: the project has no open issues")],
                  table=asked and {"columns": ["Question"], "rows": asked} or None)]


SECTIONS = [  # (§8 number, id, title, builder)
    (1, "start", "Start here", _start), (2, "architecture", "Architecture", _architecture),
    (3, "sources", "Data sources", _sources), (4, "tables", "Tables and transformations", _tables),
    (5, "model", "Semantic model", _model), (6, "measures", "Measures", _measures), (7, "glossary", "Glossary", _glossary),
    (8, "reports", "Reports", _reports), (9, "writeback", "Write-back", _writeback),
    (10, "tools", "Tools", _tools), (11, "agents", "Agents", _agents), (12, "flows", "Agent tasks and flows", _flows),
    (13, "ml", "ML models", _ml), (14, "security", "Security", _security), (15, "lineage", "Lineage map", _lineage),
    (16, "runbook", "Operations runbook", _runbook), (17, "changes", "Change log", _changelog), (18, "issues", "Open issues", _issues),
]


HIDDEN = semantic.rls.HIDDEN


def _context(project: str, user: User, values: bool = True) -> SimpleNamespace:
    """`values` False: no data values at all (row counts, worked examples, edit counts), for docs sent outside the app."""
    git.repo(project)  # 404 before touching the lake for a project that doesn't exist
    try:
        files = bundle.bundle(git.path(project))
        steps = transform.tables(project)
    except ValueError as e:
        raise ActionFailed(400, str(e))
    c = SimpleNamespace(project=project, user=user, values=values, files=files, steps=steps, now=time.time(), figures=[],
                        name=files["project.json"]["name"], model=model.load(project), cols=model.lake_columns(project),
                        graph=lineage.graph(files), texts=_explained(project, files), marks=_marks(project), gaps=model.check(project))
    c.facts = [t for t, i in model.visible(c.model)["tables"].items() if i["kind"] == "fact"]
    c.rows = {t: _read(c, f"SELECT count(*) FROM {storage._q(t)}")[0][0] for t in c.cols} if values else {}
    # SE-6: edit logs hold every row's edits; a restricted Viewer gets no counts (ponytail: per-row filtered counts with E-02.7)
    # SE-6 (E-02.5): ML figures and flow error text come from every row: none for a restricted Viewer or docs sent out
    c.hide = not values or semantic.rls.restricted(project, user.id)
    c.edits = {} if c.hide else _edits(c)  # sections 9 and 18
    c.tools, c.agents, c.flows, c.ml = ({m[1]: f for rel, f in sorted(files.items()) if (m := re.fullmatch(rf"{d}/([^/.]+)\.json", rel))}
                                  for d in ("tools", "agents", "flows", "ml"))
    with db.connect() as conn:
        c.ml_runs = {k: ml.hidden(r) if c.hide else r for k, r in ml.latest(conn, project).items()}
        c.search = {r["run"]: r for r in conn.execute(  # ML-ZOO.2: the setting each latest training run kept
            "SELECT t.run::text AS run, t.params, r.spec FROM ml_trials t JOIN ml_runs r ON r.id = t.run AND r.method = t.method"
            " WHERE t.chosen AND t.run::text = ANY(%s)", [[r["run"] for (_, k), r in c.ml_runs.items() if k == "train"]])}
        c.owners = [r["name"] for r in conn.execute(("SELECT u.name FROM project_roles r JOIN users u ON u.id = r.user_id"
                                                     " WHERE r.project = %s AND r.role = 'owner' ORDER BY 1"), [project])]
        c.users = {str(r["id"]): r["name"] for r in conn.execute(f"SELECT id, name FROM users WHERE {MEMBERS}", [project, project])}
        c.others = {}  # D-106: users named here who aren't members, by letter in the order the docs name them
        c.tool_state = {r["tool"]: r["error"] for r in conn.execute("SELECT tool, error FROM tool_state WHERE project = %s", [project])}
        c.agent_state = {r["agent"]: r["error"] for r in conn.execute("SELECT agent, error FROM agent_state WHERE project = %s", [project])}
        c.failed = {r["flow"]: r["n"] for r in conn.execute(
            "SELECT flow, count(*) AS n FROM flow_runs WHERE project = %s AND status = 'failed' GROUP BY flow", [project])}
        c.runs, c.failures = ({}, {})
        for into, where in ((c.runs, ""), (c.failures, " AND status = 'failed'")):  # the latest 5 per flow, newest first
            for r in conn.execute("SELECT * FROM (SELECT id, flow, status, error, created, finished, row_number() OVER"
                                  f" (PARTITION BY flow ORDER BY created DESC, id) AS n FROM flow_runs WHERE project = %s{where})"
                                  " x WHERE n <= 5 ORDER BY n", [project]):
                into.setdefault(r["flow"], []).append(r | {"error": r["error"] and HIDDEN} if c.hide else r)
    return c


def build(project: str, user: User, obj: str | None = None, values: bool = True, handover: bool = False) -> dict:
    """One project's docs; with `obj`, only that object's items (DC-1 per object, DC-8); `handover` adds DC-7's section."""
    # ponytail: an object's view builds every section and filters; build per section if big projects make it slow
    c = _context(project, user, values)
    sections = []
    for n, sid, title, fn in SECTIONS:
        items = [i for i in fn(c) if obj is None or i["object"] == obj]
        if items:
            sections.append({"id": sid, "number": _fig(c, "section", n), "title": title, "items": items})
    if handover and obj is None:
        sections.append({"id": "handover", "number": _fig(c, "section", 19), "title": "Handover", "items": _handover(c)})
    for s in sections:  # AGENT-CONTEXT-fix-7: the agent's generate_docs summary counts them
        _fig(c, "items", len(s["items"]))
    if obj and not sections:
        raise ActionFailed(404, f"No object '{obj}' in the docs of project '{project}'. Name it as \"<kind>:<id>\", e.g. \"table:orders\".")
    review = [{"object": o, **e} for o in sorted(c.texts) if (e := _explanation(o, c.texts, c.marks))["by"] != "human" or e["outdated"]]
    _fig(c, "to review", len(review))
    return {"project": project, "name": c.name, "sections": sections, "review": review, "figures": c.figures}


# --- Markdown bundle (DC-3) ---------------------------------------------------------------------------------------

LABEL = {"agent": "agent-written, unreviewed", "human": "human-reviewed"}


def _cell(v: str) -> str:
    return v.replace("|", "\\|").replace("\n", " ")


def _md_item(i: dict, when: str) -> str:
    out = [f"### {i['title']}" + (f" (`{i['object']}`)" if i["object"] else ""), "",
           f"*Structural: generated from metadata at {when}.*", ""]
    out += [f"- **{k}:** {v}" for k, v in i["facts"]]
    if t := i["table"]:
        out += ["", "| " + " | ".join(t["columns"]) + " |", "|" + " --- |" * len(t["columns"])]
        out += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in t["rows"]]
    if i["code"]:
        out += ["", "```" + ("sql" if i["code"].startswith("WITH") else ""), i["code"], "```"]
    if i["diagram"]:
        out += ["", "```mermaid", i["diagram"], "```"]
    if e := i["explanation"]:
        tag = LABEL.get(e["by"], "missing: no description yet") + ("; may be outdated" if e["outdated"] else "")
        out += ["", f"> **Explanatory** ({tag})" + (f"\n>\n> {e['text']}" if e["text"] else "")]
    return "\n".join(out) + "\n"


def markdown(doc: dict, when: str) -> dict[str, str]:
    """{path under docs/: text}: README.md with the contents, one file per section."""
    files = {}
    toc = [f"# {doc['name']}: documentation", "", (f"Generated {when}. Each item is marked: *Structural* is read from the"
            " system when generated; *Explanatory* text is agent-written (unreviewed) or human-reviewed."), ""]
    for s in doc["sections"]:
        name = f"{s['number']:02d}-{s['id']}.md"
        toc.append(f"{s['number']}. [{s['title']}]({name})")
        files[name] = f"# {s['number']}. {s['title']}\n\n" + "\n".join(_md_item(i, when) for i in s["items"])
    return {"README.md": "\n".join(toc) + "\n", **files}


def package(user: User, projects: list[str], workspace: bool, obj: str | None = None, values: bool = True,
            handover: bool = False, pick: bool = False) -> dict:
    """The in-app package plus its Markdown bundle; a workspace bundle has one folder per project. `pick`: only the
    items relevant to the Main Agent turn's question (D-117), when ranking applies."""
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    docs = [build(p, user, obj, values, handover) for p in projects]
    cut = [_relevant(user, d) for d in docs] if pick else []
    md = {d["project"]: markdown(d, when) for d in docs}
    # D-22: the figures shown and the file names (without the project folder: its ID is author-written, D-114) as rows
    rows = [["generated", when], *(["file", k] for k in sorted({k for m in md.values() for k in m})),
            *(f for d in docs for f in d.pop("figures"))]
    return {"generated": when, "projects": docs, "columns": ["figure", "value"], "rows": rows,
            "files": {(f"{p}/" if workspace else "") + k: v for p, m in md.items() for k, v in m.items()}} \
        | ({"relevant": relevant.NOTE} if any(cut) else {})


def _relevant(user: User, d: dict) -> bool:
    """D-117: keep the doc items closest to the turn's question, ranked on their titles, object IDs and descriptions
    only (never facts or tables, which may hold values, SC-5). True when items were dropped."""
    items = {(n, k): " ".join(filter(None, [i["title"], i["object"], (i["explanation"] or {}).get("text")]))
             for n, s in enumerate(d["sections"]) for k, i in enumerate(s["items"])}
    if (keep := relevant.ranked(user, d["project"], relevant.QUESTION.get(), items)) is None:
        return False
    d["sections"] = [s | {"items": kept} for n, s in enumerate(d["sections"])
                     if (kept := [i for k, i in enumerate(s["items"]) if (n, k) in keep])]
    return True


# --- HTML site and PDF (DC-3), branded by the project's tenant (SC-9, E-05.md decision 9) ---------------------------

CSS = ("body{margin:0;font:15px/1.5 system-ui,sans-serif;color:#1a1a1a;background:#fff}main{max-width:60rem;margin:0 auto;"
       "padding:1rem}header{padding:.75rem 1rem}header img{height:40px;vertical-align:middle;margin-right:.75rem}"
       "table{border-collapse:collapse}th,td{border:1px solid #767676;padding:.25rem .5rem;text-align:left;vertical-align:top}"
       "pre{background:#f4f4f4;padding:.5rem;white-space:pre-wrap}dt{font-weight:bold}.tag{color:#555}"
       "blockquote{border-left:4px solid #767676;margin:1rem 0;padding-left:1rem}")


def _e(v) -> str:
    return escape(str(v))  # every value is Editor or source text (names, descriptions, SQL): never markup


def _html_item(i: dict, when: str, h: int) -> str:
    out = [f"<section><h{h}>{_e(i['title'])}" + (f" (<code>{_e(i['object'])}</code>)" if i["object"] else "") + f"</h{h}>",
           f'<p class="tag"><em>Structural: generated from metadata at {_e(when)}.</em></p>']
    if i["facts"]:
        out.append("<dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in i["facts"]) + "</dl>")
    if t := i["table"]:
        out.append("<table><thead><tr>" + "".join(f'<th scope="col">{_e(c)}</th>' for c in t["columns"]) + "</tr></thead><tbody>"
                   + "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in r) + "</tr>" for r in t["rows"]) + "</tbody></table>")
    out += [f"<pre><code>{_e(c)}</code></pre>" for c in (i["code"], i["diagram"]) if c]  # mermaid stays text: no scripts
    if e := i["explanation"]:
        tag = LABEL.get(e["by"], "missing: no description yet") + ("; may be outdated" if e["outdated"] else "")
        out.append(f"<blockquote><p><strong>Explanatory</strong> ({_e(tag)})</p>" + (f"<p>{_e(e['text'])}</p>" if e["text"] else "")
                   + "</blockquote>")
    return "\n".join(out) + "</section>\n"


def _page(title: str, body: str, brand: dict | None) -> str:
    """One standalone page; a tenant's header uses its checked #rrggbb colour and the WCAG text colour computed for it."""
    head = ""
    if brand:
        logo = f'<img alt="" src="data:{brand["logo_type"]};base64,{base64.b64encode(bytes(brand["logo"])).decode()}">' if brand["logo"] else ""
        head = (f'<header style="background:{_e(brand["color"] or "#ffffff")};color:{_e(brand["text_color"] or "#000000")}">'
                f'{logo}<strong>{_e(brand["name"])}</strong></header>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
            f' initial-scale=1"><title>{_e(title)}</title><style>{CSS}</style></head><body>{head}<main>{body}</main></body></html>\n')


def _intro(doc: dict, when: str) -> tuple[str, str]:
    title = f"{doc['name']}: documentation"
    return title, (f"<h1>{_e(title)}</h1><p>Generated {_e(when)}. Each item is marked: <em>Structural</em> is read from the system"
                   " when generated; <em>Explanatory</em> text is agent-written (unreviewed) or human-reviewed.</p>")


def html(doc: dict, when: str, brand: dict | None) -> dict[str, str]:
    """Static site: index.html with the contents, one page per section."""
    title, intro = _intro(doc, when)
    files, toc = {}, []
    for s in doc["sections"]:
        name, head = f"{s['number']:02d}-{s['id']}.html", f"{s['number']}. {s['title']}"
        toc.append(f'<li value="{s["number"]}"><a href="{name}">{_e(s["title"])}</a></li>')
        files[name] = _page(f"{head} | {title}", f'<p><a href="index.html">{_e(title)}</a></p><h1>{_e(head)}</h1>'
                            + "".join(_html_item(i, when, 2) for i in s["items"]), brand)
    return {"index.html": _page(title, f"{intro}<ol>{''.join(toc)}</ol>", brand), **files}


def pdf_html(doc: dict, when: str, brand: dict | None) -> str:
    """One document for the PDF: h1 project, h2 sections, h3 items (the PDF outline)."""
    title, intro = _intro(doc, when)
    return _page(title, intro + "".join(f"<h2>{s['number']}. {_e(s['title'])}</h2>" + "".join(_html_item(i, when, 3) for i in s["items"])
                                        for s in doc["sections"]), brand)


# --- actions ------------------------------------------------------------------------------------------------------

class DocsInput(BaseModel):
    project: str | None = Field(None, pattern=storage.PROJECT, description="omit for the whole workspace (your projects)")
    object: str | None = Field(None, description='"<kind>:<id>" for one object\'s docs, e.g. "table:orders" or "measure:sales"')


def _mine(conn, user: User, project: str | None) -> list[str]:
    """DC-4: a workspace-level package covers only projects the user has a role in."""
    if project:
        return [project]
    return [p for p in git.projects() if role_in(conn, user, p) is not None]


class DocsAsk(DocsInput):
    all: bool = Field(False, description="every item, not only those relevant to your question")


@action(Role.viewer)
def docs(conn, user: User, args: DocsAsk) -> dict:
    """Living documentation (DC-1) of a project, the whole workspace, or one object: structural facts read from metadata now (tables, columns, steps with compiled SQL, model, measures with MEL, dependencies and a worked example value from semantic_query, glossary, lineage, change log, open issues) and the explanatory descriptions, each marked agent-written/human-reviewed and "may be outdated". Use it to answer "what does X mean". In a large project only the items relevant to your question are listed (`relevant` says so); `all` lists every one."""
    if args.object and not args.project:
        raise ActionFailed(400, "Name the project of the object.")
    return package(user, _mine(conn, user, args.project), not args.project, args.object, pick=not (args.all or args.object))


@action(Role.editor, definition=True)
def draft_descriptions(conn, user: User, args: storage.ProjectRef) -> dict:
    """The Documenter drafts plain-language descriptions for the project, its model tables, measures, datasets, reports and ML models that have none; they are marked agent-written and unreviewed. Returns what it drafted and what is still missing. Never blocks: if the LLM fails, nothing is drafted."""
    files = bundle.bundle(git.path(args.project))
    texts, marks = _explained(args.project, files), _marks(args.project)
    unmarked = {o: "human" for o, (t, _) in texts.items() if t is not None and (marks.get(o) or {}).get("text") != _h(t)}
    missing = [o for o, (t, _) in texts.items() if t is None]
    # ponytail: at most SAMPLE objects per call (SC-5); the next Generate drafts the rest
    meta = {o: {"definition": texts[o][1], "columns": model.lake_columns(args.project).get(o.split(":", 1)[1])}
            if o.startswith("table:") else {"definition": texts[o][1]} for o in missing[:agent.SAMPLE]}
    try:
        drafts = agent.document(user, meta, args.project) if meta else {}
    except Exception as e:  # noqa: BLE001 (DC-5: generation never blocks on the Documenter)
        drafts, error = {}, f"The Documenter could not draft descriptions: {type(e).__name__}."
    else:
        error = None
    for o, text in drafts.items():
        _set_text(args.project, o, text)
    if unmarked or drafts:  # a person's texts get marked too, so their definition changes are flagged from now on
        mark(args.project, unmarked | {o: "agent" for o in drafts})
    return {"drafted": sorted(drafts), "missing": sorted(set(missing) - drafts.keys()), **({"error": error} if error else {})}


class GenerateInput(DocsInput):
    format: Literal["markdown", "html", "pdf"] = Field("markdown", description="html (static site) and pdf need a project")
    handover: bool = Field(False, description="DC-7: add a Handover section (onboarding checklist, questions to ask the previous owner)")


@action(Role.viewer)
def generate_docs(conn, user: User, args: GenerateInput) -> dict:
    """Generate Documentation (DC-2) for a project or, without one, every project you have a role in: first the Documenter drafts missing descriptions where you may edit (DC-5; items stay marked unreviewed), then the whole package: every §8 section whose objects exist, in-app and as a Markdown bundle (`files`). With `format` "html" (`files` is a static site) or "pdf", also a download `link` (DC-3)."""
    if args.format != "markdown" and not args.project:
        raise ActionFailed(400, f"Name the project to export its documentation as {args.format.upper()}.")
    projects = _mine(conn, user, args.project)
    drafted = {}
    for p in projects:
        if role_in(conn, user, p) >= Role.editor and not actions.production_refusal("draft_descriptions", p):  # D-56
            drafted[p] = actions.run("draft_descriptions", {"project": p}, user, actions.VIA.get())
    pkg = {**package(user, projects, not args.project, handover=args.handover), "drafted": drafted}
    if args.format == "markdown":
        return pkg
    # SE-6: the file holds this user's in-app view; the artifacts route serves it to them only while the project has rules
    brand, (doc,) = tenants.branding(conn, project=args.project), pkg["projects"]
    path = builtin._new_artifact(args.project, "zip" if args.format == "html" else "pdf", user)
    if args.format == "pdf":
        builtin.html_pdf(pdf_html(doc, pkg["generated"], brand), path)
    else:
        pkg["files"] = html(doc, pkg["generated"], brand)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as f:
            for name, text in pkg["files"].items():
                f.writestr(name, text)
    return pkg | {"link": builtin.link(f"artifact:{args.project}/{path.name}"),
                  # D-22: the download's file ID and the html site's file names as rows too
                  "rows": [*pkg["rows"], ["file", path.stem], *(["file", n] for n in pkg["files"] if args.format == "html")]}


class Review(storage.ProjectRef):
    object: str = Field(pattern=r"^(project|table|measure|dataset|report|ml):.+$")
    text: str | None = Field(None, min_length=1, max_length=2000, description="replaces the description; omit to keep it")


@action(Role.editor, definition=True)
def review_description(conn, user: User, args: Review) -> dict:
    """Marks a description human-reviewed (optionally with corrected text), which also clears "may be outdated". Only a person can review: the Main Agent can't."""
    if actions.by_agent():
        raise ActionFailed(403, "Only a person can mark a description reviewed; open the Docs tab to review it.")
    texts = _explained(args.project, bundle.bundle(git.path(args.project)))
    if args.object not in texts:
        raise ActionFailed(404, f"No object '{args.object}' with a description in project '{args.project}'.")
    if args.text:
        _set_text(args.project, args.object, args.text)
    elif texts[args.object][0] is None:
        raise ActionFailed(400, f"'{args.object}' has no description yet; write one.")
    mark(args.project, {args.object: "human"})
    return {}


@action(Role.editor, definition=True)
def save_docs(conn, user: User, args: storage.ProjectRef) -> dict:
    """Writes the project's documentation as Markdown into docs/ of its repository (DC-3), replacing the earlier *.md files there, as one change to commit or undo."""
    folder = git.path(args.project) / "docs"
    # SE-6: every member reads the repo (diff, changes), so under rules the committed docs carry no data values
    out = markdown(build(args.project, user, values=not semantic.rls.rules(args.project)), time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.md"):
        old.unlink()
    for name, text in out.items():
        (folder / name).write_text(text)
    files = sorted(f"docs/{n}" for n in out)
    return {"files": files, "columns": ["file"], "rows": [[f] for f in files]}  # D-22: quotable file names

