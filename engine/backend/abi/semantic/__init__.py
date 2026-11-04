"""The semantic engine: model objects, MEL, planner, router, cache, query API. PRD §6.5 SE-*, §3 readiness; IN-5 (detect.py).

Principle 4: nothing reads data except through this package, and every read of table data runs through
`execute` (SE-5 DuckDB router, SE-7 cache keyed by SQL + snapshot ID, OB-1 query log, WB-4 hook). Consumers
ask in the semantic query format (`SemanticQuery`, action `semantic_query`); the grid (UI-2) and the step
preview (TR-3) are the two row-level reads. Detection (detect.py) reads catalog metadata from a cursor and table
data through `execute`; the audit of what else touches lake data directly is work/validation/GATE-5-fix-2.md.
"""

import contextlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Iterator
from decimal import Decimal
from typing import Annotated, Literal

import duckdb
import pyarrow as pa
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError
from sqlglot import exp

from abi import actions, db, ops, storage, transform, writeback
from abi.actions import ActionFailed, action, audit, current_user
from abi.project import ID, git
from abi.security import Role, User, role_in, usage
from abi.semantic import detect, mel, planner, rls
from abi.semantic import live as _live
from abi.storage import COLUMN, Value

log = logging.getLogger(__name__)
PREVIEW = 1000  # TR-3
CACHE_BYTES = int(os.environ.get("ABI_QUERY_CACHE_MB", "256")) * 2**20  # SE-7, D-17
# ponytail: the cap is per lake connection (project), so N busy projects hold up to N x; one global budget if that bites
_caches: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> {(sql, snapshot): (rows, bytes)}
_cache_lock = threading.Lock()


_eff: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> (snapshot, tables with edits)
_eff_lock = threading.Lock()


def _edited(con, snap: int) -> frozenset[str]:
    """WB-4: the tables with active edits. Once per snapshot it rebuilds memory.effective (the lake connection's
    in-memory database): one view per such table, its effective table (writeback.effective_sql)."""
    with _eff_lock:
        if (built := _eff.get(con)) and built[0] >= snap:
            return built[1]
        cur = con.cursor()
        cur.begin()
        try:  # one transaction: readers see the old views or the new ones, never none
            now = cur.execute("SELECT id FROM lake.current_snapshot()").fetchone()[0]
            views = writeback.views(cur)
            cur.execute("DROP SCHEMA IF EXISTS memory.effective CASCADE")
            cur.execute("CREATE SCHEMA memory.effective")
            for t, sql in views.items():
                cur.execute(f"CREATE VIEW memory.effective.{storage._q(t)} AS {sql}")
            cur.commit()
        except BaseException:
            cur.rollback()
            raise
        _eff[con] = (now, frozenset(views))
        return frozenset(views)


def _effective(cur, con, snap: int, rls_schema: str | None = None) -> None:
    """WB-4 hook: unqualified table names are the tables every query reads: the effective table (model.<t> + the
    latest active edits) of an edited table, else lake.model.<t>; for a restricted user (SE-6) first its RLS views."""
    _edited(con, snap)
    cur.execute(f"SET search_path = '{f'memory.{rls_schema},' if rls_schema else ''}memory.effective,lake.model'")


COPY_DISTINCT = 10_000  # a dimension column with more distinct values (names, keys) stays out of query copies


def _columns_of(cur, schema: str) -> dict[str, dict[str, str]]:
    """{table: {column: DuckDB type}} in column order."""
    cols: dict[str, dict[str, str]] = {}
    for t, c, ty in detect._columns(cur, schema):
        cols.setdefault(t, {})[c] = ty
    return cols


def locale(project: str) -> dict[str, str | None]:
    """SE-13 (E-11.md decision 1): the effective {time_zone, currency}: project.json settings, else the project's
    tenant's, else UTC and no currency. A zone that isn't one (a pulled project.json) answers 400 naming the setting."""
    s = git.settings(project) if (git.path(project) / "project.json").is_file() else {}
    if "time_zone" not in s or "currency" not in s:
        with db.connect() as conn:
            t = conn.execute("SELECT t.time_zone, t.currency FROM project_tenants p JOIN tenants t ON t.id = p.tenant"
                             " WHERE p.project = %s", [project]).fetchone() or {}
        s = {k: v for k, v in t.items() if v} | s
    if s.get("time_zone", "UTC") not in git.zones():
        raise ActionFailed(400, f"project.json settings.time_zone '{s['time_zone']}' is not an IANA time zone; set one"
                                " with set_project_locale (e.g. Europe/Berlin).")
    return {"time_zone": s.get("time_zone", "UTC"), "currency": s.get("currency")}


def _copies(project: str, m: dict) -> tuple[dict[str, str], dict[str, dict[str, str]], dict, tuple]:
    """({fact: name} of the query copies (S-09-fix-3) the current model and data call for, model columns, the
    planner's `days` (SE-13 local days: the zone is in the copy's SQL, so in its name), the planner's `rates`). The name
    hashes the copy's definition and the DuckLake table IDs of the fact and its dimensions; a refresh or restore
    replaces a table (new ID) and a model change changes the definition, so an outdated copy is never read."""
    con = storage.lake(project)
    cur, edited = con.cursor(), _edited(con, storage.snapshot_id(con))
    cols = _columns_of(cur, "model")
    loc, types = locale(project), cols | _live.columns(project)
    days = planner.local(m, types, loc["time_zone"])
    meta = f"{storage._META}.{storage._q(project)}"
    ids = dict(cur.execute(f"""SELECT t.table_name, t.table_id FROM {meta}.ducklake_table t JOIN {meta}.ducklake_schema s
                               USING (schema_id) WHERE t.end_snapshot IS NULL AND s.end_snapshot IS NULL
                               AND s.schema_name = 'model'""").fetchall())
    out = {}
    for fact in dict.fromkeys(r["from"]["table"] for r in m.get("relationships", [])):
        used = sorted({fact} | {r["to"]["table"] for r in m["relationships"] if r["from"]["table"] == fact})
        # ponytail: a fact whose data or dimensions have active edits reads through joins (W-01, D-16); build its
        # copy from the effective tables with the edit state in the stamp if edited big facts get slow
        if not edited & set(used) and (sql := planner.copy_sql(fact, m, cols, days)):
            stamp = actions.sha(json.dumps([sql, COPY_DISTINCT, [ids.get(t) for t in used]]))
            out[fact] = f"{fact}__{stamp[:16]}"
    return out, cols, days, (loc["currency"], planner.zoned(types, loc["time_zone"]))


def _pairs(copies: dict[str, str], ms: list[dict], dt: dict) -> dict[str, dict[str, str]]:
    """{fact: {column: name}} of the distinct-pair tables (S-09-fix-5) for each DISTINCT_COUNT column of a fact with a
    query copy. The name extends the copy's stamp (D-16) with what only the pair table reads (the column, the date
    table), so an outdated one is never read."""
    try:
        trees = mel.compile({x["id"]: x["expression"] for x in ms})
    except mel.MelError:  # broken measures: queries fail anyway
        return {}
    out: dict[str, dict[str, str]] = {}
    for n in (n for t in trees.values() for n in mel._walk(t)):
        if type(n) is mel.Agg and n.fn == "DISTINCT_COUNT" and n.col.table in copies:
            tag = actions.sha(json.dumps([n.col.column, dt.get("table")]))[:16]
            out.setdefault(n.col.table, {})[n.col.column] = f"{copies[n.col.table]}__dc_{tag}"
    return out


def build_copies(project: str) -> None:
    """Write the query copies the model calls for that don't exist yet, and drop outdated ones (worker, every minute).
    A copy holds the fact's columns plus its dimensions' low-cardinality columns (<= COPY_DISTINCT values); a query
    that uses another dimension column, or runs before its copy is written, joins the dimensions (same rows, slower)."""
    from abi.semantic import model  # not at top: model imports this package

    con, m = storage.lake(project), model._read(project, "model.json")
    names, cols, days, _ = _copies(project, m)
    pairs = _pairs(names, model._read(project, "measures.json").get("measures", []), m.get("date_table") or {})
    have = _columns_of(con.cursor(), "query")
    drop = set(have) - set(names.values()) - {p for ps in pairs.values() for p in ps.values()}
    write, copied = {}, {n: have[n] for n in names.values() if n in have}  # copy -> its columns
    for fact, name in names.items():
        if name in have:
            continue
        keep = {fact: cols[fact]}
        for d in {r["to"]["table"] for r in m["relationships"] if r["from"]["table"] == fact} & set(cols) - {fact}:
            [n] = execute(project, f"SELECT {', '.join(f'approx_count_distinct({storage._q(c)})' for c in cols[d])}"
                                   f" FROM lake.model.{storage._q(d)}", {"query_copy": d})
            keep[d] = [c for c, k in zip(cols[d], n) if k <= COPY_DISTINCT]
        if sql := planner.copy_sql(fact, m, {t: c for t, c in keep.items() if c}, days):
            write[name] = sql
            cur = con.cursor()
            cur.execute("USE lake.model")
            copied[name] = [r[0] for r in cur.execute(f"DESCRIBE {sql}").fetchall()]
    for fact, ps in pairs.items():  # after the copies: each reads its fact's copy
        for c, p in ps.items():
            if p not in have and c in copied.get(names[fact], ()) and (
                    sql := planner.pairs_sql(names[fact], c, copied[names[fact]], m.get("date_table") or {})):
                write[p] = sql

    def run(cur):
        cur.execute("CREATE SCHEMA IF NOT EXISTS lake.query")
        cur.execute("USE lake.model")
        for name in drop:
            cur.execute(f"DROP TABLE lake.query.{storage._q(name)}")
        for name, sql in write.items():
            cur.execute(f"CREATE TABLE lake.query.{storage._q(name)} AS {sql}")

    if drop or write:  # no empty snapshot: that would end every cached result (SE-7)
        # ponytail: rewrites a whole fact on any change to it or a dimension; incremental copies if refreshes get slow
        storage._commit(con, run, "worker", "build query copies")


def _bytes(sql: str, rows: list[tuple]) -> int:
    """Approximate bytes of a cached result: the SQL, the list, and the rows scaled from a sample of up to ~100.
    ponytail: sampled and shallow (a LIST/STRUCT cell counts its container only), so skewed or nested results are
    undercounted; measure every cell recursively if such results show up in the cache."""
    sample = rows[::max(1, len(rows) // 100)]
    per_row = sum(sys.getsizeof(r) + sum(map(sys.getsizeof, r)) for r in sample) / len(sample) if sample else 0
    return sys.getsizeof(sql) + sys.getsizeof(rows) + int(per_row * len(rows))


def _rls(project: str, user_id: int | None, con, snap: int, sql: str | None,
         reads: dict | None = None) -> tuple[str | None, dict]:
    """SE-6 for every read (`execute`, `export`): (a restricted user's RLS schema for `_effective`, {live table:
    its ClickHouse filter}) once DuckDB `sql` (None: ClickHouse SQL only) passes `rls.check`; (None, {}) when the
    user reads unfiltered."""
    if not rls.restricted(project, user_id):
        return None, {}
    cols = _columns_of(con.cursor(), "model")
    ctx = rls.context(project, user_id, con, snap, _edited(con, snap), cols,
                      lambda q, r: execute(project, q, {"rls": "keys"}, reads=r or None))
    if sql is not None:
        rls.check(project, user_id, sql, {*cols, *(reads or ())})
    return ctx


def execute(project: str, sql: str, asked: dict, user_id: int | None = None, live: str | None = None,
            over: str = 'SELECT * FROM "__live"', reads: dict | None = None, timeout: float | None = None) -> list[tuple]:
    """Run one read on the project's lake. SE-5: imported tables in DuckDB; with `live` (a live table), `sql` is
    ClickHouse SQL on that table's source and `over` DuckDB SQL over its result as table "__live"; with `reads`
    ({name: (live table, ClickHouse SQL)}, from `plan`), DuckDB `sql` reads each ClickHouse result as table `name`
    (live.run). Live reads are never cached and are logged with their ClickHouse SQL.
    SE-7: results are cached by (SQL, snapshot ID); the SQL is the planned query, so a changed measure or model
    is a new key. OB-1: every call is logged: what was asked, the SQL, snapshot, cache hit, rows, time, error.
    `timeout` (s): a lake read still running then is interrupted (duckdb.InterruptException, SE-10)."""
    con, start, rows, error = storage.lake(project), time.perf_counter(), None, None
    snap = storage.snapshot_id(con)  # read before the query, so a cached result is never older than its key
    with _cache_lock:  # per lake connection: a recreated lake restarts its snapshot IDs
        cache = _caches.setdefault(con, {})
    rows, cached, ctx, scanned = None, False, None, 0
    try:
        ctx, filters = _rls(project, user_id, con, snap, None if live else sql, reads)  # SE-6
        # E-02.6: each live read gets its own table's filter in the ClickHouse SQL itself, logged as run
        add = lambda t, ch: _live.filtered(ch, _live.table_sql(project, t), *filters[t]) if t in filters else ch
        sql = add(live, sql) if live else sql
        reads = reads and {n: (t, add(t, ch)) for n, (t, ch) in reads.items()}
        # SE-7: keyed by the RLS views too (their name hashes every filter), so users never share a result
        if hit := None if live or reads else cache.get((sql, snap, ctx)):  # live data has no snapshot
            rows, cached = hit[0], True
        if live:
            rows = _live.run(project, {"__live": (live, sql)}, over)
        elif reads:
            def prepare(cur):
                _effective(cur, con, snap, ctx)
                _profile(cur)
            rows, scanned = _live.run(project, reads, sql, prepare, _scanned)  # SC-11: its lake rows
        elif not cached:
            cur = con.cursor()
            _effective(cur, con, snap, ctx)
            _profile(cur)
            with limited(cur, timeout):
                rows = cur.execute(sql).fetchall()
            scanned = _scanned(cur)
            if (size := _bytes(sql, rows)) <= CACHE_BYTES:  # a result above the cap is never cached
                with _cache_lock:
                    cache[sql, snap, ctx] = rows, size
                    total = sum(s for _, s in cache.values())
                    while total > CACHE_BYTES:  # old snapshots first (never hit again), then oldest entry (FIFO)
                        total -= cache.pop(min(cache, key=lambda k: k[1]))[1]
        return rows
    except (duckdb.Error, ActionFailed) as e:
        error = str(e)
        raise
    finally:
        logged = sql + "".join(f"\n-- {n} = ClickHouse ({t}): {ch}" for n, (t, ch) in (reads or {}).items())
        if actions.inbound():  # D-32: a filter value may be the message text; the cache key keeps the real SQL
            logged = actions.redacted(sql, bool(live)) + "".join(
                f"\n-- {n} = ClickHouse ({t}): {actions.redacted(ch, True)}" for n, (t, ch) in (reads or {}).items())
            asked, error = actions.hashed(asked), error and f"<sha256:{actions.sha(error)}>"
        _log(user_id, project, asked, logged, snap, cached, None if rows is None else len(rows), start, error,
             None if rows is None or error else scanned)


def _log(user_id, project, asked, sql, snap, cached, rows, start, error, scanned: int | None = None) -> None:
    """OB-1: one query_log row. SC-11: a successful read (`scanned` given) also writes one usage row."""
    ops.QUERY.observe(time.perf_counter() - start, str(cached).lower(), "error" if error else "ok")  # H-04
    with db.connect() as conn:
        conn.execute("INSERT INTO query_log (user_id, project, query, sql, snapshot, cached, rows, ms, error)"
                     " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     [user_id, project, Jsonb(asked), sql, snap, cached, rows, (time.perf_counter() - start) * 1000, error])
        if scanned is not None:
            usage.record(conn, "query", project, user_id, rows_scanned=scanned)


@contextlib.contextmanager
def limited(cur, seconds: float | None):
    """SE-10: interrupt `cur`'s query once it has run `seconds` (DuckDB has no statement timeout); other cursors on
    the same database keep running. None: no limit."""
    timer = threading.Timer(seconds or 0, cur.interrupt)
    if seconds:
        timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _profile(cur) -> None:
    """SC-11: DuckDB profiles this cursor's next query (per cursor: other cursors are untouched)."""
    cur.execute("SET enable_profiling = 'no_output'")
    cur.execute("""SET custom_profiling_settings = '{"OPERATOR_CARDINALITY": "true", "EXTRA_INFO": "true"}'""")


def _scanned(cur) -> int | None:
    """Rows the cursor's last query read from storage: the rows its table scans produced (after zone-map pruning and
    pushed-down filters); 0 when DuckLake answered from its statistics. Not CUMULATIVE_ROWS_SCANNED: on DuckDB 1.5.6
    a DuckLake/Parquet scan reports its file's row count once per thread (work/validation/E-07.1.md). A ClickHouse
    result (live.run's read_parquet views) is not a lake read: its rows were scanned on the client's server."""
    def scans(n: dict) -> int:
        own = n.get("operator_cardinality", 0) if n.get("operator_type") == "TABLE_SCAN" and n.get(
            "extra_info", {}).get("Function") != "READ_PARQUET" else 0
        return own + sum(scans(c) for c in n.get("children", []))
    try:
        return scans(json.loads(cur.get_profiling_information(format="json")))
    except Exception as e:  # noqa: BLE001 - metering never fails the read or loses its query_log row (OB-1)
        log.error("SC-11: rows scanned not read: %s", type(e).__name__)
        return None


class Filter(BaseModel):
    column: str = Field(pattern=COLUMN, description="table.column")
    op: Literal["=", "<>", "<", "<=", ">", ">=", "in"]
    value: Value | list[Value | None] = Field(description="one value; a list for `in`")


class Order(BaseModel):
    field: str = Field(description="a dimension, measure ID or `grouping_id` of this query")
    desc: bool = False


class SemanticQuery(BaseModel):
    """The semantic query format (glossary "Semantic query"; JSON Schema in schemas/v1/datasets.json).
    Time is the date table: group by calendar.year/.quarter/.month/.week (ISO, with .week_year)/.day and filter on them; YTD and the other
    time functions are measures. Result columns: dimensions, `grouping_id` (with totals), then measure IDs.
    Row order: `order_by`, then grouping_id (detail rows before totals), then the dimensions; NULLs last."""
    measures: list[str] = Field(min_length=1, description="measure IDs (model/measures.json), never display names")
    dimensions: list[Annotated[str, Field(pattern=COLUMN)]] = Field([], description="table.column to group by")
    filters: list[Filter] = Field([], description="row filters; all must hold")
    totals: list[list[str]] = Field([], description="extra grouping sets (subsets of the dimensions); [] = grand total")
    order_by: list[Order] = []
    limit: int | None = Field(None, ge=1, le=100_000)


EXPRESSIONS = 10  # M2.1: inline expressions per query
CONTEXTS = 4  # M2 review: the distinct periods and IGNOREs a query's expressions step through (each a scan and a join)
TIMEOUT = 60  # s: a semantic_query or explain_change read still running then is stopped (sql_query's SE-10 limit)


def _steps(n, path: tuple = ()) -> set:
    """M2 review: the time functions and IGNOREs above each [measure] of an expression, outermost first. Each distinct
    path is one more aggregate context for the planner, whose plan grows faster than their number (ten expressions of 32
    ROLLING windows each planned for 452 s, before DuckDB ran)."""
    t = type(n)
    if t is mel.Ref:
        return {path} if path else set()
    step = (n.fn, n.n, n.unit) if t is mel.Time else n.cols if t is mel.Ignore else None
    kids = n.args if t is mel.Op else (n.expr,) if t in (mel.Where, mel.Ignore, mel.Time) else ()
    return set().union(*(_steps(k, path + (step,) if step else path) for k in kids))


def _inline(expressions: list[dict], ms: list[dict], asked: list[str], strict: bool = True) -> None:
    """M2.1: 400 naming the first refused one of a semantic query's inline expressions (mel.inline; not `strict`: ABI's
    own, explain_change's period WHEREs, only parsed). A name is a new result column: never a measure ID, a measure the
    query asks for or `grouping_id`. M2 review: together they step through at most CONTEXTS periods or IGNOREs."""
    if len(expressions) > EXPRESSIONS:
        raise ActionFailed(400, f"A query takes at most {EXPRESSIONS} expressions; split it into several queries.")
    ids = {m["id"] for m in ms}
    taken, steps = {*ids, *asked, "grouping_id"}, set()
    for e in expressions:
        try:
            if e["name"] in taken:
                raise mel._Bad(f"'{e['name']}' already names a measure or a column of this query.",
                               "Ask for that measure, or give the expression a name of its own.")
            steps |= _steps(mel.inline(e["expr"], ids) if strict else mel.parse(e["expr"]))
        except mel._Bad as x:
            raise ActionFailed(400, f"Expression '{e['name']}': {x} {x.fix}") from None
        taken.add(e["name"])
    if len(steps) > CONTEXTS:
        raise ActionFailed(400, f"These expressions step through {len(steps)} different periods or IGNOREs; a query takes"
                                f" at most {CONTEXTS} (each is one more read of the data): split it into several queries.")


def uses(expressions: list) -> tuple[list[str], list[str]]:
    """M2.1: (measure IDs, table.column columns) that a query's inline expressions name, for scope checks (a named
    model, a custom agent's scope, D-24); one that doesn't parse names nothing here, as planning refuses it."""
    nodes = []
    for e in expressions:
        if isinstance(e, dict) and isinstance(e.get("expr"), str):
            with contextlib.suppress(mel._Bad):
                nodes += mel._walk(mel.parse(e["expr"]))
    return [n.name for n in nodes if type(n) is mel.Ref], [f"{n.table}.{n.column}" for n in nodes if type(n) is mel.Col]


def plan(project: str, q: dict, user_id: int | None = None,
         expressions: list[dict] = (), strict: bool = True) -> tuple[dict, str, list[str], list[dict], dict]:
    """Plan a semantic query without running it (S-02 plus order and limit): (validated query, DuckDB SQL, result
    columns, every measure of the project, ClickHouse reads {DuckDB table name: (live table, ClickHouse SQL)}).
    RP-5 shows this SQL. Raises ActionFailed(400) with the planner's reason. A user restricted by row-level security
    (SE-6) gets no query copy or pair table: they're built unfiltered, so its plan reads the (filtered) tables.
    M2.1 `expressions` ([{name, expr}], semantic_query's): planned as more measures of this query only, after its
    measures, through the same row and column security; never saved (rule 7). `strict` as in `_inline`."""
    from abi.semantic import model  # not at top: model imports this package

    ms = model._read(project, "measures.json").get("measures", [])
    _inline(expressions, ms, q.get("measures") or [], strict)
    exprs = {e["name"]: e["expr"] for e in expressions}
    q = SemanticQuery.model_validate({**q, "measures": [*q["measures"], *exprs]} if exprs else q).model_dump(mode="json")
    measures = {m["id"]: m["expression"] for m in ms} | exprs
    mj = model._read(project, "model.json")
    # ponytail: ~7 ms of catalog reads per plan (S-09-fix-3); cache them by snapshot ID + model.json if that shows
    have = _columns_of(storage.lake(project).cursor(), "query")
    names, _, days, rates = _copies(project, mj)
    pairs = _pairs(names, ms, mj.get("date_table") or {})
    # ponytail: no copies at all for a restricted user, not only for facts a rule touches; per-fact if Viewers are slow
    copies = {} if rls.restricted(project, user_id) else {
        fact: (name, set(have[name]), {c: p for c, p in pairs.get(fact, {}).items() if p in have})
        for fact, name in names.items() if name in have}
    reads: dict = {}
    joins: list = []
    try:
        types = _live.columns(project)
        live = {t: (_live.table_sql(project, t), types.get(t, {})) for t in _live._sources(project)}
        sql = planner.plan(q, measures, mj, copies, live, reads, days, rates, joins)
    except (planner.PlanError, mel.MelError) as e:
        raise ActionFailed(400, str(e))
    if hid := rls.hidden(project, user_id):  # D-110: refused, never computed over a column the user can't see
        trees = mel.compile(measures)  # compiles: plan() just did
        # COL-SEC.2: and every relationship key the plan joins on (the planner lists them)
        used = {*q["dimensions"], *(f["column"] for f in q["filters"])} | {
            f"{n.table}.{n.column}" for m in q["measures"] for n in mel._walk(trees[m]) if type(n) is mel.Col} | {
            f"{r[s]['table']}.{c}" for r in joins for s in ("from", "to") for c in r[s]["columns"]}
        if bad := sorted(c for c in used if c.split(".", 1)[1].lower() in hid.get(c.split(".", 1)[0], ())):
            raise ActionFailed(403, f"Column-level security: {', '.join(bad)} is hidden from you in project '{project}',"
                                    " so this query can't use it; leave it out, or ask a project Owner.")
    cols = q["dimensions"] + (["grouping_id"] if q["totals"] else []) + q["measures"]
    if bad := [o["field"] for o in q["order_by"] if o["field"] not in cols]:
        raise ActionFailed(400, f"Can't order by {', '.join(bad)}: use one of {', '.join(cols)}.")
    order = [(o["field"], o["desc"]) for o in q["order_by"]] + [
        (c, False) for c in (["grouping_id"] if q["totals"] else []) + q["dimensions"]]
    if order:
        sql = f"SELECT * FROM ({sql}) ORDER BY " + ", ".join(
            f"{exp.to_identifier(c, quoted=True).sql('duckdb')} {'DESC' if d else 'ASC'} NULLS LAST" for c, d in order)
    if q["limit"]:
        sql += f" LIMIT {int(q['limit'])}"
    if exprs:
        q["expressions"] = list(expressions)  # what was asked (OB-1 query_log)
    return q, sql, cols, ms, reads


def query(project: str, q: dict, user_id: int | None = None, expressions: list[dict] = (), strict: bool = True,
          timeout: float | None = None) -> dict:
    """Run a semantic query: plan, execute. Raises ActionFailed(400) with the planner's or DuckDB's reason, or when it
    runs past `timeout` seconds. `expressions`, `strict` as in `plan`: the result's `measures` names each by its name,
    `expressions` gives its MEL (M2 review: an answer's figures and names never ground from it: a literal is the
    model's, not the data's). `percent`: the asked measures whose MEL multiplies by 100 (percentage points, so the
    grounding checks "23.4%" against 23.4 as for a % column; M2 review)."""
    q, sql, cols, ms, reads = plan(project, q, user_id, expressions, strict)
    try:
        rows = execute(project, sql, q, user_id, reads=reads, timeout=timeout)
    except duckdb.Error as e:
        if isinstance(e, duckdb.InterruptException) and timeout:
            raise ActionFailed(400, f"The query ran longer than the {timeout:g} s limit and was stopped; ask for less"
                                    " (fewer dimensions or a shorter period).") from None
        raise ActionFailed(400, f"The query failed: {e}")
    names = {m["id"]: m["name"] for m in ms} | {e["name"]: e["name"] for e in expressions}
    rows = [tuple(map(tidy, r)) for r in rows]
    points = [m["id"] for m in ms if m["id"] in q["measures"] and re.search(r"\*\s*100\b|\b100\s*\*", m.get("expression") or "")]
    return {"columns": cols, "measures": {m: names[m] for m in q["measures"]}, "rows": rows,
            **({"expressions": {e["name"]: e["expr"] for e in expressions}} if expressions else {}),
            **({"percent": points} if points else {}),
            "filters": q["filters"],  # AG-2: the members it filtered on ground the answer's names (LLM-2-fix-2)
            "live": sorted({t for t, _ in reads.values()}),  # RP-7: the live tables these numbers read
            **locale(project)}  # SE-13: the time zone and display currency these numbers are in


class Expression(BaseModel):
    name: str = Field(pattern=ID, description="the column it adds (not a measure ID)")
    expr: str = Field(max_length=1000, description="measures compared, e.g. [m] - PREVIOUS_PERIOD([m])")


class QueryInput(SemanticQuery):
    project: str = Field(pattern=storage.PROJECT)
    model: str | None = Field(None, description="a named model of the project (semantic_model lists them): the query may use only its tables and measures")
    expressions: list[Expression] = Field([], description="comparisons of measures for this query only, never saved; each is a result column after the measures")


@action(Role.viewer)
def semantic_query(conn, user: User, args: QueryInput) -> dict:
    """Answers a data question through the semantic model: measure IDs, grouped by table.column dimensions, with filters, optional totals, order and limit. Returns the columns (dimensions, then measure IDs), each measure's display name and the rows. Every number you state must come from these rows. `expressions` compare measures in this query only (never saved), for what no measure (e.g. a *_change) gives: each {name, expr} is one more column, which order_by takes; expr is a measure [id], under PREVIOUS_PERIOD, PREVIOUS_YEAR, YTD, QTD, MTD, ROLLING or IGNORE or not, two of those subtracted, or DIVIDE of those, with no number, WHERE or other arithmetic. A month against the one before: group by calendar.year, calendar.month and the dimension to break down, filter that month, and ask {name: "change", expr: "[m] - PREVIOUS_PERIOD([m])"} and {name: "change_pct", expr: "DIVIDE([m] - PREVIOUS_PERIOD([m]), ABS(PREVIOUS_PERIOD([m])))"} (a fraction: -0.25 is -25%). A member with no rows in one of the two periods (a café that closed or opened) has an empty change, and a period still filling is compared in part with the whole one before: rank members by their change with explain_change. DIVIDE([m], IGNORE([m], table.column)) is each member's share of the total."""
    q = args.model_dump(mode="json", exclude={"project", "model", "expressions"})
    exprs = [e.model_dump() for e in args.expressions]
    if args.model:
        from abi.semantic import model  # not at top: model imports this package

        ms, cols = uses(exprs)  # what the expressions read counts too
        model.in_scope(args.project, args.model, [*dict.fromkeys(q["measures"] + ms)],
                       [*dict.fromkeys(q["dimensions"] + [f["column"] for f in q["filters"]] + cols)])
    return query(args.project, q, user.id, exprs, timeout=TIMEOUT)


EXPORT_BATCH = 65_536  # rows per Arrow batch (D-17); DuckDB reserves a whole batch per column (duckdb#26270)


class ExportQuery(SemanticQuery):
    limit: int | None = Field(None, ge=1, description="no cap: bulk export")


def export(project: str, q: dict, fmt: Literal["arrow", "parquet"], user_id: int | None = None) -> Iterator[bytes]:
    """SE-9 bulk export of a semantic query: an Arrow IPC stream (one chunk per batch) or a Parquet file (written by
    DuckDB COPY to a temp file, streamed, then deleted). Same lake connection and WB-4 views as `execute`; never cached
    (SE-7); one query_log row when the stream ends (OB-1). Planning and query errors raise before the first chunk."""
    try:
        asked = ExportQuery.model_validate(q).model_dump(mode="json")
    except ValidationError as e:
        raise ActionFailed(422, "Invalid input.", errors=actions.invalid(e))
    _, sql, _, _, reads = plan(project, asked | {"limit": None}, user_id)
    if reads:  # ponytail: live tables aren't exportable; page them through live.run in batches if it's asked for
        live = ", ".join(sorted({t for t, _ in reads.values()}))
        raise ActionFailed(400, f"Export can't read live (ClickHouse) tables: {live}. Export a query on imported tables.")
    if asked["limit"]:
        sql += f" LIMIT {int(asked['limit'])}"
    con, start = storage.lake(project), time.perf_counter()
    snap = storage.snapshot_id(con)
    try:  # SE-6: the same RLS views and check as `execute`
        ctx, _ = _rls(project, user_id, con, snap, sql)
    except ActionFailed as e:
        _log(user_id, project, asked, sql, snap, False, None, start, str(e))
        raise
    cur, tmp = con.cursor(), tempfile.mkdtemp(prefix="abi-export-")
    path = os.path.join(tmp, "export.parquet")
    try:
        _effective(cur, con, snap, ctx)
        _profile(cur)
        if fmt == "arrow":
            reader = cur.execute(sql).to_arrow_reader(EXPORT_BATCH)
        else:
            [(count,)] = cur.execute(f"COPY ({sql}) TO '{storage._lit(path)}' (FORMAT parquet)").fetchall()
    except duckdb.Error as e:
        cur.close()
        shutil.rmtree(tmp)
        _log(user_id, project, asked, sql, snap, False, None, start, str(e))
        raise ActionFailed(400, f"The query failed: {e}")

    end = {"rows": None, "error": "The client went away before the export started."}

    def stream():
        try:
            if fmt == "parquet":
                with open(path, "rb") as f:
                    while chunk := f.read(2**20):
                        yield chunk
                rows = count
            else:
                rows, out = 0, io.BytesIO()
                with pa.ipc.new_stream(out, reader.schema) as w:
                    for batch in reader:
                        w.write_batch(batch)
                        rows += batch.num_rows
                        yield out.getvalue()
                        out.seek(0)
                        out.truncate()
                yield out.getvalue()  # end-of-stream marker
            end.update(rows=rows, error=None)
        except BaseException as e:  # GeneratorExit: the client went away
            end["error"] = str(e) or type(e).__name__
            raise
        finally:
            done()

    def finish():
        scanned = None if end["error"] else _scanned(cur)
        cur.close()
        shutil.rmtree(tmp, ignore_errors=True)
        _log(user_id, project, asked, sql, snap, False, end["rows"], start, end["error"], scanned)
        if not end["error"]:  # SC-11 decision 7: a finished export is one exported file
            usage.record(None, "export", project, user_id)

    g = stream()
    done = weakref.finalize(g, finish)  # also when the response never starts iterating (the client left early)
    return g


def tidy(v):
    """Principle 7, LLM-2-fix-8: a DOUBLE to 15 significant digits, so binary noise (0.1 * 3 = 0.30000000000000004)
    never reaches a person or a model; any other value (DECIMAL, int, text) as it is."""
    return float(f"{v:.15g}") if type(v) is float else v


def norm(v):
    """Numbers to 6 decimals, dates as ISO text, so JSON expectations compare with DuckDB values (measure and tool tests)."""
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return round(float(v), 6)
    return v if v is None or isinstance(v, (bool, str)) else str(v)


def measure_tests(project: str, measures: list[dict]) -> list[str]:
    """§3 readiness "passes its tests" (V-02 MS-1): run each measure's tests; one gap per failing test."""
    rows = lambda rs: sorted(([norm(v) for v in r] for r in rs), key=repr)
    gaps = []
    secret = bool(rls.rules(project))
    for m in measures:
        for i, t in enumerate(m.get("tests", []), 1):
            q = {"measures": [m["id"]], "dimensions": t.get("dimensions", []), "filters": t.get("filters", [])}
            try:
                got = rows(query(project, q)["rows"])
            except ActionFailed as e:
                gaps.append(f"Measure '{m['id']}' test {i} can't run: {e}")
                continue
            if got != (want := rows(t["rows"])):
                # SE-6: readiness (Viewers) and the feed show these gaps, and the test reads unfiltered rows
                gaps.append(f"Measure '{m['id']}' fails test {i}: " + (
                    "the values are hidden by row-level security; run its query to see them." if secret else
                    f"expected {json.dumps(want)}, got {json.dumps(got)}."))
    return gaps


def table_rows(project: str, table: str, offset: int, limit: int, user_id: int | None = None) -> dict:
    """A page of the table (UI-2 grid), every value as text (display only), plus column types and row count."""
    t = storage._q(table)  # unqualified: the table queries read (WB-4 hook)
    asked = {"table": table, "offset": offset, "limit": limit}
    if _live.is_live(project, table):  # IN-3: paged in ClickHouse, ordered so pages don't overlap
        ch, hid = _live.table_sql(project, table), rls.hidden(project, user_id).get(table, ())  # D-110: as its RLS read
        cols = {c: dt for c, dt in _live.columns(project).get(table, {}).items() if c.lower() not in hid}
        total = execute(project, f"SELECT count() FROM {ch}", asked, user_id, live=table)[0][0]
        name = exp.convert(table).sql("clickhouse")
        [(key,)] = execute(project, "SELECT sorting_key FROM system.tables WHERE database = currentDatabase()"
                                    f" AND name = {name}", asked, user_id, live=table) or [("",)]
        # ponytail: ORDER BY + OFFSET reads every earlier row; key-based paging if deep pages on big tables get slow
        order = ", ".join([key] * bool(key and not hid)  # a sorting key may name a hidden column
                          + [exp.to_identifier(c, quoted=True).sql("clickhouse") for c in cols])
        rows = execute(project, f"SELECT * FROM {ch} ORDER BY {order} LIMIT {int(limit)} OFFSET {int(offset)}", asked,
                       user_id, live=table, over='SELECT COLUMNS(*)::VARCHAR FROM "__live"')
        return {"columns": [{"name": c, "type": dt} for c, dt in cols.items()], "total": total, "rows": rows}
    cols = execute(project, f"DESCRIBE {t}", asked, user_id)
    total = execute(project, f"SELECT count(*) FROM {t}", asked, user_id)[0][0]
    # ponytail: OFFSET scan relies on insertion order, which storage._commit keeps; key-based paging if deep pages get slow
    rows = execute(project, f"SELECT COLUMNS(*)::VARCHAR FROM {t} LIMIT {int(limit)} OFFSET {int(offset)}", asked, user_id)
    return {"columns": [{"name": c[0], "type": c[1]} for c in cols], "total": total, "rows": rows}


def columns(project: str, table: str) -> list[dict]:
    """A model table's columns and types, in order (R-2: set_steps records them in tables/<t>.json)."""
    return [{"name": c, "type": dt} for _, c, dt in detect._columns(storage.lake(project).cursor(), "model", table)]


class Preview(storage.ProjectRef):
    steps: list[transform.Step] = Field(min_length=1)
    step: int | None = Field(None, ge=0, description="0-based index of the step to preview after; default: the last")


@action(Role.editor)  # steps hold SQL expressions from the user (M-01 trust boundary): Editor, like set_steps
def preview_steps(conn, user: User, args: Preview) -> dict:
    """Previews unsaved transform steps: column types and the first 1,000 rows (as text) after the chosen step, read from the source's loaded data."""
    steps = args.steps[:None if args.step is None else args.step + 1]
    if steps[0].type != "source":
        raise ActionFailed(400, "A table's steps start with exactly one `source` step.")
    source = steps[0].source
    if not conn.execute("SELECT 1 FROM sources WHERE project = %s AND table_name = %s", [args.project, source]).fetchone():
        raise ActionFailed(404, f"No loaded source '{source}' in project '{args.project}'.")
    asked = {"preview": args.model_dump(mode="json", exclude={"project"})}
    try:  # raw.<source> holds the source's files, as raw.<table> will after set_steps; same IN-5 typing as the refresh
        q = transform.compile(source, steps, source_sql=detect.typed_sql(
            storage.lake(args.project).cursor(), source, lambda sql: execute(args.project, sql, asked, user.id)[0]))
        cols = execute(args.project, f"DESCRIBE {q}", asked, user.id)
        rows = execute(args.project, f"SELECT COLUMNS(*)::VARCHAR FROM ({q}) LIMIT {PREVIEW}", asked, user.id)
    except (ValueError, duckdb.Error) as e:
        raise ActionFailed(400, str(e))
    return {"columns": [{"name": c[0], "type": c[1]} for c in cols], "rows": rows}


router = APIRouter(prefix="/api")
Me = Annotated[User, Depends(current_user)]
Project = Annotated[str, Path(pattern=storage.PROJECT)]


def _viewer(conn, user: User, project: str, route: str | None, table: str | None = None) -> None:
    """Any role may view (Viewer: view, filter); a refusal is audited as `route` (SC-6) on its own connection, since the
    caller's rolls back. `route` None: the caller audits every outcome itself (export)."""
    if role_in(conn, user, project) is None:
        detail = f"You have no role in project '{project}'."
        if route:
            with db.connect() as c:
                audit(c, user, "rest", route, project, table and {"table": table}, 403, detail)
        raise HTTPException(403, detail)


@router.get("/tables")
def list_tables(user: Me) -> list[dict]:
    """Loaded tables (the `sources` registry) and input tables (WB-2) in every project the user has a role in (SC-2);
    `sync: true` marks a table whose source opted in to source sync (WB-10, `database.sync`)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT project, table_name AS table FROM sources WHERE %s"
            " OR project IN (SELECT project FROM project_roles WHERE user_id = %s)",
            [user.workspace_owner, user.id],
        ).fetchall()
        mine = [p for p in git.projects() if role_in(conn, user, p) is not None]
    rows += [{"project": p, "table": t} for p in mine for t in transform.inputs(p)]  # never a sources row too (D-20)
    rows.sort(key=lambda r: (r["project"], r["table"]))
    for r in rows:
        repo = git.path(r["project"])
        with contextlib.suppress(OSError, ValueError, AttributeError):  # a broken file: sync_edits says why
            if (json.loads((repo / "sources" / f"{r['table']}.json").read_text()).get("database", {}).get("sync") is True
                    and not (repo / "tables" / f"{r['table']}.json").exists()):  # steps or input table: no sync
                r["sync"] = True
    return rows


@router.get("/projects/{project}/tables/{table}/rows")
def get_rows(project: Project, table: str, user: Me,
             offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 100):
    with db.connect() as conn:
        _viewer(conn, user, project, "table_rows", table)
        known = conn.execute("SELECT 1 FROM sources WHERE project = %s AND table_name = %s", [project, table]).fetchone()
    # checked before touching the lake, so unknown names never create one; an input table (WB-2) has no sources row
    if not known and table not in transform.inputs(project):
        raise HTTPException(404, f"No table '{table}' in project '{project}'.")
    try:
        return table_rows(project, table, offset, limit, user.id)
    except ActionFailed as e:  # a live table's source refused or failed
        if e.status == 403:  # SC-6
            with db.connect() as conn:
                audit(conn, user, "rest", "table_rows", project, {"table": table}, 403, str(e))
        raise HTTPException(e.status, e.body)


@router.get("/projects/{project}/tables/{table}/position")
def get_position(project: Project, table: str, user: Me, key: str) -> dict:
    """WB-6 review "go to cell": the 0-based grid row of the row with this key (an edit's row_key JSON,
    {key column: value}) in the order `table_rows` pages, or null when the row is gone (orphaned)."""
    types = {c["name"]: c["type"] for c in get_rows(project, table, user, 0, 1)["columns"]}  # role, table, types
    try:
        k = json.loads(key)
        where = " AND ".join(f"{storage._q(c)} = CAST('{storage._lit(str(v))}' AS {types[c]})" for c, v in k.items())
    except (ValueError, AttributeError, KeyError):
        raise HTTPException(400, "key must be a JSON object {key column: value} of this table's columns.")
    # row_number() OVER () is a streaming window: it keeps insertion order, the order the grid's OFFSET pages follow
    try:
        found = execute(project, f"SELECT __pos FROM (SELECT row_number() OVER () - 1 AS __pos, * FROM {storage._q(table)})"
                                 f" WHERE {where or 'false'} LIMIT 1", {"table": table, "position": k}, user.id)
    except duckdb.ConversionException as e:
        raise HTTPException(400, f"A key value doesn't fit its column: {e}")
    if not found and rls.restricted(project, user.id):  # SE-6: a hidden row and a gone one answer alike
        raise HTTPException(404, f"No visible row with this key in table '{table}'.")
    return {"position": found[0][0] if found else None}


@router.get("/projects/{project}/tables/{table}/steps")
def get_steps(project: Project, table: str, user: Me) -> dict:
    """UI-2 steps editor: the table's saved steps (tables/<t>.json), else a table that reads its own source.
    An input table (WB-2) has none: its rows are edits."""
    with db.connect() as conn:
        _viewer(conn, user, project, "table_steps", table)
    if table in transform.inputs(project):
        return {"steps": []}
    try:
        steps = transform.tables(project).get(table)  # looked up by name, never a path built from input
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"steps": [s.model_dump(mode="json") for s in steps] if steps else [{"type": "source", "source": table}]}


@router.get("/projects/{project}/model")
def get_model(project: Project, user: Me) -> dict:
    """UI-2 ER diagram: model.json's tables (kind, key, description), relationships and date table, plus loaded columns."""
    from abi.semantic import model  # not at top: model imports this package

    with db.connect() as conn:
        _viewer(conn, user, project, "model")
    if not git.path(project).exists():  # checked before touching the lake, so unknown names never create one
        raise HTTPException(404, f"Project '{project}' does not exist.")
    return {**model.load(project), "columns": model.lake_columns(project)}


@router.post("/projects/{project}/export")
def post_export(project: Project, payload: Annotated[dict, Body()], user: Me,
                format: Literal["arrow", "parquet"] = "arrow") -> StreamingResponse:
    """SE-9 bulk export (body = semantic query, `limit` uncapped): any project role (Viewer), a Ready model (§3).
    SC-6: every export, refused or not, has an audit_log row."""
    from abi.semantic import model  # not at top: model imports this package

    status, detail = 200, None
    try:
        with db.connect() as conn:
            _viewer(conn, user, project, None)
        if not git.path(project).exists():  # checked before touching the lake, so unknown names never create one
            raise HTTPException(404, f"Project '{project}' does not exist.")
        if gaps := model.report(project)["missing"]:
            raise ActionFailed(409, "Export needs a Ready semantic model. Missing: " + " ".join(gaps), missing=gaps)
        chunks = export(project, payload, format, user.id)
    except ActionFailed as e:
        status, detail = e.status, str(e)
        raise HTTPException(e.status, e.body)
    except HTTPException as e:
        status, detail = e.status_code, str(e.detail)
        raise
    except Exception as e:
        status, detail = 500, str(e)
        raise
    finally:
        with db.connect() as conn:
            audit(conn, user, "rest", "export", project, {"format": format, "query": payload}, status, detail)
    kind = {"arrow": "application/vnd.apache.arrow.stream", "parquet": "application/vnd.apache.parquet"}[format]
    return StreamingResponse(chunks, media_type=kind,
                             headers={"Content-Disposition": f'attachment; filename="{project}.{format}"'})
