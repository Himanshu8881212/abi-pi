#!/usr/bin/env python3
"""Harness-side grounding check (AG-2), copied from ABI's backend/abi/agents/grounding.py (rules verbatim; `template`
and the MEL/ACTIONS vocabulary dropped, which the caller passes as `vocab`). M2.2: the server-side reads are back
(`filtered`, `scope`'s members and fields), made through ABI's sql_query as the person, so RLS and hidden columns
apply; and the code labels of a dimension (`labels`).

ABI grounds its own chat; MCP/REST callers are not grounded, so the harness checks every final answer:
numbers only from tool-result `rows` (or the person's own words), names from any tool-result text. The harness passes
this exchange's results and the previous one's, never older ones.

    echo '{"answer": "...", "results": [...], "asked": "...", "vocab": [...], "project": "...", "model": {...}}' \
        | python scripts/ground.py
    -> {"numbers": [...], "names": [...], "masked": "...", "final": "..." | null, "retry": "..."}
"""
import calendar
import contextlib
import functools
import itertools
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, available_timezones


WORDS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"])}
WORDS |= {w: 10 * i for i, w in enumerate(["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"], 2)}
SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_W = "|".join(sorted(WORDS, key=len, reverse=True))  # longest first: "seventeen" before "seven"
NUM = re.compile(
    r"(?:(?:(?<![\w.])(?P<neg>[-−]))?[$€£¥]?(?<![\d.])(?P<n>\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?P<dec>\.\d+)?"
    r"|(?<![\w-])(?P<w>(?i:(?:" + _W + r")(?:[- ](?:and[- ])?(?:" + _W + r"|hundred|thousand|million|billion))*))(?![\w-]))"
    r"(?P<mult>\s?(?i:thousand|million|billion)\b|(?:k|K|M|bn)\b)?(?P<pct>\s?%|\s(?i:percent|per cent)\b)?")
# 1.2 M2: a range's first figure shares its last one's percent sign ("each fell 44–61%" is 44% to 61%; a spaced
# dash is an aside: "December 2025 – 14.6% down")
UPTO = re.compile(r"(?:[–—-]|\sto\s)[-−]?\d[\d,]*(?:\.\d+)?(?:\s?%|\s(?i:percent|per cent)\b)")
# M2 review: "between 44 and 61%" too
BETWEEN, AND = re.compile(r"\bbetween\s+$", re.I), re.compile(r"\s+and\s+[-−]?\d[\d,]*(?:\.\d+)?(?:\s?%|\s(?i:percent|per cent)\b)")
# LLM-2-fix-11, D-75: English contractions (I’ll, Don’t, Here’s) from a closed list of stems are words, never names;
# any other stem (Atlantis’ll, Bob’s) is still checked as a name
_CON = (r"(?!(?:(?:I|You|We|They|He|She|It|That|There|Here|What|Where|Who|How|Let)['’](?:ll|m|ve|d|re|s)"
        r"|(?:Don|Doesn|Didn|Can|Couldn|Won|Wouldn|Shouldn|Isn|Aren|Wasn|Weren|Haven|Hasn|Hadn)['’]t)(?![\w'’]))")
# M2.2: letters beyond ASCII too ("Bremen Süd" was checked as "Bremen S"); digits still end a name
CAP = re.compile(rf"(?<![\w'’]){_CON}[A-ZÀ-ÖØ-Þ](?:[^\W\d_]|[&'’-])*(?: +{_CON}[A-ZÀ-ÖØ-Þ](?:[^\W\d_]|[&'’-])*)*")
SEP = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*")  # markdown table separator row
MONTHS, DAYS = list(calendar.month_name)[1:], list(calendar.day_name)  # English: the app never calls setlocale
ABBR = list(calendar.month_abbr)[1:]  # GATE-9-fix-2: "Jan".."Dec" ground like the full month names
PCT = re.compile(r"%|pct|percent", re.IGNORECASE)  # reports.PCT: a column shown as a percent of its ratio
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
            "None", "Nothing", "Every",  # GATE-9-fix-2: also opening a table cell ("| None |")
            "Several", "Most", "Many", "Some", "Few", "Only",  # M2.2: "**North cafés:** Several fell sharply",
            "Why", "Whether", "What", "Which", "How", "Where", "Who",  # "**What I can't tell:** Why North fell"
            # 1.2 M2: prepositions, degree words ("**Where it sits:** Almost all of it", "**Cities:** Within North")
            "Almost", "Nearly", "About", "Around", "Roughly", "Mostly", "Mainly", "Largely", "Partly", "Just", "Even",
            "Also", "Within", "Across", "Among", "Between", "In", "At", "By", "For", "From", "With", "Without", "After",
            "Before", "During", "Since", "Over", "Under", "Unlike", "Despite", "Except", "Apart", "Outside", "Beyond"}
# 1.2 M2: words opening a phrase after a label ("**Segment:** No segment stands out", "**Bakery:** Up 6.8%"), checked when
# they stand alone: a yes/no or status value ("**Loyalty member:** No", "| Down |")
LEADS = {"No", "Up", "Down"}
# the agent's words for the state of its own work, opening a value after a label ("**Status:** Saved **disabled**");
# never data members, so not names. Data-like values (Active, Yes, No) stay checked.
STATUS = {"Saved", "Created", "Updated", "Added", "Removed", "Deleted", "Enabled", "Disabled", "Scheduled", "Unscheduled",
          "Paused", "Stopped", "Started", "Running", "Done", "Ready", "Failed", "Pending", "Draft", "Off", "On", "Kept",
          "Left", "Currently", "Still", "Not", "Deliberately", "Intentionally", "Safely", "Awaiting", "Waiting"}
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
          "waiting", "approved", "pending", "published", "draft",
          "total", "overall", "christmas", "easter", "new", "year",
          "mom", "yoy", "qoq", "wow", "eve", "boxing"}  # M2.2: "**Volume:** Total orders", "New Year closures";
# M2 review: "down 14.6% MoM", "Christmas Eve", "Boxing Day"
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
# 1.2 (owner, 5 Nov 2026): after a count, up to two words and a plural noun, then ":" or "(" opening what it counts
COUNTED = re.compile(r"(?:\s+[A-Za-z][\w-]*){0,2}?\s+[A-Za-z][\w-]*[A-Za-z](?<![sS])s[*_]*\s*[:(]")
# 1.2 final rerun: "Three northern cities stand out:" with the list right under it (a verb between the noun and
# the colon, the colon ending the line); the list under it must still name exactly that many backed items
COUNTED_LINE = re.compile(r"(?:\s+[A-Za-z][\w-]*){0,2}?\s+(?P<noun>[A-Za-z][\w-]*[A-Za-z](?<![sS])s)(?:\s+[A-Za-z][\w-]*){1,3}[*_]*\s*:[*_]*(?=[ \t]*(?:\n|$))")
NOT_NOUN = {"has", "was", "does", "this", "its", "thus", "plus", "yes", "goes"}
BULLET = re.compile(r"(\s*)(?:[-*•+]|\d+[.)])\s+(.*)")


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
        if m["n"] and b - a == 2 and re.search(r"\d:$", text[:a]) and not re.match(r"[\d.,]", text[b:b + 1]):
            continue  # the minutes of a clock time ("08:00"): the hour is checked, ":00" is no figure
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
        pct = bool(m["pct"] or m["n"] and not m["mult"] and (UPTO.match(text, m.end()) or BETWEEN.search(text, 0, m.start())
                                                         and AND.match(text, m.end())))
        out.append((m.span(), (m[0].strip(), -v if m["neg"] else v, tol + 1e-9, pct, bool(m["neg"]))))
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


def _pcts(results: list) -> list[float]:
    """M2.2: the row values of percent columns (an ID or display name with %, pct or percent, as reports show them):
    a percentage grounds from these x 100 (or as they are, from 1 up), else only from a ratio (a row value between -1 and 1)."""
    out: list[float] = []
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("columns"), list) and isinstance(r.get("rows"), list):
            names = r["measures"] if isinstance(r.get("measures"), dict) else {}
            # M2 review: never an inline expression by its name (the model picks it: "rows_pct" made a count a %); a
            # measure in percentage points (its MEL multiplies by 100: semantic.query's `percent`) is one
            mine = r["expressions"] if isinstance(r.get("expressions"), dict) else {}
            points = {str(c) for c in r["percent"]} if isinstance(r.get("percent"), list) else set()
            on = {str(c) for c in r["columns"] if str(c) in points or str(c) not in mine and (
                PCT.search(str(c)) or PCT.search(str(names.get(c, ""))))}
            for row in r["rows"]:
                pairs = row.items() if isinstance(row, dict) else zip(r["columns"], row) if isinstance(row, (list, tuple)) else []
                _walk([v for c, v in pairs if str(c) in on], out, [])
    return out


def _fold(s: str) -> str:
    """M2.2: accented letters as ASCII ("Café" for the word part cafe of cafes.cafe_name: IDs are ASCII)."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


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
        pre = text[:m.start()].rstrip(" \t*_#>•-[")  # a link label ("[Download it](…)") opens like a sentence
        phrase = m[0]
        if (not pre and not quoted) or (pre and pre[-1] in ".!?\n"):  # sentence start (also "1." list items)
            phrase = phrase.partition(" ")[2]
        elif not pre and " " not in phrase:  # LLM-2-fix-4: "Total revenue is 10,863.9" → no "Total"
            phrase = ""
        elif pre and pre[-1] in ":;(|" and (phrase.partition(" ")[0] in FUNCTION | STATUS  # LLM-2-fix-15: "Note: This sums"
                                            or phrase in LEADS and re.match(r" [a-z0-9+−-]", text[m.end():])):
            phrase = phrase.partition(" ")[2]
        at = m.end() - len(phrase.lstrip())
        phrase = re.sub(r"(['’]s?|-)$", "", phrase.strip())
        for piece in re.split(r"(['’]s +)", phrase):  # M2.2: a possessive ends a name ("North's December")
            if len(piece) > 1 and not re.fullmatch(r"['’]s +", piece):
                out.append((at, piece))
            at += len(piece)
    return out


def _cells(line: str) -> list[tuple[int, int, str]]:
    """(start, end, text) of each cell of a markdown table line."""
    bars = [i for i, ch in enumerate(line) if ch == "|"]
    cuts = ([-1] if line.strip()[:1] != "|" else []) + bars + ([len(line)] if line.strip()[-1:] != "|" else [])
    return [(a + 1, b, line[a + 1:b].strip()) for a, b in itertools.pairwise(cuts)]


def _item(s: str) -> str:
    """A listed item's name: its leading bold or code span, else its text before a "(", ": " or dash aside."""
    s = s.strip()
    s = b[2] if (b := re.match(r"(\*\*|__|`)(.+?)\1", s)) else re.split(r"\s+\(|:\s|\s[—–-]\s", s)[0]
    return s.strip(" *_`\"'“”‘’.,;:")


def _listed(answer: str, span: tuple[int, int], tok: str, named) -> bool:
    """1.2 (owner, 5 Nov 2026): a whole-number count ("3", "three") before a plural noun and ":" or "(" ("3 visuals:")
    grounds when the rest of its sentence, or else the bullet/numbered list or table right under it, names exactly that
    many distinct items and `named` backs every one. It grounds the count only: the names are still checked as names.
    Refused: a count without a list ("in 3 regions"), a list before the count ("A, B and C: all three"), decimals,
    currency, percentages, scales."""
    if not re.fullmatch(r"\d+|[A-Za-z]+(?:-[A-Za-z]+)?", tok):
        return False
    if m := COUNTED.match(answer, span[1]):
        noun = re.findall(r"[A-Za-z][\w-]*", m[0])[-1]
    elif m := COUNTED_LINE.match(answer, span[1]):
        noun = m["noun"]
    else:
        return False
    if noun.lower() in NOT_NOUN:
        return False
    n, rest = int(tok) if tok.isdigit() else _words(tok), answer[m.end():]
    line, _, below = rest.partition("\n")
    if m[0].endswith("("):
        text = line.partition(")")[0] if ")" in line else ""
    else:
        text = re.split(r"[.!?](?=\s|$)", line)[0]
    if text.strip(" *_"):
        items = re.split(r",\s*(?:and|or)\s+|,|;|\s+(?:and|or)\s+", text)
    elif m[0].endswith("("):
        return False
    else:  # the list or table under it
        lines = below.split("\n")
        i = next((i for i, x in enumerate(lines) if x.strip()), len(lines))
        if i + 1 < len(lines) and "|" in lines[i] and SEP.fullmatch(lines[i + 1]):
            items = [c for x in itertools.takewhile(lambda x: "|" in x, lines[i + 2:])
                     if (c := _cells(x)[0][2]).strip("*_` ").lower() not in ("total", "grand total")]
        else:
            items, indent = [], None
            for x in lines[i:]:
                if not x.strip():
                    continue
                if not (b := BULLET.fullmatch(x)) or indent is not None and len(b[1]) < indent:
                    break
                indent = len(b[1]) if indent is None else indent
                items += [b[2]] if len(b[1]) == indent else []  # a sub-item is part of its item
    items = [_item(x) for x in items]
    return len(items) == n == len({x.lower() for x in items}) and all(x and named(x) for x in items)


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


def check(answer: str, results: list, vocab: set[str] = frozenset(), members: set[str] = frozenset(),
          fields: set[str] = frozenset(), asked: str = "", notes: list = (), pending: list = (), question: str = "",
          spans: bool = False, today: date | None = None, labels: list[str] = ()) -> dict:
    """AG-2: {"numbers": [...], "names": [...]} in `answer` not backed by `results` (tool outputs/result rows only).
    M2.2: `labels` = the display labels of codes in the rows (`labels()`): data, so names only, never a figure.
    A percentage grounds only from a ratio x 100 (a row value between -1 and 1, or a percent column's), from a percent
    column's value as it is, or from the person's words: never from a revenue of 51.90 for "52%".
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
    # M2 review: nor an expression's MEL (its literals are the model's)
    _walk([{k: v for k, v in r.items() if k not in ("filters", "expressions")} if _query(r) else r for r in results], [], texts)
    _walk([r["rows"] for r in results if isinstance(r, dict) and isinstance(r.get("rows"), list)], have, cells)
    _walk(notes, [], said)
    told, named = [], []
    _walk([p.get("message") for p in pending], have, told)
    _walk([p.get("input") for p in pending], [], named)  # names only: never a figure or ID mask
    texts += told + [n for t in told for y, m, d in DATE.findall(t) for n in _day(y, m, d)]
    cols = [c for r in results if isinstance(r, dict) and isinstance(r.get("columns"), list) for c in r["columns"]]
    said += [p for c in cols for p in re.split(r"[^A-Za-z0-9]+", str(c))]
    low = "\n".join(texts).lower()
    found = "\n".join(texts + named + [*labels] + _calendar(results) + [v for k, v in METRICS.items()
                                                                  if re.search(rf"(?<![a-z0-9]){k}(?![a-z0-9])", low)])
    typed = {v for _, v, _, _, _ in numbers(asked, lists=True)}  # GATE-9-fix-3: "6. Alert" grounds "stage 6"
    # 1.1: a figure written in a definition's prose (a report, page or visual title, a description: three words or more
    # in one text, never an ID or a row cell) grounds that same figure exactly, like the person's own words: "the
    # 28-day trend" from a title; never rounded, a ratio or a figure of the data
    rows = set(cells)
    typed |= {v for t in texts if t not in rows and len(t.split()) >= 3 for _, v, _, _, _ in numbers(t)}
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
    # M2.2: a percentage only from a percent column or a ratio; without a minus its sign is free ("fell 51.5%" for
    # -0.515), as for any figure
    pcts = _pcts(results)
    ratios = [*pcts, *(h for h in have if 0 < abs(h) < 1)]  # x 100
    whole = [h for h in pcts if abs(h) >= 1]  # a percent column's value as it is (12.5 for 12.5%), never a fraction's
    sign = lambda h, neg: h if neg else abs(h)  # noqa: E731
    bad_nums = [(at, tok) for at, (tok, v, tol, pct, neg) in _numbers(shown, ids)
                if not (any(abs(sign(h, neg) - v) <= tol for h in (whole if pct else have))
                        or pct and any(abs(sign(h, neg) * 100 - v) <= tol for h in ratios))
                and v not in typed]

    # LLM-2-fix-21: the standard zone abbreviation (the project zone is in scope()); GATE-9-fix-2: the product's words
    words = "\n".join([*vocab, *fields, *said, "UTC", *PRODUCT])
    # LLM-2.9-fix-1, D-75: a name made of exactly the word parts of one ID, in order ("Order Count" for
    # `order_count`, "Customer ID" for `customers.customer_id`), grounds like vocab
    spelled = {" ".join(re.findall(r"[a-z0-9]+", s.lower())) for x in [*vocab, *cols]
               for s in str(x).rsplit(".", 1)}  # GATE-9-fix-10 (a): an action name spelled out ("Detect Model")
    # GATE-9-fix-2: one word part of a snake_case table/column/measure ID, or its plural ("Orders" for `order_id`,
    # chat-00), grounds the same way; other vocabulary (other IDs, a time zone, display names) only as a whole (GATE-9-fix-6)
    parts = {p for x in [*fields, *cols] for s in str(x).split(".") if re.fullmatch("[a-z][a-z0-9_]*", s) for p in s.split("_")}
    # GATE-9-fix-2: a member counts as written in the data, capitalized or in capitals ("music", "Music", "MUSIC"),
    # never in another case (the verb "shows" is not the category "Shows")
    cands = [(n, True, [(a, a + len(n))]) for a, n in _names(_zones(answer))] + [(m, False, at) for m in sorted(members) if (
        at := sorted({s for x in {m, m[:1].upper() + m[1:], m.upper()} for s in _find(x, answer, case=True)}))]
    # GATE-9-fix-10 (e): a name the person typed in this chat grounds exactly as typed (custom runs pass no `asked`,
    # D-114); (b, c): a phrase of COMMON or PRODUCT words only; (b) "Order" for an `orders` table
    part = lambda w: (f := _fold(w)) in parts or f.endswith("s") and f[:-1] in parts or f + "s" in parts  # noqa: E731
    # a yes/no column's other side: "Nonmembers", "Non-members", "Not members" for `loyalty_member` (release run 30 Oct)
    neg = lambda n: bool(m := re.fullmatch(r"(?:non[- ]?|not |no )([a-z]+)", n.lower())) and part(m[1])  # noqa: E731
    typed = lambda n: _has(n, asked, case=True) or n.endswith("s") and _has(n[:-1], asked, case=True)  # noqa: E731 ("Mondays" for "Monday")
    # M2.2: the plural of a name the results back grounds too ("other Decembers" for a December row)
    ok = lambda n, vocab_ok: _has(n, found) or n.endswith("s") and _has(n[:-1], found) or typed(n) or vocab_ok and (  # noqa: E731
        _has(n, words) or _fold(" ".join(n.lower().split())) in spelled or part(n.lower()) or neg(n)
        or all(w.lower() in COMMON or _has(w, "\n".join(PRODUCT)) for w in n.split()))
    # M2.2: a hyphenated phrase grounds when each capitalized part does ("North-region", "North-by-café": North)
    # M2.2: ordinary words opening a phrase name nothing ("Weekly North", "Total East": North, East)
    core = lambda n: " ".join(ws[next((i for i, w in enumerate(ws) if w.lower() not in COMMON), len(ws) - 1):]) if (  # noqa: E731
        ws := n.split()) else n
    # M2.2: a month or weekday opening a phrase is a word of its own ("on 30 November North had": November, North); the
    # engine's COMMON holds them
    days = {w.lower() for w in [*MONTHS, *ABBR, *DAYS, *calendar.day_abbr]}
    cal = lambda ws: len(ws) > 1 and ws[0].lower() in days and ok(ws[0], True) and ok(" ".join(ws[1:]), True)  # noqa: E731
    bad_names = [(at, n) for n, vocab_ok, at in cands if not ok(n, vocab_ok)
                 and not (vocab_ok and (core(n) != n and ok(core(n), True) or cal(n.split())))
                 and not ("-" in n and all(ok(p, vocab_ok) for p in n.split("-") if p[:1].isupper()))]
    # 1.2: a count of the items it lists, each backed as a result's own text (never by vocab)
    bad_nums = [(at, t) for at, t in bad_nums if not _listed(answer, at, t, lambda n: ok(n, False))]
    out = {"numbers": list(dict.fromkeys(t for _, t in bad_nums)), "names": list(dict.fromkeys(n for _, n in bad_names))}
    if not spans:
        return out
    # a flagged name is hidden at every occurrence in any case, also where `check` skips it (a sentence start, a
    # heading): AG-2, no unverified name stays shown, not even lower-cased (a flagged "Shows" also hides the verb)
    same = [m.span() for n in out["names"] for m in re.finditer(r"(?<![^\W_])" + re.escape(n) + r"(?![^\W_])", answer,
                                                               re.IGNORECASE)]
    return out | {"spans": [s for s, _ in bad_nums] + [s for at, _ in bad_names for s in at] + same}

MARK = "[not verified]"




def mask(answer: str, bad: dict) -> str:
    """GROUNDING-UX, GATE-9-fix-4: `answer` with exactly the flagged occurrences (`check(..., spans=True)`) replaced
    by MARK, each widened to the whole word it is glued into, hyphens included (R2, 30d, 15T05, a UUID); touching ones merge."""
    hide = [False] * len(answer)
    for a, b in bad["spans"]:
        a -= len(re.search(r"[\w-]*$", answer[:a])[0])  # GATE-9 round 4: a hyphenated ID (a UUID) whole, or each
        b += len(re.match(r"[\w-]*", answer[b:])[0])  # pass masks one part and makes the next look like "-039"
        hide[a:b] = [True] * (b - a)
    return "".join(MARK if h else "".join(c for c, _ in g) for h, g in itertools.groupby(zip(answer, hide), lambda x: x[1]))


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
            " range you work out yourself, and every name one the rows hold. Write no number in words (no \"two\","
            " \"three\", ...) and count nothing the rows list: name them, or say \"both\", \"each\", \"they\" or"
            " \"these\". For a change or a share, quote a change"
            " measure's or an expression's rows (call semantic_query), or state both values the rows give; leave out"
            " what the rows can't back. Don't mention this check, an earlier answer or a correction.\n\n"
            + (f"Rows you may quote (each result's columns, then its rows):\n{rows}" if rows else "No tool rows back this answer yet."))


def run(req: dict) -> dict:
    """One answer checked for the harness (index.ts): the flags, the draft `masked` for the retry, the `final` text for
    the last attempt (masked until it passes the same check, None: withhold it) and the retry's instruction."""
    today = date.fromisoformat(req["today"]) if req.get("today") else None
    if not numbers(req["answer"]):  # ABI checks an answer only when it states numbers (agent._ungrounded)
        return {"numbers": [], "names": []}
    seen: list = []
    results = _sampled(req.get("results", []), seen) + ([{"columns": ["shown", "omitted", "total"], "rows": seen}] if seen else [])
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
