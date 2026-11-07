#!/usr/bin/env python3
"""Harness-side grounding check (AG-2): ABI's own rules, loaded from engine/backend/abi/agents/grounding_rules.py (the
one copy ABI's chat uses too), plus the server-side reads (`filtered`, `scope`'s members and fields), made through
ABI's sql_query as the person, so RLS and hidden columns apply; and the code labels of a dimension (`labels`).

ABI grounds its own chat; MCP/REST callers are not grounded, so the harness checks every final answer:
numbers only from tool-result `rows` (or the person's own words), names from any tool-result text. The harness passes
this exchange's results and the previous one's, never older ones.

    echo '{"answer": "...", "results": [...], "asked": "...", "vocab": [...], "project": "...", "model": {...}}' \
        | python scripts/ground.py
    -> {"numbers": [...], "names": [...], "masked": "...", "final": "..." | null, "retry": "..."}
"""
import importlib.util
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

# by its path: this runs on the engine's venv, where `abi` is scripts/abi.py (the REST client), not ABI's package
_spec = importlib.util.spec_from_file_location(
    "grounding_rules", Path(__file__).resolve().parent.parent / "engine/backend/abi/agents/grounding_rules.py")
_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rules)
_rules.HARNESS = True  # the harness's own rules (clock times, link labels, status words, definition prose, ...)
globals().update({k: v for k, v in vars(_rules).items() if not k.startswith("__")})


# ---------- M2.2: what the engine's grounding reads on the server (filtered, scope), read here through ABI ----------
_MORE = re.compile(r"\.\.\. (\d+) more omitted")  # agent._cap's marker in an MCP result: not a tool value
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
OPS = {"=", "<>", "<", "<=", ">", ">=", "in"}  # the semantic query's filter operators
ROWS = 6000  # characters of rows the retry's instruction lists


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _num(v) -> str:
    return v if isinstance(v, str) else str(int(v)) if float(v).is_integer() else str(v)  # as DuckDB casts it


def _sql(project: str, sql: str, params=(), types=()) -> list:
    """One read through ABI's sql_query as the person (RLS and column security apply, as to the engine's own grounding
    reads); no rows when ABI refuses or fails it: what wasn't read grounds nothing."""
    import abi  # scripts/abi.py: the harness's REST client, with the token Pi passes

    try:
        return abi._open("POST", "/api/actions/sql_query", {"project": project, "sql": sql, "params": list(params),
                                                              "types": list(types)}, timeout=20)[1].get("rows") or []
    except Exception:  # noqa: BLE001 (a refused or failed read only means nothing grounds)
        return []


def _sampled(v, seen: list):
    """As the engine's chat grounds a capped result (agent._sampled, sampled): its lists without `_cap`'s "... N more
    omitted" marker, each cut's [shown, omitted, total] added to `seen`, so "20 of the 60 rows" grounds."""
    if isinstance(v, dict):
        return {k: _sampled(x, seen) for k, x in v.items()}
    if not isinstance(v, list):
        return v
    if v and isinstance(v[-1], str) and (m := _MORE.fullmatch(v[-1])):
        v = v[:-1]
        seen.append([len(v), int(m[1]), len(v) + int(m[1])])
    return [_sampled(x, seen) for x in v]


def _in(n: int) -> str:
    return "(" + ", ".join(f"${i}" for i in range(1, n + 1)) + ")"


def _where(fs: list) -> tuple[str, list, list] | None:
    """Date filters as SQL (all hold) with sql_query parameters (a day as DATE, a number as DOUBLE); None when one
    can't be read so (a NULL, a list for `=`)."""
    conds, params, kinds = [], [], []
    for f in fs:
        vals = f.get("value") if f.get("op") == "in" else [f.get("value")]
        if f.get("op") not in OPS or not isinstance(vals, list) or not vals or not all(
                isinstance(v, str) or isinstance(v, int | float) and not isinstance(v, bool) for v in vals):
            return None
        marks = []
        for v in vals:
            params.append(_num(v))
            kinds.append(1082 if isinstance(v, str) and DAY.fullmatch(v) else 0 if isinstance(v, str) else 701)
            marks.append(f"${len(params)}")
        x = _q(f["column"].split(".", 1)[1])
        conds.append(f"{x} IN ({', '.join(marks)})" if f["op"] == "in" else f"{x} {f['op']} {marks[0]}")
    return " AND ".join(conds), params, kinds


def filtered(project: str, model: dict, results: list, read=None) -> list:
    """grounding.filtered through ABI as the person: the text values of the `=`/`in` filters of query results that
    exist in their column, and a date-table filter's values that exist there, as rows ("revenue in 2025" grounds like
    a member). M2.2: and the first and last day each query's date filters keep (the date table's, or one date column's)
    as a row, so "from 2025-01-01 to 2025-12-31" grounds the query's own range, its inclusive end too (1.1 masked the
    ask task's end date in 19 of 20 runs). A value no row holds grounds nothing. `model`: semantic_model's result."""
    read = read or _sql
    cal, types = model.get("date_table") or {}, model.get("columns") or {}
    asked: dict[str, dict] = {}
    spans: dict[str, tuple] = {}
    for r in results:
        days: dict[tuple, list] = {}
        for f in r["filters"] if _query(r) else []:
            if not (isinstance(f, dict) and isinstance(f.get("column"), str) and "." in f["column"]):
                continue
            t, c = f["column"].split(".", 1)
            on_cal = t == cal.get("table")
            vals = f.get("value") if f.get("op") in ("=", "in") else None
            for v in vals if isinstance(vals, list) else [vals]:
                if isinstance(v, str) or on_cal and isinstance(v, int | float) and not isinstance(v, bool):
                    asked.setdefault(f["column"], {})[_num(v)] = v
            if on_cal and cal.get("column") or re.match(r"DATE|TIMESTAMP", str((types.get(t) or {}).get(c, ""))):
                days.setdefault((t, cal["column"] if on_cal else c), []).append(f)
        for (t, c), fs in days.items():
            if where := _where(fs):
                spans[json.dumps([t, c, *where])] = (t, c, *where)
    out: list = []
    for col, vals in asked.items():
        t, c = col.split(".", 1)
        x = f"CAST({_q(c)} AS VARCHAR)"
        held = {row[0] for row in read(project, f"SELECT DISTINCT {x} FROM {_q(t)} WHERE {x} IN {_in(len(vals))}", sorted(vals))}
        if t != cal.get("table"):
            out += sorted(held & vals.keys())
        elif rows := [[v] for k, v in sorted(vals.items()) if k in held]:
            out.append({"columns": [col], "rows": rows})
    for t, c, cond, params, kinds in spans.values():
        x = _q(c)
        for lo, hi in read(project, f"SELECT CAST(MIN({x}) AS VARCHAR), CAST(MAX({x}) AS VARCHAR) FROM {_q(t)} WHERE {cond}",
                           params, kinds)[:1]:
            if lo and hi:
                out.append({"columns": [f"{t}.{c}", f"{t}.{c}"], "rows": [[lo, hi]]})
    return out


def labels(project: str, model: dict, results: list, read=None) -> list[str]:
    """M2.2: display labels for codes: a code in a result column that is a dimension's key, or a fact column related to
    one (orders.product_id), may be quoted by its dimension row's text values ("Flat White" for P02), read through ABI
    as the person. `check` takes them as names only, never figures."""
    read = read or _sql
    cal = (model.get("date_table") or {}).get("table")
    keys = {f"{t}.{i['key'][0]}": (t, i["key"][0]) for t, i in (model.get("tables") or {}).items()
            if isinstance(i, dict) and i.get("kind") == "dimension" and len(i.get("key") or []) == 1 and t != cal}
    for rel in model.get("relationships") or []:
        a, b = rel.get("from") or {}, rel.get("to") or {}
        if len(a.get("columns") or []) == 1 == len(b.get("columns") or []) and (k := f"{b.get('table')}.{b['columns'][0]}") in keys:
            keys[f"{a.get('table')}.{a['columns'][0]}"] = keys[k]
    codes: dict[tuple, set] = {}
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("columns"), list) and isinstance(r.get("rows"), list):
            for i, c in enumerate(r["columns"]):
                if c in keys:
                    codes.setdefault(keys[c], set()).update(
                        str(x[i]) for x in r["rows"] if isinstance(x, (list, tuple)) and len(x) > i and x[i] is not None)
    out: list[str] = []
    for (t, k), vals in codes.items():
        vals = sorted(vals)[:100]  # ponytail: 100 codes per dimension; MCP shows a result's first 20 rows anyway
        out += [v for row in (read(project, f"SELECT * FROM {_q(t)} WHERE CAST({_q(k)} AS VARCHAR) IN {_in(len(vals))}", vals)
                              if vals else []) for v in row if isinstance(v, str)]
    return out


def scope(project: str, model: dict, read=None) -> tuple[set[str], set[str]]:
    """grounding.scope's (members, fields) from the model the harness's digest read: the distinct values (the first
    1000) of the dimension tables' text columns, read through ABI as the person (RLS applies; a hidden column reads
    nothing), and the table, column, measure and dataset IDs."""
    read = read or _sql
    tables, cols = model.get("tables") or {}, model.get("columns") or {}
    text = [(t, c) for t, i in tables.items() if isinstance(i, dict) and i.get("kind") == "dimension"
            for c, ty in (cols.get(t) or {}).items() if ty == "VARCHAR"]
    with ThreadPoolExecutor(8) as ex:  # one read per column, as the engine's scope does
        got = list(ex.map(lambda tc: read(project, f"SELECT DISTINCT {_q(tc[1])} FROM {_q(tc[0])} WHERE {_q(tc[1])}"
                                                   " IS NOT NULL LIMIT 1000"), text))
    members = {r[0] for rows in got for r in rows if isinstance(r[0], str) and len(r[0]) > 1 and re.search("[A-Za-z]", r[0])}
    fields = {*tables, *(c for cs in cols.values() if isinstance(cs, dict) for c in cs)} | {
        x["id"] for k in ("measures", "datasets") for x in model.get(k) or [] if isinstance(x, dict) and isinstance(x.get("id"), str)}
    return members, fields


def retry(bad: dict, results: list) -> str:
    """M2.2: the one retry's instruction (a hidden turn): rewrite from the rows, naming what was masked and listing the
    rows of this exchange and the one before that the answer may quote, newest first."""
    lines: list[str] = []
    for r in reversed(results):
        if isinstance(r, dict) and isinstance(r.get("columns"), list) and isinstance(r.get("rows"), list) and r["rows"]:
            lines += [json.dumps(x, ensure_ascii=False, default=str) for x in [r["columns"], *r["rows"]]] + [""]
    rows = "\n".join(lines).strip()
    rows = rows if len(rows) <= ROWS else rows[:ROWS].rpartition("\n")[0] + "\n…"
    return (f"Your answer above has {MARK} where it stated what no tool row backs: {', '.join(bad['numbers'] + bad['names'])}."
            " Rewrite the answer to the person's question from the rows below: every figure a value of these rows, as"
            " it is or rounded for display, never a sum, difference, share, percentage, bound (\"less than 200\") or"
            " range you work out yourself, and every name one the rows hold. Count only with the items named right after"
            " the count (\"3 visuals: A, B and C\"); a group you summarise (\"the other cities\") gets no number,"
            " only its rows' figures; else name them, or say \"both\", \"each\" or \"these\". For a change or a share,"
            " quote a change measure's or an expression's rows (call semantic_query), or state both values the rows"
            " give. For a report you built, read each visual's rows with visual_data and quote those, or describe"
            " each visual by its title without figures. Leave out what the rows can't back. Don't mention this check, an earlier answer or a correction.\n\n"
            + (f"Rows you may quote (each result's columns, then its rows):\n{rows}" if rows else "No tool rows back this answer yet."))


def run(req: dict) -> dict:
    """One answer checked for the harness (index.ts): the flags, the draft `masked` for the retry, the `final` text for
    the last attempt (masked until it passes the same check, None: withhold it) and the retry's instruction."""
    today = date.fromisoformat(req["today"]) if req.get("today") else None
    if not numbers(req["answer"]):  # ABI checks an answer only when it states numbers (agent._ungrounded)
        return {"numbers": [], "names": []}
    seen: list = []
    results = _sampled(_flat(req.get("results", [])), seen) + ([{"columns": ["shown", "omitted", "total"], "rows": seen}] if seen else [])
    members, fields, names = set(req.get("members", [])), set(req.get("fields", [])), []
    if (p := req.get("project")) and isinstance(m := req.get("model"), dict) and m.get("tables"):
        try:
            extra = filtered(p, m, results)
            names, (found, ids) = labels(p, m, results + extra), scope(p, m)
            results, members, fields = results + extra, members | found, fields | ids
        except Exception:  # noqa: BLE001 (an odd model or result only means less grounds, never an unchecked answer)
            names = []

    def flags(text: str) -> dict:
        return check(text, results, set(req.get("vocab", [])), members, fields, req.get("asked", ""), req.get("notes", []),
                     req.get("pending", []), spans=True, today=today, labels=names) if numbers(text) else {
            "numbers": [], "names": [], "spans": []}

    bad = flags(req["answer"])
    out = {"numbers": bad["numbers"], "names": bad["names"]}
    if out["numbers"] or out["names"]:
        out["masked"] = t = mask(req["answer"], bad)
        for _ in range(4):  # the masked draft must pass the same check, or none of it is shown (AG-2)
            if not ((again := flags(t))["numbers"] or again["names"]):
                break
            t = mask(t, again)
        out |= {"final": None if again["numbers"] or again["names"] else t, "retry": retry(out, results)}
    return out


def main() -> None:
    import sys
    print(json.dumps(run(json.load(sys.stdin))))


if __name__ == "__main__":
    main()
