"""explain_change (1.2 M2.4): "why did it change?" in one call. A measure in two periods of the date table (default: the
latest complete one at a grain and the one before), in total and by dimensions: each member's two values, its change,
% change and share of the total change. Every read is a `semantic.query` as the caller (row-level and column security,
the SE-7 cache, query_log; the two periods are inline WHERE expressions, M2.1), one per dimension in parallel; the
arithmetic on its rows is code (principle 7), so every figure is a row (AGENTS.md rule 3) and nothing is saved (rule 7).
"""

import contextvars
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from abi import semantic, storage
from abi.actions import ActionFailed, action
from abi.security import Role, User
from abi.semantic import mel, planner, rls
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


def complete(last: date, grain: str, today: date) -> date:
    """The first day of the latest complete `grain` period of data whose last day with rows is `last`: the data reaches
    the period's last day, or the period is over (`today`, the project's) and the data reaches into its last days, 2
    for a week, 3 otherwise (M2 review: a shop closed at weekends or on the 31st lost its latest month); else it may
    still be filling, and the one before is."""
    b = start(last, grain)
    end = after(b, grain) - timedelta(1)
    if end > last and not (today > end and (end - last).days <= (2 if grain == "week" else 3)):
        b = start(b - timedelta(1), grain)
    return b


def additive(n, fns: tuple = ("SUM", "COUNT", "DISTINCT_COUNT")) -> bool:
    """The measure is 0 over no rows (SUMs and COUNTs, added or subtracted), so a member without rows in one period is 0
    there, not unknown: a café that closed holds its whole November in the change. With `fns` ("SUM", "COUNT") its
    members' changes also add up to the total's, so each has a share of it (M2 review: a ratio's, an average's or a
    distinct count's don't: a customer of two regions counts in each)."""
    t = type(n)
    if t is mel.Agg:
        return n.fn in fns
    if t is mel.Op:
        return n.op in ("+", "-", "NEG") and all(additive(a, fns) for a in n.args)
    return t in (mel.Where, mel.Ignore) and additive(n.expr, fns)


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
    b: str | None = Field(None, description="the later period; default: the latest complete one (the data reaches its last day, or the period is over and the data reaches into its last days)")
    dimensions: list[Annotated[str, Field(pattern=COLUMN)]] = Field([], max_length=10, description="table.column to break the change down by, in this order; default: the model's text dimensions where the change sits most")
    filters: list[semantic.Filter] = Field([], description="row filters for both periods (not on the date table: a and b are the periods); all must hold")
    top: int = Field(3, ge=1, le=20, description="members per dimension, largest change first")
    model: str | None = Field(None, description="a named model of the project (semantic_model lists them): only its tables and measures")


@action(Role.viewer)
def explain_change(conn, user: User, args: Explain) -> dict:
    """Explains why a measure changed between two periods, in one call: its total in each and, broken down by dimensions, the members whose change is largest. Read as you (row-level security applies), computed now, never saved. Periods: by default the latest complete `period` (a month; complete when the data reaches its last day, or the month is over and the data reaches into its last days) against the one before; `a` and `b` name others (2025-11, 2025-Q4, 2025-W48, 2025-11-30, 2025). Dimensions: by default the model's text dimensions where the change sits most (one member moved far from the total's rate), the strongest first; the rest named in `also_checked`. Rows: the total first, then each dimension's top members: dimension, member, period_a, period_b, a and b (the measure in each period), change (b - a), change_pct (a fraction of a: -0.25 is -25%) and share_pct (the member's share of the total change, a fraction: 0.8 is 80%; empty for a ratio, an average or a distinct count, whose members don't add up to the total). `partial` names the dimensions with more members than their top rows; for a sum or count each has a last row "(others)": the members not shown, added up, so its rows add up to the total. These rows hold the figures a "why" needs: quote them, and name members, never count them (no row holds a count)."""
    from abi.semantic import model  # not at top: model imports this package

    p, m, grain, loc = args.project, args.measure, args.period, semantic.locale(args.project)
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
    if on := [f["column"] for f in scope if f["column"].split(".", 1)[0] == dt["table"]]:  # M2 review: it emptied period a
        raise ActionFailed(400, f"explain_change compares the periods a and b itself: leave {', '.join(on)} out of the"
                                " filters and name the periods with a and b.")
    if args.model:
        model.in_scope(p, args.model, [m], [*args.dimensions, *(f["column"] for f in scope)])
    if args.b:
        b = period(args.b, grain)
    else:  # the latest day with rows of the measure, as this person reads them
        rows = semantic.query(p, {"measures": [m], "dimensions": [day], "filters": scope, "order_by": [
            {"field": day, "desc": True}], "limit": 1}, user.id, timeout=semantic.TIMEOUT)["rows"]
        if not rows or rows[0][0] is None:
            raise ActionFailed(400, f"No rows of {m} to compare{' with these filters' if scope else ''}.")
        b = complete(date.fromisoformat(str(rows[0][0])[:10]), grain, datetime.now(ZoneInfo(loc["time_zone"])).date())
    a = period(args.a, grain) if args.a else start(b - timedelta(1), grain)
    if a == b:
        raise ActionFailed(400, f"a and b are the same {grain}: name two different periods.")
    zero, sums = additive(tree), additive(tree, ("SUM", "COUNT"))
    col = ".".join('"' + x.replace('"', '""') + '"' for x in (dt["table"], dt["column"]))
    within = lambda s: f"WHERE([{m}], {col} >= '{s}' AND {col} < '{after(s, grain)}')"  # noqa: E731
    fill = lambda x: f"IF({x} IS NULL, 0, {x})" if zero else x  # noqa: E731
    exprs = [{"name": "__a", "expr": within(a)}, {"name": "__b", "expr": within(b)},  # never a measure ID (those start with a letter)
             {"name": "__d", "expr": f"ABS({fill(within(b))} - {fill(within(a))})"}]  # the size of the change, to rank by
    span = [{"column": day, "op": ">=", "value": str(min(a, b))},
            {"column": day, "op": "<", "value": str(max(after(a, grain), after(b, grain)))}]
    dims = list(dict.fromkeys(args.dimensions)) or candidates(p, user.id, tree, model.scope(p, args.model)[0] if args.model else None)
    # 1.2.6 M5: members reached through a many-to-many link overlap, so they don't add up to the total (Owner answer 4)
    mj, facts = model._read(p, "model.json"), {n.col.table for n in mel._walk(tree) if type(n) is mel.Agg}
    links = planner.links(mj, model._read(p, "ontology.json"))
    wide = [d for d in dims if any(planner.many(mj, f, d.split(".", 1)[0], links) for f in facts)]
    if wide and args.dimensions:
        raise ActionFailed(400, f"explain_change breaks a change into members that add up to the total; {wide[0]} is reached"
                                " through a many-to-many link, whose members overlap. Ask semantic_query for its values.")
    dims = [d for d in dims if d not in wide]

    def read(d: str | None) -> list:  # M2 review: bounded, not every member of a dimension into Python
        q = {"measures": [], "dimensions": [d] if d else [], "filters": scope + span}
        if d:  # asked: the largest changes and one more (`partial`); a default with more than MEMBERS members is dropped
            q |= {"order_by": [{"field": "__d", "desc": True}], "limit": args.top + 1} if args.dimensions else {"limit": MEMBERS + 1}
        return semantic.query(p, q, user.id, exprs, strict=False, timeout=semantic.TIMEOUT)["rows"]

    with ThreadPoolExecutor(PARALLEL) as pool:  # each read in its own copy of this call's context (VIA, inbound, share)
        got = [f.result() for f in [pool.submit(contextvars.copy_context().run, read, d) for d in [None, *dims]]]
    la, lb = label(a, grain), label(b, grain)

    def row(dim, member, x, y, total) -> list:
        x, y = (0 if v is None and zero else semantic.tidy(v) for v in (x, y))
        ch = None if x is None or y is None else semantic.tidy(y - x)
        return [dim, member, la, lb, x, y, ch, semantic.tidy(float(ch) / abs(float(x))) if ch is not None and x else None,
                semantic.tidy(float(ch) / float(total)) if sums and ch is not None and total else None]

    total = row("total", None, *(got[0][0][:2] if got[0] else (None, None)), None)
    td = total[6]
    total[-1] = 1.0 if sums and td else None
    rate = float(td) / float(total[4]) if td is not None and total[4] else 0  # the total's change, a fraction of a
    blocks = []
    for d, rs in zip(dims, got[1:]):
        out = sorted((row(d, *r[:3], td) for r in rs), key=lambda x: (x[6] is None, -abs(x[6] or 0)))
        # how far one member's change departs from the total's rate: where the change sits, not which member is big
        sits = max((abs(float(x[6]) - rate * float(x[4])) for x in out if x[6] is not None and x[4] is not None), default=0)
        blocks.append((d, out, sits))
    if not args.dimensions:  # the handful where the change sits most; one member, a name per row or prose is no breakdown
        blocks = sorted((x for x in blocks if 2 <= len(x[1]) <= MEMBERS and all(
            not isinstance(r[1], str) or len(r[1]) <= LABEL and "\n" not in r[1] for r in x[1])), key=lambda x: -x[2])[:SHOWN]
        # M2 review: a third of the leader's or more; a weaker one is noise answers promoted (card payments, drawn per order)
        blocks = [x for x in blocks if 3 * x[2] >= blocks[0][2]]
    rows = [total]
    for d, out, _ in blocks:
        rows += out[:args.top]
        if sums and len(out) > args.top:  # M2 review: the members not shown, so "every" or "the only" can be checked
            rows.append(row(d, "(others)", *(total[i] - sum(r[i] for r in out[:args.top]) for i in (4, 5)), td))
    name = next((x.get("name") or m for x in ms if x["id"] == m), m)
    shown = [d for d, _, _ in blocks]
    return {"measure": m, "dimensions": shown, "partial": [d for d, out, _ in blocks if len(out) > args.top],
            "also_checked": [d for d in dims if d not in shown], "columns": COLUMNS, "rows": rows,
            "measures": {"a": f"{name}, {la}", "b": f"{name}, {lb}", "change": f"{name} change", "change_pct": f"{name} % change",
                         "share_pct": "share of the total change" if sums else
                         "share of the total change (none: this measure's members don't add up to its total)"},
            "filters": scope,  # AG-2: the members it filtered on ground the answer's names, as semantic_query's do
            **loc}
