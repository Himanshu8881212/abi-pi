"""AG-2 grounding check (PRD §3 "Grounding check", §5 #5, principle 7, DECISIONS D-22): the rules are
abi.agents.grounding_rules (pure, stdlib only, shared with the harness's scripts/ground.py); here are the engine's own
vocabulary for `check` (MEL's words, the action names) and the reads on the server: `filtered()` (the filter values
that exist in the data), `scope()` (the model's own vocabulary and the in-scope text dimension members), `today()`;
and `template()`, the fallback answer built from the results.
"""
import contextlib
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import duckdb

from abi.actions import ACTIONS, ActionFailed
from abi.agents import grounding_rules as rules
from abi.agents.grounding_rules import DAYS, MARK, _has, _query, mask, numbers  # noqa: F401 (callers use grounding.*)

rules.ENGINE = True  # the engine's own rules (AGENT-CONTEXT-fix-9)


def check(*args, **kw) -> dict:
    """`grounding_rules.check` with the engine's vocabulary: MEL's words ground a name, an action name spelled out too."""
    from abi.semantic import mel  # not at top: abi.semantic imports the agent package

    return rules.check(*args, **kw, terms=(*mel.USAGE, *mel.CONDITION), actions=tuple(ACTIONS))


def filtered(project: str, user_id: int | None, results: list) -> list:
    """LLM-2-fix-2: the text values of the `=`/`in` filters of query results that exist in their column (read as
    `user_id` through the semantic engine, so RLS applies): pass them to `check` with the results. A value no row of
    the column holds is never returned (an invented member stays ungrounded); a measure's total proves nothing
    (IGNORE/WHERE measures). Live (ClickHouse) columns and failed reads ground nothing.
    ABI-PI: a number or date filter on the date table (calendar.year = 2025, calendar.day >= '2025-12-01') that exists
    there comes back as a result row, so "revenue in 2025" or "in December 2025" grounds like a member; a range's end
    never does, and measure figures still come only from result rows."""
    from abi import semantic, storage
    from abi.semantic import live, model

    cal = ((model._read(project, "model.json") or {}).get("date_table") or {}).get("table")
    num = lambda v: v if isinstance(v, str) else str(int(v)) if float(v).is_integer() else str(v)  # noqa: E731 (as DuckDB casts it)
    asked: dict[str, set[str]] = {}
    dates: dict[str, set] = {}
    for r in results:
        for f in r["filters"] if _query(r) else []:
            if not (isinstance(f, dict) and isinstance(f.get("column"), str) and "." in f["column"]):
                continue
            on_cal = f["column"].split(".", 1)[0] == cal
            if on_cal and f.get("op") == ">=" and isinstance(f.get("value"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["value"]):
                dates.setdefault(f["column"], set()).add(f["value"])  # a date range's start ("December 2025"); never its end
            vals = f.get("value") if f.get("op") in ("=", "in") else None
            for v in vals if isinstance(vals, list) else [vals]:
                if isinstance(v, str):
                    asked.setdefault(f["column"], set()).add(v)
                    if on_cal and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                        dates.setdefault(f["column"], set()).add(v)
                elif isinstance(v, int | float) and not isinstance(v, bool) and on_cal:
                    dates.setdefault(f["column"], set()).add(v)

    def held(col: str, vals: set[str]) -> list[str]:
        t, c = col.split(".", 1)
        if live.is_live(project, t):
            return []
        x = f"CAST({storage._q(c)} AS VARCHAR)"
        sql = f"SELECT DISTINCT {x} FROM {storage._q(t)} WHERE {x} IN (" + ", ".join(
            "'" + storage._lit(v) + "'" for v in sorted(vals)) + ")"
        with contextlib.suppress(ActionFailed, duckdb.Error):  # a refused or failed read only means nothing grounds
            return [r[0] for r in semantic.execute(project, sql, {"grounding": col}, user_id)]
        return []
    out: list = []
    for col, vals in asked.items():
        out += held(col, vals)
    for col, vals in dates.items():
        found = set(held(col, {num(v) for v in vals}))
        if rows := [[v] for v in sorted(vals, key=str) if num(v) in found]:
            out.append({"columns": [col], "rows": rows})
    return out


def today(project: str | None) -> tuple[str, date]:
    """GATE-9-fix-9: the server's date in the project's SE-13 time zone (UTC without a project, or with a broken zone
    setting) as (the sentence for the agent's instructions, the date for `check(..., today=)`); nothing else in the
    instructions grounds anything."""
    zone = "UTC"
    if project:
        from abi import semantic  # not at top: abi.semantic imports the agent package

        with contextlib.suppress(ActionFailed):
            zone = semantic.locale(project)["time_zone"]
    day = datetime.now(ZoneInfo(zone)).date()
    told = (f"Today is {day.isoformat()} ({DAYS[day.weekday()]}) in the time zone {zone}; use this date for today,"
            " this year and other relative dates, never another.")
    return told, day


# GATE-9-fix-4: the retry turn shows the model its draft masked (never the flagged values) and asks for a fresh answer,
# so it has nothing to quote back in an apology the person would see as "[not verified]" talk about a hidden check
RETRY = (f"Your answer above has {MARK} where it stated a number or name that no tool result backs. Answer the"
         " person's question again from the start, stating only numbers and names that tool results show (call a"
         " tool if you need one) and leaving out what you can't back. Don't mention this check, an earlier answer or"
         " a correction.")


def template(results: list) -> str:
    """The fallback answer after a failed retry: the tool results as returned, no LLM words around the figures."""
    if not results:
        return "I couldn't back my answer with a tool result, so I won't state it. Ask me to look it up with a query."
    lines = [("I couldn't back every number and name in my answer with a tool result. Here are the results as the"
              " tools returned them:")]
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("columns"), list) and isinstance(r.get("rows"), list):
            rows = [list(x.values()) if isinstance(x, dict) else x for x in r["rows"]]
            lines += [" | ".join(map(str, r["columns"]))] + [" | ".join(map(str, x)) for x in rows[:20]]
            lines += [f"... {len(rows) - 20} more rows"] if len(rows) > 20 else []
        else:
            lines.append(json.dumps(r, default=str))
    return "\n".join(lines)


VOCAB_KEYS = {"id", "name", "plural", "term", "table", "column", "title"}  # 1.2.6: an object type's plural ("Cafés")


def scope(project: str, user_id: int | None = None, tables: set[str] | None = None) -> tuple[set[str], set[str], set[str]]:
    """(vocab, members, fields) for `check`: names/IDs in the project's repo files plus table and column names, the
    distinct values of text columns of the model's dimension tables (only `tables` when given), and the table, column
    and measure IDs. Reads data through the semantic engine (principle 4)."""
    from abi import semantic, storage
    from abi.project import git
    from abi.semantic import model

    root = git.path(project)
    if not root.exists():
        return set(), set(), set()
    vocab: set[str] = set()

    def collect(x):
        if isinstance(x, dict):
            vocab.update(v for k, v in x.items() if k in VOCAB_KEYS and isinstance(v, str))
            x = list(x.values())
        if isinstance(x, list):
            for v in x:
                collect(v)

    for p in root.rglob("*.json"):
        if ".git" not in p.relative_to(root).parts:
            collect(json.loads(p.read_text()))
    cols = model.lake_columns(project)
    fields = set(cols) | {c for cs in cols.values() for c in cs} | {
        m["id"] for m in model._read(project, "measures.json").get("measures", []) if isinstance(m.get("id"), str)}
    # DEMO-BRIEF-fix-3: the IDs of the project's reports, flows, agents, ML models and datasets ground their word parts
    # as names too (never figures, D-114): "Creator" for `creator_performance`
    fields |= {p.name.removesuffix(".json") for d in ("reports", "flows", "agents", "ml") for p in (root / d).glob("*.json")
               if not p.name.endswith(".eval.json")}
    fields |= {d["id"] for d in model._read(project, "datasets.json").get("datasets", []) if isinstance(d.get("id"), str)}
    vocab |= fields
    with contextlib.suppress(ActionFailed):  # LLM-2-fix-21: the project's own time zone ("Europe/Berlin") is a known name
        z = semantic.locale(project)["time_zone"]
        vocab |= {z, z.replace("_", " ")}  # "America/New_York" is written "New York"
    members: set[str] = set()
    hid = semantic.rls.hidden(project, user_id)  # D-110: not in the user's reads, so never members
    for t, info in model.load(project)["tables"].items():
        if info["kind"] != "dimension" or (tables is not None and t not in tables):
            continue
        for c in (c for c, dt in cols.get(t, {}).items() if dt == "VARCHAR" and c.lower() not in hid.get(t, ())):
            # ponytail: first 1000 distinct members per column; a members index if big dimensions need full coverage
            rows = semantic.execute(project, f"SELECT DISTINCT {storage._q(c)} FROM {storage._q(t)} WHERE {storage._q(c)}"
                                    " IS NOT NULL LIMIT 1000", {"grounding": f"{t}.{c}"}, user_id)
            members |= {r[0] for r in rows if len(r[0]) > 1 and re.search("[A-Za-z]", r[0])}
    return vocab, members, fields
