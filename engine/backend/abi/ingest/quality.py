"""IN-8 data-quality checks per source (E-10.1): freshness, missing periods, volume drop, NULL-share rise.

History is one `quality_loads` row per successful load (rows, NULL count per column), captured after the load
committed (`capture`, never raises). Every data read goes through `semantic.execute` (principle 4); every number in a
result comes from these reads (principle 7). Thresholds are fixed (work/validation/E-10.md).
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from statistics import median

from sqlglot import exp

from abi import actions, db, flows, semantic, transform
from abi.actions import action
from abi.agent import notify
from abi.project import git
from abi.project.bundle import loads
from abi.project.git import ProjectRef
from abi.security import Role, User, credentials
from abi.semantic import live, model, planner, rls
from abi.semantic.subscriptions import fingerprint
from abi.storage import _q

DAILY_STALE_H = 26  # a daily load plus IN-6's 1 h maximum back-off, and slack
HISTORY, MIN_HISTORY = 10, 3  # earlier loads compared against, and the fewest that count as a history
VOLUME_DROP, NULL_RISE = 0.5, 0.2  # rows below half the median; NULL share up >= 20 points
PERIODS, GAP_RANGES = 400, 20  # ponytail: missing periods only over the last 400 periods, as at most 20 ranges
OK_DATES = "BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'"  # LESSONS CAL-EXTEND: no infinity/BC/5-digit years
COLUMNS = ["table", "refresh", "monitored", "last_load", "rows", "fresh", "period", "missing_periods", "gaps",
           "volume_drop", "null_rise", "problems"]


def capture(project: str, table: str) -> None:
    """Store the load's stats. A side step after the load committed: any failure only goes to the server log."""
    try:
        cols = list(model.lake_columns(project).get(table, {}))
        counts = ", ".join(f"count({_q(c)})" for c in cols)
        [(n, *have)] = semantic.execute(project, f"SELECT count(*){', ' if cols else ''}{counts} FROM {_q(table)}",
                                        {"quality_stats": table})
        with db.connect() as c:
            c.execute("INSERT INTO quality_loads (project, table_name, row_count, nulls)"
                      " VALUES (%s, %s, %s, %s)", [project, table, n,
                                                       json.dumps({c: n - h for c, h in zip(cols, have)})])
    except Exception as e:  # noqa: BLE001 - IN-8 must never fail a load (LESSONS CAL-EXTEND)
        logging.getLogger(__name__).error("Load stats of %s/%s not stored: %s", project, table, type(e).__name__)
    watch(project, [table])


def _shift(d: date, period: str, n: int) -> date:
    if period == "month":
        m = d.year * 12 + d.month - 1 + n
        return date(m // 12, m % 12 + 1, 1)
    return d + timedelta(days=n * (7 if period == "week" else 1))


def gaps(dates: list[date]) -> tuple[str | None, list[list[str]], int]:
    """Period (month if every date is a 1st, week if all share a weekday, else day), the missing ranges [from, to]
    over the last PERIODS periods up to the latest date (at most GAP_RANGES), and the total missing periods."""
    if not dates:
        return None, [], 0
    have = set(dates)
    period = ("month" if all(d.day == 1 for d in have) else "week" if len({d.weekday() for d in have}) == 1 else "day")
    hi, lo = max(have), min(have)
    grid = [d for d in (_shift(hi, period, -i) for i in range(PERIODS - 1, -1, -1)) if d >= lo]
    ranges: list[list[date]] = []
    for prev, d in zip([None, *grid], grid):
        if d in have:
            continue
        if ranges and prev == ranges[-1][1]:
            ranges[-1][1] = d
        else:
            ranges.append([d, d])
    total = sum(d not in have for d in grid)
    return period, [[a.isoformat(), b.isoformat()] for a, b in ranges[-GAP_RANGES:]], total


def _dates(project: str, t: str, m: dict) -> list[date]:
    """The distinct dates (latest PERIODS + 1) of `t`'s column related to the date table (none without one)."""
    dt = (m["date_table"] or {}).get("table")
    col = next((r["from"]["columns"][0] for r in m["relationships"]
                if r["from"]["table"] == t and r["to"]["table"] == dt), None)
    if not dt or not col:
        return []
    # SE-13: the local day the date-table joins see; PERIODS + 1 distinct dates always cover the last PERIODS periods
    days = planner.local(m, model.lake_columns(project), semantic.locale(project)["time_zone"])
    d = planner.day(exp.column(col, quoted=True), days.get((t, col))).sql("duckdb")
    return [r[0] for r in semantic.execute(project, f"SELECT DISTINCT {d} FROM {_q(t)} WHERE {d} {OK_DATES}"
                                                    f" ORDER BY 1 DESC LIMIT {PERIODS + 1}", {"quality_periods": t})]


def history(hist: list[dict]) -> tuple[list | None, list[list]]:
    """Latest load against the median of up to HISTORY earlier ones (MIN_HISTORY needed): the volume drop as
    [rows, median rows] (None: no drop or no history), and [column, share, median share] per NULL-share rise."""
    last, before = hist[-1], hist[-HISTORY - 1:-1]
    if len(before) < MIN_HISTORY:
        return None, []
    base = median(r["row_count"] for r in before)
    drop = [last["row_count"], base] if last["row_count"] < VOLUME_DROP * base else None
    share = lambda r, c: r["nulls"][c] / r["row_count"] if r["row_count"] else 0.0
    rise = []
    for c in last["nulls"]:
        old = [share(r, c) for r in before if c in r["nulls"]]
        if len(old) >= MIN_HISTORY and share(last, c) - median(old) >= NULL_RISE:
            rise.append([c, round(share(last, c), 4), round(median(old), 4)])
    return drop, rise


def _schedule(project: str, t: str, steps: dict) -> tuple[str, int | None]:
    """The refresh of `t`'s source and its freshness limit in hours (None: no schedule)."""
    f = git.path(project) / "sources" / f"{steps[t][0].source if t in steps else t}.json"
    doc = loads(f.read_text()) if f.is_file() else {}
    refresh = doc.get("refresh", "on_change")
    if hours := (doc.get("api") or {}).get("every_hours"):  # API-SRC.2: plus the 1 h back-off, and slack
        return refresh, hours + DAILY_STALE_H - 24
    return refresh, DAILY_STALE_H if refresh == "daily" else (doc.get("quality") or {}).get("fresh_hours")


def check(conn, project: str, t: str, m: dict, steps: dict, inputs: set, loaded: set) -> list:
    """One `COLUMNS` row for table `t`."""
    if t in inputs or live.is_live(project, t):  # IN-8 is per copied source: no load, no history
        return [t, "input" if t in inputs else "live", False, *[None] * 7, [], []]
    refresh, limit = _schedule(project, t, steps)
    hist = conn.execute("SELECT * FROM (SELECT row_count, nulls, loaded_at, id FROM quality_loads WHERE project = %s"
                        " AND table_name = %s ORDER BY id DESC LIMIT %s) h ORDER BY id",
                        [project, t, HISTORY + 1]).fetchall()
    last = hist[-1]["loaded_at"] if hist else None
    fresh = None if limit is None else last is not None and datetime.now(UTC) - last <= timedelta(hours=limit)
    problems = []
    if fresh is False:
        problems.append(f"no successful load in the last {limit} h (last: {last.isoformat() if last else 'never'})")
    period, ranges, missing = gaps(_dates(project, t, m)) if t in loaded else (None, [], 0)
    if missing:
        problems.append(f"{missing} missing {period}(s) in the date column, latest {ranges[-1][0]} to {ranges[-1][1]}")
    drop, rise = history(hist) if hist else (None, [])
    if drop:
        problems.append(f"{drop[0]} rows, below half the median ({drop[1]}) of the previous loads")
    problems += [f"NULL share of {c} rose to {s:.0%} (median {o:.0%})" for c, s, o in rise]
    return [t, refresh, True, last and last.isoformat(), hist[-1]["row_count"] if hist else None, fresh, period,
            missing, ranges, drop, rise, problems]


def _rows(conn, project: str, only: list[str] | None = None, scheduled: bool = False) -> list[list]:
    """`COLUMNS` rows of every table of the project (sources registry, source files, input tables), or of `only`;
    `scheduled`: only tables with a freshness limit."""
    try:
        steps = transform.tables(project)
    except ValueError:  # a broken tables/<id>.json: its table still shows from the registry
        steps = {}
    inputs = transform.inputs(project)
    names = {r["table_name"] for r in conn.execute("SELECT table_name FROM sources WHERE project = %s", [project])}
    names |= {f.stem for f in (git.path(project) / "sources").glob("*.json")} | set(steps) | inputs
    m, loaded = model.load(project), set(model.lake_columns(project))
    names = (names & set(only)) if only is not None else names
    return [check(conn, project, t, m, steps, inputs, loaded) for t in sorted(names)
            if not scheduled or _schedule(project, t, steps)[1] is not None]


@action(Role.viewer)
def data_quality(conn, user: User, args: ProjectRef) -> dict:
    """IN-8: per table of the project: last successful load, freshness (true/false; null: no schedule), the date column's period and missing periods (`gaps`: [from, to] ranges, `missing_periods`: total), a volume drop ([rows, median rows of earlier loads]), NULL-share rises ([column, share, median share]) and plain `problems`. Live and input tables are not monitored. Quote numbers only from `rows`."""
    rows = _rows(conn, args.project)
    if rls.restricted(args.project, user.id):  # SE-6: counts, dates and problems come from every row
        rows = [r[:4] + [r[4] and rls.HIDDEN, r[5]] + [x and rls.HIDDEN for x in r[6:]] for r in rows]
    return {"columns": COLUMNS, "rows": rows}


def watch(project: str, only: list[str] | None = None) -> None:
    """IN-8 (E-10.2), after a load (`only` that table) and every minute (None: the tables that can go stale, since
    the other checks change only with a load): a table's new problem text posts once to the feed and its recovery
    once (D-10), each post audited as worker (SC-6). A new problem first starts each enabled `data_quality` flow on
    that table, keyed by the last post and the new text, so a crash before the post repeats the same run keys (FL-4
    dedups them). After a flow-made load it posts but starts nothing (D-23). Never raises."""
    # ponytail: a tick racing a load's check can post one text twice; a per-table advisory lock if that shows up
    by_flow = actions.VIA.get() == "flow" or flows.IN_FLOW.get()
    try:
        with db.connect() as conn:
            rows = [r for r in _rows(conn, project, only, scheduled=only is None) if r[2]]
        alerts = [f for f in flows._enabled(project) if f["trigger"]["type"] == "data_quality"]
        for t, *_, problems in rows:
            msg = f"Data quality of '{t}': " + ("; ".join(problems) if problems else "is fine again.")
            with db.connect() as conn:
                last = conn.execute("SELECT id, message FROM notifications WHERE project = %s AND kind = 'quality'"
                                    " AND ref = %s ORDER BY id DESC LIMIT 1", [project, t]).fetchone()
                if (last and last["message"]) == msg or not (problems or last):
                    continue
                for f in [] if by_flow or not problems else alerts:
                    if t in f["trigger"].get("tables", [t]):
                        flows.start(project, f["id"], f"quality-{t}-{last['id'] if last else 0}-{fingerprint(msg)[:16]}",
                                    {"table": t, "problems": problems, "message": msg})
                # SE-6: problems quote ranges and counts of every row, so restricted Viewers see only the head
                notify("quality", msg, project, t, len(f"Data quality of '{t}': ") if problems else len(msg))
                actions.audit(conn, None, "worker", "check_quality", project, {"table": t}, 200, msg)  # SC-6
    except Exception as e:  # noqa: BLE001 - a side step after a load, and one project must not stop the tick
        logging.getLogger(__name__).error("Data-quality check of %s failed: %s", project, credentials.log_text(e))
