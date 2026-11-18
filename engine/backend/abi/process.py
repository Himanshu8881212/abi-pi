"""Processes: an event log over the ontology, as code. 1.2.7 M1 (docs/plans/1.2.7.md items 10-15).

A process is `processes/<id>.json` (schemas/v1/process.json): the tables its events come from (`events`: an event
table, or a table with several timestamp columns, unpivoted), the objects each event is about (a key on the event's
own row, or a bridge table for several objects of one type) and the clean-up.

`build` writes, in one snapshot and by DuckDB SQL alone, model table pm_<id>_events (one row per event) and one
pm_<id>_<type> per object type (the events of each object of that type in order: `seq`, `next_activity`,
`wait_seconds`), then puts them into model.json with keys and many-to-one relationships to the object tables (as
ml._predictions does). So a type's table is filtered by its object's row rules like any table pointing at a filtered
one, and pm_<id>_events, which holds every event, shows restricted readers no rows once a rule filters something it
is built from (lineage `_process`, rls.unfiltered_outputs: before model.json has the relationships too). A build runs
in the worker (`job`) after save_process, after an import, a pull, a revert or an undo that brings another definition
(`changed`) and after a refresh of a table the process reads (`after_refresh`, called by ingest as ml's is); its
outcome is a row of Postgres `analysis_runs`, with the dropped rows by reason.

Count once: an event about several objects is one row of pm_<id>_events and one row per object in a type's table. An
event's own figures (an amount, its duration) are summed from pm_<id>_events; a measure that sums, counts or averages
them over a type's table would weigh the event by its objects and is refused at save (`references`; MIN, MAX and
DISTINCT_COUNT may: an event that is there once per object changes none of them).
"""

import json
import logging
import re
import time
import uuid

import duckdb
from psycopg.types.json import Jsonb
from pydantic import Field
from sqlglot import exp

from abi import actions, db, ml, security, semantic, storage
from abi.actions import ActionFailed, action
from abi.agent import notify_change
from abi.project import SCHEMA, git, imports
from abi.project.bundle import bundle
from abi.security import Role, User, credentials, role_in
from abi.semantic import lineage, live, mel, model, ontology, planner, rls

log = logging.getLogger(__name__)
_q, _lit = storage._q, storage._lit
EVENT = ("event_id", "activity", "started_at", "ended_at", "duration_seconds")  # an event's columns, before its attributes
MADE = {*EVENT, "ord", "seq", "next_activity", "wait_seconds"}  # the column names a build makes itself
WHY = ("no_timestamp", "no_activity", "filtered", "activity", "no_object", "duplicate")  # why a row is dropped, in the order tried
TIME = re.compile(r"DATE$|TIMESTAMP")
OFFSET = re.compile(r'\d\d:\d\d(:\d\d(\.\d+)?)?\s*(Z|UTC|[+-]\d\d(:?\d\d){0,2})\s*"', re.I)  # a time with an offset, last in a (JSON) string
FAULT = "The event log could not be built because something went wrong on ABI's side. Save the process again; if it happens again, tell whoever looks after ABI for you."
STALE = "The build did not finish. Save the process again, or refresh a table its events come from."
# MiB of DuckDB memory a build uses at most (less where the server's limit is lower); above it DuckDB spills to disk. With
# the server's own 1500MB a build of 5 million events took the heavy worker to 1.7 GB; with this it stays under 1.5 GB for
# a few seconds more (bench/RESULTS.md, 1.2.7 M1: the worker's memory varied by 300 MB from run to run at 600, not at 400).
MEMORY = 400
THREADS = 3  # DuckDB threads a build uses at most: each needs some of that memory to work in, and with 3 it was measured
KEEP = 20  # runs kept per process: the latest ones
# Minutes after which a run still queued or running is one nobody works on (`sweep`). A build of 5 million events takes
# 35 s (bench/RESULTS.md) and a killed worker's run is failed within two minutes (worker.retry_stalled_runs: no heartbeat
# for 30 s, looked for every minute), so after 30 minutes its job was lost or its outcome could not be written. A run
# of the process being queued that only waits that long behind a load is failed too: its job then finds it no longer
# queued and does nothing, and whoever asked has a new run (`_queue` sweeps that process's runs only: nobody would
# queue another process's again).
# ponytail: a build that really runs for more than 30 minutes (some fifty times the 35 s of 5 million events) is failed
# here while it writes: a delete of its process or project then goes through, the tables it writes afterwards are left
# in the lake (denied to restricted readers: `leftover`) and its run ends ok. A heartbeat on the run if builds get there.
DEAD = 30


# ---------- the file: what a build writes, and the import checks ----------

def _own(name: str) -> bool:
    """`name` is a column the event log makes itself, or one of the build's (DuckDB's names are the same whatever their case)."""
    return name.lower() in MADE or name.startswith("__")


def table_of(pid: str, typ: str | None = None) -> str:
    """The model table a build writes: pm_<id>_events, or pm_<id>_<type> for an object type (a `-` of its ID as `_`)."""
    return f"pm_{pid}_{typ.replace('-', '_') if typ else 'events'}"


def types(p: dict) -> list[str]:
    """The object types its events are about, in file order."""
    return list(dict.fromkeys(x["type"] for s in p["events"] for x in s["objects"]))


def tables(p: dict) -> dict[str, str | None]:
    """{each table its build writes: the object type it is for (None: the events table)}."""
    return {table_of(p["id"]): None} | {table_of(p["id"], t): t for t in types(p)}


def reads(p: dict) -> set[str]:
    """The tables a build reads: the sources and the bridge tables."""
    return {s["table"] for s in p["events"]} | {x["through"] for s in p["events"] for x in s["objects"] if "through" in x}


def generated(project: str) -> dict[str, str | None]:
    """`tables` of every process of the project; {} without one (nothing read but the folder)."""
    return {t: typ for p in imports.objects(project, "processes") for t, typ in tables(p).items()}


def leftover(project: str) -> set[str]:
    """The lake tables a build wrote whose process file is gone (an import, an undo, a revert took it): named pm_…,
    written by no process of the project and nobody's own table (a loaded, stepped or input table has a file).
    Row-level security denies them (rls.unfiltered_outputs): nothing says any more what they were made from. Read from
    the lake as it is now (its table names, kept per snapshot): the same files can come back with or without them."""
    con = storage.lake(project)
    pm = [t for t in semantic._model_columns(con, semantic._snapshot(con)) if t.startswith("pm_")]
    made, root = generated(project) if pm else {}, git.path(project)
    return {t for t in pm if t not in made and not any((root / d / f"{t}.json").is_file() for d in ("sources", "tables"))}


def references(project: str, files: dict) -> list[dict]:
    """Import checks of the process files (imports._references, after the schema passed): tables and columns exist
    (columns once the table is loaded), timestamps are DATE or TIMESTAMP, the activity is text, each object type exists
    and the columns named for it have its key's types, a `through` table joins the event's id, SLA IDs are unique, the
    tables a build writes are no one else's and no build's source. And of the measures: none but MIN, MAX and DISTINCT_COUNT
    aggregates an event's own figure over a type's table (a sum, a count, an average or a median would weigh the event by
    its objects; the smallest, the largest and the number of different values are the same however often it is there)."""
    err, errs = imports._err, []
    some = {rel: p for rel, p in files.items() if imports._kind(rel) == "process"}
    if not some:  # a project without processes pays nothing
        return []
    onto, mj = files.get(ontology.FILE) or {}, files.get("model/model.json") or {}
    known = sorted(t["table"] for t in mj.get("tables", []) if not t.get("hidden"))
    hidden = {t["table"] for t in mj.get("tables", []) if t.get("hidden")}
    lake, lv, makes = ontology._cols(project), live.columns(project), {}
    built = {t: p["id"] for p in some.values() for t in tables(p)}  # what the builds write: no build reads it
    for rel, p in some.items():
        def e(at: str, error: str, fix: str, rel=rel) -> None:
            errs.append(err(rel, at, error, fix))

        def table(at: str, t: str) -> bool:
            if t in built:  # (its own: a lineage loop; another's: never rebuilt when that one is)
                e(at, f"'{t}' is a table the build of process '{built[t]}' writes; an event log is built from loaded tables.",
                  f"Name the tables process '{built[t]}' reads instead, or leave it out.")
            elif t in hidden:
                e(at, f"Table '{t}' is hidden (staging), so it is out of the model.", "Show it with set_table hidden=false, or use another table.")
            elif t not in known:
                e(at, f"Unknown model table '{t}'.", f"Use a table of model/model.json: {', '.join(known) or 'none yet (load data, then build_model)'}.")
            elif t in lv:
                e(at, f"'{t}' is a live table; an event log is built from loaded tables.", "Load the table (a source that imports it), or leave it out.")
            return t in known and t not in lv and t not in built

        def column(at: str, t: str, c: str, want: str | None = None) -> str | None:
            """Column c of table t (checked once t is loaded), of kind `want` ("time", "text") if given; its type."""
            if (cols := lake.get(t)) is None:
                return None
            if c not in cols:
                return e(at, f"Table '{t}' has no column '{c}'.", f"Use one of: {', '.join(cols)}.")
            if want == "time" and not TIME.match(cols[c]):
                return e(at, f"Column '{t}.{c}' is {cols[c]}, not a date or a time.",
                         "Name a DATE or TIMESTAMP column, or make it one with a set_type step (set_steps).")
            if want == "text" and cols[c] != "VARCHAR":
                return e(at, f"Column '{t}.{c}' is {cols[c]}, not text: an activity is a name.",
                         "Name a text column, or make it one with a set_type step (set_steps).")
            return cols[c]

        if p["id"] != imports._id(rel):
            e("$.id", f"The id '{p['id']}' differs from the file name.", f"Set id to '{imports._id(rel)}' or rename the file.")
        names, attrs, seen = tables(p), {a.lower() for s in p["events"] for a in s.get("attributes", [])}, {}
        if len(names) <= len(types(p)) or any(makes.setdefault(t, rel) != rel for t in names):
            e("$.id", f"Two tables a build writes would have one name (of {', '.join(names)}).",
              "Give the process another id, or the object type named 'events' (or one differing only by - and _) another id.")
        for t in names:
            if len(t) > 63 - len("-event_id"):  # its relationships' IDs are <table>-<column>, at most 63 characters, one each
                e("$.id", f"The build's table '{t}' would have a name longer than {63 - len('-event_id')} characters, too long for its relationships' IDs.",
                  "Give the process a shorter id.")
            if own := next((f for f in (f"sources/{t}.json", f"tables/{t}.json") if f in files), None):
                e("$.id", f"Table '{t}' is already a table of this project ({own}), and this process's build writes a table of that name.",
                  f"Rename that table, or give the process another id (its build writes {', '.join(names)}).")
        if p["case"] not in types(p):
            e("$.case", f"No event is about object type '{p['case']}'.", f"Use one of the types under `objects`: {', '.join(types(p))}.")
        for i, s in enumerate(p["events"]):
            at, t = f"$.events[{i}]", s["table"]
            if not table(f"{at}.table", t):
                continue
            for k, want in (("id", None), ("activity", "text"), ("start", "time"), ("end", "time")):
                if k in s:
                    column(f"{at}.{k}", t, s[k], want)
            for a, c in s.get("stamps", {}).items():
                column(f"{at}.stamps[{json.dumps(a, ensure_ascii=False)}]", t, c, "time")
            for j, a in enumerate(s.get("attributes", [])):
                if _own(a):
                    e(f"{at}.attributes[{j}]", f"'{a}' is the name of a column the event log makes itself (names starting with __ are its own too).",
                      f"Leave it out, or rename the column in a step (set_steps). The log makes: {', '.join(sorted(MADE))}.")
                elif (typ := column(f"{at}.attributes[{j}]", t, a)) and (was := seen.setdefault(a.lower(), (a, typ, i)))[:2] != (a, typ):
                    e(f"{at}.attributes[{j}]", f"Attribute '{a}' is {typ} here and '{was[0]}' is {was[1]} in events[{was[2]}]: one attribute has one name and one type.",
                      "Give both columns one type with a set_type step (set_steps) and write the name one way, or leave one out.")
            for k, x in enumerate(s["objects"]):
                ax, got = f"{at}.objects[{k}]", ontology.keyed(onto, mj, x["type"])
                if got is None:
                    e(f"{ax}.type", f"Unknown object type '{x['type']}' (or its table has no key).", "Use an object type of model/ontology.json: "
                      f"{', '.join(sorted(o['id'] for o in onto.get('objects', []))) or 'none yet (propose_ontology, then save_ontology)'}.")
                    continue
                obj, key = got[0], [c.split(".", 1)[1] for c in got[1]]
                if clash := sorted(k for k in key if _own(k) or k.lower() in attrs):
                    e(f"{ax}.type", f"The key column '{clash[0]}' of object type '{x['type']}' has the name of an event's column or attribute.",
                      "Leave that attribute out, or rename the column in a step (set_steps).")
                if len(f"{table_of(p['id'], x['type'])}-{'-'.join(key)}") > 63:
                    e(f"{ax}.type", f"The relationship of table '{table_of(p['id'], x['type'])}' to its {obj['name']} would have an ID longer than 63 characters.",
                      "Give the process a shorter id.")
                if ("through" in x) != ("event" in x):
                    e(ax, "`through` and `event` go together: the table with one row per event and object, and its column holding the event's id.",
                      "Give both, or neither (then `columns` are columns of the source table).")
                    continue
                if "through" not in x and any(y["type"] == x["type"] and "through" not in y for y in s["objects"][:k]):
                    e(f"{ax}.type", f"Object type '{x['type']}' is named twice on this source's own row.",
                      "Keep one (an event's row holds one object of a type), or name a `through` table for several.")
                src = x.get("through", t)
                if "through" in x:
                    if not table(f"{ax}.through", src):
                        continue
                    if "id" not in s:
                        e(f"{ax}.through", "A `through` table finds its events by their id, and this source names no `id`.",
                          f"Add `id` (the column of '{t}' that identifies an event), or give the object's key in `columns` of '{t}' itself.")
                    elif (a := column(f"{ax}.event", src, x["event"])) and (b := lake.get(t, {}).get(s["id"])) and a != b:
                        e(f"{ax}.event", f"Column '{src}.{x['event']}' is {a}, and the event's id '{t}.{s['id']}' is {b}.",
                          f"Name the column of '{src}' that holds the event's id, or give both one type with a set_type step (set_steps).")
                if len(x["columns"]) != len(key):
                    e(f"{ax}.columns", f"A {obj['name']} is identified by {len(key)} column(s) ({', '.join(key)}); {len(x['columns'])} are named.",
                      f"Name the column(s) of '{src}' holding them, in that order.")
                    continue
                for j, (c, kc) in enumerate(zip(x["columns"], key)):
                    typ, kt = column(f"{ax}.columns[{j}]", src, c), lake.get(obj["table"], {}).get(kc)
                    if typ and kt and typ != kt:
                        e(f"{ax}.columns[{j}]", f"Column '{src}.{c}' is {typ}, and '{obj['table']}.{kc}', the key of object type '{x['type']}', is {kt}.",
                          f"Name the column of '{src}' that holds the {obj['name']}'s key, or give both one type with a set_type step (set_steps).")
        for j, f in enumerate((p.get("clean") or {}).get("where", [])):
            ft, _, fc = f["column"].rpartition(".")
            mine = [s["table"] for s in p["events"] if ft in ("", s["table"])]
            if (f["op"] == "in") != isinstance(f["value"], list) or f["value"] == []:
                e(f"$.clean.where[{j}].value", "A list goes with op 'in', one value with any other op.", "Use op 'in' with a list of values, or give one value.")
            if not mine:
                e(f"$.clean.where[{j}].column", f"'{ft}' is no source of this process.", f"Use one of: {', '.join(s['table'] for s in p['events'])}.")
            elif all(t in lake for t in mine) and not any(fc in lake[t] for t in mine):
                e(f"$.clean.where[{j}].column", f"No source table has a column '{fc}'.",
                  f"Use a column of {', '.join(mine)}: {', '.join(dict.fromkeys(c for t in mine for c in lake[t]))}.")
            elif any(lake.get(t, {}).get(fc) == "TIMESTAMP WITH TIME ZONE" for t in mine) and OFFSET.search(json.dumps(f["value"])):
                e(f"$.clean.where[{j}].value", f"'{fc}' is an instant, read as wall-clock time in the project's time zone; this value has an offset.",
                  "Write the time as the project's time zone reads it, without an offset or Z (2025-03-01 00:00:00).")
        ids = [x["id"] for x in p.get("sla", [])]
        for j, x in enumerate(ids):
            if x in ids[:j]:
                e(f"$.sla[{j}].id", f"Two SLAs have the id '{x}'.", "Give each SLA its own id.")
    # the count-once rule: an event's own figure, summed over a type's table, counts the event once per object
    per = {table_of(p["id"], t): p for p in some.values() for t in types(p)}
    for i, m in enumerate((files.get(imports.MEASURES) or {}).get("measures", []) if per else []):
        try:
            tree = list(mel._walk(mel.parse(m["expression"])))
        except mel._Bad:  # doesn't parse: the MEL compile reports it
            continue
        for n in tree:
            c = n.col.amount if type(n) is mel.Agg and type(n.col) is mel.Conv else getattr(n, "col", None)
            if type(n) is mel.Agg and n.fn not in ("MIN", "MAX", "DISTINCT_COUNT") and (p := per.get(c.table)) and c.column in (
                    {"duration_seconds"} | {a for s in p["events"] for a in s.get("attributes", [])}):
                what = ontology.keyed(onto, mj, tables(p)[c.table])
                errs.append(err(imports.MEASURES, f"$.measures[{i}].expression",
                                f"Measure '{m['id']}' takes {n.fn} of {c.table}.{c.column}, an event's own figure, over a table that holds an event"
                                f" once per {what[0]['name'] if what else 'object'}: an event about several of them would count several times.",
                                f"Use the events table: {n.fn}({table_of(p['id'])}.{c.column})."))
    return errs


# ---------- the build: DuckDB SQL, one snapshot ----------

def _keys(p: dict, onto: dict, mj: dict) -> dict[str, tuple[dict, list[str]]]:
    """{object type: (the type, its key columns)} of the types its events are about (after `references` passed)."""
    return {t: (x, [c.split(".", 1)[1] for c in key]) for t in types(p) for x, key in [ontology.keyed(onto, mj, t)]}


def _time(c: str, typ: str, zone: str) -> str:
    """Column c as the naive TIMESTAMP the log holds: an instant (TIMESTAMP WITH TIME ZONE) as the project's zone reads
    it, by the planner's own rule (SE-13, planner.wall), not as the worker's DuckDB session would; a DATE or a naive
    TIMESTAMP as it is."""
    return exp.cast(planner.wall(exp.column(c, quoted=True), zone if typ == "TIMESTAMP WITH TIME ZONE" else None), "TIMESTAMP").sql("duckdb")


def _source(i: int, s: dict, p: dict, lake: dict, zone: str) -> str:
    """Source i's candidate events, one row each: __src, __rn (its place in the source: rows in the table's order, a
    row's stamps in its columns' order), __id (its own id as text, if the source names one), event_id, activity,
    started_at, ended_at, its attributes, the key columns of the objects on its own row as __<type's place>_<n>,
    __where (it passes clean.where) and __via (a bridge table names an object for it). Only the columns the file names
    are read, each under the build's own name, so no column of a source can clash with them. An event's id is the
    source's own; with several sources naming one, each is told apart by its source's place ("0:17"); without one it
    is the source's place and the row's ("0.17", a stamp's "1.17.2").
    ponytail: reads the loaded rows (lake.model), so a write-back edit of a source table is not in the log; read the
    effective tables (writeback.views) when an event table is edited by hand."""
    t, cols, order = s["table"], lake[s["table"]], types(p)
    conds = []
    for f in (p.get("clean") or {}).get("where", []):
        ft, _, fc = f["column"].rpartition(".")
        if ft in ("", t) and fc in cols:
            # an instant is compared as the log holds it: a filter's "2025-01-01" is the project's midnight
            lhs, typ = (_time(fc, cols[fc], zone), "TIMESTAMP") if cols[fc] == "TIMESTAMP WITH TIME ZONE" else (_q(fc), cols[fc])
            lits = [rls.cast_lit(v, typ) for v in (f["value"] if isinstance(f["value"], list) else [f["value"]])]
            conds.append(f"{lhs} IN ({', '.join(lits)})" if f["op"] == "in" else f"{lhs} {f['op']} {lits[0]}")
    own = [(c, f"__{order.index(x['type'])}_{j}") for x in s["objects"] if "through" not in x for j, c in enumerate(x["columns"])]
    read = "".join(f", {_q(c)} AS {k}" for c, k in own) + f", ({' AND '.join(conds) or 'TRUE'}) AS __where FROM lake.model.{_q(t)}"
    kept = "".join(f", {k}" for _, k in own) + ", __where"
    if "stamps" in s:  # one struct per timestamp column, unnested: one scan however many stamps
        stamps = sorted(s["stamps"].items(), key=lambda x: list(cols).index(x[1]))
        each = ", ".join(f"{{'k': {k}, 'a': '{_lit(a)}', 't': __t{k}}}" for k, (a, _) in enumerate(stamps))
        rows = "SELECT row_number() OVER () AS __n" + "".join(f", {_time(c, cols[c], zone)} AS __t{k}" for k, (_, c) in enumerate(stamps)) + read
        return (f"SELECT {i} AS __src, __n * {len(stamps)} + __u.k AS __rn, NULL::VARCHAR AS __id, concat('{i}.', __n, '.', __u.k) AS event_id,"
                f" __u.a AS activity, __u.t AS started_at, NULL::TIMESTAMP AS ended_at{kept}, FALSE AS __via"
                f" FROM (SELECT *, unnest([{each}]) AS __u FROM ({rows})) WHERE __u.t IS NOT NULL")
    attrs = "".join(f", {_q(a)}" for a in s.get("attributes", []))
    end = _time(s["end"], cols[s["end"]], zone) if "end" in s else "NULL::TIMESTAMP"
    tag = f"'{i}:' || " if sum("id" in x for x in p["events"]) > 1 else ""
    rows = (f"SELECT row_number() OVER () AS __rn, {_q(s['id']) if 'id' in s else 'NULL'} AS __raw, {_q(s['activity'])} AS activity,"
            f" {_time(s['start'], cols[s['start']], zone)} AS started_at, {end} AS ended_at" + attrs + read)
    via = " OR ".join(f"EXISTS (SELECT 1 FROM lake.model.{_q(x['through'])} b WHERE b.{_q(x['event'])} = s.__raw"
                      + "".join(f" AND b.{_q(c)} IS NOT NULL" for c in x["columns"]) + ")" for x in s["objects"] if "through" in x)
    return (f"SELECT {i} AS __src, __rn, CAST(__raw AS VARCHAR) AS __id, coalesce({tag}CAST(__raw AS VARCHAR), concat('{i}.', __rn)) AS event_id,"
            f" activity, started_at, ended_at{attrs}{kept}, ({via or 'FALSE'}) AS __via FROM ({rows}) s")


def _sql(p: dict, keys: dict, lake: dict, zone: str) -> tuple[str, str, dict[str, str]]:
    """(every candidate event with why it is dropped as __why (NULL: kept): the build keeps it as temporary table
    __pm, so every row is numbered once and everything after reads that; the events table's SELECT over __pm; {object
    type: its table's SELECT over __pm, the bridge tables and the events table})."""
    clean, order, ev = p.get("clean") or {}, types(p), table_of(p["id"])
    attrs = list(dict.fromkeys(a for s in p["events"] for a in s.get("attributes", [])))
    own = {t: [f"__{order.index(t)}_{j}" for j in range(len(keys[t][1]))] for t in order
           if any(x["type"] == t and "through" not in x for s in p["events"] for x in s["objects"])}  # the types named on a source's own row
    raw = " UNION ALL BY NAME ".join(_source(i, s, p, lake, zone) for i, s in enumerate(p["events"]))
    if clean.get("rename"):
        named = " ".join(f"WHEN '{_lit(a)}' THEN '{_lit(b)}'" for a, b in clean["rename"].items())
        raw = f"SELECT * REPLACE (CASE activity {named} ELSE activity END AS activity) FROM ({raw})"
    has = " OR ".join(["__via", *("(" + " AND ".join(f"{c} IS NOT NULL" for c in cs) + ")" for cs in own.values())])
    drop = ", ".join(f"'{_lit(a)}'" for a in clean.get("drop", []))
    log_sql = (f"SELECT *, CASE WHEN started_at IS NULL THEN 'no_timestamp' WHEN activity IS NULL OR trim(activity) = '' THEN 'no_activity'"
               " WHEN __where IS NOT TRUE THEN 'filtered'" + (f" WHEN activity IN ({drop}) THEN 'activity'" if drop else "")
               + f" WHEN NOT ({has}) THEN 'no_object' END AS __why FROM ({raw})")
    if clean.get("duplicates") == "drop":  # the same in every column an event has, among the rows still kept: the first stays
        same = ", ".join(["__why IS NULL", "__src", "__id", "activity", "started_at", "ended_at", *map(_q, attrs), *(c for cs in own.values() for c in cs)])
        log_sql = ("SELECT * REPLACE (CASE WHEN __why IS NULL AND __n > 1 THEN 'duplicate' ELSE __why END AS __why) FROM"
                   f" (SELECT *, row_number() OVER (PARTITION BY {same} ORDER BY __rn) AS __n FROM ({log_sql}))")
    events = ("SELECT event_id, activity, started_at, ended_at, epoch(ended_at - started_at) AS duration_seconds"
              + "".join(f", {_q(a)}" for a in attrs) + ", row_number() OVER (ORDER BY started_at, coalesce(ended_at, started_at), __src, __rn) AS ord"
              " FROM __pm WHERE __why IS NULL")
    per = {}
    for t, (_, key) in keys.items():
        ks = ", ".join(map(_q, key))
        # an event's objects: those on its own kept row, and those a bridge table names for a kept event of the bridge's own source
        links = [f"SELECT event_id, {', '.join(f'{c} AS {_q(k)}' for c, k in zip(own[t], key))} FROM __pm WHERE __why IS NULL"] if t in own else []
        links += [f"SELECT s.event_id, {', '.join(f'b.{_q(c)} AS {_q(k)}' for c, k in zip(x['columns'], key))} FROM lake.model.{_q(x['through'])} b"
                  f" JOIN __pm s ON s.__src = {i} AND s.__id = CAST(b.{_q(x['event'])} AS VARCHAR) WHERE s.__why IS NULL"
                  for i, s in enumerate(p["events"]) for x in s["objects"] if x["type"] == t and "through" in x]
        gap = "epoch(lead(e.started_at) OVER w - coalesce(e.ended_at, e.started_at))"
        per[t] = (f"SELECT {', '.join(f'l.{_q(k)}' for k in key)}, e.* EXCLUDE (ord), row_number() OVER w AS seq, lead(e.activity) OVER w AS next_activity,"
                  f" CASE WHEN {gap} < 0 THEN 0 ELSE {gap} END AS wait_seconds"
                  f" FROM (SELECT DISTINCT * FROM ({' UNION ALL '.join(links)}) WHERE {' AND '.join(f'{_q(k)} IS NOT NULL' for k in key)}) l"
                  f" JOIN lake.model.{_q(ev)} e USING (event_id) WINDOW w AS (PARTITION BY {', '.join(f'l.{_q(k)}' for k in key)} ORDER BY e.ord)"
                  f" ORDER BY {ks}, seq")
    return log_sql, events, per


def build(project: str, pid: str, user: User | None) -> dict:
    """Writes process `pid`'s tables from the file and the lake as they are now, then (as `user`, an Editor who saved
    it; None: the data only) their model.json entries. Returns the rows per table, the dropped rows by reason and, per
    object type, how many events are about more than one object of it. Refuses (the run's error: file, field, fix)
    what the import check would."""
    start, files = time.monotonic(), bundle(git.path(project))
    p, rel = files.get(f"processes/{pid}.json"), f"processes/{pid}.json"
    if p is None:
        raise ActionFailed(404, f"Process '{pid}' was deleted before its event log was built.")
    if errs := [e for e in references(project, files) if e["file"] == rel]:
        raise ActionFailed(422, f"{rel} {errs[0]['path']}: {errs[0]['error']} {errs[0]['fix']}")
    lake = model.lake_columns(project)
    if missing := sorted(reads(p) - lake.keys()):
        raise ActionFailed(409, f"{rel}: table '{missing[0]}' isn't loaded yet. Load it; the event log builds after its refresh.")
    onto, mj = files.get(ontology.FILE) or {}, files.get("model/model.json") or {}
    keys, ev = _keys(p, onto, mj), table_of(pid)
    log_sql, events, per = _sql(p, keys, lake, semantic.locale(project)["time_zone"])
    out = {"tables": {}, "dropped": {}, "several": {}}

    def write(cur) -> None:
        cur.execute(f"CREATE OR REPLACE TEMP TABLE __pm AS {log_sql}")  # this cursor's own; the temp database takes writes beside the lake's
        n = dict(cur.execute("SELECT __why, count(*) FROM __pm GROUP BY 1").fetchall())
        out["dropped"] = {w: n.get(w, 0) for w in WHY}
        cur.execute(f"CREATE OR REPLACE TABLE lake.model.{_q(ev)} AS {storage._ordered(events)}")
        rows, ids = cur.execute(f"SELECT count(*), count(DISTINCT event_id) FROM lake.model.{_q(ev)}").fetchone()
        if rows != ids:  # nothing is written: the transaction rolls back
            at = next((f"$.events[{i}].id" for i, s in enumerate(p["events"]) if "id" in s), "$.events")
            raise ActionFailed(422, f"{rel} {at}: {rows - ids} event(s) have the id of another event, so events can't be told apart."
                                    " Name a column that identifies one event as `id`, or set clean.duplicates to \"drop\" if rows were loaded twice.")
        out["tables"][ev] = rows
        for t, sql in per.items():
            cur.execute(f"CREATE OR REPLACE TABLE lake.model.{_q(table_of(pid, t))} AS {storage._ordered(sql)}")
            out["tables"][table_of(pid, t)], out["several"][t] = cur.execute(
                f"SELECT coalesce(sum(n), 0), count(*) FILTER (n > 1) FROM (SELECT count(*) AS n FROM lake.model.{_q(table_of(pid, t))} GROUP BY event_id)").fetchone()

    con = storage.connect(project)  # the build's own DuckDB instance: its memory is capped here and goes back when the build ends
    try:
        size, unit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0].split()  # as DuckDB says it: "1.3 GiB"
        if float(size) * 1024 ** ("KMGT".find(unit[0]) - 1) > MEMORY:
            con.execute(f"SET memory_limit = '{MEMORY}MiB'")
        if storage.THREADS > THREADS:  # on a host with many cores (this instance runs nothing else: no query waits on the change)
            con.execute(f"SET threads = {THREADS}")
        snap = storage._commit(con, write, user.name if user else "worker", f"build process {pid}")
    except duckdb.ConversionException:  # the one cast of a value the file gives: a filter's
        raise ActionFailed(422, f"{rel} $.clean.where: a value doesn't fit its column's type. Give each filter a value of its column's kind"
                                " (a date as YYYY-MM-DD, a number for a number).") from None
    except duckdb.Error as e:  # DuckDB's text holds SQL and values: the server log's, scrubbed
        log.warning("Building the event log of %s/%s failed: %s", project, pid, credentials.log_text(e))
        raise ActionFailed(400, f"{rel}: the build failed ({type(e).__name__}); the details are in the server log.") from None
    finally:
        con.close()
    if user is not None and bundle(git.path(project)).get(rel) != p:  # no entries for a type or a process that is gone since
        out["note"] = CHANGED
    elif user is not None:
        with git.change(project, user, "build_process"):
            _model(project, p, keys)
    return out | {"events": out["tables"][ev], "snapshot": snap, "seconds": round(time.monotonic() - start, 1)}


OURS = " Written by its build."  # how a description this module wrote ends: a person's own stays
CHANGED = ("The process was changed or deleted while its event log was built, so the tables' entries in the data model were not written;"
           " a changed process builds again and writes them.")


def _model(project: str, p: dict, keys: dict) -> None:
    """Its tables in model.json, as ml._predictions puts ml_<id> there: each keyed and described (readiness asks for
    both), a type's table many-to-one to its object's table and to the events table. What a person set on one of the
    tables since (hidden, a kind, a description of their own, a relationship from an attribute's column to another
    table) stays; a relationship's ID is its table and column, so one from a key column or event_id is the build's
    again. A build after a refresh finds them as they are and changes nothing."""
    mj, ev, rels = model.load(project), table_of(p["id"]), []

    def put(t: str, key: list[str], text: str) -> None:
        was = mj["tables"].get(t) or {}
        mine = not was.get("description") or was["description"].endswith(OURS)
        mj["tables"][t] = {"kind": "fact"} | was | {"key": key, "description": text + OURS if mine else was["description"]}

    put(ev, ["event_id"], f"Every event of process '{p['name']}', one row per event.")
    for typ, (x, key) in keys.items():
        t = table_of(p["id"], typ)
        put(t, [*key, "event_id"], f"The events of each {x['name']} in process '{p['name']}', in order; an event about several {x['plural']} is here once for each.")
        rels += [{"id": model.slug(f"{t}-{'-'.join(key)}"), "from": {"table": t, "columns": key}, "to": {"table": x["table"], "columns": key}},
                 model._rel(f"{t}.event_id", f"{ev}.event_id")]
    to = {ev, *(x["table"] for x, _ in keys.values())}  # the build's own relationships are those to these tables
    mj["relationships"] = ml._in_place(mj["relationships"], rels, lambda r: r["from"]["table"] in tables(p) and r["to"]["table"] in to)
    model.save(project, mj)


# ---------- runs: in the worker, kept in analysis_runs ----------

def sweep(conn, project: str, pid: str | None = None) -> None:
    """The builds nobody works on any more (queued or running for DEAD minutes: a lost job, an outcome that could not
    be written) end failed, so none holds up the next build or a delete: process `pid`'s, or the whole project's
    (None: before the project is deleted or renamed)."""
    conn.execute("UPDATE analysis_runs SET status = 'failed', finished = now(), error = %s WHERE project = %s AND kind = 'process'"
                 " AND (%s::text IS NULL OR name = %s) AND status IN ('queued', 'running')"
                 " AND coalesce(started, created) < now() - make_interval(mins => %s)", [STALE, project, pid, pid, DEAD])


def _queue(conn, project: str, pid: str, user_id: int | None, key: str | None = None) -> str | None:
    """A build of process `pid` for the worker, its run's ID. A run of it that waits for the worker already is that
    build (it reads the file and the tables when it starts): nothing more is queued, and it runs for whoever asked
    last (`user_id`, if given). None when the run `key` exists (one per refresh snapshot). The latest KEEP runs of a
    process stay."""
    sweep(conn, project, pid)
    if wait := conn.execute("UPDATE analysis_runs SET user_id = coalesce(%s, user_id) WHERE id = (SELECT id FROM analysis_runs WHERE project = %s"
                            " AND kind = 'process' AND name = %s AND status = 'queued' ORDER BY created DESC LIMIT 1) RETURNING id::text",
                            [user_id, project, pid]).fetchone():
        return wait["id"]
    run_id = str(uuid.uuid4())
    if not conn.execute("INSERT INTO analysis_runs (id, project, kind, name, user_id, key, status) VALUES (%s, %s, 'process', %s, %s, %s, 'queued')"
                        " ON CONFLICT DO NOTHING", [run_id, project, pid, user_id, key]).rowcount:
        return None
    conn.execute("DELETE FROM analysis_runs WHERE project = %s AND kind = 'process' AND name = %s AND status IN ('ok', 'failed') AND id NOT IN"
                 " (SELECT id FROM analysis_runs WHERE project = %s AND kind = 'process' AND name = %s ORDER BY created DESC LIMIT %s)",
                 [project, pid, project, pid, KEEP])
    conn.commit()  # before the worker can pick the job up
    from abi import worker  # not at top: the worker imports every action module

    try:
        worker.defer(worker.build_process, run_id=run_id)
    except Exception:
        conn.execute("DELETE FROM analysis_runs WHERE id = %s", [run_id])
        conn.commit()  # the error rolls back whatever follows: without this the run stays queued with no job
        raise
    return run_id


def dropped(old: dict, files: dict) -> set[str]:
    """The tables no build writes any more when the project's files go from `old` to `files`: a removed process's, a
    dropped type's; never one that has a file of its own."""
    was, now = ({t for rel, p in x.items() if imports._kind(rel) == "process" for t in tables(p)} for x in (old, files))
    return {t for t in was - now if f"sources/{t}.json" not in files and f"tables/{t}.json" not in files}


def changed(conn, project: str, old: dict, files: dict, user: User) -> dict[str, str]:
    """What follows a change of the project's files from `old` to `files`, as the last step of whatever made it (a save
    or a delete here, an import, a pull, a revert, an undo, set_project_locale): the tables no build writes any more
    (`dropped`) go to the 30-day archive, and every added or changed process gets a build queued as `user`: {process
    ID: its run}; every process does when the project's time zone changed (instants are read in it: `_time`). Until
    that build ends `process` shows it, not the build of the definition before.
    ponytail: it commits (`_queue`: the run before its job), so when queuing fails here (worker.defer, or the second of
    two processes) what the action wrote to Postgres before stays; the files go back (git.change), but an undo's stay
    undone (it runs after the undo's own restore). In a pull (remote.pull, through git.apply_tree) it runs before HEAD
    moves and before the pull's commit, so when one of those fails the files go back with builds queued for them: such
    a build reads the file when it starts and builds the definition of before again (one the pull added fails: gone).
    Builds queued after the action's own commit (an outbox) if that ever shows up.
    ponytail: an undo, a revert or an import while a removed process's build runs archives its tables before that build
    writes them again, so they stay in the lake as leftovers (`leftover`: where rules exist, denied to restricted readers
    and named by readiness, with how to archive them); refuse such a change while the build runs, as _change does, if it bites."""
    git._archive(conn, user, project, dropped(old, files))
    zone = lambda x: ((x.get("project.json") or {}).get("settings") or {}).get("time_zone")  # noqa: E731
    return {p["id"]: _queue(conn, project, p["id"], user.id) for rel, p in sorted(files.items())
            if imports._kind(rel) == "process" and (old.get(rel) != p or zone(old) != zone(files))}


NO_EDITOR = ("The tables' entries in the data model were not written: the person this build runs for is no longer an Editor of the project"
             " (or the account is gone). An Editor saves the process again (save_process) to write them.")


def job(run_id: str) -> None:
    """The worker's job (abi.worker.build_process): a queued build; the outcome, or why it failed, stays on its run and
    a failure goes to the feed once (and once when it builds again). model.json is written in the name of the person
    the run was queued for only while they are an Editor of the project (as ml.after_refresh scores): else the tables
    are built, the model is left as it is and the run says so. Audited either way (SC-6)."""
    with db.connect() as conn:
        r = conn.execute("UPDATE analysis_runs SET status = 'running', started = now() WHERE id = %s AND status = 'queued'"
                         " RETURNING project, name, user_id, key", [run_id]).fetchone()
        if r is None:
            return
        u = security.user(conn, r["user_id"]) if r["user_id"] else None
        may = u is not None and (role_in(conn, u, r["project"]) or Role.viewer) >= Role.editor
        actions.audit(conn, u, "worker", "build_process", r["project"], {"project": r["project"], "process": r["name"], "key": r["key"]},
                      200 if may else 403, f"run {run_id}" if may else "not an Editor of the project: the tables are built, model.json is not written")
    out = error = None
    try:
        out = build(r["project"], r["name"], u if may else None) | ({} if may else {"note": NO_EDITOR})
    except Exception as e:  # noqa: BLE001 - any failure is the run's outcome
        if not isinstance(e, ActionFailed):
            log.exception("process build %s failed", run_id)
        error = str(e) if isinstance(e, ActionFailed) else FAULT
    with db.connect() as conn:
        conn.execute("UPDATE analysis_runs SET status = %s, finished = now(), result = %s, error = %s WHERE id = %s",
                     ["failed" if error else "ok", Jsonb(out) if out else None, error, run_id])
    notify_change("refresh", r["project"], f"Event log of process '{r['name']}': ", error and f"not built. {error}", "is built again.", table_of(r["name"]))


def interrupted(run_id: str) -> None:
    """A build whose worker died (worker.retry_stalled_runs) ends failed, so it never reads "running" for ever."""
    with db.connect() as conn:
        conn.execute("UPDATE analysis_runs SET status = 'failed', finished = now(), error = %s WHERE id = %s AND status IN ('queued', 'running')",
                     [STALE, run_id])


def after_refresh(project: str, table: str, snapshot: int) -> None:
    """A refresh snapshot of `table` queues one build (key snapshot-<n>) of each process that reads it, as the person
    its last build ran for. A table no process reads queues none. Never fails the refresh (D-26): errors go to the log."""
    try:
        for p in imports.objects(project, "processes"):  # [] without the folder: a project without processes pays nothing
            if table in reads(p):
                with db.connect() as conn:
                    last = conn.execute("SELECT user_id FROM analysis_runs WHERE project = %s AND kind = 'process' AND name = %s"
                                        " ORDER BY created DESC LIMIT 1", [project, p["id"]]).fetchone()
                    _queue(conn, project, p["id"], last and last["user_id"], f"snapshot-{snapshot}")
    except Exception as e:  # noqa: BLE001 - the refresh worked
        log.error("Queueing process builds after refreshing %s/%s failed: %s", project, table, type(e).__name__)


# ---------- proposing one: read-only, from the model, the ontology and the data, as the caller ----------

def _label(c: str) -> str:
    return ontology._label(re.sub(r"_(at|on|date|time|ts)$", "", c))


def _stamps(times: list[str]) -> dict[str, str]:
    """{activity name: column} of a table's time columns; two that would get one name (created_at, created_on) keep
    their columns' own."""
    out = {}
    for c in times:
        out[_label(c) if _label(c) not in out else ontology._label(c)] = c
    return out


def propose(project: str, user_id: int | None) -> dict:
    """Draft processes, not saved, one per group of sources that share an object type. A source is a table that is no
    object type's own and has a timestamp, a text column with few values and a way to an object type (a relationship
    to its key, or a bridge table): an event table; or a table with three or more timestamp columns that is an object
    type's own, or points at one: stamps. Every read of rows is the caller's (semantic.execute as them)."""
    o, mj, m = ontology.read(project), model._read(project, "model.json"), model.visible(model.load(project))
    keys, lv, hid = ontology._keys(mj), live.columns(project), rls.hidden(project, user_id)
    objs = {x["table"]: x for x in o.get("objects", []) if keys.get(x["table"])}
    if not objs:
        raise ActionFailed(409, f"A process is about objects, and project '{project}' has no object types yet. Save an ontology first"
                                " (propose_ontology, then save_ontology), then propose the process.")
    made, date = generated(project), (m["date_table"] or {}).get("table")
    cols = {t: {c: ty for c, ty in cs.items() if c.lower() not in hid.get(t, ())}  # D-110: never a hidden column
            for t, cs in model.lake_columns(project).items() if t in m["tables"] and t not in lv and t not in made and t != date}
    points = lambda t: [(r["from"]["columns"], objs[r["to"]["table"]]) for r in m["relationships"] if r["from"]["table"] == t  # noqa: E731
                        and r["to"]["table"] in objs and r["to"]["columns"] == keys[r["to"]["table"]] and set(r["from"]["columns"]) <= set(cols.get(t, ()))]
    events, found = [], []
    for t, tc in cols.items():
        times = [c for c, ty in tc.items() if TIME.match(ty)]
        direct = [{"type": x["id"], "columns": cs} for cs, x in points(t)]
        if not times:
            continue
        if t in objs:  # an object's own table: its timestamps are what happened to the object
            if len(times) >= 3 and set(keys[t]) <= set(tc):
                events.append({"table": t, "stamps": _stamps(times), "objects": [{"type": objs[t]["id"], "columns": keys[t]}]})
                found.append({"table": t, "activities": list(events[-1]["stamps"])})
            continue
        key = [k for k in keys.get(t) or [] if k in tc] if len(keys.get(t) or []) == 1 else []  # an event's id: one column the caller may read
        # a bridge: a table pointing at this table's one-column key and at an object type's key
        bridged = [{"type": x["id"], "through": b, "event": r["from"]["columns"][0], "columns": cs} for r in m["relationships"]
                   if r["to"]["table"] == t and key and r["to"]["columns"] == key and (b := r["from"]["table"]) in cols
                   and r["from"]["columns"][0] in cols[b] for cs, x in points(b) if x["table"] != t]
        used = {c for x in direct for c in x["columns"]} | set(key)
        text = [c for c, ty in tc.items() if ty == "VARCHAR" and c not in used]
        if text and (direct or bridged):
            n, *distinct = semantic.execute(project, "SELECT count(*)" + "".join(f", approx_count_distinct({_q(c)})" for c in text)
                                            + f" FROM {_q(t)}", {"propose_process": t}, user_id)[0]  # RLS: the caller's rows
            few = sorted((d, c) for c, d in zip(text, distinct) if d <= 50 and d < n)  # few values, and they repeat
            hint = [c for _, c in few if re.search(r"activity|event|step|status|action|stage|task|state", c, re.I)]
            if few:
                act = hint[0] if hint else few[0][1]
                start = next((c for c in times if re.search(r"start|begin", c, re.I)), times[0])
                end = next((c for c in times if c != start and re.search(r"end|finish|complet", c, re.I)), None)
                s = {"table": t, **({"id": key[0]} if key else {}), "activity": act, "start": start, **({"end": end} if end else {}),
                     "attributes": [c for c in tc if c not in {act, *used, *times} and not _own(c)][:10], "objects": direct + (bridged if key else [])}
                seen = semantic.execute(project, f"SELECT {_q(act)}, count(*) FROM {_q(t)} WHERE {_q(act)} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 50",
                                        {"propose_process": t, "activities": act}, user_id)
                events.append({k: v for k, v in s.items() if v != []})
                found.append({"table": t, "rows": n, "activities": [a for a, _ in seen]})
                # the clean-up it can see: one activity spelled several ways becomes its most frequent spelling
                first = {}
                for a, _ in seen:
                    first.setdefault(" ".join(a.lower().split()), a)
                if rename := {a: first[" ".join(a.lower().split())] for a, _ in seen if first[" ".join(a.lower().split())] != a}:
                    found[-1]["rename"] = rename
                continue
        if len(times) >= 3 and direct:  # no column of activities (none, or none with few values): its timestamps are the steps
            events.append({"table": t, "stamps": _stamps(times), "objects": direct})
            found.append({"table": t, "activities": list(events[-1]["stamps"])})
    if not events:
        return {"processes": [], "found": [], "note": "No table looks like a source of events: none has a timestamp, a text column with few values"
                                                      " and a relationship to an object type's key, and none has three or more timestamp columns."
                                                      " Add the relationship (add_relationship) or the object type (save_ontology), or write the process by hand."}
    groups: list[tuple[set, list]] = []  # sources that share an object type are one process
    for s in events:
        about = {x["type"] for x in s["objects"]}
        hit = [g for g in groups if g[0] & about]
        groups = [g for g in groups if g not in hit] + [(about.union(*(g[0] for g in hit)), [x for g in hit for x in g[1]] + [s])]
    docs = []
    for _, sources in groups:
        about = [x["type"] for s in sources for x in s["objects"]]
        case = max(dict.fromkeys(about), key=about.count)
        rename = {a: b for f in found if f["table"] in {s["table"] for s in sources} for a, b in f.get("rename", {}).items()}
        docs.append({"$schema": SCHEMA + "process.json", "id": case.replace("-", "_")[:31],
                     "name": f"{next(x['name'] for x in objs.values() if x['id'] == case)} process", "case": case, "events": sources,
                     **({"clean": {"rename": rename}} if rename else {})})
    return {"processes": docs, "found": [{k: v for k, v in f.items() if k != "rename"} for f in found],
            "note": "Drafts from the model, the ontology and your data, not saved: one per group of sources that share an object type. Each source is a"
                    " guess from column types: take out the ones that are no steps of the process, name the activities of `stamps` in the business's"
                    " words (search the vault and cite it), choose `case`, give it an id and a name, add `clean` (rename, drop, where, duplicates) for"
                    " what `found` shows, then save_process each; the person confirms. Rows without a timestamp or an object are always dropped and"
                    " counted in the build."}


# ---------- actions ----------

class Ref(storage.ProjectRef):
    process: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$", description="process ID (file name in processes/)")


class Save(storage.ProjectRef):
    process: dict = Field(description=(
        "The process (schemas/v1/process.json; propose_process drafts one): {id, name, description?, case: an object type ID, events: [an event"
        " table {table, id?, activity, start, end?, attributes?, objects} or a table of timestamp columns {table, stamps: {activity name: column},"
        " objects}], clean?: {rename: {from: to}, drop: [activity], where: [{column, op, value}], duplicates: keep|drop}, reference?, sla?,"
        " refresh_minutes?}; objects: [{type: an object type ID, columns: its key on the event's row} or {type, through: a table with one row"
        " per event and object, event: its column with the event's id, columns}]."))


def _say(p: dict) -> str:
    return f"process '{p['id']}' ({p['name']}: {len(p['events'])} source(s) of events about {', '.join(types(p))})"


def _change(conn, user: User, project: str, pid: str, new: dict | None) -> dict:
    """Replaces processes/<pid>.json with `new` (None: removes it), as one confirmed, undoable change through the import
    pipeline: the person confirms a preview of the change, the tables its build writes or that go (archived, with their
    model.json entries) and what reads them (lineage downstream)."""
    files, rel = bundle(git.path(project)), f"processes/{pid}.json"
    old = files.get(rel)
    if new is None and old is None:
        raise ActionFailed(404, f"No process '{pid}' in project '{project}'.")
    after = {k: v for k, v in files.items() if k != rel} | ({rel: new} if new else {})
    imports.check(conn, project, after)  # file, JSON path and fix, before anything is asked
    if old == new:  # nothing to confirm: the same file builds again
        return {"changes": [], "build": _queue(conn, project, pid, user.id)}
    was, now = (tables(x) if x else {} for x in (old, new))
    gone = sorted(dropped(files, after))  # the tables that go: its own, never one a person loaded under that name since (it has a file)
    sweep(conn, project, pid)
    if gone and conn.execute("SELECT 1 FROM analysis_runs WHERE project = %s AND kind = 'process' AND name = %s AND status IN ('queued', 'running')",
                             [project, pid]).fetchone():  # its build would write the tables after they went
        raise ActionFailed(409, f"The event log of “{old['name']}” is being built right now" + ("" if new is None else f", and this change takes"
                                f" {', '.join(gone)} away") + f". {'Delete it' if new is None else 'Save it'} once the build has ended (`process` shows it).")
    from abi import ingest  # not at top: ingest imports this module

    for t in gone:  # their model.json entries go with them: a measure or a report reading one refuses the change (422)
        after = ingest._without(after, t)
    if gone:
        imports.check(conn, project, after)
    changes = [f"Adds {_say(new)}." if old is None else f"Removes {_say(old)}." if new is None else
               f"Changes {_say(new)}: " + ", ".join(k for k in sorted(old.keys() | new.keys()) if old.get(k) != new.get(k)) + "."]
    own = {f"process:{pid}", *(f"table:{t}" for t in was | now)}
    ga, gb = lineage.graph(files), lineage.graph(after)
    read = {n: r for n in sorted(own) if (r := [d for d in lineage.reach(ga if n not in gb else gb, {n}, "downstream") if d not in own])}
    preview = {"changes": changes, "tables": sorted(now), "removes": gone, "reads": read}
    why = (f"This changes the processes of project '{project}': {changes[0]}"
           + (f" Its build writes the tables {', '.join(sorted(now))} (every event once, and each object type's events in order)." if now else "")
           + (f" It removes the table(s) {', '.join(gone)} from the data model; what a build has written of them is archived for 30 days." if gone else ""))
    out = imports.apply(conn, user, project, after, ask=(why, preview)) | {"process": preview}
    builds = changed(conn, project, files, after, user)  # last: `gone` to the archive, the saved file's build queued
    if new is None:
        conn.execute("DELETE FROM analysis_runs WHERE project = %s AND kind = 'process' AND name = %s", [project, pid])
        return out
    return out | {"build": builds[pid]}


@action(Role.viewer)
def propose_process(conn, user: User, args: storage.ProjectRef) -> dict:
    """Drafts processes from the model, the ontology and the data, as you (row-level security applies); saves nothing. Sources: tables with a timestamp, a text column of few values and a way to an object type, and tables with three or more timestamps. Refine, then save_process."""
    return propose(args.project, user.id)


@action(Role.editor, definition=True, confirms=True)
def save_process(conn, user: User, args: Save) -> dict:
    """Saves a process (processes/<id>.json), checked like an import (file, JSON path, fix). The person confirms a preview: the change, the tables its build writes and what reads them. Then the event log builds in the worker (`process` shows it); an unchanged save only builds again."""
    p = {**args.process, "$schema": SCHEMA + "process.json"}
    return _change(conn, user, args.project, imports.object_id(p, "A process"), p)


@action(Role.editor, definition=True, confirms=True)
def delete_process(conn, user: User, args: Ref) -> dict:
    """Deletes a process and the tables its build wrote (their data is archived for 30 days) after the person confirms a preview of what reads them. Refused while a measure or a report reads one of those tables."""
    return _change(conn, user, args.project, args.process, None)


@action(Role.viewer)
def process(conn, user: User, args: storage.ProjectRef) -> dict:
    """The project's processes as saved, each with the tables its build writes and its latest build: status, what started it (key: a refresh's snapshot), events, rows dropped by reason, events about several objects of a type. A restricted reader gets the names and the status only."""
    ps = imports.objects(args.project, "processes")
    if not ps:  # a project without processes: no read
        return {"processes": []}
    runs = {r.pop("name"): r for r in conn.execute(
        "SELECT DISTINCT ON (name) name, id::text AS run, key, status, created::text, started::text, finished::text, error, result FROM analysis_runs"
        " WHERE project = %s AND kind = 'process' ORDER BY name, created DESC", [args.project])}
    hide = rls.restricted(args.project, user.id)  # a build counts every row, and the file may quote data (a filter's value, a description)
    out = []
    for p in ps:
        r = runs.get(p["id"]) or {"status": "never built"}
        out.append({k: p[k] for k in ("id", "name", "case")} | {"types": types(p), "build": {"status": r["status"]}} if hide else
                   {k: v for k, v in p.items() if k != "$schema"} | {"tables": tables(p), "build": {k: v for k, v in r.items() if k != "result"} | (r.get("result") or {})})
    return {"processes": out}
