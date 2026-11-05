"""Query planner: semantic query + measures + model -> one DuckDB SQL statement. PRD SE-4 (and SE-2, SE-3).

`plan(query, measures, model)`; query = {measures: [id], dimensions: ["table.column"], filters: [{column, op, value}],
totals: [[dimension, ...]]}. Semantics are the ones the golden suite fixes (work/validation/S-03.md):
- One aggregate query (CTE) per fact and grouping (IGNORE, time), combined by FULL OUTER JOIN on the query's
  dimensions (NULL-safe, so a NULL member joins across facts). Row filters (WHERE, query filters) that differ
  between its aggregates become `agg(...) FILTER (WHERE ...)`, so the fact is scanned once (S-09-fix-2).
  A context that IGNOREs a query dimension doesn't create rows: it groups by the dimensions it keeps and is LEFT
  JOINed onto the rows the other contexts made. IGNORE of columns the query doesn't group by creates rows as usual.
- Totals: GROUPING SETS (full grain + each `totals` set); `grouping_id` = GROUPING_ID(dimensions in query order).
- Time intelligence rewrites the date filter: each date-table member at the query's grain, within the query's
  date-table filters, gets its own date window. SUM/COUNT/MIN/MAX/AVG read the fact aggregated once per day inside
  the windows' span, and the windows join those day rows (S-09-fix-1); DISTINCT_COUNT and MEDIAN aren't decomposable,
  so a context with one joins the fact rows themselves.
- Query copies (S-09-fix-3): `copies` names, per fact, a denormalized copy in lake.query (`copy_sql`: the fact's
  rows LEFT JOINed to each dimension it has one relationship to, dimension columns named "table.column"). A context
  whose dimension columns are all in the copy reads it instead of joining; the rows are the same, so are the results.
- One joined dimension (S-09-fix-4): a context that must join exactly one dimension (a column not in the copy) and
  has only decomposable aggregates groups the fact by its foreign key first, then joins and regroups those rows.
- Distinct pairs (S-09-fix-5): a context of DISTINCT_COUNTs of one fact column, grouped and filtered by the date table's
  year/month only (no time function), reads the column's distinct (year, month, column) table next to the copy.
- Snowflake dimensions (D-06): a fact reaches a table through the shortest chain of many-to-one relationships
  (fact -> dimension -> dimension ...), LEFT JOINed hop by hop, so the fact keeps its rows. Query copies fold in
  only one-hop dimensions; a context using a further table joins.
- Live ClickHouse tables (SE-5 mixed, D-02.2): a live fact's context reads one ClickHouse query that groups the fact by
  every fact column the context uses (foreign keys, its own grouped and filtered columns) with partials (sum, count,
  min, max; AVG = sum / count) and only the conditions on the fact's own columns; DuckDB joins, filters, regroups
  (GROUPING SETS too) and combines those rows as if they were the fact's. A live joined table is read as its used
  columns, grouped. No join runs in ClickHouse; each read is a DuckDB table `__live<n>` named in `reads`.
  D-02.3: a time context reads those partials per day (grouped by the fact's date column too) and DuckDB applies the
  date windows; a DISTINCT_COUNT/MEDIAN context runs whole in ClickHouse (uniqExact, quantileExactInclusive) and may
  use only the live fact's own columns (D-8).
- Shared partials (M3.6, `partials`): a context over a listed lake fact with no DISTINCT_COUNT, MEDIAN, AVG or CONVERT reads
  the fact as a live fact's context does (its partials grouped by every fact column it reads), grouped also by the
  listed columns, as DuckDB table `__lake<n>` named in `shared`; the planned SQL then regroups them the same way.
- Refused (DECISIONS D-8): time intelligence without a date-table dimension, and a filter or grouping on a table a
  fact in the query has no relationship path to, or more than one shortest path to.
"""

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal

from sqlglot import exp

from abi.semantic import mel

OPS = {"=": exp.EQ, "<>": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}
BINARY = {**OPS, "+": exp.Add, "-": exp.Sub, "*": exp.Mul, "/": exp.Div, "AND": exp.And, "OR": exp.Or}
AGG = {"SUM": exp.Sum, "AVG": exp.Avg, "MIN": exp.Min, "MAX": exp.Max, "COUNT": exp.Count, "MEDIAN": exp.Median}
HOLISTIC = ("DISTINCT_COUNT", "MEDIAN")  # not decomposable: computed from the fact rows themselves
GRAIN = {"day": "1 DAY", "month": "1 MONTH", "quarter": "3 MONTH", "year": "1 YEAR"}  # PREVIOUS_PERIOD step


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Ctx:
    """The filter context of an aggregate. Hashable; contexts equal but for `filters` share one CTE."""
    fact: str | None = None
    filters: tuple = ()  # row conditions (mel.Op) on fact rows
    members: tuple = ()  # time only: the date-table conditions that pick the members (the rewritten date filter)
    ignored: frozenset = frozenset()  # "table.column" dropped from grouping (IGNORE)
    time: tuple = ()  # time only: (fn, n, unit), outermost first


def _col(name: str) -> mel.Col:
    t, _, c = name.partition(".")
    if not (t and c):
        raise PlanError(f"'{name}' is not a column; write it as table.column.")
    return mel.Col(t, c)


def _name(c: mel.Col) -> str:
    return f"{c.table}.{c.column}"


def _cols(cond) -> set[str]:
    return {_name(n) for n in mel._walk(cond) if type(n) is mel.Col}


def _filter(f: dict) -> mel.Op:
    """A query filter {column, op, value} as the same row condition a MEL WHERE gives."""
    op, value = f.get("op"), f.get("value")
    scalar = lambda v: v is None or isinstance(v, (str, int, float, bool))
    if op == "in" and isinstance(value, list) and value and all(map(scalar, value)):
        return mel.Op("IN", (_col(f.get("column", "")), *map(mel.Lit, value)))
    if op in OPS and scalar(value) and value is not None:
        return mel.Op(op, (_col(f.get("column", "")), mel.Lit(value)))
    raise PlanError(f"Bad filter {json.dumps(f)}: use op {', '.join(OPS)} with one value, or 'in' with a list.")


def _sql(n, leaf):
    """MEL node -> sqlglot expression; `leaf` renders Agg and Col."""
    t = type(n)
    if t in (mel.Agg, mel.Col):
        return leaf(n)
    if t is mel.Lit:
        return exp.Literal.number(str(n.value)) if isinstance(n.value, Decimal) else exp.convert(n.value)
    return _op(n.op, [_sql(x, leaf) for x in n.args])


def _op(op: str, a: list) -> exp.Expression:
    match op:
        case _ if op in BINARY:
            return exp.Paren(this=BINARY[op](this=a[0], expression=a[1]))
        case "NEG":
            return exp.Neg(this=exp.Paren(this=a[0]))
        case "NOT":
            return exp.Not(this=exp.Paren(this=a[0]))
        case "IN":
            return exp.In(this=a[0], expressions=a[1:])
        case "IS NULL":
            return exp.Is(this=a[0], expression=exp.null())
        case "IS NOT NULL":
            return exp.Not(this=exp.Is(this=a[0], expression=exp.null()))
        case "DIVIDE":
            return exp.Div(this=a[0], expression=exp.Nullif(this=a[1], expression=exp.convert(0)))
        case "IF":
            return exp.If(this=a[0], true=a[1], false=a[2])
        case "ROUND":
            return exp.Round(this=a[0], decimals=a[1])
        case "ABS":
            return exp.Abs(this=a[0])
    raise PlanError(f"Unsupported operator {op}.")  # unreachable for compiled MEL


def path(model: dict, fact: str, table: str, measure: str = "", dim: str = "") -> list[dict]:
    """The chain of many-to-one relationships fact -> ... -> table (D-06 snowflake: a dimension's own dimensions),
    the shortest one; each hop keeps one row per fact row. D-8: no path is refused, never silently dropped, and so is
    more than one shortest path (the planner can't choose)."""
    hidden = {t["table"] for t in model.get("tables", []) if t.get("hidden")}  # MODEL-HIDE: staging tables link nothing
    rels, paths, seen, hit = [r for r in model.get("relationships", []) if not {r["from"]["table"], r["to"]["table"]} & hidden], [[]], {fact}, []
    while paths and not hit:
        paths = [p + [r] for p in paths for r in rels
                 if r["from"]["table"] == (p[-1]["to"]["table"] if p else fact) and r["to"]["table"] not in seen]
        seen |= {p[-1]["to"]["table"] for p in paths}
        hit = [p for p in paths if p[-1]["to"]["table"] == table]
    if not hit:
        raise PlanError(f"Measure '{measure}' can't be filtered or grouped by {dim}: its fact table '{fact}' has no "
                        f"relationship to '{table}'. Remove {dim} from the query or add a relationship.")
    if len(hit) > 1:  # ponytail: role-playing dimensions (two dates to one calendar) are refused, pick one when needed
        ways = ", ".join(" -> ".join(r["id"] for r in p) for p in hit)
        raise PlanError(f"Measure '{measure}': '{fact}' has more than one relationship to '{table}' "
                        f"({ways}); the planner can't choose.")
    return hit[0]


def local(model: dict, types: dict[str, dict[str, str]], zone: str) -> dict[tuple[str, str], str | None]:
    """SE-13 (E-11.md decision 2): {(table, column): zone, or None for a naive TIMESTAMP} of each timestamp column a
    relationship takes to the date table (`types`: {table: {column: DuckDB type}}). DATE columns are left out."""
    dt, out = (model.get("date_table") or {}).get("table"), {}
    for r in model.get("relationships", []):
        for c in r["from"]["columns"] if r["to"]["table"] == dt else ():
            ty = types.get(r["from"]["table"], {}).get(c, "")
            if ty.startswith("TIMESTAMP"):
                out[r["from"]["table"], c] = zone if ty == "TIMESTAMP WITH TIME ZONE" else None
    return out


def day(e: exp.Expression, zone: str | None) -> exp.Expression:
    """The local day of `e`: an instant's date in `zone` (whatever the session TimeZone), else `CAST(e AS DATE)`
    (a naive TIMESTAMP is wall-clock time already: never shifted)."""
    if zone:
        e = exp.Anonymous(this="timezone", expressions=[exp.convert(zone), e])
    return exp.Cast(this=e, to=exp.DataType.build("DATE"))


def zoned(types: dict[str, dict[str, str]], zone: str) -> dict[tuple[str, str], str]:
    """SE-13: {(table, column): zone} of every TIMESTAMP WITH TIME ZONE column, for CONVERT's date (`plan`'s `rates`)."""
    return {(t, c): zone for t, cs in types.items() for c, ty in cs.items() if ty == "TIMESTAMP WITH TIME ZONE"}


def _converted(src, fact: str, convs: list, model: dict, rates: tuple | None, m: str) -> exp.Expression:
    """SE-13 (E-11.md decisions 5-7): the fact's rows (`src`, aliased as the fact) plus one column per CONVERT: a NULL
    amount stays NULL, the display currency's amount stays as it is, any other is amount × the rate of the latest
    rates row from its currency to the display currency on or before the row's local day (ASOF LEFT JOIN), exact
    DECIMAL arithmetic in DuckDB. A row without such a rate fails the query, naming the pair and the day."""
    currency, zones = rates or (None, {})
    if not currency:
        raise PlanError(f"Measure '{m}' converts amounts with CONVERT, but no display currency is set: set the project's "
                        "with set_project_locale, or its tenant's with set_tenant (workspace owner).")
    if not (table := model.get("rates_table")):
        raise PlanError(f"Measure '{m}' converts amounts with CONVERT, but the model has no rates table: declare one "
                        "with set_rates_table.")
    to, eq = exp.convert(currency), lambda a, b: exp.EQ(this=a, expression=b)
    q = exp.select(exp.Column(this=exp.Star(), table=exp.to_identifier(fact, quoted=True))).from_(src)
    for i, cv in enumerate(convs):
        r = lambda c, i=i: exp.column(c, f"__r{i}", quoted=True)
        amount = exp.column(cv.amount.column, fact, quoted=True)
        own = type(cv.currency) is mel.Col
        cur = exp.column(cv.currency.column, fact, quoted=True) if own else exp.convert(cv.currency.value)
        d = day(exp.column(cv.date.column, fact, quoted=True), zones.get((fact, cv.date.column)))
        rows = exp.select(exp.Star()).from_(exp.table_(table, quoted=True)).where(eq(exp.column("to", quoted=True), to))
        rows = rows if own else rows.where(eq(exp.column("from", quoted=True), cur))
        on = exp.and_(*([eq(r("from"), cur)] if own else []), exp.GTE(this=d, expression=r("date")))
        q = q.join(exp.alias_(exp.Subquery(this=rows), f"__r{i}", quoted=True), on=on, join_type="asof left")
        null = lambda e: exp.Is(this=e, expression=exp.null())
        value = exp.Case().when(exp.or_(null(amount), eq(cur.copy(), to)), amount.copy()).else_(
            exp.Mul(this=amount.copy(), expression=r("rate")))
        missing = exp.Case().when(exp.and_(exp.Not(this=null(amount.copy())), exp.NullSafeNEQ(this=cur.copy(), expression=to),
                                           null(r("rate"))), exp.Anonymous(this="concat", expressions=[
            exp.convert("No "), cur.copy(), exp.convert(f" to {currency} rate on or before "),
            exp.Cast(this=d.copy(), to=exp.DataType.build("VARCHAR"))]))
        q = q.select(exp.alias_(value, cv.column, quoted=True), exp.alias_(missing, cv.column + "_missing", quoted=True))
    return exp.alias_(exp.Subquery(this=q), fact, quoted=True)


def _checked(cv: mel.Conv) -> exp.Expression:
    """A converted amount as an aggregate reads it: a row without a rate raises its message (`_converted`) there, so
    only rows the query's joins and filters keep can fail it.
    ponytail: under FILTER (another aggregate of the context reads rows this one's filter drops) DuckDB computes the
    argument for those rows too, so such a row without a rate fails the query; split contexts if that shows up."""
    col = lambda c: exp.column(c, cv.table, quoted=True)
    return exp.Case().when(exp.Is(this=col(cv.column + "_missing"), expression=exp.null()), col(cv.column)).else_(
        exp.Anonymous(this="error", expressions=[col(cv.column + "_missing")]))


def _from(table: str, column: str, days: dict) -> exp.Expression:
    e = exp.column(column, table, quoted=True)
    return day(e, days[table, column]) if (table, column) in days else e


def _on(rel: dict, days: dict) -> exp.Expression:
    f, t = rel["from"], rel["to"]
    return exp.and_(*(exp.EQ(this=exp.column(b, t["table"], quoted=True), expression=_from(f["table"], a, days))
                      for a, b in zip(f["columns"], t["columns"])))


def _window(time: tuple, lo: exp.Expression, end: exp.Expression, grain: str) -> tuple:
    """[start, end) of each member (lo = first day, end = the day after the last), rewritten by each time function,
    outermost first: YTD(PREVIOUS_YEAR(x)) reads x over the previous year's YTD window."""
    iv = lambda x, sign, n: exp.maybe_parse(f"({x.sql('duckdb')}) {sign} INTERVAL {n}", dialect="duckdb")
    trunc = lambda part: exp.maybe_parse(f"date_trunc('{part}', ({end.sql('duckdb')}) - INTERVAL 1 DAY)", dialect="duckdb")
    for fn, n, unit in time:
        if fn in ("YTD", "QTD", "MTD"):
            lo = trunc({"YTD": "year", "QTD": "quarter", "MTD": "month"}[fn])
        elif fn == "PREVIOUS_YEAR":
            lo, end = iv(lo, "-", "1 YEAR"), iv(end, "-", "1 YEAR")
        elif fn == "PREVIOUS_PERIOD":
            lo, end = iv(lo, "-", GRAIN[grain]), lo
        else:  # ROLLING: n units back from the member's end
            lo = iv(end, "-", f"{3 * n} MONTH" if unit == "quarter" else f"{n} {unit.upper()}")
    return lo, end


def copy_sql(fact: str, model: dict, columns: dict[str, list[str]], days: dict | None = None) -> str | None:
    """The query copy of `fact` (None: it has no dimension to fold in). `columns`: each loaded table's columns;
    `days`: as in `plan`."""
    # ponytail: one-hop dimensions only, so snowflake (D-06) queries join; fold in chains (and stamp them in
    # semantic._copies) if a big snowflake model misses the p95 target
    rels = [r for r in model.get("relationships", []) if r["from"]["table"] == fact and r["to"]["table"] != fact]
    one = sorted((r for r in rels if [x["to"]["table"] for x in rels].count(r["to"]["table"]) == 1
                  and r["to"]["table"] in columns), key=lambda r: r["to"]["table"])
    names = [(r["to"]["table"], c) for r in one for c in columns[r["to"]["table"]]]
    if not one or fact not in columns or set(columns[fact]) & {f"{t}.{c}" for t, c in names}:
        return None  # a role-playing dimension is refused anyway (_rel); a fact column named "t.c" would clash
    q = exp.select(exp.Column(this=exp.Star(), table=exp.to_identifier(fact, quoted=True)),
                   *(exp.alias_(exp.column(c, t, quoted=True), f"{t}.{c}", quoted=True) for t, c in names))
    q = q.from_(exp.table_(fact, quoted=True))
    for r in one:
        q = q.join(exp.table_(r["to"]["table"], quoted=True), on=_on(r, days or {}), join_type="left")
    return q.sql("duckdb")


def pair_grains(copy_cols, dt: dict) -> list[str]:
    """The date-table columns a distinct-pair table keeps (S-09-fix-5): year and month, when the copy has them."""
    return [f"{dt.get('table')}.{g}" for g in ("year", "month") if f"{dt.get('table')}.{g}" in copy_cols]


def pairs_sql(copy: str, column: str, copy_cols, dt: dict) -> str | None:
    """The distinct (year, month, column) rows of query copy `copy` for a DISTINCT_COUNT column (S-09-fix-5), or None
    (the copy has no year/month). A context grouped and filtered only by those columns counts the same distinct values
    in it as in the fact rows: it is their projection."""
    if not (grains := pair_grains(copy_cols, dt)):
        return None
    return (exp.select(*(exp.column(c, quoted=True) for c in [*grains, column])).distinct()
            .from_(exp.table_(copy, db="query", catalog="lake", quoted=True)).sql("duckdb"))


def plan(query: dict, measures: dict[str, str], model: dict, copies: dict[str, tuple] | None = None,
         live: dict[str, tuple] | None = None, reads: dict | None = None, days: dict | None = None,
         rates: tuple | None = None, joins: list | None = None, partials: dict[str, list[str]] | None = None,
         shared: dict | None = None) -> str:
    """Semantic query -> DuckDB SQL. Columns: dimensions ("table.column", query order), `grouping_id` when totals
    are asked, then measures by ID. `copies`: {fact: (name of its current query copy in lake.query, its columns,
    {DISTINCT_COUNT column: name of its current distinct-pair table})}. `live`: {live table: (its ClickHouse
    "database"."table", {column: DuckDB type})}; `reads` is filled with {DuckDB table name: (live table, ClickHouse SQL)} for each read.
    `days`: `local(...)`, the timestamp columns reaching the date table, joined and windowed by their local day (SE-13).
    `rates`: (display currency or None, `zoned(...)`) for CONVERT, which reads model.json `rates_table` (`_converted`).
    `joins` is filled with every relationship the SQL joins on (COL-SEC.2: hidden keys are refused).
    `partials`: {lake fact: more columns to group its partials by}; `shared` is filled with {DuckDB table name: (fact,
    DuckDB SQL of its partials over the unqualified fact)} for each such read (M3.6).
    Raises PlanError (query/model problems) or mel.MelError (measures)."""
    names, dims = query.get("measures") or [], query.get("dimensions") or []
    if not names:
        raise PlanError("A query needs at least one measure.")
    if unknown := [m for m in names if m not in measures]:
        raise PlanError(f"Unknown measure {', '.join(unknown)}; use one of: {', '.join(sorted(measures))}.")
    tables = {t["table"] for t in model.get("tables", []) if not t.get("hidden")}  # MODEL-HIDE: staging tables are out
    hidden = {t["table"] for t in model.get("tables", [])} - tables
    for d in dims:
        if _col(d).table not in tables:
            raise PlanError(f"Unknown table in dimension {d}; use a model table: {', '.join(sorted(tables))}.")
    filters = tuple(_filter(f) for f in query.get("filters") or [])
    sets = [dims] + [s for s in query.get("totals") or [] if s != dims]
    if bad := [s for s in sets if not isinstance(s, list) or not set(s) <= set(dims)]:
        raise PlanError(f"Totals {json.dumps(bad)} must list dimensions of the query.")
    totals = bool(query.get("totals"))
    dt = model.get("date_table") or {}
    trees = mel.compile(measures)

    subs: dict[Ctx, dict] = {}  # context -> {Agg: output column}
    owner: dict[Ctx, str] = {}  # context -> first measure using it (named in errors)

    def walk(n, ctx: Ctx, m: str):
        t = type(n)
        if t is mel.Agg:
            if n.col.table in hidden:
                raise PlanError(f"Measure '{m}' reads '{n.col.table}', a hidden staging table that is not part of the "
                                "semantic model; use the table it feeds, or show it with set_table hidden=false.")
            c = replace(ctx, fact=n.col.table, filters=())
            owner.setdefault(c, m)
            aggs = subs.setdefault(c, {})  # (filters, Agg) -> output column
            return exp.column(aggs.setdefault((ctx.filters, n), f"a{len(aggs)}"), f"s{list(subs).index(c)}", quoted=True)
        if t is mel.Where:
            return walk(n.expr, replace(ctx, filters=ctx.filters + (n.cond,)), m)
        if t is mel.Ignore:
            drop = {_name(c) for c in n.cols}
            keep = lambda fs: tuple(f for f in fs if not _cols(f) & drop)
            return walk(n.expr, replace(ctx, filters=keep(ctx.filters), members=keep(ctx.members),
                                        ignored=ctx.ignored | drop), m)
        if t is mel.Time:
            if not ctx.time:  # the outermost time function rewrites the date filter: date-table conditions pick members
                on_dt = lambda f: {c.split(".")[0] for c in _cols(f)} == {dt.get("table")}
                ctx = replace(ctx, filters=tuple(f for f in ctx.filters if not on_dt(f)),
                              members=tuple(f for f in ctx.filters if on_dt(f)))
            return walk(n.expr, replace(ctx, time=ctx.time + ((n.fn, n.n, n.unit),)), m)
        if t is mel.Op:
            return _op(n.op, [walk(x, ctx, m) for x in n.args])
        return _sql(n, None)

    finals = {m: walk(trees[m], Ctx(filters=filters), m) for m in names}
    reads = {} if reads is None else reads
    ctes = {f"s{i}": _aggregate(c, aggs, owner[c], dims, sets, totals, model, dt, (copies or {}).get(c.fact), live or {}, reads,
                                    days or {}, rates, [] if joins is None else joins, partials or {},
                                    {} if shared is None else shared)
            for i, (c, aggs) in enumerate(subs.items())}

    ids = list(ctes)
    # a context that IGNOREs a query dimension makes no rows; one that keeps every dimension does (SE-IGNORE-1)
    creators = [s for s, c in zip(ids, subs) if not c.ignored & set(dims)]
    cols = dims + (["grouping_id"] if totals else []) + names
    if not creators:
        return exp.select(*(exp.alias_(exp.null(), c, quoted=True) for c in cols)).where(exp.false()).sql("duckdb")
    first = lambda c, among: exp.Coalesce(this=exp.column(c, among[0], quoted=True),
                                          expressions=[exp.column(c, s, quoted=True) for s in among[1:]]) \
        if len(among) > 1 else exp.column(c, among[0], quoted=True)  # the key of the fact(s) that have the row
    key = lambda i, among: first(f"k{i}", among)
    gkey = lambda among: first("g", among)
    q = exp.select(*(exp.alias_(key(i, creators), d, quoted=True) for i, d in enumerate(dims)))
    if totals:
        q = q.select(exp.alias_(gkey(creators), "grouping_id", quoted=True))
    q = q.select(*(exp.alias_(e, m, quoted=True) for m, e in finals.items())).from_(exp.table_(creators[0], quoted=True))
    for j, s in enumerate(creators[1:], 1):  # full outer join on the shared dimensions (and the totals level)
        on = [exp.NullSafeEQ(this=key(i, creators[:j]), expression=exp.column(f"k{i}", s, quoted=True)) for i in range(len(dims))]
        on += [exp.EQ(this=gkey(creators[:j]), expression=exp.column("g", s, quoted=True))] if totals else []
        q = q.join(exp.table_(s, quoted=True), on=exp.and_(*on) if on else exp.true(), join_type="full")
    for s, c in zip(ids, subs):
        if s in creators:
            continue
        kept = [i for i, d in enumerate(dims) if d not in c.ignored]
        on = [exp.NullSafeEQ(this=key(i, creators), expression=exp.column(f"k{i}", s, quoted=True)) for i in kept]
        if totals:  # same level: the grouping bits of the dimensions this context keeps
            mask = sum(1 << (len(dims) - 1 - i) for i in kept)
            on.append(exp.EQ(this=exp.BitwiseAnd(this=gkey(creators), expression=exp.convert(mask)),
                             expression=exp.column("g", s, quoted=True)))
        q = q.join(exp.table_(s, quoted=True), on=exp.and_(*on) if on else exp.true(), join_type="left")
    for s, body in ctes.items():
        q = q.with_(s, as_=body)
    return q.sql("duckdb")


def _aggregate(c: Ctx, aggs: dict, m: str, dims: list, sets: list, totals: bool, model: dict, dt: dict,
               copy: tuple | None, live: dict[str, tuple], reads: dict, days: dict, rates: tuple | None,
               joins: list, partials: dict | None = None, shared: dict | None = None) -> exp.Select:
    """One aggregate query: the fact's rows under context `c`, grouped by the dimensions the context keeps; each
    aggregate in `aggs` ({(filters, Agg): alias}) reads the rows its own filters keep. `copy`: the fact's query copy
    (name, its "table.column" columns); read when it holds every dimension column the context uses. `live`, `reads`:
    as in `plan`."""
    kept = [(i, d) for i, d in enumerate(dims) if d not in c.ignored]
    time_dims = [d for _, d in kept if _col(d).table == dt.get("table")] if c.time else []
    fn = c.time[0][0] if c.time else None
    # D-02.3: DISTINCT_COUNT/MEDIAN over a live fact run whole in ClickHouse, so only on the fact's own columns (D-8)
    convs = list(dict.fromkeys(a.col for _, a in aggs if type(a.col) is mel.Conv))
    if convs and c.fact in live:  # ponytail: convert per-day partials in DuckDB if live facts need conversion
        raise PlanError(f"Measure '{m}' converts amounts with CONVERT over the live ClickHouse table '{c.fact}'; "
                        "conversion reads imported tables only (DECISIONS D-8).")
    whole = c.fact in live and next((a.fn for _, a in aggs if a.fn in HOLISTIC), None)
    # M3.6: shared partials, read as a live fact's are (none for a holistic or converted aggregate, nor AVG: DuckDB's
    # own AVG of a DECIMAL or DOUBLE, or of a BIGINT's HUGEINT sum in long double on x86, isn't SUM/COUNT to the bit)
    lake = c.fact in (partials or {}) and c.fact not in live and not convs and all(
        a.fn not in (*HOLISTIC, "AVG") for _, a in aggs)
    if whole and (other := [x for x in [*(d for _, d in kept), *(x for fs, _ in aggs for f in fs for x in sorted(_cols(f)))]
                            if _col(x).table != c.fact]):
        raise PlanError(f"Measure '{m}' uses {whole} over the live ClickHouse table '{c.fact}', so it can be grouped "
                        f"and filtered only by that table's own columns, not {other[0]} (DECISIONS D-8).")
    if whole and (text := [a.col.column for _, a in aggs if a.fn == "MEDIAN"
                           and not re.fullmatch(rf"{INT}|FLOAT|DOUBLE|DECIMAL\(.*", live[c.fact][1].get(a.col.column, ""))]):
        raise PlanError(f"Measure '{m}' uses MEDIAN of {c.fact}.{text[0]}, which ClickHouse computes exactly only for "
                        "numbers; the live table's column isn't one (DECISIONS D-8).")
    if c.time and not time_dims:
        raise PlanError(f"Measure '{m}' uses {fn}, which needs a date dimension in the query: add "
                        f"{dt.get('table', 'the date table')}.year, .quarter, .month or .{dt.get('column', 'day')} "
                        "to the dimensions (DECISIONS D-8).")
    cols = {x.column for x in map(_col, time_dims)}
    if other := cols - {"year", "quarter", "month", dt.get("column")}:
        raise PlanError(f"Measure '{m}' uses {fn}, which can't group by {', '.join(sorted(other))}: its periods "
                        f"aren't contiguous. Use {dt['table']}.year, .quarter, .month or .{dt['column']}.")
    if time_dims and not cols & {"year", dt["column"]}:
        raise PlanError(f"Measure '{m}' uses {fn}: add {dt['table']}.year (or .{dt['column']}) to the dimensions, "
                        "so each row is one period, not the same month or quarter across years.")
    grain = "day" if dt.get("column") in cols else next((g for g in ("month", "quarter", "year") if g in cols), "year")
    grouped = [[d for d in s if d not in c.ignored] for s in sets]
    grouped = [s for i, s in enumerate(grouped) if s not in grouped[:i]]
    if time_dims and any(not set(time_dims) <= set(s) for s in grouped):
        # ponytail: a total across date members needs its own window per level; refused until a report needs it
        raise PlanError(f"Measure '{m}' uses {fn}, so totals must keep every date dimension ({', '.join(time_dims)}).")

    need = {_col(d).table: d for _, d in kept if d not in time_dims}
    fsets = list(dict.fromkeys(fs for fs, _ in aggs))
    for f in dict.fromkeys(f for fs in fsets for f in fs):  # ordered, so the SQL text (SE-7 cache key) is stable
        need.update({x.split(".")[0]: x for x in _cols(f)})
    need.pop(c.fact, None)  # a column of the fact itself needs no join
    # {table: relationship joining it} in join order, every hop of each path (D-06). Paths never disagree on a table: a
    # shortest path's prefix is the shortest path to that table, and `path` refuses two. D-8 checks run with or without a copy
    rels = {r["to"]["table"]: r for t, d in need.items() for r in path(model, c.fact, t, m, d)}
    joins += [*rels.values(), *(path(model, c.fact, dt["table"], m, time_dims[0]) if time_dims else ())]
    wanted = {d for _, d in kept if d not in time_dims} | {x for fs in fsets for f in fs for x in _cols(f)}
    # S-09-fix-5: DISTINCT_COUNTs of one column, grouped and filtered by year/month (or that column) only, read its
    # distinct-pair table. Its rows are distinct, so when every grouping keeps all its date columns, COUNT is exact.
    dc = {a.col.column for _, a in aggs}.pop() if len({a.col for _, a in aggs}) == 1 else None
    grains = pair_grains(copy[1], dt) if copy else []
    pairs = (not c.time and dc and all(a.fn == "DISTINCT_COUNT" for _, a in aggs) and grains
             and wanted <= {*grains, f"{c.fact}.{dc}"} and copy[2].get(dc))
    once = bool(pairs) and all(set(grains) <= set(s) for s in grouped)
    copy = pairs or (copy[0] if copy and {x for x in wanted if _col(x).table != c.fact} <= copy[1] else None)
    rels = {} if copy else rels
    # one joined dimension (e.g. a >10,000-value column, not in the copy) under decomposable aggregates: aggregate the
    # fact by its foreign key first, then join the dimension to those rows and group again (S-09-fix-4).
    # ponytail: one joined table only; with two, grouping by both keys can make more groups than the join saves.
    # A snowflake chain behind that table (D-06) is fine: the fact is grouped by its one key either way.
    by_key = (not time_dims and c.fact not in live and not lake and [r["from"]["table"] for r in rels.values()].count(c.fact) == 1
              and all(a.fn not in HOLISTIC for _, a in aggs)
              and not {_col(x).table for fs in fsets for f in fs for x in _cols(f)} & set(rels))

    own = lambda col: _checked(col) if type(col) is mel.Conv else exp.column(col.column, col.table, quoted=True)
    # with a query copy, a dimension column is the copy's "table.column" column, read as <fact>."table.column"
    plain = (lambda col: own(col) if col.table == c.fact else exp.column(_name(col), c.fact, quoted=True)) if copy else own
    # time contexts over decomposable aggregates read the fact pre-aggregated per day (`__f`, S-09-fix-1).
    # ponytail: one DISTINCT_COUNT/MEDIAN puts its whole context on the row-level join; split contexts if a visual mixes them
    pre = bool(time_dims) and all(a.fn not in HOLISTIC for _, a in aggs)
    ref = lambda col: (exp.column(col.column, "__m", quoted=True) if _name(col) in time_dims else
                       exp.column(f"k{dims.index(_name(col))}", "__f", quoted=True) if pre else plain(col))
    cond = lambda fs: exp.and_(*(_sql(f, plain) for f in fs))
    top = exp.EQ(this=exp.Anonymous(this="GROUPING_ID", expressions=[ref(_col(d)) for _, d in kept]),
                 expression=exp.convert((1 << len(kept)) - 1)) if kept and totals else exp.false()
    only = lambda fs: cond(fs) if fs and len(fsets) > 1 else None  # the aggregate's own FILTER
    src, part = exp.table_(c.fact, quoted=True), None
    if copy:
        src = exp.table_(copy, db="query", catalog="lake", quoted=True, alias=exp.to_identifier(c.fact, quoted=True))
    elif whole:  # rendered for ClickHouse at the end (`_live_whole`)
        src = exp.to_table(live[c.fact][0], dialect="clickhouse").as_(c.fact, quoted=True)
    elif c.fact in live or lake:  # a time context also reads the fact's date column, to group the partials per day
        dated = {**rels, dt["table"]: path(model, c.fact, dt["table"], m, time_dims[0])[0]} if time_dims else rels
        src, part = _live_fact(c, aggs, fsets, kept, dated, live, reads, (partials[c.fact], shared) if lake else None)
    if convs:
        src = _converted(src, c.fact, convs, model, rates, m)
    leaves = {alias: _leaf(replace(a, fn="COUNT") if once else a, only(fs), plain, kept and top, part)
              for (fs, a), alias in aggs.items()}
    where = [exp.or_(*(cond(fs) for fs in fsets))] if all(fsets) else []  # rows no aggregate reads make no group
    table = lambda t: _live_table(t, rels, wanted, live, reads) if t in live else exp.table_(t, quoted=True)

    q = exp.select(*(exp.alias_(ref(_col(d)), f"k{i}", quoted=True) for i, d in kept))
    if time_dims:
        rel, *hops = path(model, c.fact, dt["table"], m, time_dims[0])
        if hops:  # ponytail: the date window reads the fact's own date column; a date reached through a dimension is refused
            raise PlanError(f"Measure '{m}' uses {fn}, which needs a relationship from '{c.fact}' straight to the "
                            f"date table '{dt['table']}' (it reaches it only through {rel['to']['table']}).")
        day = exp.column(dt["column"], quoted=True)
        members = exp.alias_(exp.Subquery(this=(
            exp.select(*(exp.column(x, quoted=True) for x in sorted(cols)), exp.alias_(exp.Min(this=day), "lo"),
                       exp.alias_(exp.maybe_parse(f"max({day.sql()}) + INTERVAL 1 DAY", dialect="duckdb"), "hi"))
            .from_(exp.table_(dt["table"], quoted=True))
            .where(*([_sql(f, own) for f in c.members] or [exp.true()]))
            .group_by(*(exp.column(x, quoted=True) for x in sorted(cols))))), "__m", quoted=True)
        lo, end = _window(c.time, exp.column("lo", "__m"), exp.column("hi", "__m"), grain)
        date = _from(c.fact, rel["from"]["columns"][0], days)
        if pre:  # one row per day (and other dimension) inside the members' span; the windows then sum those rows
            span = lambda agg, x: exp.Subquery(this=exp.select(agg(this=x)).from_(members.copy()))
            keys = [exp.alias_(date, "__d", quoted=True)] + [exp.alias_(plain(_col(d)), f"k{i}", quoted=True)
                                                             for i, d in kept if d not in time_dims]
            inner = exp.select(*keys).from_(src)
            for t, r in rels.items():
                inner = inner.join(table(t), on=_on(r, days), join_type="left")
            parts, leaves = _partials(aggs, only, plain, part=part)
            src = exp.alias_(exp.Subquery(this=inner.select(*parts).where(
                *where, exp.GTE(this=date, expression=span(exp.Min, lo)), exp.LT(this=date, expression=span(exp.Max, end)))
                .group_by(*(k.this for k in keys))), "__f", quoted=True)
            date, rels, where = exp.column("__d", "__f", quoted=True), {}, []
        q = q.from_(members).join(src, on=exp.and_(exp.GTE(this=date, expression=lo), exp.LT(this=date, expression=end)))
    elif by_key:  # the subquery is named like the fact, so its key and fact columns keep their references
        keys = [exp.column(x, c.fact, quoted=True) for x in dict.fromkeys(
            [*next(iter(rels.values()))["from"]["columns"], *(_col(d).column for _, d in kept if _col(d).table == c.fact)])]
        parts, leaves = _partials(aggs, only, plain, c.fact, kept and top)
        inner = exp.select(*keys, *parts).from_(src).group_by(*keys)
        q = q.from_(exp.alias_(exp.Subquery(this=inner.where(*where) if where else inner), c.fact, quoted=True))
        where = []
    else:
        q = q.from_(src)
    for t, rel in rels.items():
        q = q.join(table(t), on=_on(rel, days), join_type="left")
    if totals:
        gid = (exp.Anonymous(this="GROUPING_ID", expressions=[ref(_col(d)) for d in dims])
               if len(kept) == len(dims) and dims else
               sum((exp.Mul(this=exp.Anonymous(this="GROUPING", expressions=[ref(_col(d))]),
                            expression=exp.convert(1 << (len(dims) - 1 - i))) for i, d in kept), exp.convert(0)))
        q = q.select(exp.alias_(gid, "g", quoted=True))
    meds = {alias: (plain(a.col), only(fs)) for (fs, a), alias in aggs.items() if a.fn == "MEDIAN" and not whole}
    q = q.select(*(exp.alias_(e, alias, quoted=True) for alias, e in leaves.items() if alias not in meds))
    if where:
        q = q.where(*where)
    if kept and totals:
        q = q.group_by(exp.GroupingSets(expressions=[exp.Tuple(expressions=[ref(_col(d)) for d in s]) for s in grouped]))
    elif kept:
        q = q.group_by(*(ref(_col(d)) for _, d in kept))
    if whole:
        return _live_whole(c.fact, q, live, reads)
    if not meds:
        return q
    keys = {f"k{i}": len(dims) - 1 - i for i, _ in kept}  # key column -> its bit in `g`
    sets = [[f"k{dims.index(d)}" for d in s] for s in grouped] if totals else None
    m = [(alias, _median(q, x, w, alias, keys, sets)) for alias, (x, w) in meds.items()]
    if len(m) == len(aggs) and all(sets or [keys]):
        # MEDIANs only: their own passes make the groups, so the context needs no scan of its own. Not with a grand
        # total (no keys, or a `()` set): its row exists even when no row is read, and the passes group by bucket.
        (_, q), m = m[0], m[1:]
    elif len(m) == len(aggs):
        q = q.select(exp.alias_(exp.Count(this=exp.Star()), "__rows", quoted=True))  # an aggregate: one row per group
    if not m:
        return q
    col = lambda c, t: exp.column(c, t, quoted=True)
    out = exp.select(exp.Star()).from_(exp.alias_(exp.Subquery(this=q), "__q", quoted=True))
    for j, (alias, mq) in enumerate(m):  # the groups are the same; join the other MEDIANs onto them
        on = [exp.NullSafeEQ(this=col(k, "__q"), expression=col(k, f"__m{j}")) for k in keys]
        on += [exp.EQ(this=col("g", "__q"), expression=col("g", f"__m{j}"))] if sets else []
        out = out.select(col(alias, f"__m{j}")).join(exp.alias_(exp.Subquery(this=mq), f"__m{j}", quoted=True),
                                                     on=exp.and_(*on) if on else exp.true(), join_type="left")
    return out


MEDIAN_BUCKETS = 1024


def _median(q: exp.Select, x: exp.Expression, where, alias: str, keys: dict[str, int], sets: list | None) -> exp.Select:
    """Exact MEDIAN of `x` (rows `where` keeps) per group of the aggregate query `q`, in memory the DuckDB cap bounds
    (GATE-3-fix-1): DuckDB's MEDIAN keeps every value outside its buffer manager (~3 GB at 100M rows). Columns: q's
    key columns, `g` (q's grouping bits; 0 without totals), `alias`. Only spillable operators touch all the rows:
    pass 1 counts rows per group and value bucket (buckets split [min, max] evenly, a non-decreasing function of the
    value, so a group's k-th value lies in the bucket where the running count reaches k); pass 2 ranks the rows of the
    one or two buckets that hold the middle ranks, and MEDIAN of those middle one or two values is the median of the
    group. A non-numeric column puts every row in bucket 0 (exact, only slower); NaN and infinity land in the last
    bucket (DuckDB orders NaN above every number), which keeps the buckets in value order."""
    # ponytail: 1,024 even buckets; values piled into one bucket (heavy skew, a huge outlier) make pass 2 rank most rows
    # (exact, spills, slower). Split the middle bucket again (a third pass) if a real model shows that.
    q = q.copy()
    v = exp.If(this=where, true=x) if where is not None else x
    q.set("expressions", [e for e in q.expressions if e.alias_or_name in keys] + [exp.alias_(v, "__v", quoted=True)])
    q.set("group", None)
    kk = lambda t="": "".join(f'{t}"{c}", ' for c in keys)  # key columns, each followed by ", "
    g = " + ".join(f'GROUPING("{c}") * {1 << bit}' for c, bit in keys.items()) if sets else "0"
    groups = ("GROUPING SETS (" + ", ".join("(" + "".join(f'"{c}", ' for c in s) + "__bk)" for s in sets) + ")"
              if sets else f"{kk()}__bk")
    same = " AND ".join(f'(__t."{c}" IS NOT DISTINCT FROM __b."{c}"' + (f" OR ((__t.g >> {bit}) & 1) = 1)" if sets else ")")
                        for c, bit in keys.items())
    d = "TRY_CAST(__v AS DOUBLE)"  # ordered like the value for every numeric type (text isn't bucketed: `num`)
    num = r"regexp_full_match(typeof(min(__v)), 'U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|FLOAT|DOUBLE|DECIMAL\(.*')"
    # pass 2 joins only the rows between the middle buckets' smallest and largest values, so its hash table is small;
    # that range is a plain comparison on the column, which the scan filters without computing buckets (S-09-fix-6)
    sql = f"""WITH __r AS NOT MATERIALIZED ({q.sql("duckdb")}),
      __s AS MATERIALIZED (SELECT lo, CASE WHEN num AND hi > lo THEN {MEDIAN_BUCKETS} / (hi - lo) END AS sc
                           FROM (SELECT TRY_CAST(min(__v) AS DOUBLE) AS lo, TRY_CAST(max(__v) AS DOUBLE) AS hi, {num} AS num FROM __r)),
      __b AS NOT MATERIALIZED (SELECT __r.*, CASE WHEN __v IS NULL THEN NULL WHEN sc IS NOT NULL THEN
          least(floor(({d} - lo) * sc), {MEDIAN_BUCKETS - 1})::INTEGER ELSE 0 END AS __bk FROM __r, __s),
      __t AS MATERIALIZED (SELECT * FROM (SELECT *, sum(__c) OVER (PARTITION BY {kk("__h.")}__h.g ORDER BY __bk) - __c AS __pre,
                                    sum(__c) OVER (PARTITION BY {kk("__h.")}__h.g) AS __n
                             FROM (SELECT {kk()}{g} AS g, __bk, count(__v) AS __c, min(__v) AS __lo, max(__v) AS __hi
                                   FROM __b GROUP BY {groups}) AS __h)
              WHERE __pre < (__n + 2) // 2 AND __pre + __c >= (__n + 1) // 2)
    SELECT {kk()}g, MEDIAN(__v) FILTER (WHERE __rk IN ((__n + 1) // 2, (__n + 2) // 2)) AS "{alias}"
    FROM (SELECT {kk("__t.")}__t.g, __t.__n, __b.__v,
                 __t.__pre + row_number() OVER (PARTITION BY {kk("__t.")}__t.g, __t.__bk ORDER BY __b.__v) AS __rk
          FROM __t LEFT JOIN (SELECT * FROM __b WHERE __v BETWEEN (SELECT min(__lo) FROM __t) AND (SELECT max(__hi) FROM __t)) AS __b
          ON __b.__bk = __t.__bk{" AND " + same if same else ""})
    GROUP BY {kk()}g"""
    return exp.maybe_parse(sql, dialect="duckdb")


def _partials(aggs: dict, only, plain, src: str = "__f", top=None, part=None) -> tuple[list, dict]:
    """Partials of decomposable aggregates per day or key (inner select list, read from `src`) and how to combine
    them ({alias: outer}). `top`: the grand-total condition, where COUNT is 0, never NULL (as in `_leaf`). `part`: a
    live fact's partial columns (`_live_fact`), which the per-day partials then combine."""
    # "__" names: the by-key subquery also carries fact columns, which may be called a0 and the like
    leaf = lambda fn, a, fs, alias: exp.alias_(_leaf(replace(a, fn=fn), only(fs), plain, exp.false(), part), f"__{alias}", quoted=True)
    f = lambda alias: exp.column(f"__{alias}", src, quoted=True)
    inner, outer = [], {}
    for (fs, a), alias in aggs.items():
        if a.fn == "AVG":
            inner += [leaf("SUM", a, fs, alias + "s"), leaf("COUNT", a, fs, alias + "c")]
            outer[alias] = exp.Div(this=exp.Sum(this=f(alias + "s")), expression=exp.Sum(this=f(alias + "c")))
        else:  # COUNT partials are NULL where the FILTER keeps no rows, so the sum stays NULL there (SE-3)
            inner.append(leaf(a.fn, a, fs, alias))
            outer[alias] = AGG["SUM" if a.fn == "COUNT" else a.fn](this=f(alias))
            if a.fn == "COUNT" and top:  # no partial rows at all (a filter kept none): the grand total counts 0
                outer[alias] = exp.Coalesce(this=outer[alias], expressions=[exp.If(this=top, true=exp.convert(0))])
    return inner, outer


# SUM/MIN/MAX of no rows are NULL, as in DuckDB; a GROUPING SETS total's key is NULL, not the type's default; and
# `grouping` is 1 for a column the set leaves out (SQL standard), whatever the server profile says
LIVE = (" SETTINGS aggregate_functions_null_for_empty = 1, group_by_use_nulls = 1,"
        " force_grouping_standard_compatibility = 1")
INT = r"U?(TINY|SMALL|BIG)?INT(EGER)?"  # DuckDB integer types (HUGEINT has no wider ClickHouse sum)


def _is(types: dict, n, regex: str) -> bool:
    """Is `n` a column whose DuckDB type (`types`, a live table's {column: type}) matches `regex`?"""
    return isinstance(n, exp.Column) and re.fullmatch(regex, types.get(n.name, "")) is not None


def _live_read(t: str, q: exp.Select, live: dict, reads: dict) -> exp.Table:
    """Register ClickHouse query `q` on live table `t`; DuckDB reads its result as `__live<n>`, aliased `t`. Exact
    functions only and no silent wrap (principle 7): COUNT(DISTINCT) is uniqExact, MEDIAN quantileExactInclusive, an
    integer SUM adds Decimal(38, 0) (Int64 would wrap past 2^63), and an integer column under arithmetic is Int128
    (UInt32 - UInt32 would wrap below 0)."""
    types = live[t][1]
    wide = lambda n, *to: exp.Anonymous(this=to[0], expressions=[n, *map(exp.convert, to[1:])])  # keeps Nullable

    def fix(n):
        if isinstance(n, exp.Count) and isinstance(n.this, exp.Distinct):
            return exp.Anonymous(this="uniqExact", expressions=n.this.expressions)
        if isinstance(n, exp.Median):
            return exp.ParameterizedAgg(this="quantileExactInclusive", expressions=[exp.convert(0.5)], params=[n.this])
        if isinstance(n, exp.Anonymous) and n.name.upper() == "GROUPING_ID":
            return exp.Anonymous(this="grouping", expressions=n.expressions)
        if isinstance(n, exp.Sum) and _is(types, n.this, INT):
            return exp.Sum(this=wide(n.this, "toDecimal128", 0))
        up = n.parent
        while isinstance(up, exp.Paren):  # -(x) is Neg(Paren(x))
            up = up.parent
        if isinstance(up, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg)) and _is(types, n, INT):
            return wide(n, "toInt128")
        return n

    name = f"__live{len(reads)}"
    reads[name] = (t, q.transform(fix).sql("clickhouse") + LIVE)
    return exp.table_(name, quoted=True, alias=exp.to_identifier(t, quoted=True))


def _live_whole(t: str, q: exp.Select, live: dict, reads: dict) -> exp.Select:
    """A context with DISTINCT_COUNT/MEDIAN over live fact `t`'s own columns, run whole in ClickHouse (GROUPING SETS
    too). A DECIMAL MEDIAN (quantileExactInclusive takes no Decimal) reads the exact lower and upper middle values, and DuckDB's
    own MEDIAN of those two gives the value and type a DuckDB MEDIAN would."""
    q, out = q.copy(), []
    for e in q.expressions:
        if (m := e.find(exp.Median)) and _is(live[t][1], m.this, r"DECIMAL\(.*"):
            hi, a = e.copy(), e.alias
            for x, fn, side in ((e, "quantileExactLow", "lo"), (hi, "quantileExactHigh", "hi")):
                x.find(exp.Median).replace(exp.ParameterizedAgg(this=fn, expressions=[exp.convert(0.5)], params=[m.this.copy()]))
                x.set("alias", exp.to_identifier(f"__{side}_{a}", quoted=True))
            out.append((a, hi))
    q.select(*(hi for _, hi in out), copy=False)
    src = _live_read(t, q, live, reads)
    pick = lambda a, side, *t: exp.column(f"__{side}_{a}", *t, quoted=True)
    star = exp.Star(**{"except_": [pick(a, s) for a, _ in out for s in ("lo", "hi")]} if out else {})
    return exp.select(star, *(exp.alias_(exp.Anonymous(this="list_median", expressions=[exp.Array(
        expressions=[pick(a, "lo", t), pick(a, "hi", t)])]), a, quoted=True) for a, _ in out)).from_(src)


def _live_fact(c: Ctx, aggs: dict, fsets: list, kept: list, rels: dict, live: dict, reads: dict,
               lake: tuple | None = None) -> tuple:
    """(DuckDB table, partial column of (fn, mel.Col)) of a live fact's context: its rows grouped by every fact column
    the context reads, so each DuckDB condition, join and regrouping sees the values its fact rows had. `lake`: (more
    columns to group by, `shared`) for M3.6 shared partials of a lake fact: DuckDB SQL, registered in `shared`."""
    own = lambda col: exp.column(col.column, quoted=True)
    keys = dict.fromkeys([*(_col(d).column for _, d in kept if _col(d).table == c.fact),
                          *(x for r in rels.values() if r["from"]["table"] == c.fact for x in r["from"]["columns"]),
                          *(x.column for fs in fsets for f in fs for x in mel._walk(f)
                            if type(x) is mel.Col and x.table == c.fact), *(lake[0] if lake else ())])
    parts: dict[tuple, str] = {}  # (fn, column) -> partial alias
    for _, a in aggs:
        for fn in ("SUM", "COUNT") if a.fn == "AVG" else (a.fn,):
            parts.setdefault((fn, a.col.column), f"__p{len(parts)}")
    q = exp.select(*(exp.column(k, quoted=True) for k in keys), *(
        exp.alias_(AGG[fn](this=exp.column(col, quoted=True)), alias, quoted=True) for (fn, col), alias in parts.items()))
    # ponytail: a time context reads every day of the fact; push the members' date span down if live facts get long
    q = q.from_(exp.table_(c.fact, quoted=True) if lake else exp.to_table(live[c.fact][0], dialect="clickhouse"))
    # the rows any aggregate reads: each one's conditions on fact columns only (the rest is checked in DuckDB)
    pushed = list(dict.fromkeys(tuple(f for f in fs if {x.split(".")[0] for x in _cols(f)} <= {c.fact}) for fs in fsets))
    if all(pushed):
        q = q.where(exp.or_(*(exp.and_(*(_sql(f, own) for f in fs)) for fs in pushed)))
    if keys:
        q = q.group_by(*(exp.column(k, quoted=True) for k in keys))
    if lake:  # DuckDB sums integers as HUGEINT already, and sums those the same
        shared = lake[1]
        shared[name := f"__lake{len(shared)}"] = (c.fact, q.sql("duckdb"))
        return exp.table_(name, quoted=True, alias=exp.to_identifier(c.fact, quoted=True)), \
            lambda fn, col: exp.column(parts[fn, col.column], c.fact, quoted=True)
    ints = live[c.fact][1]

    def part(fn, col):  # an integer sum comes back as DECIMAL(38, 0); DuckDB sums integers as HUGEINT
        p = exp.column(parts[fn, col.column], c.fact, quoted=True)
        return exp.Cast(this=p, to=exp.DataType.build("HUGEINT")) if fn == "SUM" and _is(ints, exp.column(col.column), INT) else p
    return _live_read(c.fact, q, live, reads), part


def _live_table(t: str, rels: dict, wanted: set, live: dict, reads: dict) -> exp.Table:
    """A joined live table: only the columns the context uses (join keys, grouped and filtered columns), grouped."""
    cols = dict.fromkeys([*rels[t]["to"]["columns"],
                          *(x for r in rels.values() if r["from"]["table"] == t for x in r["from"]["columns"]),
                          *(_col(x).column for x in sorted(wanted) if _col(x).table == t)])
    cols = [exp.column(x, quoted=True) for x in cols]
    return _live_read(t, exp.select(*cols).from_(exp.to_table(live[t][0], dialect="clickhouse")).group_by(*cols),
                      live, reads)


def _leaf(a: mel.Agg, where, plain, top, part=None) -> exp.Expression:
    """Aggregate `a`, reading only the rows `where` keeps (None: all the query's rows). `top`: the condition for a
    grand-total row (falsy: the query has no GROUP BY). `part`: a live fact's partial columns (`_live_fact`)."""
    filt = lambda e: e if where is None else exp.Filter(this=e, expression=exp.Where(this=where.copy()))
    total = lambda fn: filt(exp.Sum(this=part(fn, a.col)))
    if part is None:
        agg = filt(exp.Count(this=exp.Distinct(expressions=[plain(a.col)])) if a.fn == "DISTINCT_COUNT"
                   else AGG[a.fn](this=plain(a.col)))
    elif a.fn == "AVG":
        agg = exp.Div(this=total("SUM"), expression=total("COUNT"))
    elif a.fn == "COUNT":  # 0, not NULL, over no rows, as COUNT is
        agg = exp.Coalesce(this=total("COUNT"), expressions=[exp.convert(0)])
    else:  # SUM of sums, MIN of minimums, MAX of maximums
        agg = filt(AGG[a.fn](this=part(a.fn, a.col)))
    if where is None:
        return agg
    if a.fn not in ("COUNT", "DISTINCT_COUNT") or not top:
        return agg
    # a group the other aggregates made but this one has no rows in is NULL, as if it had its own query; a
    # grand-total level (GROUPING SETS ()) always has its row, so COUNT stays 0 there
    has = exp.GT(this=exp.Filter(this=exp.Count(this=exp.Star()), expression=exp.Where(this=where)),
                 expression=exp.convert(0))
    return exp.If(this=exp.or_(has, top), true=agg)
