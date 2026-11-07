"""Lineage graph. PRD SE-11, glossary "Lineage", DC-8 ("what breaks if I change Y"), PC-3 (import lineage impact).

Derived only from the project files (a bundle {path: content}, M-05), never from data or an LLM.
`graph(files)` is {node: {upstream node: {via, ...}}}: a node is "<kind>:<id>" and `via` says how the node
reads that upstream object (a step type, table.column, [measure]). Each object type adds its nodes and edges
with one function in BUILDERS; reports, tools, agents, flows and ML append theirs when they exist.
Objects that are referenced but have no definition still appear as nodes (with no upstream).
"""

import re
from collections import deque
from typing import Literal

from pydantic import BaseModel, Field

from abi.actions import ActionFailed, action
from abi.project import bundle, git
from abi.security import Role, User
from abi.semantic import mel, planner


def _ids(files: dict, folder: str) -> dict:
    """{id: content} of <folder>/<id>.json"""
    return {m[1]: c for rel, c in files.items() if (m := re.fullmatch(rf"{folder}/([^/]+)\.json", rel))}


def _tables(files: dict):
    """TR-4: a source with no table file loads into the table of its ID; a table's steps read a source and the
    tables they merge or append. Raw steps, not transform.validate: lineage also maps files that don't compile."""
    tables = _ids(files, "tables")
    for sid, s in _ids(files, "sources").items():
        yield f"source:{sid}", None, None
        if sid not in tables:
            yield f"table:{sid}", f"source:{sid}", "loads from an API" if isinstance(s, dict) and s.get("api") else "loads"
    for t, c in tables.items():
        yield f"table:{t}", None, None
        for s in c.get("steps") or []:
            ups = {"source": [f"table:{s['table']}" if s.get("table") else f"source:{s.get('source')}"], "merge": [f"table:{s.get('table')}"],
                   "append": [f"table:{x}" for x in s.get("tables", [])]}.get(s.get("type"), [])
            for up in ups:
                if up != f"table:{t}":
                    yield f"table:{t}", up, s["type"]


def _model(files: dict):
    """SE-1/SE-2: model tables, entities (keyed tables) and relationships."""
    m = files.get("model/model.json") or {}
    for t in m.get("tables", []):
        yield f"table:{t['table']}", None, None
    for e in m.get("entities", []):
        yield f"entity:{e['id']}", f"table:{e['table']}", "key " + ", ".join(e["key"])
    for r in m.get("relationships", []):
        for end in (r["from"], r["to"]):
            yield f"relationship:{r['id']}", f"table:{end['table']}", ", ".join(f"{end['table']}.{c}" for c in end["columns"])


def _measures(files: dict):
    """SE-3: the columns and [measures] a MEL expression names; time intelligence reads the date table."""
    date = ((files.get("model/model.json") or {}).get("date_table") or {}).get("table")
    for m in (files.get("model/measures.json") or {}).get("measures", []):
        node = f"measure:{m['id']}"
        yield node, None, None
        try:
            tree = list(mel._walk(mel.parse(m["expression"])))
        except mel._Bad:  # doesn't parse: no edges; the import's MEL compile and readiness report it
            tree = []
        for n in tree:
            if type(n) is mel.Ref:
                yield node, f"measure:{n.name}", f"[{n.name}]"
            elif type(n) is mel.Col:
                yield node, f"table:{n.table}", f"{n.table}.{n.column}"
            elif type(n) is mel.Time and date:
                yield node, f"table:{date}", "date table"
        yield from _joins(files, node, [m["id"]], {n.table for n in tree if type(n) is mel.Col})


def _facts(files: dict, measures: list) -> set[str]:
    """The fact tables the measures aggregate, through their [Measure] references."""
    ms = {m["id"]: m["expression"] for m in (files.get("model/measures.json") or {}).get("measures", [])}
    out, seen, todo = set(), set(), list(measures)
    while todo:
        if (x := todo.pop()) in seen or x not in ms:
            continue
        seen.add(x)
        try:
            tree = mel._walk(mel.parse(ms[x]))
        except mel._Bad:
            continue
        for n in tree:
            if type(n) is mel.Agg:
                out.add(n.col.table)
            elif type(n) is mel.Ref:
                todo.append(n.name)
    return out


def _joins(files: dict, node: str, measures: list, tables: set, named: list = ()):
    """D-06: the relationships the planner joins from the measures' facts to `tables`, the whole snowflake chain, so
    changing a dimension in the middle of it shows what breaks; 1.2.6 M5: and the link it follows (`planner.route`:
    `named`, the query's links, or a default one). A path the planner refuses adds no edge."""
    model = files.get("model/model.json") or {}
    try:
        links = planner.links(model, files.get("model/ontology.json"), named)
    except planner.PlanError:  # a named link that doesn't hold: the query is refused, so the routes are the model's
        links = planner.links(model, files.get("model/ontology.json"))
    for f in sorted(_facts(files, measures)):
        for t in sorted(tables - {f}):
            try:
                rels, link = planner.route(model, f, t, links=links)
            except planner.PlanError:
                continue
            if link and link not in named:  # a named one has its edge from `_query`
                yield node, f"link:{link}", "join"
            for r, _ in rels:
                yield node, f"relationship:{r['id']}", "join"


def _query(files: dict, node: str, q: dict):
    """A semantic query (S-04) reads its measures, its table.column dimensions and filters, and the relationships
    joining them."""
    yield node, None, None
    for m in q.get("measures", []):
        yield node, f"measure:{m}", "measure"
    cols = q.get("dimensions", []) + [f["column"] for f in q.get("filters", [])]
    for c in cols:
        yield node, "table:" + c.split(".", 1)[0], c
    for ln in q.get("links", []):  # 1.2.6 M5: the links it names decide its routes
        yield node, f"link:{ln}", "link"
    yield from _joins(files, node, q.get("measures", []), {c.split(".", 1)[0] for c in cols}, q.get("links", []))


def _datasets(files: dict):
    """SE-1 datasets: the measures and table.column dimensions and filters of their saved query."""
    for d in (files.get("model/datasets.json") or {}).get("datasets", []):
        yield from _query(files, f"dataset:{d['id']}", d["query"])


def _glossary(files: dict):
    """SE-1 glossary terms: the measures and columns that implement them."""
    for t in (files.get("model/glossary.json") or {}).get("terms", []):
        node = f"term:{t['id']}"
        yield node, None, None
        for m in t.get("measures", []):
            yield node, f"measure:{m}", "measure"
        for c in t.get("columns", []):
            yield node, f"table:{c['table']}", f"{c['table']}.{c['column']}"


def _reports(files: dict):
    """RP-2/RP-4: report → page → visual; a data visual reads its query plus the report and page filters (as RP-5 shows)."""
    for rid, r in _ids(files, "reports").items():
        yield f"report:{rid}", None, None
        for p in r.get("pages", []):
            page = f"page:{rid}/{p['id']}"
            yield f"report:{rid}", page, "page"
            for v in p.get("visuals", []):
                visual = f"visual:{rid}/{p['id']}/{v['id']}"
                yield page, visual, "visual"
                if q := v.get("query"):  # a text visual reads nothing
                    yield from _query(files, visual, {**q, "filters": [*r.get("filters", []), *p.get("filters", []), *q.get("filters", [])]})


def _tools(files: dict):
    """TL-1: a tool reads its semantic query."""
    for tid, t in _ids(files, "tools").items():
        yield from _query(files, f"tool:{tid}", t.get("query") or {})


def _flows(files: dict):
    """FL-1/FL-2: a flow reads the tables it triggers on or refreshes, the queries of its data condition and condition
    steps, the tools, reports and agents its steps use
    (a step type's own fields, or an action step's input: `table`, `tool`, `report`, `data.tool`, `report.report`)."""
    for fid, f in _ids(files, "flows").items():
        node = f"flow:{fid}"
        yield node, None, None
        trigger = f.get("trigger") or {}
        for t in trigger.get("tables", []):
            yield node, f"table:{t}", "write-back" if trigger.get("type") == "writeback" else "after refresh"
        for c in [trigger, *(s for s in f.get("steps", []) if s.get("type") == "condition")]:
            if c.get("query"):  # a data-condition trigger or condition step reads its query
                yield from _query(files, node, c["query"])
        for s in f.get("steps", []):
            p = (s.get("input") or {}) if s.get("type") == "action" else s
            data, report = p.get("data"), p.get("report")
            for kind, v in [("table", p.get("table")), ("tool", p.get("tool")), ("agent", p.get("agent")), ("ml", p.get("model")),
                            ("tool", data.get("tool") if isinstance(data, dict) else None),
                            ("report", report.get("report") if isinstance(report, dict) else report)]:
                if isinstance(v, str) and "{{" not in v:  # a placeholder is only known at run time
                    yield node, f"{kind}:{v}", f"step {s.get('id')}"


def _agents(files: dict):
    """AG-1: a custom agent reads its tools and its knowledge scope (tables, measures, datasets, glossary terms)."""
    for aid, a in _ids(files, "agents").items():
        if "." in aid:  # agents/<id>.eval.json: agent IDs have no dot
            continue
        node = f"agent:{aid}"
        yield node, None, None
        for t in a.get("tools", []):
            yield node, f"tool:{t}", "tool"
        for x in a.get("agents", []):  # AGENT-AS-TOOL
            yield node, f"agent:{x}", "asks"
        scope = a.get("scope") or {}
        for kind, key in [("table", "tables"), ("measure", "measures"), ("dataset", "datasets"), ("term", "glossary")]:
            for x in scope.get(key, []):
                yield node, f"{kind}:{x}", "scope"


def _ml(files: dict):
    """ML-2: an ML model trains on its dataset; ML-5: its predictions table (ml_<id>, once scored) comes from it."""
    tables = {t["table"] for t in (files.get("model/model.json") or {}).get("tables", [])}
    for mid, m in _ids(files, "ml").items():
        yield f"ml:{mid}", f"dataset:{m.get('dataset')}", "trains on"
        if f"ml_{mid}" in tables:
            yield f"table:ml_{mid}", f"ml:{mid}", "predictions"


def _ontology(files: dict):
    """1.2.6 model/ontology.json: an object type reads its table, one-to-one property tables and measures; a link its
    two types and the relationships it walks; an action type its object type, the table it writes and the flow it starts."""
    o = files.get("model/ontology.json") or {}
    for x in o.get("objects", []):
        node = f"object:{x['id']}"
        yield node, f"table:{x['table']}", "object"
        for p in x.get("properties", []):
            if "." in p["column"]:
                yield node, "table:" + p["column"].split(".", 1)[0], p["column"]
        for m in x.get("measures", []):
            yield node, f"measure:{m}", "measure"
    for ln in o.get("links", []):
        node = f"link:{ln['id']}"
        yield node, f"object:{ln['from']}", "from"
        yield node, f"object:{ln['to']}", "to"
        for r in ln["via"]:
            yield node, f"relationship:{r}", "via"
    for a in o.get("actions", []):
        node = f"action:{a['id']}"
        yield node, f"object:{a['object']}", "object"
        if w := a.get("writes"):
            yield node, f"table:{w['table']}", "writes"
        if a.get("flow"):
            yield node, f"flow:{a['flow']}", "starts"


BUILDERS = [_tables, _model, _measures, _datasets, _glossary, _reports, _tools, _flows, _agents, _ml, _ontology]


def graph(files: dict) -> dict[str, dict[str, set[str]]]:
    g: dict = {}
    for build in BUILDERS:
        for node, up, via in build(files):
            preds = g.setdefault(node, {})
            if up:
                preds.setdefault(up, set()).add(via)
                g.setdefault(up, {})
    return g


def reach(g: dict, start: set[str], direction: Literal["downstream", "upstream"]) -> list[str]:
    """Every node reachable from `start` (excluded), nearest first. Downstream = what reads it, directly or not."""
    nxt = g
    if direction == "downstream":
        nxt = {n: set() for n in g}
        for n, ups in g.items():
            for u in ups:
                nxt[u].add(n)
    seen, queue, out = set(start), deque(sorted(start)), []
    while queue:
        for n in sorted(nxt.get(queue.popleft(), ())):
            if n not in seen:
                seen.add(n)
                out.append(n)
                queue.append(n)
    return out


class LineageInput(BaseModel):
    project: str = Field(pattern=git.ID)
    object: str = Field(description='"<kind>:<id>": kind is source, table, entity, relationship, measure, dataset, term,'
                                    ' report, page ("<report>/<page>"), visual ("<report>/<page>/<visual>"), tool, flow, agent, ml,'
                                    ' object (an ontology object type), link or action (an ontology action type),'
                                    ' e.g. "table:orders" or "measure:sales" (measure IDs from semantic_model)')
    direction: Literal["downstream", "upstream"] = Field(
        "downstream", description="downstream: what breaks if it changes; upstream: where it comes from")


@action(Role.viewer)
def lineage(conn, user: User, args: LineageInput) -> dict:
    """Lineage from the project's definitions: downstream lists every object that reads the given object, directly or not (what breaks if it changes); upstream lists everything it comes from. Returns the objects, nearest first, and the edges between them with how each one reads the other."""
    try:
        g = graph(bundle.bundle(git.path(args.project)))
    except ValueError as e:  # a project file that isn't valid JSON
        raise ActionFailed(400, str(e))
    if args.object not in g:
        like = sorted(n for n in g if n.split(":", 1)[1] == args.object.split(":", 1)[-1])
        raise ActionFailed(404, f"No object '{args.object}' in project '{args.project}'. "
                                + (f"Did you mean {', '.join(like)}?" if like else 'Name it as "<kind>:<id>", e.g. "table:orders".'))
    objects = reach(g, {args.object}, args.direction)
    scope = {args.object, *objects}
    edges = [{"from": u, "to": n, "via": sorted(vias)} for n in sorted(scope) for u, vias in sorted(g[n].items()) if u in scope]
    return {"object": args.object, "direction": args.direction, "objects": objects, "edges": edges}
