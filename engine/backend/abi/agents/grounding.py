"""AG-2 grounding check (PRD §3 "Grounding check", §5 #5, principle 7, DECISIONS D-22).

`check(answer, results, vocab, members, asked)` is pure and deterministic: it returns the numbers and entity names in
`answer` that no tool result backs. Only result row values ground numbers (D-22), never tool inputs, filter values
or action metadata; the one exception is a figure the person typed in their own question (`asked`, LLM-2-fix-5):
repeating "top 5" or "above 250" back is not a number the agent made up.
A number is grounded when it equals a result value as written, rounded to the shown decimals, or (shown as a percent)
a ratio × 100. An entity name is grounded when a result value contains it, or it is a text member a query result
was filtered on that exists in the data (`filtered`, LLM-2-fix-2), or a month/weekday name or Yes/No whose number,
date or boolean a result row carries (LLM-2-fix-4). Labels over text columns of markdown tables are not names; a quoted phrase is
checked by its capitalized words and numbers (LLM-2-fix-4). `scope()` gathers the model's own
vocabulary and the in-scope text dimension members; `template()` is the fallback answer built from the results.
"""

import calendar
import contextlib
import functools
import itertools
import json
import re
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, available_timezones

import duckdb

from abi.actions import ACTIONS, ActionFailed

WORDS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"])}
WORDS |= {w: 10 * i for i, w in enumerate(["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"], 2)}
SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_W = "|".join(sorted(WORDS, key=len, reverse=True))  # longest first: "seventeen" before "seven"
NUM = re.compile(
    r"(?:(?:(?<![\w.])(?P<neg>[-−]))?[$€£¥]?(?<![\d.])(?P<n>\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?P<dec>\.\d+)?"
    r"|(?<![\w-])(?P<w>(?i:(?:" + _W + r")(?:[- ](?:and[- ])?(?:" + _W + r"|hundred|thousand|million|billion))*))(?![\w-]))"
    r"(?P<mult>\s?(?i:thousand|million|billion)\b|(?:k|K|M|bn)\b)?(?P<pct>\s?%|\s(?i:percent|per cent)\b)?")
# LLM-2-fix-11, D-75: English contractions (I’ll, Don’t, Here’s) from a closed list of stems are words, never names;
# any other stem (Atlantis’ll, Bob’s) is still checked as a name
_CON = (r"(?!(?:(?:I|You|We|They|He|She|It|That|There|Here|What|Where|Who|How|Let)['’](?:ll|m|ve|d|re|s)"
        r"|(?:Don|Doesn|Didn|Can|Couldn|Won|Wouldn|Shouldn|Isn|Aren|Wasn|Weren|Haven|Hasn|Hadn)['’]t)(?![\w'’]))")
CAP = re.compile(rf"(?<![\w'’]){_CON}[A-Z][A-Za-z&'’-]*(?: +{_CON}[A-Z][A-Za-z&'’-]*)*")
SEP = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*")  # markdown table separator row
MONTHS, DAYS = list(calendar.month_name)[1:], list(calendar.day_name)  # English: the app never calls setlocale
ABBR = list(calendar.month_abbr)[1:]  # GATE-9-fix-2: "Jan".."Dec" ground like the full month names
# D-75: generic table header labels, never entity names; any other header word must ground like a name
HEADERS = {"metric", "measure", "value", "values", "total", "count", "month", "year", "quarter", "week", "day", "date",
           "region", "country", "category", "name", "table", "column", "type", "key", "rows", "min", "max", "earliest",
           "latest", "first", "last", "share", "change", "difference", "rank", "item", "field", "description", "kind",
           "rule", "user", "viewer", "allowed", "row", "range", "note", "notes", "status", "result", "step", "source",
           "time", "action", "verdict"}  # DEMO-BRIEF-fix-3: "| Model | Result | Baseline | Verdict |"
DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?:-(\d{2}))?(?!\d)")
# LLM-2-fix-7: spelled-out names of the ML metric keys (abi.ml `_score`, forecast `_errors`, drift `psi`), grounded
# where a result text holds the key as a word ("metrics mae", "recent_mae")
METRICS = {"mae": "Mean absolute error", "wape": "Weighted absolute percentage error", "r2": "R-squared",
           "balanced_accuracy": "Balanced accuracy", "psi": "Population stability index"}
# LLM-2-fix-15: a hex identifier (a Git commit id or its 7+ character prefix) is grounded as a whole when a result text
# carries it, never read as digit runs
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
HEX = re.compile(r"(?<!\w)(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}(?!\w)")
# LLM-2-fix-19, D-104: an all-digit prefix of a carried ID counts as the ID only as a whole backtick code span
# (`1234567`) or right after commit/sha/revision/version id ("commit #1234567"); anywhere else it is a figure
ID_LABEL = re.compile(r"(?i)\b(?:commit|sha|revision|version id)\s*[:#]?\s*$")
ID_AFTER = re.compile(r"(?:$|[\s)\]`'\"!?]|[.,;:](?!\d))")
# LLM-2-fix-15, D-75: function words opening a clause after ":", ";" or "(" ("Note: This sums ..."), never names
FUNCTION = {"A", "An", "The", "This", "These", "That", "Those", "It", "Its", "There", "Here", "We", "You", "They", "He",
            "She", "I", "Our", "Your", "Their", "And", "But", "Or", "So", "If", "When", "Each", "All", "Both",
            "None", "Nothing", "Every"}  # GATE-9-fix-2: also opening a table cell ("| None |")
# GATE-9-fix-2, D-75: the product's own words (role names, MEL; the MEL function names and conditions and the action
# names spelled out are added in `check`). GATE-9-fix-10 (a): its technical words, from the code's own vocabulary
PRODUCT = {"Viewer", "Viewers", "Editor", "Editors", "Owner", "Owners", "MEL",
           "Main Agent", "Documenter", "Chat", "Workspace", "Account", "Model", "Agent", "Confirm", "Reject", "Approve",
           "Undo", "Cancel",  # app parts, the agent skill (agent.DOCUMENTER), confirmation button labels
           "DuckDB", "DuckLake", "Postgres", "PostgreSQL", "ClickHouse", "MySQL", "Parquet", "SQL", "Git", "Markdown",
           "CSV", "Excel", "PDF", "JSON", "Slack", "Teams", "URL", "URLs", "API", "OIDC", "SSO", "MCP", "SDK", "ID",
           "ML", "LightGBM", "ETS", "ARIMA", "Holdout",  # engines, file formats, channel kinds, ml forecast METHODS
           "VARCHAR", "TEXT", "DECIMAL", "INTEGER", "BIGINT", "DOUBLE", "FLOAT", "BOOLEAN", "DATE", "TIMESTAMP",
           "INTERVAL", "SELECT", "FROM", "AS", "NULL", "DISTINCT", "TRIM", "CAST",  # DuckDB type names, SQL words
           "MAE", "WAPE", "R2", "PSI"}  # METRICS keys as written
# GATE-9-fix-10 (b, c): ordinary English words (lower-cased, matched in any case) that GATE-9 rounds 3-4 saw flagged
# after a bold label, a quote or in a header, and the status words the app writes (edit log, flow and ML runs). A
# closed list, never a position or shape (GATE-9-fix-3): no noun that could name a member ("Gold", "Spring"); a word
# here that IS a member is still checked as a member
COMMON = {"monthly", "daily", "weekly", "yearly", "tell", "load", "ask", "training", "today", "so", "its", "grand",
          "any", "done", "not", "on", "chosen", "time", "average", "main", "then", "also",
          "active", "proposed", "undone", "rejected", "synced", "queued", "running", "ok", "failed", "stopped",
          "waiting", "approved", "pending", "published", "draft"}
# AGENT-CONTEXT-fix-9: English month and weekday names, full and three-letter, are calendar words, never entity names;
# the figures next to them ("March 2026") are still checked
COMMON |= {w.lower() for w in [*MONTHS, *ABBR, *DAYS, *calendar.day_abbr]}
# GATE-9-fix-3: a whole identifier token (URL, email, ISO date/timestamp, IPv4 host, UUID, hyphenated ID starting with
# a letter and holding a digit such as `claude-sonnet-5-5`) that a result text or the person's question carries is quoted as one ID,
# never read as figures; only between positive delimiters (the lookbehind's list and ID_END)
IDTOK = re.compile(r"(?<![^\s(\[`'\"‘“*|<])(?:https?://(?:(?![\"'`<>()\[\]*|])[!-~])+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
                   r"|\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)?)?|\d{1,3}(?:\.\d{1,3}){3}"
                   r"|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|(?=[\w-]*\d)[A-Za-z]\w*(?:-\w+)+)", re.ASCII)  # round 3:
# visible ASCII only (a URL with U+2800/U+200B "spaces" would swallow a sentence)
ID_END = re.compile(r"$|[\s`'\"’”)\]>*|!?]|[.,;:](?!\w)")
# GATE-9-fix-10 (d): a whole IANA time zone name ("Europe/Berlin"), checked against the stdlib's own list
ZONE = re.compile(r"(?<![\w/])[A-Z][A-Za-z_]+(?:/[A-Za-z_]+)+(?![\w/])")
QUOTED = re.compile(r"\"([^\"\n]{2,100})\"|“([^”\n]{2,100})”|(?<!\w)'([^'\n]{2,100})'(?!\w)|‘([^’\n]{2,100})’")


def _words(s: str) -> float:
    total = cur = 0
    for w in re.split(r"[- ]+", s.lower()):
        if w in WORDS:
            cur += WORDS[w]
        elif w == "hundred":
            cur = (cur or 1) * 100
        elif w in SCALES:
            total, cur = total + (cur or 1) * SCALES[w], 0
    return total + cur


def _ids(text: str):
    """GATE-9-fix-3: (span, token) of each identifier token in `text` that ends at an ID_END delimiter."""
    for m in IDTOK.finditer(text):
        tok = m[0].rstrip(".,;:!?") if m[0].startswith("http") else m[0]
        if ID_END.match(text, m.start() + len(tok)):
            yield (m.start(), m.start() + len(tok)), tok


def _carried(tok: str, carried: set[str]) -> bool:
    """A date/timestamp is carried as itself or as the start of a carried timestamp up to a component boundary
    ("2026-10-15 05:19" of "2026-10-15T05:19:52.4+00:00", T read as a space); any other ID only exactly."""
    if not DATE.match(tok):
        return tok in carried
    t = tok.replace("T", " ")
    return any(c == t or c.startswith(t) and not c[len(t)].isdigit() for c in (x.replace("T", " ") for x in carried))


def numbers(text: str, ids: set[str] = frozenset(), lists: bool = False) -> list[tuple[str, float, float, bool, bool]]:
    return [n for _, n in _numbers(text, ids, lists)]


def _numbers(text: str, ids: set[str], lists: bool = False) -> list[tuple[tuple[int, int], tuple]]:
    """(span, number) of every number stated in `text`: (as written, signed value, rounding tolerance, shown as %,
    explicit minus).
    Digits with thousands separators, decimals, currency signs, k/M/bn or word scales, and spelled-out numbers.
    LLM-2-fix-19: a bare 7+ digit run that a carried hex ID in `ids` starts with is that ID, not a number, when quoted
    as an ID (D-104: whole backtick code span, or after commit/sha/revision/version id).
    `lists`: keep list markers ("6. Alert") as numbers (the person's own numbered items, GATE-9-fix-3)."""
    out = []
    for m in NUM.finditer(text):
        if m["w"] and m["w"].lower() == "one" and not (m["mult"] or m["pct"]):
            continue  # "one of", "no one": a pronoun far more often than a figure
        if not lists and m["n"] and text[m.end():m.end() + 2] in (". ", ") ") \
                and not text[:m.start()].rstrip(" \t").rpartition("\n")[2]:
            continue  # "1. " / "2) " list markers
        tok = m["n"] and re.search(r"\w*$", text[:m.start("n")])[0] + re.match(r"\w*", text[m.start("n"):])[0]
        if tok and "_" in tok and re.search("[A-Za-z]", tok):
            continue  # LLM-2-fix-7: digits in an identifier ("llm23_dev"); "5x", "$3B", "FY2023", "7kg" stay figures
        a, b = m.span("n")
        if (m[0].strip() == m["n"] and m["n"].isdigit() and len(m["n"]) >= 7 and any(i.startswith(m["n"]) for i in ids)
                and (text[a - 1:a] == text[b:b + 1] == "`" or ID_LABEL.search(text[:a]) and ID_AFTER.match(text, b))):
            continue
        if m["w"]:
            v, tol = _words(m["w"]), 0.5
        else:
            v, tol = float(m["n"].replace(",", "") + (m["dec"] or "")), 0.5 * 10 ** -(len(m["dec"] or ".") - 1)
        if mult := (m["mult"] or "").strip().lower():
            f = SCALES.get(mult) or {"k": 1e3, "m": 1e6, "bn": 1e9}[mult]
            v, tol = v * f, tol * f
        out.append((m.span(), (m[0].strip(), -v if m["neg"] else v, tol + 1e-9, bool(m["pct"]), bool(m["neg"]))))
    return out


def _walk(x, nums: list[float], texts: list[str]) -> None:
    if x is None or isinstance(x, bool):
        return
    if isinstance(x, (int, float, Decimal)):
        nums.append(float(x))
    elif isinstance(x, dict):
        for k, v in x.items():
            texts.append(str(k))  # keys are column/measure IDs: they ground names, not numbers
            _walk(v, nums, texts)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _walk(v, nums, texts)
    else:
        s = str(x)  # dates, text cells: their embedded numbers count as values too
        texts.append(s)
        if not (HEX.fullmatch(s) or UUID.fullmatch(s)):  # GATE-9-fix-8: a run/batch ID grounds as an ID, never its digits
            nums.extend(v for _, v, _, _, _ in numbers(s))


def _find(name: str, text: str, case: bool = False) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, 0 if case else re.IGNORECASE)]


def _has(name: str, text: str, case: bool = False) -> bool:
    return bool(_find(name, text, case))


def _caps(text: str, quoted: bool = False) -> list[tuple[int, str]]:
    """(start, phrase): capitalized phrases not at a sentence start (a phrase opening a sentence loses its first word).
    In a quoted sentence the opening word alone is a sentence start too; an opening phrase of several capitalized
    words stays whole."""
    out = []
    for m in CAP.finditer(text):
        pre = text[:m.start()].rstrip(" \t*_#>•-")
        phrase = m[0]
        if (not pre and not quoted) or (pre and pre[-1] in ".!?\n"):  # sentence start (also "1." list items)
            phrase = phrase.partition(" ")[2]
        elif not pre and " " not in phrase:  # LLM-2-fix-4: "Total revenue is 10,863.9" → no "Total"
            phrase = ""
        elif pre and pre[-1] in ":;(|" and phrase.partition(" ")[0] in FUNCTION:  # LLM-2-fix-15: "Note: This sums"
            phrase = phrase.partition(" ")[2]
        at = m.end() - len(phrase.lstrip())
        phrase = re.sub(r"(['’]s?|-)$", "", phrase.strip())
        if len(phrase) > 1:
            out.append((at, phrase))
    return out


def _cells(line: str) -> list[tuple[int, int, str]]:
    """(start, end, text) of each cell of a markdown table line."""
    bars = [i for i, ch in enumerate(line) if ch == "|"]
    cuts = ([-1] if line.strip()[:1] != "|" else []) + bars + ([len(line)] if line.strip()[-1:] != "|" else [])
    return [(a + 1, b, line[a + 1:b].strip()) for a, b in itertools.pairwise(cuts)]


def _headers(answer: str) -> str:
    """LLM-2-fix-4, D-75: `answer` with the markdown table header cells that are generic labels blanked: every word of
    the cell is a `HEADERS` word ("Earliest date", "Row count", GATE-9-fix-1).
    Every other header cell is checked like any name: it grounds by a result column, a model/measure ID or name
    (vocab) or a member in the rows, never by what the column below holds."""
    lines = answer.split("\n")
    for i in range(len(lines) - 1):
        if "|" in lines[i] and SEP.fullmatch(lines[i + 1]):
            for a, b, cell in _cells(lines[i]):
                if all(w.strip("*_`") in HEADERS for w in cell.strip("*_` ").lower().split()):
                    lines[i] = lines[i][:a] + " " * (b - a) + lines[i][b:]
    return "\n".join(lines)


@functools.cache
def _tz() -> frozenset[str]:
    return frozenset(available_timezones())


def _zones(text: str) -> str:
    """GATE-9-fix-10 (d): `text` with every whole IANA zone name blanked: a zone is no entity name. Its parts alone
    ("Berlin", "Europe") are checked as names as before."""
    return ZONE.sub(lambda m: " " * len(m[0]) if m[0] in _tz() else m[0], text)


def _ranks(text: str) -> str:
    """GATE-9-fix-10 (f): `text` with the first cell of each row of a markdown table whose first header is Rank or #
    blanked when it is exactly that row's position (1, 2, ...); any other value there stays a figure."""
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        if "|" in lines[i] and SEP.fullmatch(lines[i + 1]) and _cells(lines[i])[0][2].strip("*_` ").lower() in ("rank", "#"):
            for k, j in enumerate(itertools.takewhile(lambda j: "|" in lines[j], range(i + 2, len(lines))), 1):
                a, b, cell = _cells(lines[j])[0]
                if cell == str(k):
                    lines[j] = lines[j][:a] + " " * (b - a) + lines[j][b:]
    return "\n".join(lines)


def _names(answer: str) -> list[tuple[int, str]]:
    """(start, name): quoted strings (a one-word quote whole, a quoted phrase by its capitalized words), then
    capitalized phrases outside quotes and outside text-column table headers."""
    out = []
    for m in QUOTED.finditer(answer):
        i = next(i for i, g in enumerate(m.groups(), 1) if g)
        q, at = m[i].strip(), m.start(i) + len(m[i]) - len(m[i].lstrip())
        out += [(at + a, n) for a, n in _caps(q, quoted=True)] if " " in q else [(at, q)]
    return out + _caps(QUOTED.sub(lambda m: " " * len(m[0]), _headers(answer)))


def _calendar(results: list) -> list[str]:
    """LLM-2-fix-4: words that result rows carry as other values: month and weekday names for a month number (1-12)
    in a column named *month or for a date (YYYY-MM-DD; YYYY-MM for the month alone), Yes/True and No/False for
    booleans (detect_model's `key`), "Total" for a rolled-up row (`grouping_id` > 0, LLM-2-fix-7). Dates count anywhere
    inside a cell (nested lists/dicts, date objects; LLM-2-fix-21)."""
    out = set()
    for r in results:
        if not (isinstance(r, dict) and isinstance(r.get("rows"), list)):
            continue
        cols = r.get("columns") if isinstance(r.get("columns"), list) else []
        for row in r["rows"]:
            for c, v in row.items() if isinstance(row, dict) else zip(cols, row) if isinstance(row, (list, tuple)) else []:
                if v is True or v is False:  # a boolean shown as Yes/No or True/False
                    out.update(("Yes", "True") if v else ("No", "False"))
                elif c == "grouping_id" and v:  # LLM-2-fix-7: a rolled-up row, shown as "Total" (charts.rolled)
                    out.add("Total")
                elif type(v) is int and 1 <= v <= 12 and str(c).lower().endswith("month"):
                    out.update((MONTHS[v - 1], ABBR[v - 1]))
                _walk(v, [], strs := [])  # LLM-2-fix-21: dates nested in a cell (an inbox post's row), date objects too
                out.update(n for s in strs for y, m, d in DATE.findall(s) for n in _day(y, m, d))
    return sorted(out)


def _day(y: str, m: str, d: str) -> list[str]:
    """The month name (and its abbreviation, GATE-9-fix-2) of a YYYY-MM[-DD] match, and its weekday name when the day
    is given; [] for no real date."""
    try:
        day = date(int(y), int(m), int(d or 1))
    except ValueError:
        return []
    return [MONTHS[day.month - 1], ABBR[day.month - 1]] + ([DAYS[day.weekday()]] if d else [])


def _query(r) -> bool:
    return isinstance(r, dict) and isinstance(r.get("filters"), list) and isinstance(r.get("measures"), dict) \
        and isinstance(r.get("columns"), list) and isinstance(r.get("rows"), list)


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


def _commits(x) -> list[str]:
    """D-114: the commit IDs git itself wrote into results (`commit` values, `history`'s `commits[].id`), never any other
    text: an agent can save a hex-looking term or ID."""
    if isinstance(x, list):
        return [c for v in x for c in _commits(v)]
    if not isinstance(x, dict):
        return []
    out = [x["commit"]] if isinstance(x.get("commit"), str) else []
    out += [c["id"] for c in x.get("commits") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)]
    return out + [c for k, v in x.items() if k != "commit" for c in _commits(v)]


def _typed(name: str, asked: str, members: set[str]) -> bool:
    """AGENT-CONTEXT-fix-9: a name the person typed as a whole word in its singular or plural (+s) form, the first
    letter in either case: typed "stages" grounds "Stage", typed "report" grounds "Reports"; never "Stagecoach", and
    never a form in `members` (lower-cased text values a query filtered on: claimed data, grounded only by data)."""
    forms = {name, name + "s"} | ({name[:-1]} if name.endswith("s") else set())
    return not {f.lower() for f in forms} & members and any(
        _has(f, asked, case=True) or _has(f[:1].lower() + f[1:], asked, case=True) for f in forms)


def check(answer: str, results: list, vocab: set[str] = frozenset(), members: set[str] = frozenset(),
          fields: set[str] = frozenset(), asked: str = "", notes: list = (), pending: list = (), question: str = "",
          spans: bool = False, today: date | None = None) -> dict:
    """AG-2: {"numbers": [...], "names": [...]} in `answer` not backed by `results` (tool outputs/result rows only).
    `vocab` = the model's own words (measure, table, column, glossary, tool, agent names): never entity names.
    `members` = in-scope text dimension members; one found in the answer must appear in the results.
    `fields` = the project's table, column and measure IDs: a word part of one grounds a name (GATE-9-fix-6), not of any ID.
    Numbers count only from the `rows` of a result (D-22): ids, versions, counts or echoed inputs never ground one.
    A query result's `filters` ground nothing here: pass `filtered(...)` (the values found in data) with the results.
    `asked` = the person's own words (LLM-2-fix-5, D-75 positive rule): a figure in it grounds the same figure, exactly
    as typed (no rounding, no ratio); a name in it grounds the same name in the same case (GATE-9-fix-10).
    LLM-2-fix-7: `notes` = the run's tool messages that aren't results (confirmation `{status, message}`, refusals
    `{error}`); their words and the word parts of result column IDs ground names like `vocab`, never numbers.
    LLM-2-fix-15: a hex ID (or its 7+ character prefix) that a result text carries is quoted as a whole, not as figures,
    also when the quoted prefix is all digits (LLM-2-fix-19, see `numbers`); so is a whole URL, email, ISO date or
    timestamp, IPv4 host or hyphenated ID (GATE-9-fix-3, `_ids`) that a result row cell or `asked` carries; any other
    text (result keys, notes, `question`, pending messages) carries it only when it holds no digit (D-114).
    LLM-2.8.1-fix-2: `pending` = confirmations shown to the person in this run ({message, input}): the action they are
    about to approve, not data. The server-written message grounds figures and names (with month/weekday names of its
    dates); the agent-written input grounds names only (D-22: inputs never ground a figure).
    `question` = a custom agent run's question (often LLM-written): never a figure, only digit-free ID tokens (D-114).
    GATE-9-fix-4: `spans=True` adds "spans", the (start, end) in `answer` of every flagged occurrence, for `mask`.
    GATE-9-fix-9: `today` = the server's date (`today()`): its year grounds as a figure, exactly; its day and month only
    inside that date written out (ISO, "October 15", "15th of Oct"), never as bare figures; its month and weekday name."""
    have, texts, said, cells = [], [], [], []
    _walk([{k: v for k, v in r.items() if k != "filters"} if _query(r) else r for r in results], [], texts)
    _walk([r["rows"] for r in results if isinstance(r, dict) and isinstance(r.get("rows"), list)], have, cells)
    _walk(notes, [], said)
    told, named = [], []
    _walk([p.get("message") for p in pending], have, told)
    _walk([p.get("input") for p in pending], [], named)  # names only: never a figure or ID mask
    texts += told + [n for t in told for y, m, d in DATE.findall(t) for n in _day(y, m, d)]
    cols = [c for r in results if isinstance(r, dict) and isinstance(r.get("columns"), list) for c in r["columns"]]
    said += [p for c in cols for p in re.split(r"[^A-Za-z0-9]+", str(c))]
    low = "\n".join(texts).lower()
    found = "\n".join(texts + named + _calendar(results) + [v for k, v in METRICS.items()
                                                    if re.search(rf"(?<![a-z0-9]){k}(?![a-z0-9])", low)])
    typed = {v for _, v, _, _, _ in numbers(asked, lists=True)}  # GATE-9-fix-3: "6. Alert" grounds "stage 6"
    if today:
        typed.add(float(today.year))
        found += "\n" + "\n".join(_day(str(today.year), f"{today.month:02}", f"{today.day:02}"))
    # GATE-9-fix-3: whole ID tokens, blanked only where no figure `numbers()` would read runs across its edge
    # ("claude-sonnet-5-5 %" is "5 %"). D-114: one holding a digit only from row cells and the person's own text; other
    # text (result keys, a custom run's question, objects an agent saved earlier) may echo what an agent chose
    carried = {t for s in [*cells, asked] for _, t in _ids(s)} | {
        t for s in [*texts, question] for _, t in _ids(s) if not re.search(r"\d", t)}
    shown = _ranks(answer)
    if today:  # the day next to today's month name, read as a date only before the year or punctuation/the end:
        # "Oct 15, 2026", "15 October." ground; "Oct 15 stores", "15 Oct orders", "Oct 15.5", "Oct 15,000" don't
        mon, day = rf"(?:{MONTHS[today.month - 1]}|{ABBR[today.month - 1]})\.?", rf"0?{today.day}(?:st|nd|rd|th)?"
        # verifier round 3: no sign, currency, decimal or thousands part may sit before the day ("$15 Oct", "3.15 Oct")
        shown = re.sub(rf"(?<![\w.,$€£¥₹−+-])(?<![$€£¥₹−+-]\s)(?:{mon}\s+{day}|{day}\s+(?:of\s+)?{mon})(?![.,]\d)"
                       rf"(?=,?\s+{today.year}(?!\w|[.,]\d)|\s*[,.;:!?)\]]|\s*$)",
                       lambda m: re.sub(r"\d", " ", m[0]), shown)
        carried.add(today.isoformat())
    figs = [m.span() for m in NUM.finditer(answer)]
    for (a, b), t in _ids(answer):
        if _carried(t, carried) and not any(s < a < e or s < b < e for s, e in figs):
            shown = shown[:a] + " " * (b - a) + shown[b:]
    # D-114 (verifier round 1): a hex ID too, plus git's own commit IDs wherever a result names them
    ids = {h for t in [*cells, asked, *_commits(results)] for h in HEX.findall(t)}
    shown = HEX.sub(lambda m: " " * len(m[0]) if any(i.startswith(m[0]) for i in ids) else m[0], shown)
    bad_nums = [(at, tok) for at, (tok, v, tol, pct, neg) in _numbers(shown, ids)
                if not any(abs((h if neg else abs(h)) - v) <= tol or (pct and abs(h * 100 - v) <= tol) for h in have)
                and v not in typed]
    from abi.semantic import mel  # not at top: abi.semantic imports the agent package

    # LLM-2-fix-21: the standard zone abbreviation (the project zone is in scope()); GATE-9-fix-2: the product's words
    words = "\n".join([*vocab, *fields, *said, "UTC", *PRODUCT, *mel.USAGE, *mel.CONDITION])
    # LLM-2.9-fix-1, D-75: a name made of exactly the word parts of one ID, in order ("Order Count" for
    # `order_count`, "Customer ID" for `customers.customer_id`), grounds like vocab
    spelled = {" ".join(re.findall(r"[a-z0-9]+", s.lower())) for x in [*vocab, *cols, *ACTIONS]
               for s in str(x).rsplit(".", 1)}  # GATE-9-fix-10 (a): an action name spelled out ("Detect Model")
    # GATE-9-fix-2: one word part of a snake_case table/column/measure ID, or its plural ("Orders" for `order_id`,
    # chat-00), grounds the same way; other vocabulary (other IDs, a time zone, display names) only as a whole (GATE-9-fix-6)
    parts = {p for x in [*fields, *cols] for s in str(x).split(".") if re.fullmatch("[a-z][a-z0-9_]*", s) for p in s.split("_")}
    # AGENT-CONTEXT-fix-9: text a query filtered on is a claimed member; it grounds only from data (`filtered`)
    asks = {v.lower() for r in results if _query(r) for f in r["filters"] if isinstance(f, dict)
            for v in (f.get("value") if isinstance(f.get("value"), list) else [f.get("value")]) if isinstance(v, str)}
    # GATE-9-fix-2: a member counts as written in the data, capitalized or in capitals ("music", "Music", "MUSIC"),
    # never in another case (the verb "shows" is not the category "Shows")
    cands = [(n, True, [(a, a + len(n))]) for a, n in _names(_zones(answer))] + [(m, False, at) for m in sorted(members) if (
        at := sorted({s for x in {m, m[:1].upper() + m[1:], m.upper()} for s in _find(x, answer, case=True)}))]
    # GATE-9-fix-10 (e): a name the person typed in this chat grounds exactly as typed (custom runs pass no `asked`,
    # D-114); (b, c): a phrase of COMMON or PRODUCT words only; (b) "Order" for an `orders` table
    bad_names = [(at, n) for n, vocab_ok, at in cands if not _has(n, found) and not _has(n, asked, case=True) and not (
        vocab_ok and (_typed(n, asked, asks) or _has(n, words) or " ".join(n.lower().split()) in spelled or n.lower() in parts
                      or n.endswith("s") and n.lower()[:-1] in parts or n.lower() + "s" in parts
                      or all(w.lower() in COMMON or _has(w, "\n".join(PRODUCT)) for w in n.split())))]
    out = {"numbers": list(dict.fromkeys(t for _, t in bad_nums)), "names": list(dict.fromkeys(n for _, n in bad_names))}
    if not spans:
        return out
    # a flagged name is hidden at every occurrence in any case, also where `check` skips it (a sentence start, a
    # heading): AG-2, no unverified name stays shown, not even lower-cased (a flagged "Shows" also hides the verb)
    same = [m.span() for n in out["names"] for m in re.finditer(r"(?<![^\W_])" + re.escape(n) + r"(?![^\W_])", answer,
                                                               re.IGNORECASE)]
    return out | {"spans": [s for s, _ in bad_nums] + [s for at, _ in bad_names for s in at] + same}


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


MARK = "[not verified]"


# GATE-9-fix-4: the retry turn shows the model its draft masked (never the flagged values) and asks for a fresh answer,
# so it has nothing to quote back in an apology the person would see as "[not verified]" talk about a hidden check
RETRY = (f"Your answer above has {MARK} where it stated a number or name that no tool result backs. Answer the"
         " person's question again from the start, stating only numbers and names that tool results show (call a"
         " tool if you need one) and leaving out what you can't back. Don't mention this check, an earlier answer or"
         " a correction.")


def mask(answer: str, bad: dict) -> str:
    """GROUNDING-UX, GATE-9-fix-4: `answer` with exactly the flagged occurrences (`check(..., spans=True)`) replaced
    by MARK, each widened to the whole word it is glued into, hyphens included (R2, 30d, 15T05, a UUID); touching ones merge."""
    hide = [False] * len(answer)
    for a, b in bad["spans"]:
        a -= len(re.search(r"[\w-]*$", answer[:a])[0])  # GATE-9 round 4: a hyphenated ID (a UUID) whole, or each
        b += len(re.match(r"[\w-]*", answer[b:])[0])  # pass masks one part and makes the next look like "-039"
        hide[a:b] = [True] * (b - a)
    return "".join(MARK if h else "".join(c for c, _ in g) for h, g in itertools.groupby(zip(answer, hide), lambda x: x[1]))


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


VOCAB_KEYS = {"id", "name", "term", "table", "column", "title"}


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
