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
from typing import Literal

import duckdb
from jsonschema.exceptions import best_match
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
# 1.2.7 M2: a case's own figures, written on each of its rows of a type's table (its variant's ID and its cycle time;
# the reads take them from there, so a read is one pass over one row per case or per row, and the text of a variant is
# read for the variants shown only; M4 builds pm_<id>_<type>_cases from these columns), and two flags:
# one row of each activity in a case, and one of each pair of activities one after the other (cases = the flagged rows). A
# case's rows are visible together or not at all (its object's rules), so the flags hold for every reader.
CASE = ("variant_id", "cycle_seconds")
FLAGS = ("first_visit", "first_pair")
MADE = {*EVENT, "ord", "seq", "next_activity", "wait_seconds", *CASE, *FLAGS}  # the column names a build makes itself
ARROW = " → "  # between a variant's activities
# a variant's ID: the first 12 hex digits of the MD5 of the sum, over its steps, of the low 64 bits of the MD5 of "<place>:<activity>"
# (the same activities in the same places give the same sum), so the same wherever and whenever it is read
VID = "substring(md5({}), 1, 12)"
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
            if type(n) is mel.Agg and n.fn not in ("MIN", "MAX", "DISTINCT_COUNT") and (p := per.get(c.table)) and c.column in (*CASE, *FLAGS):
                what = ontology.keyed(onto, mj, tables(p)[c.table])
                name = what[0]["name"] if what else "case"
                errs.append(err(imports.MEASURES, f"$.measures[{i}].expression",
                                f"Measure '{m['id']}' takes {n.fn} of {c.table}.{c.column}, a {name}'s own figure written on each of its"
                                f" events: a {name} with several events would count several times.",
                                f"Use MIN, MAX or DISTINCT_COUNT of {c.table}.{c.column}: it is the same on every row of a {name}."))
            elif type(n) is mel.Agg and n.fn not in ("MIN", "MAX", "DISTINCT_COUNT") and (p := per.get(c.table)) and c.column in (
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
        part = ", ".join(f"l.{_q(k)}" for k in key)
        whole = f"OVER (PARTITION BY {part} ORDER BY e.ord ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"  # w's sort, the whole case
        rows = (f"SELECT {', '.join(f'l.{_q(k)}' for k in key)}, e.* EXCLUDE (ord), row_number() OVER w AS seq, lead(e.activity) OVER w AS next_activity,"
                f" CASE WHEN {gap} < 0 THEN 0 ELSE {gap} END AS wait_seconds, min(e.started_at) {whole} AS __t0,"
                f" max(coalesce(e.ended_at, e.started_at)) {whole} AS __t1"
                f" FROM (SELECT DISTINCT * FROM ({' UNION ALL '.join(links)}) WHERE {' AND '.join(f'{_q(k)} IS NOT NULL' for k in key)}) l"
                f" JOIN lake.model.{_q(ev)} e USING (event_id) WINDOW w AS (PARTITION BY {', '.join(f'l.{_q(k)}' for k in key)} ORDER BY e.ord)")
        # each case's own figures (CASE) on each of its rows and the flags (FLAGS), by windows over the rows (no grouped
        # aggregate: one of a million cases' ordered lists needed more than the build's memory). Two sorts in all, the
        # order's (w: its first and last time too) and one by activity and next activity in a case, which flags one row
        # of each activity and of each pair (the one of the earliest next activity, then place) and sums the variant's hash
        by = f"OVER (PARTITION BY {ks} ORDER BY activity, next_activity, seq"
        per[t] = (f"SELECT * EXCLUDE (__vk, __t0, __t1, first_visit, first_pair), {VID.format('CAST(__vk AS VARCHAR)')} AS variant_id,"
                  " date_diff('second', __t0, __t1) AS cycle_seconds, first_visit, first_pair FROM (SELECT *,"
                  f" sum(md5_number_lower(seq || ':' || activity)) {by} ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS __vk,"
                  f" lag(activity) {by}) IS DISTINCT FROM activity AS first_visit,"
                  f" lag(activity) {by}) IS DISTINCT FROM activity OR lag(next_activity) {by}) IS DISTINCT FROM next_activity AS first_pair"
                  f" FROM ({rows})) ORDER BY {ks}, seq")
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


# ---------- reads (1.2.7 M2): the map, variants, performance and the usual way, as the person, one query each ----------
# Each read is one SQL through semantic.execute as the caller (row rules, column rules, rls.check, the query log) over
# one object type's table pm_<id>_<type>: a case is one object of that type. An event's own figures (how many events,
# its duration) count each event once (DISTINCT event_id), also one about several objects of the type.

FILTERS = ("Cases to keep, all must hold (schemas/v1/process_filters.json): {kind: time, when: started|ended|open, from?, to?},"
           " {kind: variant, in: [IDs], not?}, {kind: property, column: of the case's object, op, value}, {kind: attribute,"
           " column: an event attribute, op, value}, {kind: start|end, activities: [...], not?}, {kind: contains, activity, not?},"
           " {kind: follows, from, to, not?}, {kind: duration, op: < <= > >=, hours}; a property's or an attribute's op: = <> < <= > >= in;"
           " times YYYY-MM-DD[ HH:MM], from included, to not.")
REF = 200, 2000, 1000  # a reference's most start or end activities, flows, and times a step may repeat (schemas/v1/process.json)


class Read(Ref):
    type: str | None = Field(None, pattern=r"^[a-z][a-z0-9_-]{0,62}$",
                             description="the object type a case is, one the process's events are about (default: its `case`)")
    filters: list[dict] = Field([], max_length=20, description=FILTERS)


def _h(s) -> float | None:
    """Seconds as hours, to two places."""
    return None if s is None else round(float(s) / 3600, 2)


def filter_errors(p: dict, x: dict, t: str, filters: list, cols: dict, hid: dict) -> list[dict]:
    """The errors of `filters` (the call's argument "filters") for a read of type table `t` of process `p` (object
    type `x`) as a reader with hidden columns `hid`: the shape (schemas/v1/process_filters.json), then each column and
    value, a column hidden from the reader answered as an unknown one. Each names the JSON path and the fix."""
    err, kind = lambda path, error, fix: imports._err("filters", path, error, fix), "process_filters"  # noqa: E731
    if errs := [err(imports.json_path(b, kind), *imports._explain(b, kind)) for e in imports._validator(kind).iter_errors(filters)
                for b in [best_match([e])]]:
        return errs
    attrs = {a for s in p["events"] for a in s.get("attributes", []) if a.lower() not in hid.get(t, set())} & cols[t].keys()
    props = [c for c in cols.get(x["table"], {}) if c.lower() not in hid.get(x["table"], set())]
    con = duckdb.connect()  # literals are cast on constants here, before any data is read

    def fits(lit: str) -> bool:
        try:
            con.execute(f"SELECT {lit}")
            return True
        except duckdb.Error:
            return False

    try:
        for i, f in enumerate(filters):
            here = f"$[{i}]"
            if f["kind"] == "time":
                if "from" not in f and "to" not in f:
                    errs.append(err(here, "A time filter needs `from`, `to` or both.", "Add `from` (included) and/or `to` (not included), as YYYY-MM-DD."))
                errs += [err(f"{here}.{k}", f"'{f[k]}' is no date or time.", "Write it as YYYY-MM-DD or YYYY-MM-DD HH:MM.")
                         for k in ("from", "to") if k in f and not fits(rls.cast_lit(f[k], "TIMESTAMP"))]
            elif f["kind"] in ("property", "attribute"):
                c, prop = f["column"], f["kind"] == "property"
                if c not in (props if prop else attrs):
                    errs.append(err(f"{here}.column", f"'{c}' is no {'property of a ' + x['name'] if prop else 'attribute of the events of process ' + repr(p['id'])} you can read.",
                                    f"Use one of: {', '.join(sorted(props if prop else attrs)) or 'none (the process keeps no attributes on its events)'}."))
                    continue
                vals, typ = f["value"] if isinstance(f["value"], list) else [f["value"]], cols[x["table"] if prop else t][c]
                if (f["op"] == "in") != isinstance(f["value"], list) or not vals:
                    errs.append(err(f"{here}.value", "A list goes with op 'in', one value with any other op.", "Use op 'in' with a list of values, or give one value."))
                elif bad := next((v for v in vals if not fits(rls.cast_lit(v, typ))), None):
                    errs.append(err(f"{here}.value", f"'{bad}' doesn't fit '{c}', which is {typ}.",
                                    "Give a value of the column's kind (a date as YYYY-MM-DD, a number for a number)."))
    finally:
        con.close()
    return errs


def _scope(t: str, x: dict, key: list[str], filters: list, cols: dict) -> tuple[list[str], str]:
    """(the CTEs, the FROM item): type table `t` as c, kept by a semi-join on the case key to the cases every filter
    holds for (worked out once, the keys only: each case once, whatever holds for it); the table itself without filters."""
    if not filters:
        return [], f"{_q(t)} c"
    ks = ", ".join(f"x.{_q(k)}" for k in key)
    acts = lambda xs: ", ".join(f"'{_lit(a)}'" for a in xs)  # noqa: E731
    first, last = "min(x.started_at)", "max(coalesce(x.ended_at, x.started_at))"
    have, where = [], []  # `where`: a case's own figures (each of its rows has them), so its rows are kept or left out together
    for f in filters:
        k, no = f["kind"], f.get("not", False)
        if k == "time":  # open: started before `to`, ended at or after `from`
            lo, hi = (first, last) if f["when"] == "open" else ((first,) * 2 if f["when"] == "started" else (last,) * 2)
            have += ([f"{hi} >= {rls.cast_lit(f['from'], 'TIMESTAMP')}"] if "from" in f else []) + (
                [f"{lo} < {rls.cast_lit(f['to'], 'TIMESTAMP')}"] if "to" in f else [])
        elif k == "duration":
            where.append(f"x.cycle_seconds {f['op']} {float(f['hours']) * 3600}")
        elif k == "variant":
            where.append(f"x.variant_id {'NOT ' if no else ''}IN ({acts(f['in'])})")
        elif k in ("property", "attribute"):
            col, typ = (f"o.{_q(f['column'])}", cols[x["table"]][f["column"]]) if k == "property" else (f"x.{_q(f['column'])}", cols[t][f["column"]])
            vals = f["value"] if isinstance(f["value"], list) else [f["value"]]
            cond = f"{col} IN ({', '.join(rls.cast_lit(v, typ) for v in vals)})" if f["op"] == "in" else f"{col} {f['op']} {rls.cast_lit(vals[0], typ)}"
            (where if k == "property" else have).append(cond if k == "property" else f"count(CASE WHEN {cond} THEN 1 END) > 0")
        else:
            cond = {"start": lambda: f"x.seq = 1 AND x.activity IN ({acts(f['activities'])})",
                    "end": lambda: f"x.next_activity IS NULL AND x.activity IN ({acts(f['activities'])})",
                    "contains": lambda: f"x.activity = '{_lit(f['activity'])}'",
                    "follows": lambda: f"x.activity = '{_lit(f['from'])}' AND x.next_activity = '{_lit(f['to'])}'"}[k]()
            have.append(f"count(CASE WHEN {cond} THEN 1 END) {'=' if no else '>'} 0")
    using = ", ".join(map(_q, key))
    obj = f" JOIN {_q(x['table'])} o USING ({using})" if any(f["kind"] == "property" for f in filters) else ""
    cases = (f"SELECT {ks} FROM {_q(t)} x{obj}" + (f" WHERE {' AND '.join(where)}" if where else "")
             + f" GROUP BY {ks}" + (f" HAVING {' AND '.join(have)}" if have else ""))
    return [f"f AS MATERIALIZED ({cases})"], f"{_q(t)} c JOIN f USING ({using})"


def _with(*ctes: str) -> str:
    return f"WITH {', '.join(ctes)} " if ctes else ""


def _read(project: str, args: Read, user: User, what: str, more: dict | None = None) -> tuple[dict, str, dict, list[str], tuple, dict]:
    """(the process, the type read, the object type, its key, its cases (`_scope`), what was asked, for the query log),
    once the arguments and the filters are checked and a type table row-level security denies the reader is refused
    (never read as unfiltered rows, nor shown as an empty map). Each read then runs ONE query as the reader (`_run`)."""
    f = git.path(project) / "processes" / f"{args.process}.json"
    if not f.is_file():
        raise ActionFailed(404, f"No process '{args.process}' in project '{project}': `process` lists them.")
    p = json.loads(f.read_text())
    typ = args.type or p["case"]
    if typ not in types(p):
        raise ActionFailed(400, f"`type`: the events of process '{p['id']}' are about {', '.join(types(p))}; use one of them.")
    if (got := ontology.keyed(ontology.read(project), model._read(project, "model.json"), typ)) is None:
        raise ActionFailed(409, f"Object type '{typ}' of process '{p['id']}' is no longer in the ontology with a key: put it back (save_ontology),"
                                " or take it out of the process (save_process).")
    x, key = got[0], [c.split(".", 1)[1] for c in got[1]]
    t, cols = table_of(p["id"], typ), model.lake_columns(project)
    if t not in cols:
        raise ActionFailed(409, f"The event log of process '{p['name']}' isn't built yet: `process` shows its build.")
    if not {*key, *CASE, *FLAGS} <= cols[t].keys():
        raise ActionFailed(409, f"The event log of process '{p['name']}' was built before its {x['name']}'s key or this version of ABI: save"
                                " the process again (save_process) to build it again; `process` shows the build.")
    if rls.restricted(project, user.id) and t in rls.unfiltered_outputs(project):
        raise ActionFailed(403, f"Row-level security: the events of each {x['name']} in process '{p['name']}' are made from tables that security"
                                " rules filter, or hide columns of, and have no rule of their own, so you see none of them. An Owner of the project"
                                " can add that rule (readiness names it).")
    if errs := filter_errors(p, x, t, args.filters, cols, rls.hidden(project, user.id)):
        raise ActionFailed(422, f"The filters were refused: {len(errs)} error(s); nothing was read.", errors=errs)
    return p, typ, x, key, _scope(t, x, key, args.filters, cols), {"process": p["id"], "type": typ, "filters": args.filters, what: True, **(more or {})}


def _run(project: str, sql: str, asked: dict, user: User) -> list[tuple]:
    try:
        return [tuple(map(semantic.tidy, r)) for r in semantic.execute(project, sql, asked, user.id, timeout=semantic.TIMEOUT)]
    except duckdb.InterruptException:
        raise ActionFailed(400, f"The read ran longer than the {semantic.TIMEOUT:g} s limit and was stopped. Read fewer cases: add a"
                                " `time` filter (cases started in a shorter range) or a `property` filter.") from None
    except duckdb.Error as e:  # DuckDB's text holds SQL and values: the server log's
        log.warning("A process read of %s failed: %s", project, credentials.log_text(e))
        raise ActionFailed(400, f"The read failed ({type(e).__name__}); the details are in the server log.") from None


def _per(x: dict) -> str:
    return f"per {x['name']}"


def _cut(rows: list, share: float, weight) -> list:
    """The first rows (most frequent first) until those kept hold `share` of all rows' `weight`."""
    total, kept, out = sum(map(weight, rows)), 0, []
    for r in rows:
        if out and kept >= share * total:
            break
        out.append(r)
        kept += weight(r)
    return out


class MapRead(Read):
    detail: float = Field(1, gt=0, le=1, description="keep the most frequent activities, and pairs among them, until they cover this share of the cases' visits (1: all)")


@action(Role.viewer)
def process_map(conn, user: User, args: MapRead) -> dict:
    """A process's map as you may see it: activities (cases, events, median duration), pairs one directly after another (cases, count, median and p90 wait), start and end activities; per case of one object type, `filters` applied. Times in hours."""
    p, typ, x, key, (ctes, src), asked = _read(args.project, args, user, "process_map", {"detail": args.detail})
    dur = any("end" in s for s in p["events"])
    rows = _run(args.project, _with(*ctes) + (
        "SELECT 'a' AS k, activity AS a, NULL::VARCHAR AS b, count(CASE WHEN first_visit THEN 1 END) AS n1, count(DISTINCT event_id) AS n2,"
        " count(CASE WHEN seq = 1 THEN 1 END) AS n3, count(CASE WHEN next_activity IS NULL THEN 1 END) AS n4, NULL::DOUBLE AS x1,"
        f" NULL::DOUBLE AS x2 FROM {src} GROUP BY activity"
        " UNION ALL SELECT 'p', activity, next_activity, count(CASE WHEN first_pair THEN 1 END), count(*), NULL, NULL, median(wait_seconds),"
        f" quantile_cont(wait_seconds, 0.9) FROM {src} WHERE next_activity IS NOT NULL GROUP BY activity, next_activity"
        + (" UNION ALL SELECT 'd', activity, NULL, NULL, NULL, NULL, NULL, median(duration_seconds), NULL FROM (SELECT DISTINCT"
           f" event_id, activity, duration_seconds FROM {src} WHERE duration_seconds IS NOT NULL) GROUP BY activity" if dur else "")),
        asked, user)
    took = {r[1]: _h(r[7]) for r in rows if r[0] == "d"}
    acts = sorted(([a, n1, n2, took.get(a), n3, n4] for k, a, _, n1, n2, n3, n4, _, _ in rows if k == "a"), key=lambda r: (-r[1], -r[2], r[0]))
    pairs = sorted(([a, b, n1, n2, _h(m), _h(q)] for k, a, b, n1, n2, _, _, m, q in rows if k == "p"), key=lambda r: (-r[2], -r[3], r[0], r[1]))
    keep = _cut(acts, args.detail, lambda r: r[1])
    names = {r[0] for r in keep}
    near = [r for r in pairs if r[0] in names and r[1] in names]
    shown = _cut(near, args.detail, lambda r: r[2]) if args.detail < 1 else near
    out = {"process": p["id"], "type": typ, "per": _per(x),
           "totals": {"columns": ["cases", "events"], "rows": [[sum(r[4] for r in acts), sum(r[2] for r in acts)]]},
           "activities": {"columns": ["activity", "cases", "events", "median_duration_hours"], "rows": [r[:4] for r in keep]},
           "pairs": {"columns": ["from", "to", "cases", "count", "median_wait_hours", "p90_wait_hours"], "rows": shown},
           "start": {"columns": ["activity", "cases"], "rows": sorted(([r[0], r[4]] for r in keep if r[4]), key=lambda r: (-r[1], r[0]))},
           "end": {"columns": ["activity", "cases"], "rows": sorted(([r[0], r[5]] for r in keep if r[5]), key=lambda r: (-r[1], r[0]))},
           "left_out": {"activities": len(acts) - len(keep), "pairs": len(pairs) - len(shown)}}
    note = (f"Per {x['name']}: a case is one {x['name']}. `cases` counts {x['plural']}; `events` counts each event once, also one about"
            f" several {x['plural']}; a pair's `count` is how often one activity directly follows another in a case, its wait from the"
            " first one's end (or start, without an end) to the next one's start (0 when they overlap).")
    if out["left_out"]["activities"] or out["left_out"]["pairs"]:
        note += (f" Detail {args.detail}: {len(keep)} of {len(acts)} activities and {len(shown)} of {len(pairs)} pairs are shown;"
                 f" {out['left_out']['activities']} activities and {out['left_out']['pairs']} pairs are left out.")
    return out | {"note": note}


class VariantRead(Read):
    top: int = Field(10, ge=1, le=100, description="the most frequent variants listed one by one; the others are one row")


@action(Role.viewer)
def process_variants(conn, user: User, args: VariantRead) -> dict:
    """A process's variants (a case's activities in order) as you may see them: the `top` most frequent, each with its ID (the same wherever it is read), cases, share and median cycle time, then one row for the other variants. Per case of one object type."""
    p, typ, x, key, (ctes, src), asked = _read(args.project, args, user, "process_variants", {"top": args.top})
    ks = ", ".join(map(_q, key))
    # the text of the variants shown: the activities in order of one case of each (the first by key)
    rows = _run(args.project, _with(
        *ctes, f"cs AS MATERIALIZED (SELECT {ks}, variant_id AS i, cycle_seconds AS s FROM {src} WHERE seq = 1)",
        "vs AS (SELECT i, count(*) AS n, median(s) AS m FROM cs GROUP BY i)",
        "r AS MATERIALIZED (SELECT *, row_number() OVER (ORDER BY n DESC, i) AS rk FROM vs)",
        f"one AS (SELECT {ks}, i FROM (SELECT {ks}, i, row_number() OVER (PARTITION BY i ORDER BY {ks}) AS nth FROM cs"
        f" WHERE i IN (SELECT i FROM r WHERE rk <= {args.top})) WHERE nth = 1)",
        f"tx AS (SELECT one.i, string_agg(activity, '{ARROW}' ORDER BY seq) AS v FROM {src} JOIN one USING ({ks}) GROUP BY one.i)") + (
        f"SELECT r.rk, tx.v, r.i, r.n, r.m, 1 FROM r JOIN tx USING (i) WHERE r.rk <= {args.top}"
        f" UNION ALL SELECT NULL, NULL, NULL, count(*), median(s), count(DISTINCT i) FROM cs WHERE i NOT IN (SELECT i FROM r WHERE rk <= {args.top})"),
        asked, user)
    top = sorted((r for r in rows if r[0] is not None), key=lambda r: (-r[3], r[1]))  # the most cases first, then by the text
    other = next((r for r in rows if r[0] is None and r[3]), None)
    n = sum(r[3] for r in top) + (other[3] if other else 0)
    share = lambda c: round(100 * c / n, 4) if n else None  # noqa: E731
    out = [[v, i, c, share(c), _h(m)] for _, v, i, c, m, _ in top]
    if other:
        out.append([f"other variants ({other[5]})", None, other[3], share(other[3]), _h(other[4])])
    return {"process": p["id"], "type": typ, "per": _per(x),
            "totals": {"columns": ["cases", "variants"], "rows": [[n, len(top) + (other[5] if other else 0)]]},
            "variants": {"columns": ["variant", "id", "cases", "share_pct", "median_cycle_hours"], "rows": out},
            "note": f"Per {x['name']}: a variant is the activities of a {x['name']} in order, joined by →; its cycle time runs from its first"
                    f" event's start to its last event's end (or start). Shares are of the {n} {x['plural']} read; a listed variant's `id`"
                    " filters by it (the row of the other variants has none)."}


class PerfRead(Read):
    period: Literal["day", "week", "month", "quarter", "year"] = Field("month", description="the period of the throughput")


@action(Role.viewer)
def process_performance(conn, user: User, args: PerfRead) -> dict:
    """A process's performance as you may see it: cases started and ended (or still running) per period, the cycle-time distribution, waiting time per pair (ranked by total wait: the bottlenecks, with their share) and working time per activity where events have an end. Per case of one object type; hours."""
    p, typ, x, key, (ctes, src), asked = _read(args.project, args, user, "process_performance", {"period": args.period})
    ks, day = ", ".join(map(_q, key)), lambda c: f"strftime(date_trunc('{args.period}', {c}), '%Y-%m-%d')"  # noqa: E731
    ends = (p.get("reference") or {}).get("end")  # with them a case has ended once its last activity is one; else it ends at its last event
    done = f"last IN ({', '.join(chr(39) + _lit(a) + chr(39) for a in ends)})" if ends else "TRUE"
    rows = _run(args.project, _with(
        *ctes, f"cs AS MATERIALIZED (SELECT min(started_at) AS t0, max(coalesce(ended_at, started_at)) AS t1, max(cycle_seconds) AS s,"
               f" max(CASE WHEN next_activity IS NULL THEN activity END) AS last FROM {src} GROUP BY {ks})") + (
        f"SELECT 's' AS k, {day('t0')} AS a, NULL::VARCHAR AS b, count(*) AS n, NULL::DOUBLE AS x1, NULL::DOUBLE AS x2, NULL::DOUBLE AS x3,"
        f" NULL::DOUBLE AS x4, NULL::DOUBLE AS x5, NULL::DOUBLE AS x6, NULL::DOUBLE AS x7 FROM cs GROUP BY 2"
        f" UNION ALL SELECT 'e', {day('t1')}, NULL, count(*), NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM cs WHERE {done} GROUP BY 2"
        f" UNION ALL SELECT 'r', {day('t0')}, NULL, count(*), NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM cs WHERE NOT ({done}) GROUP BY 2"
        " UNION ALL SELECT 'c', NULL, NULL, count(*), avg(s), " + ", ".join(f"quantile_cont(s, {q})" for q in (0.1, 0.25, 0.5, 0.75, 0.9))
        + f", max(s) FROM cs WHERE {done}"
        f" UNION ALL SELECT 'w', activity, next_activity, count(*), median(wait_seconds), quantile_cont(wait_seconds, 0.9), sum(wait_seconds),"
        f" NULL, NULL, NULL, NULL FROM {src} WHERE next_activity IS NOT NULL GROUP BY activity, next_activity"
        " UNION ALL SELECT 'd', activity, NULL, count(*), median(duration_seconds), sum(duration_seconds), NULL, NULL, NULL, NULL, NULL"
        f" FROM (SELECT DISTINCT event_id, activity, duration_seconds FROM {src} WHERE duration_seconds IS NOT NULL) GROUP BY activity"),
        asked, user)
    by = {}
    for r in rows:
        if r[0] in ("s", "e", "r"):
            by.setdefault(r[1], [r[1], 0, 0, 0])[{"s": 1, "e": 2, "r": 3}[r[0]]] = r[3]
    cyc = next((r for r in rows if r[0] == "c"), None)
    waits = sorted((r for r in rows if r[0] == "w"), key=lambda r: (-(r[6] or 0), r[1], r[2]))
    total = sum(r[6] or 0 for r in waits)
    works = sorted(([a, n, _h(m), _h(t)] for k, a, _, n, m, t, *_ in rows if k == "d"), key=lambda r: (-(r[3] or 0), r[0]))
    flow = (f"A case has ended once its last activity is an end of the process's reference ({', '.join(ends)}): `ended` counts them in the"
            " period of their last event, `running` the others in the period they started, and the cycle times are the ended cases'."
            if ends else "The process has no reference with end activities, so nothing says which cases have ended: `last_event` counts each"
            " case in the period of its last event so far, and the cycle times run to it, also for cases still running.")
    return {"process": p["id"], "type": typ, "per": _per(x),
            "throughput": {"columns": ["period", "started", "ended", "running"] if ends else ["period", "started", "last_event"],
                           "rows": [by[a] if ends else by[a][:3] for a in sorted(by)]},
            "cycle": {"columns": ["cases", "mean_hours", "p10_hours", "p25_hours", "median_hours", "p75_hours", "p90_hours", "max_hours"],
                      "rows": [[cyc[3], *map(_h, cyc[4:11])]] if cyc and cyc[3] else []},
            "waiting": {"columns": ["from", "to", "count", "median_wait_hours", "p90_wait_hours", "total_wait_hours", "share_pct"],
                        "rows": [[a, b, n, _h(m), _h(q9), _h(t), round(100 * t / total, 2) if total and t is not None else None]
                                 for _, a, b, n, m, q9, t, *_ in waits]},
            "working": {"columns": ["activity", "events", "median_hours", "total_hours"], "rows": works},
            "note": f"Per {x['name']}. A period is named by its first day. A case starts at its first event. {flow} Waiting is from one"
                    " activity's end (or start) to the next one's start; the pairs come by total waiting time, the most first, with their"
                    " share of all waiting. Working time is each event's own, once per event"
                    + ("." if works else "; no event of this process has an end, so there is none.")}


def mine(cases: int, acts: dict, pairs: dict, twice: dict, starts: dict, ends: dict, threshold: float, min_share: float) -> tuple[dict, list, list, list]:
    """The heuristic miner over the counts of one read: `acts` {activity: (cases with it, the most times in one case)},
    `pairs` {(a, b): times a is directly followed by b}, `twice` {(a, b): times a, b, a come one after another},
    `starts` and `ends` {activity: cases}. An activity, a pair, a start or an end below `min_share` of the cases is noise;
    a pair is kept when its dependency reaches `threshold`: (a>b - b>a) / (a>b + b>a + 1), a loop of length one
    a>a / (a>a + 1), and a loop of length two keeps both its pairs when (a>>b + b>>a) / (a>>b + b>>a + 1) does.
    Returns (the reference-model shape of a process file, within its schema's limits (REF): the most frequent starts,
    ends and flows, a repeat at most 1,000; its pairs' rows, its activities' rows, what the limits cut).
    ponytail: no `optional` or `any_order`, and no rule keeps every activity connected (a kept activity can be left
    without a kept pair); the flexible heuristic miner's all-connected heuristic if that bites."""
    least = min_share * cases
    keep = {a for a, (c, _) in acts.items() if c >= least}
    rows, flows, loops = [], [], set()
    for (a, b), ab in sorted(pairs.items()):
        ba = pairs.get((b, a), 0)
        d = ab / (ab + 1) if a == b else (ab - ba) / (ab + ba + 1)
        if a != b and (aba := twice.get((a, b), 0) + twice.get((b, a), 0)) >= least and aba / (aba + 1) >= threshold:
            d, loop = max(d, aba / (aba + 1)), True
        else:
            loop = a == b
        ok = a in keep and b in keep and ab >= least and d >= threshold
        rows.append([a, b, ab, round(d, 4), ok])
        if ok:
            flows.append((ab, a, b))
            loops |= {a, b} if loop else set()
    most = lambda xs, n: sorted(sorted(xs, key=lambda a: (-xs[a], a))[:n])  # noqa: E731
    pick = lambda xs: {a: c for a, c in xs.items() if a in keep and c >= least}  # noqa: E731
    starts, ends, kept = pick(starts), pick(ends), sorted(flows, key=lambda f: (-f[0], f[1], f[2]))[:REF[1]]
    reference = {"from": "discovered", "start": most(starts, REF[0]), "end": most(ends, REF[0]), "flows": sorted([a, b] for _, a, b in kept)}
    if repeat := {a: min(acts[a][1], REF[2]) for a in sorted(loops) if acts[a][1] > 1}:
        reference["repeat"] = repeat
    cut = [w for w, n in (("start activities", len(starts) - len(reference["start"])), ("end activities", len(ends) - len(reference["end"])),
                          ("flows", len(flows) - len(kept))) if n > 0] + [f"the repeats of {a}" for a in repeat if acts[a][1] > REF[2]]
    return reference, rows, [[a, c, m, a in keep] for a, (c, m) in sorted(acts.items(), key=lambda i: (-i[1][0], i[0]))], cut


class Discover(Read):
    threshold: float = Field(0.9, ge=0, le=1, description="the dependency a pair needs to be kept (0 to 1)")
    min_share: float = Field(0.02, ge=0, le=1, description="the share of cases below which an activity, a pair, a start or an end is noise")


@action(Role.viewer)
def discover_process(conn, user: User, args: Discover) -> dict:
    """The usual way of a process, discovered from its log as you may see it (heuristic miner: dependency threshold, loops of length one and two), in the shape of a process's `reference`; saves nothing. Per case of one object type, `filters` applied."""
    p, typ, x, key, (ctes, src), asked = _read(args.project, args, user, "discover_process", {"threshold": args.threshold, "min_share": args.min_share})
    ks = ", ".join(map(_q, key))
    on = " AND ".join(f"y.{_q(k)} = x.{_q(k)}" for k in key)
    rows = _run(args.project, _with(*ctes, f"l AS MATERIALIZED (SELECT {ks}, activity, next_activity, seq FROM {src})") + (
        "SELECT 'a' AS k, activity AS a, NULL::VARCHAR AS b, count(*) AS n, max(m) AS m FROM"
        f" (SELECT activity, count(*) AS m FROM l GROUP BY {ks}, activity) GROUP BY activity"
        " UNION ALL SELECT 'p', activity, next_activity, count(*), NULL FROM l WHERE next_activity IS NOT NULL GROUP BY activity, next_activity"
        f" UNION ALL SELECT 'l', x.activity, x.next_activity, count(*), NULL FROM l x JOIN l y ON {on} AND y.seq = x.seq + 1"
        " WHERE y.next_activity = x.activity AND x.activity <> x.next_activity GROUP BY x.activity, x.next_activity"
        " UNION ALL SELECT 's', activity, NULL, count(*), NULL FROM l WHERE seq = 1 GROUP BY activity"
        " UNION ALL SELECT 'e', activity, NULL, count(*), NULL FROM l WHERE next_activity IS NULL GROUP BY activity"),
        asked, user)
    got = lambda tag: {r[1] if r[2] is None else (r[1], r[2]): r[3] for r in rows if r[0] == tag}  # noqa: E731
    starts = got("s")
    reference, pairs, acts, cut = mine(sum(starts.values()), {r[1]: (r[3], r[4]) for r in rows if r[0] == "a"}, got("p"), got("l"), starts,
                                       got("e"), args.threshold, args.min_share)
    return {"process": p["id"], "type": typ, "per": _per(x), "reference": reference,
            "totals": {"columns": ["cases"], "rows": [[sum(starts.values())]]},
            "pairs": {"columns": ["from", "to", "count", "dependency", "kept"], "rows": pairs},
            "activities": {"columns": ["activity", "cases", "most_in_one_case", "kept"], "rows": acts},
            "note": f"The usual way of process '{p['name']}', per {x['name']}, discovered from the log; nothing is saved. It has the shape of a"
                    " process's `reference`: saved there (save_process) it is kept in the file, and nothing checks cases against it yet."
                    + (f" To fit a reference ({REF[0]} start or end activities, {REF[1]:,} flows, a step repeated at most {REF[2]:,} times)"
                       f" it keeps only the most frequent of these: {', '.join(cut)}." if cut else "")}
