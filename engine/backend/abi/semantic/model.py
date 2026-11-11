"""Semantic model objects, date table, glossary, readiness. PRD SE-1, SE-2, §3 readiness, §5 #6 and proactive help.

The model is project definition (§6.6): `model/model.json` and `model/glossary.json` (schemas in
schemas/v1, M-04), changed only by `definition=True` actions, so every edit is an undoable Git
change (F-08). `build_model` stores what `detect` proposes (principle 5: automatic, then
overridable) and generates a calendar when a fact has dates and none exists.
SE-1 mapping: entity = a keyed table (`entities`), dimension/fact = `tables[].kind` (SE-2),
time = `date_table`. Measures (S-01) and datasets (S-04) get their own files.
"""

import hashlib
import json
import logging
import re

import duckdb
from pydantic import BaseModel, Field
from sqlglot import exp

from abi import actions, db, semantic, storage, transform
from abi.actions import ActionFailed, action
from abi.agent import notify, notify_change, relevant
from abi.project import ID, SCHEMA, git
from abi.project.bundle import dumps
from abi.security import Role, User, credentials, role_in
from abi.semantic import live, mel, planner
from abi.semantic.detect import detect
from abi.storage import COLUMN, _q

CALENDAR = "calendar"


def slug(s: str) -> str:
    """A stable object ID (schema pattern ^[a-z][a-z0-9_-]{0,62}$) derived from a name."""
    # ponytail: two names with the same slug share an ID (e.g. tables Sales and sales); disambiguate if that shows up
    s = re.sub(r"[^a-z0-9_]+", "-", s.lower()).strip("-_")
    return (s if re.match("[a-z]", s) else "x-" + s)[:63]


def _read(project: str, file: str) -> dict:
    p = git.path(project) / "model" / file
    return json.loads(p.read_text()) if p.exists() else {}


def _write(project: str, file: str, data: dict) -> None:
    p = git.path(project) / "model" / file
    p.parent.mkdir(exist_ok=True)
    data = {"$schema": SCHEMA + file, **{k: v for k, v in data.items() if v is not None}}
    p.write_bytes(dumps(data).encode())  # PC-4 canonical formatting


def load(project: str) -> dict:
    """model.json with tables and entities merged into {table: {kind, key, description[, hidden]}} for editing."""
    f = _read(project, "model.json")
    keys = {e["table"]: e["key"] for e in f.get("entities", [])}
    tables = {t["table"]: {"kind": t["kind"], "key": keys.pop(t["table"], None), "description": t.get("description"),
                           **({"hidden": True} if t.get("hidden") else {})} for t in f.get("tables", [])}
    tables |= {t: {"kind": "fact", "key": k, "description": None} for t, k in keys.items()}
    return {"tables": tables, "relationships": f.get("relationships", []), "date_table": f.get("date_table"),
            "rates_table": f.get("rates_table"), "removed_relationships": f.get("removed_relationships", [])}


def _file(m: dict) -> dict:
    """`load`'s model as model.json holds it (without $schema)."""
    ts = m["tables"]
    return {
        "tables": [{"table": t, "kind": i["kind"], **({"description": i["description"]} if i["description"] else {}),
                    **({"hidden": True} if i.get("hidden") else {})} for t, i in ts.items()],
        "entities": [{"id": slug(t), "table": t, "key": i["key"]} for t, i in ts.items() if i["key"]],
        "relationships": m["relationships"],
        "date_table": m["date_table"],
        "rates_table": m.get("rates_table"),
        "removed_relationships": sorted(set(m.get("removed_relationships", []))) or None,
    }


def save(project: str, m: dict) -> None:
    f = _file(m)
    _follow_kept(project, f)
    _write(project, "model.json", f)


def _follow_kept(project: str, f: dict) -> None:
    """1.2.6 M3, owner answer 1: every action that saves model.json (add_relationship re-adding a relationship under
    its ID with other columns, set_table, scoring, ...) and changes what an `rls: follow` link means (rls.resolve:
    the relationships it walks, their columns, a table on the way hidden) needs the Owner and a confirmation naming
    who sees other rows, as an import does (imports.gate); an Editor gets 403 before anything is written. Editors keep
    every other relationship, those the follow links don't walk included."""
    if not any(ln.get("rls") == "follow" for ln in _read(project, "ontology.json").get("links", [])):
        return
    from abi.project import bundle, imports  # not at top: imports imports this module

    old = bundle.bundle(git.path(project))
    new = old | {"model/model.json": {"$schema": SCHEMA + "model.json", **f}}
    if (was := imports.follow_links(old)) == (now := imports.follow_links(new)):
        return
    user = actions._call.get((None,))[0]
    with db.connect() as conn:
        if user is None or (role_in(conn, user, project) or 0) < Role.owner:
            raise ActionFailed(403, f"Only a project Owner may change the relationships or tables a link that row-level security"
                                    f" follows walks (rls: follow, model/ontology.json); nothing changed in project '{project}'.")
        why = imports.fewer(conn, project, old, new)
    actions.require_confirmation(why, {"follow": {"old": was, "new": now}})


def visible(m: dict) -> dict:
    """`load`'s model without hidden (staging) tables and the relationships that touch them (MODEL-HIDE): what the
    planner, readiness, the agent's semantic_model and docs treat as the semantic model. The data stays loaded."""
    tables = {t: i for t, i in m["tables"].items() if not i.get("hidden")}
    return {**m, "tables": tables, "relationships": [r for r in m["relationships"] if not {
        r["from"]["table"], r["to"]["table"]} & (m["tables"].keys() - tables.keys())]}


def _staging(project: str) -> set[str]:
    """Raw inputs of other tables: tables whose source another table loads, or that another table appends (e.g.
    per-country files appended into one table). A `merge` lookup is left visible: it is usually a dimension too."""
    try:
        steps = transform.tables(project)
    except ValueError:  # a broken tables/<id>.json: the import checks report it; hide nothing
        return set()
    return {x for t, s in steps.items() for x in {s[0].reads} | {a for st in s if st.type == "append" for a in st.tables}
            if x != t}


def _rel(frm: str, to: str) -> dict:
    (ft, fc), (tt, tc) = frm.split(".", 1), to.split(".", 1)
    return {"id": slug(f"{ft}-{fc}"), "from": {"table": ft, "columns": [fc]}, "to": {"table": tt, "columns": [tc]}}


def _name(side: dict) -> str:
    return f"{side['table']}.{','.join(side['columns'])}"


def lake_columns(project: str, schema: str = "model") -> dict[str, dict[str, str]]:
    cols = semantic._columns_of(storage.lake(project).cursor(), schema)
    return cols | live.columns(project) if schema == "model" else cols  # IN-3: live tables are model tables too


def _calendar(project: str, dates: list[dict], author: str) -> None:
    """Generate lake.model.calendar: one row per day over the full years spanned by the facts' dates."""
    lo, hi = min(r["min"] for r in dates)[:10], max(r["max"] for r in dates)[:10]
    sql = (f"SELECT d::DATE AS day, year(d) AS year, quarter(d) AS quarter, month(d) AS month,"
           f" week(d) AS week, isoyear(d) AS week_year"  # ISO week and its year (ML-SEASONAL.2)
           f" FROM generate_series(date_trunc('year', DATE '{lo}'), date_trunc('year', DATE '{hi}')"
           f" + INTERVAL 1 YEAR - INTERVAL 1 DAY, INTERVAL 1 DAY) g(d)")
    storage._commit(storage.lake(project),
                    lambda cur: cur.execute(f"CREATE OR REPLACE TABLE lake.model.{CALENDAR} AS {sql}"),
                    author, "generate date table")


def extend_calendar(project: str, table: str, author: str) -> None:
    """IN-5 on refresh: when `table`'s dates linked to the generated calendar fall outside its years, regenerate it over
    the full years of both (one snapshot right after the refresh). A user-declared date table is never touched.
    The calendar lives in the lake, not the repo: when a pull, import or revert brings a model naming a calendar this
    lake never generated, it is generated here over every linked loaded table (X-04-fix-1)."""
    m, types = load(project), lake_columns(project)
    if (m["date_table"] or {}).get("table") != CALENDAR or CALENDAR in lake_columns(project, "raw") or any(
            (git.path(project) / d / f"{CALENDAR}.json").is_file() for d in ("sources", "tables")):
        return  # another date table, or 'calendar' is a loaded or built table (M4.2: one with no steps keeps no raw.<t>)
    have = CALENDAR in types
    cols = {}
    for r in m["relationships"]:
        t = r["from"]["table"]
        if r["to"]["table"] == CALENDAR and (t == table or not have):
            cols.setdefault(t, []).extend(  # the new file may have dropped the column or no longer hold dates
                c for c in r["from"]["columns"] if types.get(t, {}).get(c, "").startswith(("DATE", "TIMESTAMP")))
    cols = {t: cs for t, cs in cols.items() if cs}
    if not cols:
        return
    # ponytail: dates outside years 1..9999 (infinity sentinels, BC, 5-digit years) never extend it; they had no calendar row before either
    ok = "FILTER (WHERE {0} BETWEEN DATE '0001-01-01' AND DATE '9999-12-31')"  # 4-digit ISO text below
    span = lambda f, g: f"{f}(" + ", ".join(  # least(min…) / greatest(max…) over each linked table
        f"(SELECT {f}({', '.join(f'{g}({d(t, c)}) {ok.format(d(t, c))}' for c in cs)}) FROM {_q(t)})" for t, cs in cols.items()) + ")::VARCHAR"
    cal = f"min(day)::VARCHAR, max(day)::VARCHAR FROM {CALENDAR}" if have else "NULL, NULL"
    try:  # D-26: the refresh has committed; a failure here leaves the calendar as it was and says so in the feed
        days = planner.local(m, types, semantic.locale(project)["time_zone"])  # SE-13: the days the joins see
        d = lambda t, c: planner.day(exp.column(c, quoted=True), days.get((t, c))).sql("duckdb")
        [(a, b, lo, hi)] = semantic.execute(project, (  # ISO text compares as dates
            f"SELECT {span('least', 'min')}, {span('greatest', 'max')}, {cal}"), {"extend_calendar": table})
        old = have and "week" not in types[CALENDAR]  # generated before ML-SEASONAL.2: regenerate with the week columns
        if a and (not lo or a < lo or b > hi) or old and lo:
            _calendar(project, [{"min": a, "max": b}] * bool(a) + [{"min": lo, "max": hi}] * bool(lo), author)
    except (duckdb.Error, ActionFailed) as e:  # ActionFailed: a project.json zone that isn't one
        logging.getLogger(__name__).error("Extending the date table for %s/%s failed: %s", project, table,
                                          credentials.log_text(e))
        msg = f"Date table not extended for {table}: {type(e).__name__}"
        notify("refresh", msg, project, table, len(msg))  # an error type name only, no data


def extend_calendars(project: str, author: str) -> None:
    """IN-5/SE-13: a new time zone can move any linked table's local days past the generated calendar's years."""
    for t in dict.fromkeys(r["from"]["table"] for r in load(project)["relationships"]):
        extend_calendar(project, t, author)


@action(Role.editor, definition=True)
def build_model(conn, user: User, args: storage.ProjectRef) -> dict:
    """Builds the semantic model from detection: stores each table's kind (fact/dimension) and key, the many-to-one relationships and the date table, and generates a calendar table when a fact has dates but no date table exists. A new table that only feeds another table's steps (a staging input) is stored hidden: kept out of the model, queries and docs (set_table hidden=false shows it). Existing model entries (your overrides) are kept. Returns the readiness report."""
    found = detect(args.project, user.id)
    dated = [r for i in found["tables"].values() if i["kind"] == "fact" for r in i["date_range"].values() if r["min"]]
    if not found["date_table"] and dated and CALENDAR not in found["tables"]:
        _calendar(args.project, dated, user.name)
        found = detect(args.project, user.id)  # links the facts' DATE columns to the new calendar
    m, staging = load(args.project), _staging(args.project)
    for t, info in found["tables"].items():
        m["tables"].setdefault(t, {"kind": info["kind"], "key": info["key"] and [info["key"]], "description": None,
                                   "hidden": t in staging})
    for t in live.columns(args.project):  # IN-3: no key/relationship scan over live data; add_relationship sets them
        m["tables"].setdefault(t, {"kind": "fact", "key": None, "description": None})
    # DEMO-BRIEF-fix-1: one link from a table to a target (a second DATE column to the calendar made every query
    # ambiguous), the existing one kept; a relationship the person removed stays removed
    have = {r["id"] for r in m["relationships"]} | set(m["removed_relationships"])
    linked = {(r["from"]["table"], r["to"]["table"]) for r in m["relationships"]}
    for r in (_rel(x["from"], x["to"]) for x in found["relationships"]):
        if r["id"] not in have and (pair := (r["from"]["table"], r["to"]["table"])) not in linked:
            m["relationships"].append(r)
            linked.add(pair)
    m["date_table"] = m["date_table"] or found["date_table"]
    save(args.project, m)
    return report(args.project)


class TableDef(storage.ProjectRef):
    table: str = Field(min_length=1)
    kind: str | None = Field(None, pattern="^(fact|dimension)$")
    key: list[str] | None = Field(None, min_length=1, description="key column(s)")
    description: str | None = Field(None, min_length=1, max_length=2000)
    hidden: bool | None = Field(None, description="true keeps a staging table out of the semantic model (queries, readiness, docs); its data stays loaded")


@action(Role.editor, definition=True)
def set_table(conn, user: User, args: TableDef) -> dict:
    """Overrides a model table's kind (fact or dimension), key column(s), description and/or hidden flag (a hidden staging table stays loaded but is out of the semantic model); omitted fields stay as they are."""
    cols = lake_columns(args.project).get(args.table)
    if cols is None:
        raise ActionFailed(404, f"No table '{args.table}' in project '{args.project}'.")
    if missing := set(args.key or []) - set(cols):
        raise ActionFailed(400, f"Table '{args.table}' has no column {sorted(missing)}.")
    m = load(args.project)
    t = m["tables"].setdefault(args.table, {"kind": "fact", "key": None, "description": None})
    from abi.semantic import ontology  # not at top: ontology imports this module

    if args.key and t["key"] and args.key != t["key"] and (used := ontology.uses(args.project, f"table:{args.table}")):
        raise ActionFailed(409, f"The key of '{args.table}' is the identity of {', '.join(used)} in the ontology; change or"
                                " remove those first (save_ontology, delete_ontology_item).", uses=used)
    t.update(args.model_dump(include={"kind", "key", "description", "hidden"}, exclude_none=True))
    ontology.guard(args.project, _file(m), f"Table '{args.table}'")  # 1.2.6: hidden or unkeyed while a type uses it
    save(args.project, m)
    if args.description:  # §8: marked agent-written when the Main Agent wrote it, else human-reviewed
        from abi import docs  # not at top: docs imports this module

        docs.mark(args.project, {f"table:{args.table}": "agent" if actions.by_agent() else "human"})
    return {"table": t, "readiness": report(args.project)}


class Relationship(storage.ProjectRef):
    from_: str = Field(alias="from", pattern=COLUMN, description="fact.column (the many side)")
    to: str = Field(pattern=COLUMN, description="dimension.key (the one side)")


@action(Role.editor, definition=True)
def add_relationship(conn, user: User, args: Relationship) -> dict:
    """Declares a many-to-one relationship from a column to another table's key (SE-2); that table becomes a dimension."""
    cols, m, r = lake_columns(args.project), load(args.project), _rel(args.from_, args.to)
    ft, fc, tt = r["from"]["table"], r["from"]["columns"][0], r["to"]["table"]
    if fc not in cols.get(ft, {}):
        raise ActionFailed(400, f"No column '{args.from_}' in project '{args.project}'.")
    if ft == tt or m["tables"].get(tt, {}).get("key") != r["to"]["columns"]:
        raise ActionFailed(400, f"'{args.to}' must be the key of another model table (many-to-one only); set it with set_table first.")
    m["relationships"] = [x for x in m["relationships"] if x["id"] != r["id"]] + [r]
    m["removed_relationships"] = [i for i in m["removed_relationships"] if i != r["id"]]
    m["tables"][tt]["kind"] = "dimension"
    save(args.project, m)
    extend_calendar(args.project, ft, user.name)  # IN-5/SE-13: a newly linked (timestamp) column's local days get rows
    return {"id": r["id"], "readiness": report(args.project)}


class ObjectRef(storage.ProjectRef):
    id: str


@action(Role.editor, destructive=True, definition=True)
def remove_relationship(conn, user: User, args: ObjectRef) -> dict:
    """Deletes a model relationship by its ID; build_model won't add it back (add_relationship can)."""
    m = load(args.project)
    kept = [r for r in m["relationships"] if r["id"] != args.id]
    if len(kept) == len(m["relationships"]):
        raise ActionFailed(400, f"No relationship '{args.id}'.")
    m["relationships"] = kept
    m["removed_relationships"].append(args.id)
    from abi.semantic import ontology  # not at top: ontology imports this module

    ontology.guard(args.project, _file(m), f"Relationship '{args.id}'")  # 1.2.6: refused while a link walks it
    save(args.project, m)
    return report(args.project)


class DateTable(storage.ProjectRef):
    column: str = Field(pattern=COLUMN, description="calendar.day: a DATE column with one row per day")


@action(Role.editor, definition=True)
def set_date_table(conn, user: User, args: DateTable) -> dict:
    """Declares which table is the model's date table (its DATE key column)."""
    t, c = args.column.split(".", 1)
    if lake_columns(args.project).get(t, {}).get(c) != "DATE":
        raise ActionFailed(400, f"'{args.column}' is not a DATE column of a model table.")
    m = load(args.project)
    m["date_table"] = {"table": t, "column": c}
    m["tables"].setdefault(t, {"kind": "dimension", "key": [c], "description": None})
    save(args.project, m)
    return report(args.project)


RATES = {"date": "DATE", "from": "VARCHAR", "to": "VARCHAR", "rate": "DECIMAL"}  # SE-13: E-11.md decision 4


def _rates_problem(project: str, t: str, cols: dict) -> str | None:
    """Why `t` can't be the rates table (None: it can)."""
    if t not in cols:
        return f"Rates table '{t}' is not a loaded model table."
    if live.is_live(project, t):
        return f"Rates table '{t}' is a live table; conversion reads an imported or input table (DECISIONS D-8)."
    have = cols[t]
    if set(have) != set(RATES) or any(not have[c].startswith(ty) for c, ty in RATES.items()):
        return (f"Rates table '{t}' must have exactly the columns date DATE, from VARCHAR, to VARCHAR and rate DECIMAL "
                f"(a DOUBLE rate isn't exact: change it with set_type); it has "
                + ", ".join(f"{c} {ty}" for c, ty in have.items()) + ".")
    return None


class RatesTable(storage.ProjectRef):
    table: str = Field(description="the model table of currency rates; empty removes it")


@action(Role.editor, definition=True)
def set_rates_table(conn, user: User, args: RatesTable) -> dict:
    """Declares the model's currency rates table (SE-13): exactly the columns date DATE, from VARCHAR, to VARCHAR and rate DECIMAL, one row per (from, to, date). CONVERT in measures converts amounts with the latest rate on or before each row's day into the display currency."""
    if args.table and (why := _rates_problem(args.project, args.table, lake_columns(args.project))):
        raise ActionFailed(400, why)
    m = load(args.project)
    m["rates_table"] = args.table or None
    save(args.project, m)
    return report(args.project)


def check(project: str) -> list[str]:
    """§3 readiness: what is missing, in plain language. Empty = Ready. Hidden (staging) tables aren't checked."""
    m, cols = visible(load(project)), lake_columns(project)
    gaps = [f"Table '{t}' is in the model but not loaded." for t in m["tables"] if t not in cols]
    tables = {t: i for t, i in m["tables"].items() if t in cols}
    if not tables:
        return gaps + ["No model yet: load data and run build_model."]

    valid_keys = set()
    for t, i in tables.items():
        k = i["key"]
        if not k:
            if i["kind"] == "dimension":
                gaps.append(f"Dimension '{t}' has no key; declare one with set_table.")
            continue
        ks = ", ".join(map(_q, k))
        sql = (f"SELECT count(*), count(DISTINCT ({ks})), count(*) FILTER (WHERE"
               f" {' AND '.join(f'{_q(c)} IS NOT NULL' for c in k)}) FROM {_q(t)}")
        if on := live.is_live(project, t):  # the same check in ClickHouse SQL (uniqExact: exact)
            ch = [exp.to_identifier(c, quoted=True).sql("clickhouse") for c in k]
            sql = (f"SELECT count(), uniqExact({', '.join(ch)}), countIf({' AND '.join(f'{c} IS NOT NULL' for c in ch)})"
                   f" FROM {live.table_sql(project, t)}")
        if set(k) <= set(cols[t]) and len(set(semantic.execute(  # principle 4: unqualified = the table queries read
                project, sql, {"readiness": f"key {t}"}, live=t if on else None)[0])) == 1:
            valid_keys.add(_name({"table": t, "columns": k}))
        else:
            gaps.append(f"Key '{_name({'table': t, 'columns': k})}' is missing or not unique and non-null.")

    ok, bad = set(), set()
    for r in m["relationships"]:
        ft = r["from"]["table"]
        valid = ft in tables and set(r["from"]["columns"]) <= set(cols[ft]) and _name(r["to"]) in valid_keys
        (ok if valid else bad).add(ft)
        if not valid:
            gaps.append(f"Relationship {_name(r['from'])} -> {_name(r['to'])} is not a valid many-to-one link.")
    if not any(i["kind"] == "fact" and t in ok and t not in bad for t, i in tables.items()):
        gaps.append("No fact table with valid many-to-one relationships to its dimensions.")

    ms = _read(project, "measures.json").get("measures", [])
    try:  # §3 "every measure compiles and passes its tests"
        mel.compile({x["id"]: x["expression"] for x in ms}, {t: set(cols[t]) for t in tables})
    except mel.MelError as e:
        gaps += [f"Measure '{x['measure']}' doesn't compile: {x['error']} {x['fix']}" for x in e.errors]
    else:
        gaps += semantic.measure_tests(project, ms)

    dt = m["date_table"]
    has_dates = any(i["kind"] == "fact" and any(v == "DATE" or v.startswith("TIMESTAMP") for v in cols[t].values())
                    for t, i in tables.items())
    if has_dates and not (dt and cols.get(dt["table"], {}).get(dt["column"]) == "DATE"):
        gaps.append("A fact has dates but there is no date table; run build_model or set_date_table.")

    if rt := m["rates_table"]:  # SE-13: E-11.md decision 4
        if why := _rates_problem(project, rt, cols):
            gaps.append(why)
        elif dup := semantic.execute(project, f'SELECT count(*) FROM (SELECT 1 FROM {_q(rt)} GROUP BY "from", "to", date'
                                              " HAVING count(*) > 1)", {"readiness": f"rates {rt}"})[0][0]:
            gaps.append(f"Rates table '{rt}' has {dup} (from, to, date) with more than one rate; keep one per pair and day.")
    elif any(re.search(r"\bCONVERT\s*\(", x["expression"], re.IGNORECASE) for x in ms):
        gaps.append("A measure uses CONVERT but the model has no rates table; declare one with set_rates_table.")

    gaps += [f"Table '{t}' has no description." for t, i in tables.items() if not i["description"]]
    gaps += [f"Table '{t}' {'holds ML predictions' if t.startswith('ml_') else 'is built by steps'} made from rows that"
             " row-level security rules filter; restricted Viewers see none of its rows until a rule on it (or a"
             " relationship to each filtered table it is made from) filters it."
             for t in semantic.rls.unfiltered_outputs(project)]  # model tables or not: SQL and grids read them too
    from abi.semantic import ontology  # not at top: ontology imports this module
    gaps += ontology.gaps(project)  # 1.2.6: dangling references, only once an ontology exists
    from abi import ml  # not at top: ml imports this module
    gaps += [f"Table '{t}' was pre-filled from an ML forecast that row-level security rules now filter, and has no rule of"
             " its own, so every Viewer sees every pre-filled value; add a rule on it." for t in ml.prefill_gaps(project)]
    return gaps


def report(project: str) -> dict:
    """Readiness report; posts it to the feed when the gaps changed since the last post (§5 proactive help, quiet)."""
    gaps = check(project)
    notify_change("readiness", project, "Semantic model is ", "not Ready: " + " ".join(gaps) if gaps else None, "Ready.")
    return {"ready": not gaps, "missing": gaps}


@action(Role.viewer)
def readiness(conn, user: User, args: storage.ProjectRef) -> dict:
    """Reports whether the semantic model is Ready (§3) and lists what is missing."""
    return report(args.project)


class ModelInput(storage.ProjectRef):
    all: bool = Field(False, description="every item, not only those relevant to your question")
    model: str | None = Field(None, description="a named model (see `models`): only its tables, relationships and measures")


@action(Role.viewer)
def semantic_model(conn, user: User, args: ModelInput) -> dict:
    """What semantic_query can ask for (in one named model with `model`; `models` lists them): measures (ID, display name, MEL expression, description), model tables (kind, key, description) with their columns, relationships, the date table, datasets (ID, name), glossary terms (ID, term, definition, linked measures, columns, object types and links) and, with an ontology, its object types (table, key, title column, each link by ID with the type it leads to) and links, whose descriptions are listed as glossary terms too (IDs object:…, property:…, link:…). Hidden staging tables are left out. In a large project only the tables, measures and terms relevant to your question are listed (`relevant` says so); `all` lists every one."""
    ms = [{k: x[k] for k in ("id", "name", "expression", "description") if k in x} for x in _read(args.project, "measures.json").get("measures", [])]
    m = load(args.project)
    cols = {t: c for t, c in lake_columns(args.project).items() if not m["tables"].get(t, {}).get("hidden")}
    out = {**visible(m), "columns": cols, "measures": ms,
           "datasets": [{"id": d["id"], "name": d["name"]} for d in _read(args.project, "datasets.json").get("datasets", [])],
           "glossary": [{k: t[k] for k in ("id", "term", "definition", "measures", "columns", "objects", "links") if k in t} for t in masked(
               args.project, _read(args.project, "glossary.json").get("terms", []), semantic.rls.restricted(args.project, user.id))],
           "models": named_models(args.project)}
    from abi.semantic import ontology  # not at top: ontology imports this module
    if (git.path(args.project) / ontology.FILE).is_file():  # 1.2.6 M6: object types and links, as the reader sees them
        d = ontology.describe(args.project, user.id)
        out["objects"] = [{k: x[k] for k in ("id", "name", "plural", "table", "key", "title", "description", "measures", "links", "actions")
                           if x.get(k)} for x in d["objects"]]
        out["links"] = [{k: ln[k] for k in ("id", "from", "to", "name", "inverse", "cardinality", "description") if ln.get(k)} for ln in d["links"]]
        out["glossary"] += ontology.terms(d)  # their descriptions are their definitions, beside the free terms
    if args.model:
        tables, keep = scope(args.project, args.model)
        out |= {"model": args.model, "tables": {t: i for t, i in out["tables"].items() if t in tables},
                "columns": {t: c for t, c in out["columns"].items() if t in tables},
                "relationships": [r for r in out["relationships"] if {r["from"]["table"], r["to"]["table"]} <= tables],
                "measures": [x for x in out["measures"] if x["id"] in keep]}
    out = out if args.all else _relevant(user, args.project, out)
    if "objects" in out:  # only the types on a listed table, and the links between them
        out["objects"] = [x for x in out["objects"] if x["table"] in out["tables"]]
        out["links"] = [ln for ln in out["links"] if {ln["from"], ln["to"]} <= {x["id"] for x in out["objects"]}]
    return out


def _relevant(user: User, project: str, out: dict) -> dict:
    """D-117: the tables, measures and glossary terms closest to the turn's question, ranked over this user's own result."""
    q, size = relevant.QUESTION.get(), len(out["tables"]) + len(out["measures"]) + len(out["glossary"])
    tables = relevant.ranked(user, project, q, {t: " ".join([t, i.get("description") or "", *out["columns"].get(t, {})])
                                                for t, i in out["tables"].items()}, size)
    ms = relevant.ranked(user, project, q, {x["id"]: " ".join(str(x.get(k) or "") for k in ("id", "name", "expression", "description"))
                                            for x in out["measures"]}, size)
    terms = relevant.ranked(user, project, q, {x["id"]: f"{x['term']}: {x['definition']}" for x in out["glossary"]}, size)
    if tables is None and ms is None and terms is None:
        return out
    if tables is not None:
        out |= {"tables": {t: i for t, i in out["tables"].items() if t in tables},
                "columns": {t: c for t, c in out["columns"].items() if t in tables or t not in out["tables"]}}
    out["measures"] = [x for x in out["measures"] if ms is None or x["id"] in ms]
    out["glossary"] = [x for x in out["glossary"] if terms is None or x["id"] in terms]
    return out | {"relevant": relevant.NOTE}


def _sha(t: dict) -> str:
    return hashlib.sha256(json.dumps([t["term"], t["definition"]]).encode()).hexdigest()


def masked(project: str, terms: list[dict], hide: bool) -> list[dict]:
    """H-01.4-fix-5 (SE-6, SC-5, D-47): with `hide` (a restricted Viewer, or readers unknown), a term whose text no
    person wrote shows rls.MARK as its term and definition: an agent's text may quote rows; an imported or older one
    has no known writer (fail closed). Its ID and links stay."""
    if not hide or not terms:
        return terms
    with db.connect() as conn:
        people = {r["sha"] for r in conn.execute("SELECT sha FROM glossary_people WHERE project = %s", [project])}
    return [t if _sha(t) in people else t | {"term": semantic.rls.MARK, "definition": semantic.rls.MARK} for t in terms]


class Term(storage.ProjectRef):
    term: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=2000)
    columns: list[str] = Field([], description="table.column items that implement the term")
    measures: list[str] = Field([], description="measure IDs that implement the term")
    objects: list[str] = Field([], description="object type IDs (`ontology`) the term is about")
    links: list[str] = Field([], description="link IDs (`ontology`) the term is about")


@action(Role.editor, definition=True)
def set_glossary_term(conn, user: User, args: Term) -> dict:
    """Adds or replaces a glossary term: a business term, its definition, the columns/measures that implement it and the ontology's object types and links it is about. Returns its ID."""
    cols = lake_columns(args.project)
    for c in args.columns:
        if not re.fullmatch(COLUMN, c) or c.split(".", 1)[1] not in cols.get(c.split(".", 1)[0], {}):
            raise ActionFailed(400, f"'{c}' is not a table.column of project '{args.project}'.")
    known = {x["id"] for x in _read(args.project, "measures.json").get("measures", [])}
    if missing := sorted(set(args.measures) - known):
        raise ActionFailed(400, f"No measure {missing} in project '{args.project}'.")
    t = {"id": slug(args.term), "term": args.term, "definition": args.definition}
    t |= {"columns": [dict(zip(("table", "column"), c.split(".", 1))) for c in args.columns]} if args.columns else {}
    t |= {"measures": sorted(set(args.measures))} if args.measures else {}
    if args.objects or args.links:  # 1.2.6 M6
        from abi.semantic import ontology  # not at top: ontology imports this module
        o = ontology.read(args.project)
        for k in ("objects", "links"):
            if missing := sorted(set(getattr(args, k)) - {x["id"] for x in o.get(k, [])}):
                raise ActionFailed(400, f"No {k[:-1]} {missing} in the ontology of project '{args.project}'.")
            t |= {k: sorted(set(getattr(args, k)))} if getattr(args, k) else {}
    g = _read(args.project, "glossary.json").get("terms", [])
    _write(args.project, "glossary.json", {"terms": [x for x in g if x["id"] != t["id"]] + [t]})
    if actions.VIA.get() == "rest" and not actions.inbound():  # a person typed it (not an LLM, flow or inbound text)
        conn.execute("INSERT INTO glossary_people VALUES (%s, %s) ON CONFLICT DO NOTHING", [args.project, _sha(t)])
    return {"id": t["id"]}


@action(Role.editor, destructive=True, definition=True)
def delete_glossary_term(conn, user: User, args: ObjectRef) -> dict:
    """Deletes a glossary term by its ID and unlinks it from project memory entries."""
    g = _read(args.project, "glossary.json").get("terms", [])
    kept = [x for x in g if x["id"] != args.id]
    if len(kept) == len(g):
        raise ActionFailed(400, f"No glossary term '{args.id}'.")
    _write(args.project, "glossary.json", {"terms": kept})
    conn.execute("UPDATE memory SET terms = array_remove(terms, %s) WHERE project = %s", [args.id, args.project])
    return {}


def terms(project: str) -> set[str]:
    """Glossary term IDs, which memory entries link to (§5 #6)."""
    return {x["id"] for x in _read(project, "glossary.json").get("terms", [])}


# ---------- named models: subsets of the one model for one audience (model/models.json) ----------
# ABI-PI: several models in one project, as perspectives. Joins and measures stay the project's own, so a number is
# the same in every model; a model only narrows what a report or a question may use.

class NamedModel(BaseModel):
    id: str = Field(pattern=ID, description="lowercase ID, e.g. finance")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    tables: list[str] = Field(min_length=1, description="model tables in it (the date table is always in)")
    measures: list[str] = Field([], description="measure IDs in it; empty: every measure over its tables")


class ModelSave(storage.ProjectRef):
    model: NamedModel


def named_models(project: str) -> list[dict]:
    return _read(project, "models.json").get("models", [])


def scope(project: str, mid: str) -> tuple[set[str], set[str]]:
    """The tables and measures named model `mid` allows. Raises 404 with the models there are."""
    m = next((x for x in named_models(project) if x["id"] == mid), None)
    if m is None:
        have = ", ".join(x["id"] for x in named_models(project)) or "none yet"
        raise ActionFailed(404, f"No model '{mid}' in project '{project}' (models: {have}).")
    date = (_read(project, "model.json").get("date_table") or {}).get("table")
    tables = set(m["tables"]) | ({date} if date else set())
    ms = {x["id"]: x["expression"] for x in _read(project, "measures.json").get("measures", [])}
    if m.get("measures"):
        return tables, set(m["measures"]) & ms.keys()
    trees = mel.compile(ms)
    used = {i: {n.table if type(n) is mel.Col else n.col.table for n in mel._walk(t) if type(n) in (mel.Col, mel.Agg)}
            for i, t in trees.items()}
    return tables, {i for i, ts in used.items() if ts <= tables}


def in_scope(project: str, mid: str, measures: list[str], columns: list[str]) -> None:
    """Refuses (400) measures or table.column items outside named model `mid`, saying which and what it has."""
    tables, ms = scope(project, mid)
    bad = [m for m in measures if m not in ms] + [c for c in columns if c.split(".", 1)[0] not in tables]
    if bad:
        raise ActionFailed(400, f"{', '.join(bad)} {'is' if len(bad) == 1 else 'are'} not in model '{mid}': it has tables "
                                f"{', '.join(sorted(tables))} and measures {', '.join(sorted(ms)) or 'none'}.")


@action(Role.editor, definition=True)
def save_model(conn, user: User, args: ModelSave) -> dict:
    """Adds or replaces a named model of the project: a subset of its model tables and measures for one audience or subject (e.g. Finance, Marketing), kept in model/models.json. Reports and questions can name it to stay inside it; joins and measures are shared, so numbers match across models."""
    m = args.model
    known = visible(load(args.project))["tables"]
    if missing := sorted(set(m.tables) - known.keys()):
        raise ActionFailed(400, f"No model table {missing} in project '{args.project}' (hidden staging tables can't be in a model).")
    ms = {x["id"] for x in _read(args.project, "measures.json").get("measures", [])}
    if missing := sorted(set(m.measures) - ms):
        raise ActionFailed(400, f"No measure {missing} in project '{args.project}'.")
    entry = {"id": m.id, "name": m.name, **({"description": m.description} if m.description else {}),
             "tables": sorted(set(m.tables)), **({"measures": sorted(set(m.measures))} if m.measures else {})}
    _write(args.project, "models.json", {"models": [x for x in named_models(args.project) if x["id"] != m.id] + [entry]})
    return {"id": m.id}


@action(Role.editor, destructive=True, definition=True)
def delete_model(conn, user: User, args: ObjectRef) -> dict:
    """Deletes a named model (not its tables or measures). Refused while a report is built in it."""
    ms = named_models(args.project)
    if not any(x["id"] == args.id for x in ms):
        raise ActionFailed(400, f"No model '{args.id}'.")
    users = sorted(f.stem for f in (git.path(args.project) / "reports").glob("*.json") if json.loads(f.read_text()).get("model") == args.id)
    if users:
        raise ActionFailed(409, f"Report(s) {', '.join(users)} are built in model '{args.id}': move them to another model first.")
    _write(args.project, "models.json", {"models": [x for x in ms if x["id"] != args.id]})
    return {}


class Dataset(storage.ProjectRef):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=200, description="display name")
    description: str | None = None
    query: semantic.SemanticQuery


def _datasets(conn, user: User, project: str, change) -> dict:
    """model/datasets.json changed by `change(datasets) -> datasets`, applied like an import (PC-3: schema, unknown measures, uses)."""
    from abi.project import bundle, imports  # not at top: imports imports this module

    files = bundle.bundle(git.path(project))
    ds = change((files.get("model/datasets.json") or {}).get("datasets", []))
    return imports.apply(conn, user, project, files | {"model/datasets.json": {"$schema": SCHEMA + "datasets.json", "datasets": ds}})


@action(Role.editor, definition=True)
def save_dataset(conn, user: User, args: Dataset) -> dict:
    """Adds or replaces a dataset (model/datasets.json): a named semantic query at a defined grain, reused by reports, tools, flows, agents and ML. Checked like an import (unknown measures refused)."""
    d = {"id": args.id, "name": args.name} | args.model_dump(mode="json", include={"description", "query"}, exclude_none=True, exclude_defaults=True)

    def put(ds):  # SH-1: the creator owns it; owner and sharing stay as stored (changed by set_sharing or a gated import)
        old = {x["id"]: x for x in ds}
        keep = old.get(args.id, {"owner": str(user.id)})
        return list((old | {args.id: d | {k: keep[k] for k in ("owner", "sharing") if k in keep}}).values())
    return _datasets(conn, user, args.project, put)


class DatasetRef(storage.ProjectRef):
    dataset: str = Field(pattern=ID)


@action(Role.viewer)
def dataset_data(conn, user: User, args: DatasetRef) -> dict:
    """Rows of one dataset (model/datasets.json): its saved semantic query run through the semantic engine as you (row-level security applies). Columns: the dimensions, then the measure IDs."""
    if not (d := next((x for x in _read(args.project, "datasets.json").get("datasets", []) if x["id"] == args.dataset), None)):
        raise ActionFailed(404, f"No dataset '{args.dataset}' in project '{args.project}'.")
    return {"id": d["id"], "name": d["name"], **semantic.query(args.project, d["query"], user.id, timeout=semantic.TIMEOUT)}


@action(Role.editor, destructive=True, definition=True)
def delete_dataset(conn, user: User, args: ObjectRef) -> dict:
    """Deletes a dataset. Refused while an agent's knowledge scope or an ML model uses it."""
    def drop(ds):
        if all(x["id"] != args.id for x in ds):
            raise ActionFailed(404, f"No dataset '{args.id}' in project '{args.project}'.")
        return [x for x in ds if x["id"] != args.id]
    try:
        return _datasets(conn, user, args.project, drop)
    except ActionFailed as e:  # the import check words a use as a dangling reference; say it as a use
        if used := [f"{x['file']} {x['path']}" for x in e.body.get("errors", []) if x["error"] == f"Unknown dataset '{args.id}'."]:
            raise ActionFailed(422, f"Dataset '{args.id}' is still used by {', '.join(used)}; remove it there first.", uses=used) from None
        raise
