"""explain_change (1.2 M2.4): "why did it change?" in one call. A measure in two periods of the date table (default: the
latest complete one at a grain and the one before), in total and by dimensions: each member's two values, its change,
% change and share of the total change. Every read is a `semantic.query` as the caller (row-level and column security,
the SE-7 cache, query_log; the two periods are inline WHERE expressions, M2.1), one per dimension in parallel; the
arithmetic on its rows is code (principle 7), so every figure is a row (AGENTS.md rule 3) and nothing is saved (rule 7).
"""

import contextvars
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import Field

from abi import semantic, storage
from abi.actions import ActionFailed, action
from abi.security import Role, User
from abi.semantic import mel, rls
from abi.storage import COLUMN

COLUMNS = ["dimension", "member", "period_a", "period_b", "a", "b", "change", "change_pct", "share_pct"]
SHOWN, CHECKED, MEMBERS = 5, 12, 50  # default dimensions shown, checked at most, members a breakdown has at most
LABEL = 60  # characters of a member's text at most, without a line break: longer is a note or a description
STEP = {"day": 1, "week": 7, "month": 31, "quarter": 92, "year": 366}  # days from a period's start into the next one
PARALLEL = 4  # reads at a time (each also runs on DuckDB's own threads)


def start(d: date, grain: str) -> date:
    """The first day of the `grain` period that holds day `d` (ISO weeks start on Monday)."""
    return {"day": d, "week": d - timedelta(d.weekday()), "month": d.replace(day=1),
            "quarter": d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1), "year": d.replace(month=1, day=1)}[grain]


def after(s: date, grain: str) -> date:
    """The first day of the period after the one that starts on `s`."""
    return start(s + timedelta(STEP[grain]), grain)


def label(s: date, grain: str) -> str:
    """The period holding `s` as explain_change takes and shows it: 2025-11-30, 2025-W48, 2025-11, 2025-Q4, 2025."""
    return {"day": s.isoformat(), "week": "%d-W%02d" % tuple(s.isocalendar()[:2]), "month": f"{s:%Y-%m}",
            "quarter": f"{s.year}-Q{(s.month + 2) // 3}", "year": str(s.year)}[grain]


def period(text: str, grain: str) -> date:
    """The first day of the `grain` period `text` names, written as `label` writes it; 400 for anything else."""
    try:
        y, w, q, m, d = re.fullmatch(r"(\d{4})(?:-W(\d\d)|-Q(\d)|-(\d\d)(?:-(\d\d))?)?", text).groups()
        s = date.fromisocalendar(int(y), int(w), 1) if w else date(int(y), int(m or 3 * int(q or 1) - 2), int(d or 1))
    except (AttributeError, ValueError):  # no match, or no such day
        s = None
    if s is None or label(s, grain) != text:
        raise ActionFailed(400, f"'{text}' is not a {grain}: write it like {label(date(2025, 11, 30), grain)}.")
    return s


def additive(n) -> bool:
    """The measure is 0 over no rows (SUMs and COUNTs, added or subtracted), so a member without rows in one period is 0
    there, not unknown: a café that closed holds its whole November in the change."""
    t = type(n)
    if t is mel.Agg:
        return n.fn in ("SUM", "COUNT", "DISTINCT_COUNT")
    if t is mel.Op:
        return n.op in ("+", "-", "NEG") and all(map(additive, n.args))
    return t in (mel.Where, mel.Ignore) and additive(n.expr)


def candidates(project: str, user_id: int, tree, tables: set | None) -> list[str]:
    """The default dimensions: text and yes/no columns of the measure's facts and of the tables each reaches by exactly
    one relationship (what a query copy folds in), never a key, the date table, a staging table, a column hidden from
    the user or a table outside the named model (`tables`); the first CHECKED in the model's column order."""
    from abi.semantic import model  # not at top: model imports this package

    m = model.visible(model.load(project))
    rels = m["relationships"]
    keys = {f"{t}.{c}" for t, i in m["tables"].items() for c in i.get("key") or []} | {
        f"{r[s]['table']}.{c}" for r in rels for s in ("from", "to") for c in r[s]["columns"]}

    def near(fact: str) -> set[str]:
        to = [r["to"]["table"] for r in rels if r["from"]["table"] == fact]
        return {fact, *(t for t in to if to.count(t) == 1)}

    facts = {n.col.table for n in mel._walk(tree) if type(n) is mel.Agg}
    reach = (set.intersection(*map(near, facts)) if facts else set()) & m["tables"].keys() - {(m["date_table"] or {}).get("table")}
    hid = rls.hidden(project, user_id)
    # ponytail: the first CHECKED columns only (one query each); rank more by their names if a wide model misses drivers
    return [f"{t}.{c}" for t, cs in model.lake_columns(project).items() if t in reach and (tables is None or t in tables)
            for c, ty in cs.items() if ty in ("VARCHAR", "BOOLEAN") and f"{t}.{c}" not in keys
            and c.lower() not in hid.get(t, ())][:CHECKED]


class Explain(storage.ProjectRef):
    measure: str = Field(description="the measure ID to explain (e.g. revenue; not a *_change or other time-intelligence measure)")
    period: Literal["day", "week", "month", "quarter", "year"] = Field("month", description="the grain of the two periods")
    a: str | None = Field(None, description="the earlier period, written like 2025-11 (a month), 2025-Q4, 2025-W48, 2025-11-30 or 2025; default: the one before b")
    b: str | None = Field(None, description="the later period; default: the latest complete one (the data reaches its last day)")
    dimensions: list[Annotated[str, Field(pattern=COLUMN)]] = Field([], max_length=10, description="table.column to break the change down by, in this order; default: the model's text dimensions where the change sits most")
    filters: list[semantic.Filter] = Field([], description="row filters for both periods; all must hold")
    top: int = Field(3, ge=1, le=20, description="members per dimension, largest change first")
    model: str | None = Field(None, description="a named model of the project (semantic_model lists them): only its tables and measures")


@action(Role.viewer)
def explain_change(conn, user: User, args: Explain) -> dict:
    """Explains why a measure changed between two periods, in one call: its total in each and, broken down by dimensions, the members whose change is largest. Read as you (row-level security applies), computed now, never saved. Periods: by default the latest complete `period` (a month; complete when the data reaches its last day) against the one before; `a` and `b` name others (2025-11, 2025-Q4, 2025-W48, 2025-11-30, 2025). Dimensions: by default the model's text dimensions, those where the change sits most first (one member moved far from the total's rate), the rest named in `also_checked`. Rows: the total first, then each dimension's top members: dimension, member, period_a, period_b, a and b (the measure in each period), change (b - a), change_pct (a fraction of a: -0.25 is -25%) and share_pct (the member's share of the total change, a fraction: 0.8 is 80%). `partial` names the dimensions with more members than their top rows (the others aren't shown). These rows hold the figures a "why" needs: quote them, and name members, never count them (no row holds a count)."""
    from abi.semantic import model  # not at top: model imports this package

    p, m, grain = args.project, args.measure, args.period
    ms = model._read(p, "measures.json").get("measures", [])
    try:
        tree = mel.compile({x["id"]: x["expression"] for x in ms}).get(m)
    except mel.MelError as e:
        raise ActionFailed(400, str(e))
    if tree is None:
        raise ActionFailed(400, f"Unknown measure {m}; use one of: {', '.join(sorted(x['id'] for x in ms))}.")
    if fn := next((n.fn for n in mel._walk(tree) if type(n) is mel.Time), None):
        raise ActionFailed(400, f"Measure '{m}' uses {fn}, and explain_change compares the periods itself: give the measure"
                                " it steps back from (revenue, not revenue_change).")
    if not (dt := model._read(p, "model.json").get("date_table")):
        raise ActionFailed(400, "explain_change compares periods of the date table, and the model has none: set_date_table sets one.")
    day, scope = f"{dt['table']}.{dt['column']}", [f.model_dump(mode="json") for f in args.filters]
    if args.model:
        model.in_scope(p, args.model, [m], [*args.dimensions, *(f["column"] for f in scope)])
    if args.b:
        b = period(args.b, grain)
    else:  # the latest day with rows of the measure, as this person reads them
        rows = semantic.query(p, {"measures": [m], "dimensions": [day], "filters": scope,
                                  "order_by": [{"field": day, "desc": True}], "limit": 1}, user.id)["rows"]
        if not rows or rows[0][0] is None:
            raise ActionFailed(400, f"No rows of {m} to compare{' with these filters' if scope else ''}.")
        last = date.fromisoformat(str(rows[0][0])[:10])
        b = start(last, grain)
        if after(b, grain) - timedelta(1) > last:  # the data doesn't reach the period's last day: it may still be filling
            b = start(b - timedelta(1), grain)
    a = period(args.a, grain) if args.a else start(b - timedelta(1), grain)
    if a == b:
        raise ActionFailed(400, f"a and b are the same {grain}: name two different periods.")
    col = ".".join('"' + x.replace('"', '""') + '"' for x in (dt["table"], dt["column"]))
    within = lambda s: f"WHERE([{m}], {col} >= '{s}' AND {col} < '{after(s, grain)}')"  # noqa: E731
    exprs = [{"name": "__a", "expr": within(a)}, {"name": "__b", "expr": within(b)}]  # never a measure ID (those start with a letter)
    span = [{"column": day, "op": ">=", "value": str(min(a, b))},
            {"column": day, "op": "<", "value": str(max(after(a, grain), after(b, grain)))}]
    dims = args.dimensions or candidates(p, user.id, tree, model.scope(p, args.model)[0] if args.model else None)

    def read(d: str | None) -> list:
        return semantic.query(p, {"measures": [], "dimensions": [d] if d else [], "filters": scope + span}, user.id, exprs)["rows"]

    with ThreadPoolExecutor(PARALLEL) as pool:  # each read in its own copy of this call's context (VIA, inbound, share)
        got = [f.result() for f in [pool.submit(contextvars.copy_context().run, read, d) for d in [None, *dims]]]
    la, lb, zero = label(a, grain), label(b, grain), additive(tree)

    def row(dim, member, x, y, total) -> list:
        x, y = (0 if v is None and zero else v for v in (x, y))
        ch = None if x is None or y is None else y - x
        return [dim, member, la, lb, x, y, ch, semantic.tidy(float(ch) / abs(float(x))) if ch is not None and x else None,
                semantic.tidy(float(ch) / float(total)) if ch is not None and total else None]

    total = row("total", None, *(got[0][0] if got[0] else (None, None)), None)
    total[-1] = 1.0 if (td := total[6]) else None
    rate = float(td) / float(total[4]) if td is not None and total[4] else 0  # the total's change, a fraction of a
    blocks = []
    for d, rs in zip(dims, got[1:]):
        out = sorted((row(d, *r, td) for r in rs), key=lambda x: (x[6] is None, -abs(x[6] or 0)))
        # how far one member's change departs from the total's rate: where the change sits, not which member is big
        sits = max((abs(float(x[6]) - rate * float(x[4])) for x in out if x[6] is not None and x[4] is not None), default=0)
        blocks.append((d, out, sits))
    if not args.dimensions:  # the handful where the change sits most; one member, a name per row or prose is no breakdown
        blocks = sorted((x for x in blocks if 2 <= len(x[1]) <= MEMBERS and all(
            not isinstance(r[1], str) or len(r[1]) <= LABEL and "\n" not in r[1] for r in x[1])), key=lambda x: -x[2])[:SHOWN]
    name = next((x.get("name") or m for x in ms if x["id"] == m), m)
    shown = [d for d, _, _ in blocks]
    return {"measure": m, "dimensions": shown, "partial": [d for d, out, _ in blocks if len(out) > args.top],
            "also_checked": [d for d in dims if d not in shown], "columns": COLUMNS,
            "rows": [total] + [r for _, out, _ in blocks for r in out[:args.top]],
            "measures": {"a": f"{name}, {la}", "b": f"{name}, {lb}", "change": f"{name} change", "change_pct": f"{name} % change",
                         "share_pct": "share of the total change"},
            "filters": scope,  # AG-2: the members it filtered on ground the answer's names, as semantic_query's do
            **semantic.locale(p)}
