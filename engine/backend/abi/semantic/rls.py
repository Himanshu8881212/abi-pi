"""Row-level security (PRD SE-6, SC-3, DECISIONS D-42, work/validation/E-02.md).

Rules live in the repo as security/rls.json (schemas/v1/rls.json). They restrict users whose project role is below
Editor; a restricted user missing from a rule on T sees no rows of T (default deny), and a fact related to a filtered
table sees only rows whose key the user may see. `semantic.execute` applies the context: unqualified table names
resolve to `memory.rls_<hash>.<t>` views (constant-list filters over the effective tables, never a join or subquery,
so row order holds per GATE-4-fix-1), and `check` refuses SQL that could name anything else."""

import functools
import hashlib
import json
import math
import re
import threading
import weakref
from itertools import pairwise
from typing import Annotated

import duckdb
import sqlglot
from pydantic import BaseModel, Field, StringConstraints
from sqlglot import exp
from sqlglot.dialects.duckdb import DuckDB
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from abi import actions, db, security, storage
from abi.actions import ActionFailed, action
from abi.project import SCHEMA, git
from abi.project.bundle import bundle, dumps
from abi.security import MEMBERS, Role, User, role_in
from abi.semantic import planner

RLS = git.RLS
HIDDEN = "not shown: row-level security rules apply to this project"  # in place of figures or text read from every row
MARK = "[hidden by row-level security]"  # D-47: in place of data-derived text in a restricted Viewer's inbox copy
_built: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> {schema names built}
_ctx: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> {(user, snap, rules sha): ctx}
_where: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> {schema: {table: its row filter}}
_outputs: dict = {}  # (project, bundle sha) -> unfiltered_outputs
_outputs_lock = threading.Lock()
_passed: dict = {}  # (SQL, its known table names) that `check` let through: the same pair always passes


def _file(project: str) -> dict:
    p = git.path(project) / RLS
    return json.loads(p.read_text()) if p.exists() else {}


def rules(project: str) -> list[dict]:
    """Every rule, row and column (D-110): a project with any counts as ruled wherever derived text is hidden."""
    f = _file(project)
    return f.get("rules", []) + f.get("columns", [])


def row_rules(project: str) -> list[dict]:
    return _file(project).get("rules", [])


def digest(project: str) -> str:
    """M3.6: sha256 of the definition files a restricted user's filters and plans are made from (rules, model,
    measures, datasets, ML models, sources, tables, project.json), once per read action."""
    return actions.memo(("rls.digest", project), lambda: _digest(project))


def _digest(project: str) -> str:
    root, h = git.path(project), hashlib.sha256()
    for f in [root / RLS, root / "project.json", *sorted(root.glob("model/*.json")), *sorted(root.glob("ml/*.json")),
              *sorted(root.glob("sources/*.json")), *sorted(root.glob("tables/*.json"))]:
        if f.is_file():
            h.update(f"{f.relative_to(root)}\0".encode() + f.read_bytes() + b"\0")
    return h.hexdigest()


def hidden(project: str, user_id: int | None) -> dict[str, set[str]]:
    """D-110: {table: lower-cased names of the columns hidden from this user}; {} when the user reads unfiltered."""
    return actions.memo(("rls.hidden", project, user_id), lambda: _hidden(project, user_id))


def _hidden(project: str, user_id: int | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if restricted(project, user_id):
        for r in _file(project).get("columns", []):
            out.setdefault(r["table"], set()).update(c.lower() for c in r["columns"])
    return out


def restricted(project: str, user_id: int | None) -> bool:
    """D-42: a user below Editor in a project with rules. System reads (user_id None) are never restricted."""
    if user_id is None or not rules(project):
        return False
    return actions.memo(("rls.restricted", project, user_id), lambda: _restricted(project, user_id))


def _restricted(project: str, user_id: int) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT u.workspace_owner, r.role FROM users u LEFT JOIN project_roles r"
                           " ON r.user_id = u.id AND r.project = %s WHERE u.id = %s", [project, user_id]).fetchone()
    return not row or not (row["workspace_owner"] or (row["role"] and Role[row["role"]] >= Role.editor))


def cast_lit(text, typ: str) -> str:
    """The exact value of `text` (as DuckDB renders it) in DuckDB type `typ`: only ever a quoted, escaped literal."""
    return f"CAST('{storage._lit(str(text))}' AS {typ})"


def _in(cols: list[str], keys: list[tuple]) -> str:
    """`cols IN keys` as list_contains over constants, from `keys` rows of (text, DuckDB type) per column as DuckDB
    rendered them, so `CAST('<text>' AS <type>)` is the exact value (no Python round trip: DECIMAL, TIMESTAMP_NS,
    infinity, nan). IN becomes a MARK join when DuckDB can't push it into the scan (an edited table's CASE columns,
    composite keys), and a join scrambles grid pages (GATE-4-fix-1). A key with a NULL is left out.
    ponytail: no zone-map pruning (IN pushes into the scan); IN for aggregate-only reads if restricted queries get slow"""
    vals = [[cast_lit(*k[i:i + 2]) for i in range(0, len(k), 2)] for k in keys if None not in k]
    if not vals:
        return "FALSE"
    if len(cols) == 1:
        return f"list_contains([{', '.join(v[0] for v in vals)}], {storage._q(cols[0])})"
    return (f"list_contains([{', '.join('(' + ', '.join(v) + ')' for v in vals)}],"
            f" ({', '.join(storage._q(c) for c in cols)}))")


def _int(text: str) -> tuple[bool, str]:
    """An integer for `_ch_in`: a number literal only within 64 bits. ClickHouse reads a larger bare literal as
    Float64, which rounds (2^64 + 1 matches 2^64), so it is compared as text."""
    return (False, text) if -2**63 <= int(text) < 2**64 else (True, text)


def _part(text: str | None, typ: str) -> tuple[bool, str] | None:
    """A key part for `_ch_in` from DuckDB's text and type: (is text, literal); None for NULL, nan and inf. A DECIMAL
    is text with its trailing zeros dropped, as ClickHouse prints one: as a bare literal it would be a Float64 and
    round past 15 digits (1234567890123456.01 matches .00)."""
    if text is None:
        return None
    if re.fullmatch(rf"{planner.INT}|U?HUGEINT", typ):
        return _int(text)
    if typ in ("FLOAT", "DOUBLE"):  # Float64 literals are exact for Float32/64 values
        return (False, text) if math.isfinite(float(text)) else None
    if typ.startswith("DECIMAL("):
        return True, text.rstrip("0").rstrip(".") if "." in text else text
    return (False, "1" if text == "true" else "0") if typ == "BOOLEAN" else (True, text)


def _value(v) -> tuple[bool, str] | None:
    """A rule value (JSON) as a `_ch_in` key part."""
    if isinstance(v, bool):
        return False, "1" if v else "0"
    if isinstance(v, float):
        return (False, repr(v)) if math.isfinite(v) else None
    return (True, v) if isinstance(v, str) else _int(str(v))


def _ch_in(cols: tuple[str, ...], types: dict[str, str], keys: list[tuple]) -> str:
    """ClickHouse `cols IN keys` (E-02.6), built with sqlglot, from key rows of `_part`s. A 64-bit integer or a float
    on a number column stays a number literal: ClickHouse converts those exactly in IN (Int `IN (1.6)` matches
    nothing). Any other value is text, and text is
    never parsed into the column's type (ClickHouse reads '10.005' as Decimal 10.00 and clamps '1900-01-01' to a Date of
    1970-01-01): it is compared with the column itself when that is VARCHAR, else with toString(column). A key with a
    None part is left out. ponytail: toString(column) skips the index and misses values ClickHouse prints differently
    from DuckDB (DateTime64 '…:00.000'), which hides rows, never shows them; typed literals if such keys get rules"""
    # a number goes to a number column only: ClickHouse turns `IN (0)` on a Date into 1970-01-01 (round 2)
    num = [re.fullmatch(rf"{planner.INT}|U?HUGEINT|FLOAT|DOUBLE|DECIMAL\(.*|BOOLEAN", types.get(c, "")) for c in cols]
    groups: dict[tuple, list] = {}
    for k in keys:
        if None not in k:
            k = tuple((text or not n, v) for (text, v), n in zip(k, num))
            groups.setdefault(tuple(p[0] for p in k), []).append(k)
    lit = lambda p: exp.Literal.string(p[1]) if p[0] else exp.Literal.number(p[1])
    side = lambda c, text: (exp.Anonymous(this="toString", expressions=[exp.column(c, quoted=True)])
                            if text and types.get(c) != "VARCHAR" else exp.column(c, quoted=True))
    ors = [exp.Tuple(expressions=[side(c, x) for c, x in zip(cols, kinds)]).isin(
        *(exp.Tuple(expressions=list(map(lit, r))) for r in rows)) if len(cols) > 1
           else side(cols[0], kinds[0]).isin(*(lit(r[0]) for r in rows)) for kinds, rows in groups.items()]
    return exp.paren(exp.or_(*ors)).sql("clickhouse") if ors else "FALSE"


def conditions(project: str, user_id: int, read, types: dict | None = None) -> dict[str, str]:
    """{table: the filter of every row this user may see}: its own rules ANDed, plus `fk IN (<keys>)` for each
    relationship to a filtered table (SE-2 direction, any depth). DuckDB SQL for lake tables, ClickHouse SQL for live
    ones (E-02.6). `read(sql, reads)` runs an unfiltered system read (`reads`: ClickHouse reads, as `execute`).
    `types`: {lake table: {column: DuckDB type}} of tables read as they are (no edits); M3.6: where a relationship's
    foreign key has its key's types, `fk IN (<keys>)` takes the dimension's visible keys, never a scan of t (a key
    no row of t holds matches nothing; a NULL key is left out by `_in`, as a NULL fk joins nothing)."""
    from abi.semantic import live, model  # not at top: circular imports

    lv = live.columns(project)

    @functools.cache  # fixpoint rounds repeat unchanged reads: run each once
    def keys(src: str, cols: tuple[str, ...], reads: tuple = ()) -> list:
        """Distinct `cols` rows of DuckDB `FROM src` as (text, type) pairs, sorted so the same rows give the same text
        (the fixpoint compares texts). Raises on a missing table or column."""
        q = ", ".join(f"CAST({storage._q(c)} AS VARCHAR), typeof({storage._q(c)})" for c in cols)
        return sorted(read(f"SELECT DISTINCT {q} FROM {src}", dict(reads)), key=str)

    def allowed(t: str, cols: tuple[str, ...], where: str, reads: tuple = ()) -> str:
        """The filter keeping rows of lake table t whose `cols` values pass `where` (IN semantics, a system read)."""
        try:
            return _in(cols, keys(f"{storage._q(t)} WHERE {where}", cols, reads))
        except (duckdb.Error, ActionFailed):  # a rule or relationship naming a missing table or column: deny
            return "FALSE"

    def ch(t: str, cols: tuple[str, ...], src: str, key: tuple[str, ...], reads: tuple) -> str:
        """The filter keeping rows of live table t whose `cols` are a `key` row of DuckDB `FROM src`."""
        try:
            return _ch_in(cols, lv[t], [tuple(_part(*k[i:i + 2]) for i in range(0, len(k), 2)) for k in keys(src, key, reads)])
        except (duckdb.Error, ActionFailed):
            return "FALSE"

    direct: dict[str, list] = {}
    for r in row_rules(project):
        vals, t, c = r["users"].get(str(user_id)), r["table"], r["column"]
        if t in lv:  # a live table: the rule's values as ClickHouse constants (a missing column denies)
            cond = _ch_in((c,), lv[t], [(_value(v),) for v in vals or []]) if c in lv[t] else "FALSE"
        else:
            where = exp.column(c, quoted=True).isin(*map(exp.convert, vals)) if vals else exp.false()
            cond = allowed(t, (c,), where.sql("duckdb"))
        direct.setdefault(t, []).append(cond)
    rels = model._read(project, "model.json").get("relationships", [])

    def step(t: str, conds: dict) -> list:
        cs = list(direct.get(t, []))
        for r in rels:
            d = r["to"]["table"]
            if r["from"]["table"] == t and (dc := conds.get(d)):
                # ponytail: the dimension's visible keys are inlined as constants; a key list of millions makes
                # a long view text, push the rule into a lake table if dimensions that large get rules
                fk, key = tuple(r["from"]["columns"]), tuple(r["to"]["columns"])
                if d in lv:  # its visible keys: a ClickHouse read (no snapshot: `context` doesn't cache these)
                    q = exp.select(*(exp.column(k, quoted=True) for k in key)).from_(exp.to_table(
                        live.table_sql(project, d), dialect="clickhouse")).where(" AND ".join(dc), dialect="clickhouse")
                    src, reads = '"__d" __d', (("__d", (d, q.sql("clickhouse"))),)
                else:
                    src, reads = f"(SELECT * FROM {storage._q(d)} WHERE {' AND '.join(dc)}) __d", ()
                if t in lv:
                    cs.append(ch(t, fk, src, key, reads))
                elif d not in lv and all((types or {}).get(t, {}).get(f, 1) == (types or {}).get(d, {}).get(k, 2)
                                         for f, k in zip(fk, key)):
                    try:
                        cs.append(_in(fk, keys(src, key)))
                    except (duckdb.Error, ActionFailed):
                        cs.append("FALSE")
                else:
                    on = " AND ".join(f"__d.{storage._q(k)} = {storage._q(t)}.{storage._q(f)}" for f, k in zip(fk, key))
                    cs.append(allowed(t, fk, f"EXISTS (SELECT 1 FROM {src} WHERE {on})", reads))
        return cs

    # A row is visible when it passes its own rules and every row it points to is visible: the greatest fixpoint,
    # so relationship cycles and self-loops (E-02.2-fix-1) stay complete. Visible sets only shrink, so it ends.
    # ponytail: one round per hop of the longest chain of hidden rows (a deep self-referencing hierarchy means many
    # rounds); a recursive CTE per cycle if that gets slow
    tables = sorted(set(direct) | {r["from"]["table"] for r in rels})
    conds, last = {t: cs for t in tables if (cs := direct.get(t))}, None
    while conds != last:
        last, conds = conds, {t: cs for t in tables if (cs := step(t, conds))}
    return {t: " AND ".join(cs) for t, cs in conds.items()} | {t: "FALSE" for t in unfiltered_outputs(project)}


def where(con, schema: str, t: str) -> str | None:
    """M3.6: the row filter user schema `schema`'s view of t applies (None: no filter), as `context` built it; raises
    KeyError when that schema has no view of t."""
    return _where[con][schema][t]


def key_columns(project: str, t: str) -> list[str]:
    """M3.6: the columns of t that any user's filter of t reads (`conditions`): its row rules' columns and the foreign
    keys of its relationships into filtered tables. The same for every user."""
    from abi.semantic import model  # not at top: model imports the semantic package

    done, rels = filtered(project), model._read(project, "model.json").get("relationships", [])
    return list(dict.fromkeys([r["column"] for r in row_rules(project) if r["table"] == t] + [
        c for r in rels if r["from"]["table"] == t and r["to"]["table"] in done for c in r["from"]["columns"]]))


def filtered(project: str) -> set[str]:
    """Tables a rule filters: ruled ones plus every table with a relationship into one (SE-2 direction, any depth)."""
    from abi.semantic import model  # not at top: model imports the semantic package

    done = {r["table"] for r in row_rules(project)}
    rels = model._read(project, "model.json").get("relationships", [])
    while new := {r["from"]["table"] for r in rels if r["to"]["table"] in done} - done:
        done |= new
    return done


def unfiltered_outputs(project: str) -> list[str]:
    """Tables built from rows a rule filters but not filtered by those same rules: TR-1 tables whose merge/append steps
    read a filtered table or whose source step loads a filtered table's source (`source:<t>`, any depth,
    H-01.4-fix-1) and ML predictions tables (ml_<id>, ML-5). A table with no rule of its own counts as filtered only
    when its relationships reach every ruled table its inputs are filtered by (a relationship to another ruled table
    doesn't hide the input's rows). D-110: also every table built from a table with a column rule unless it has a
    column rule of its own (its Owner chose what it hides). Restricted Viewers see none of their rows (default deny);
    readiness names them. ponytail: table-level lineage, so an output that never read a hidden column is denied too;
    column lineage if that bites. M3.6: kept per bundle content (any change to the folder is a new key)."""
    if not rules(project):
        return []
    sha = lambda: hashlib.sha256(dumps(bundle(git.path(project))).encode()).hexdigest()
    if (hit := _outputs.get(key := (project, sha()))) is None:
        hit = _unfiltered_outputs(project)
        with _outputs_lock:
            if sha() == key[1]:  # unchanged while it ran: the result is this content's
                _outputs[key] = hit
            while len(_outputs) > 64:  # oldest first
                del _outputs[next(iter(_outputs))]
    return list(hit)


def _unfiltered_outputs(project: str) -> list[str]:
    from abi.semantic import lineage, model  # noqa: I001 - not at top: both import the semantic package

    ruled, cols = {r["table"] for r in row_rules(project)}, {r["table"] for r in _file(project).get("columns", [])}
    if not ruled | cols:
        return []
    rels = model._read(project, "model.json").get("relationships", [])

    def roots(t: str) -> set[str]:
        """The ruled tables whose rules filter t: t itself if ruled, and every one its relationships reach."""
        seen, todo = {t}, [t]
        while todo:
            x = todo.pop()
            todo += (new := {r["to"]["table"] for r in rels if r["from"]["table"] == x} - seen)
            seen |= new
        return seen & ruled

    g = lineage.graph(bundle(git.path(project)))
    ups = lambda n: {u.split(":", 1)[1] for u in lineage.reach(g, {n}, "upstream") if u.startswith(("table:", "source:"))}
    return sorted(t for n in g if n.startswith("table:") and (
        (t := n[6:]) not in ruled and set().union(*map(roots, ups(n))) - roots(t) or t not in cols and ups(n) & cols))


def explain(project: str, user_id: int) -> str:
    """RP-5 for a restricted user: SQL comment lines naming the rows each filtered table keeps for them (their own
    rule values only, never another user's), prefixed to the SQL "How is this calculated?" shows."""
    from abi.semantic import model  # not at top: model imports the semantic package

    lines = [f"-- {r['table']}: columns {', '.join(r['columns'])} are hidden from you (D-110)"
             for r in _file(project).get("columns", [])]
    for r in row_rules(project):
        vals = r["users"].get(str(user_id))
        cond = (exp.column(r["column"], quoted=True).isin(*map(exp.convert, vals)).sql("duckdb") if vals
                else "no rows (you are not in this rule)")
        lines.append(f"-- {r['table']}: {cond}")
    rels, done = model._read(project, "model.json").get("relationships", []), filtered(project)
    # every relationship into a filtered table is ANDed (`conditions`), also onto a table with its own rule or a cycle
    lines += [f"-- {t}: only rows whose {fk} is a row of {d} you may see" for t, d, fk in sorted(
        {(r["from"]["table"], r["to"]["table"], ", ".join(r["from"]["columns"])) for r in rels if r["to"]["table"] in done})]
    lines += [f"-- {t}: no rows (made from filtered rows; it needs a rule of its own)" for t in unfiltered_outputs(project)]
    return "-- Row-level security (SE-6): every table below is read through these filters\n" + "\n".join(lines) + "\n"


def context(project: str, user_id: int, con, snap: int, edited: frozenset, model_columns: dict, read) -> tuple[str, dict]:
    """(the search-path schema of a restricted user's views, {live table: (its ClickHouse filter or None, columns
    hidden from the user)}), built once per lake
    connection and snapshot. The schema name hashes the view SQL, so it is also the user's part of the SE-7 cache key.
    Filters that read a live table's rows (a relationship into one) are rebuilt per call: live data has no snapshot.
    M3.3: one build per user at a time, so their reads at once (a page's visuals) wait for it instead of each making
    it (six cold visuals at once took 1.96 s, one after another 1.10 s)."""
    with storage.project_lock(f"rls user {user_id}", project):
        return _context(project, user_id, con, snap, edited, model_columns, read)


def _context(project: str, user_id: int, con, snap: int, edited: frozenset, model_columns: dict, read) -> tuple[str, dict]:
    from abi.semantic import live, model  # not at top: circular imports

    # rules + relationships make the filters; datasets + ML models the ml_<id> denials, sources + tables live columns
    key = (user_id, snap, digest(project))
    lv = live.columns(project)
    fresh = any(r["to"]["table"] in lv for r in model._read(project, "model.json").get("relationships", []))
    with storage.project_lock("rls", project):  # M3.1: one project's view build never stalls another's reads
        hit = None if fresh else _ctx.setdefault(con, {}).get(key)
    if hit:
        return hit
    # built from the files as they are now, kept under the action's digest only if that is still theirs (a rule
    # committed since the action began, or during this build, would put one version's views under another's key)
    fresh = fresh or _digest(project) != key[2]
    conds = conditions(project, user_id, read, {t: c for t, c in model_columns.items() if t not in edited})
    hid = _hidden(project, user_id)

    def drop(t: str, cols) -> tuple[str | None, list[str]]:
        """D-110: (t's row filter, its columns to leave out); a table whose every column is hidden shows no rows
        (an EXCLUDE of every column, or of one the table lacks, is an error)."""
        out = [c for c in cols if c.lower() in hid.get(t, ())]
        return ("FALSE", []) if out and len(out) == len(cols) else (conds.get(t), out)

    views, wheres = {}, {}
    for t, cols in model_columns.items():
        if t in conds or t in hid:
            where, out = drop(t, cols)
            wheres[t] = where
            views[t] = (f"SELECT *{' EXCLUDE (' + ', '.join(map(storage._q, out)) + ')' if out else ''} FROM"
                        f" {'memory.effective' if t in edited else 'lake.model'}.{storage._q(t)}" + (f" WHERE {where}" if where else ""))
    schema = "rls_" + hashlib.sha256(json.dumps(views, sort_keys=True).encode()).hexdigest()[:16]
    out = schema, {t: drop(t, cols) for t, cols in lv.items() if t in conds or t in hid}
    with storage.project_lock("rls", project):
        if schema not in _built.setdefault(con, set()):
            cur = con.cursor()
            cur.begin()
            try:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS memory.{schema}")
                for t, sql in views.items():
                    cur.execute(f"CREATE OR REPLACE VIEW memory.{schema}.{storage._q(t)} AS {sql}")
                cur.commit()
            except BaseException:
                cur.rollback()
                raise
            _built[con].add(schema)  # ponytail: schemas are never dropped (a few views each); drop old ones if many
        _where.setdefault(con, {})[schema] = wheres
        cache = _ctx.setdefault(con, {})
        for k in [k for k in cache if k[1] < snap]:  # older snapshots never come back
            del cache[k]
        if not fresh and _digest(project) == key[2]:
            cache[key] = out
    return out


# `AS NOT MATERIALIZED (`, `* EXCLUDE (`, `COLUMNS(*)` (star expression, the grid read): tokens before "(" that call nothing
_SYNTAX = {"materialized", "exclude", "columns"}


def _name(f: exp.Func) -> str:
    return (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()


def refused_calls(sql: str, tree: exp.Expression) -> set[str]:
    """Calls outside ALLOWED: every `name(` in the text as DuckDB reads it (sqlglot may rename a function), and every
    function node sqlglot parses, including keyword-named ones (glob, range, unnest, show)."""
    toks = DuckDB().tokenize(sql)
    return {t.text.lower() for t, nxt in pairwise(toks) if nxt.token_type == TokenType.L_PAREN and t.token_type in (
        TokenType.VAR, TokenType.IDENTIFIER) and t.text.lower() not in ALLOWED | _SYNTAX} | {
        _name(f) for f in tree.find_all(exp.Func) if _name(f) not in _NODES}


# D-44: the only DuckDB functions restricted SQL may call. A denylist was never complete (time travel, extension table
# functions, catalog-reading macros like pg_get_viewdef, stats() showing the base table's min/max). The planner's own
# functions (test_SE6_planner_functions_allowed fails when it emits a new one: add it here) ...
ALLOWED = {"count", "sum", "avg", "min", "max", "median", "list_median", "abs", "floor", "round", "least", "coalesce",
           "nullif", "if", "grouping", "grouping_id", "row_number", "regexp_full_match", "typeof", "date_trunc", "cast",
           "try_cast",
           # ... and reviewed pure scalar functions (no reads, no state)
           "ceil", "greatest", "lower", "upper", "length", "concat", "substring", "trim", "year", "quarter", "month",
           "day", "strftime", "date_diff", "sqrt", "power", "ln", "exp", "sign",
           "timezone",  # SE-13: an instant's local time in a named zone (planner.day)
           "error",  # SE-13: raises its own text (a missing currency rate, planner._checked); reads nothing
           "approx_count_distinct"}  # DC-1 docs table profile (docs._profile): a HyperLogLog aggregate; reads nothing
# the same names as sqlglot's expression names (date_trunc is timestamp_trunc), plus syntax it parses as functions
_NODES = {"and", "or", "case", "cast", "try_cast", "array", "columns"} | {
    _name(f) for n in ALLOWED - {"cast", "try_cast"}
    for f in sqlglot.parse_one(f"SELECT {n}(1, 2, 3)", read="duckdb", error_level=ErrorLevel.IGNORE).find_all(exp.Func)}
_PLAIN = {"this", "alias", "joins", "laterals", "pivots", "sample"}  # never `when`/`version`: AT (VERSION => n) skips views


def check(project: str, user_id: int, sql: str, known: set[str]) -> None:
    """Refuse (403, audited) a restricted user's DuckDB SQL unless it is one SELECT or DESCRIBE that names only
    unqualified known tables or CTEs visible in its scope: a qualified name (lake.model.t, lake.query copies,
    lake.edits, raw), a table function (read_csv, query_table, duckdb_tables) or a file name ('x.csv', replacement
    scan) would read around the views. M3.6 round 2: the answer depends on `sql` and `known` only, so SQL that passed
    once with those names passes again unparsed (a planned visual's SQL is the same for every person)."""
    key = (sql, frozenset(k.lower() for k in known))
    if key in _passed:
        return
    try:
        [tree] = sqlglot.parse(sql, read="duckdb")
        if not isinstance(tree, exp.Query | exp.Describe):
            raise ValueError("not a SELECT")  # noqa: TRY004 caught below
        ctes = {id(t) for sc in traverse_scope(tree) for t in sc.tables if t.name in sc.cte_sources} \
            if isinstance(tree, exp.Query) else set()
        known = {k.lower() for k in known}
        for t in tree.find_all(exp.Table):
            if t.catalog or t.db or not isinstance(t.this, exp.Identifier) or {k for k, v in t.args.items() if v} - _PLAIN or (
                    id(t) not in ctes and t.name.lower() not in known):
                raise ValueError(f"reads {t.sql('duckdb')}")
        if bad := refused_calls(sql, tree):
            raise ValueError(f"calls {', '.join(sorted(bad))}")
        with _outputs_lock:
            _passed[key] = None
            while len(_passed) > 4096:  # oldest first
                del _passed[next(iter(_passed))]
    except Exception as e:  # noqa: BLE001 anything we can't prove safe is refused
        with db.connect() as conn:
            reason = f"<sha256:{actions.sha(str(e))}>" if actions.inbound() else str(e)
            actions.audit(conn, security.user(conn, user_id), actions.VIA.get(), "execute", project,
                          actions.hashed({"sql": sql}) if actions.inbound() else {"sql": sql}, 403, reason)
        raise ActionFailed(403, "Row-level security: this read names a table or function outside the model"
                                f" tables ({reason}).") from None


class Rule(BaseModel):
    project: str
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    table: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,65}$")  # ml_<model id> may hold a hyphen
    column: str = Field(min_length=1)
    users: dict[Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")], list[str | int | float | bool]] = Field(
        description="{user ID: the values of `column` this Viewer may see}; a Viewer not listed sees no rows")


class ColumnRule(BaseModel):
    project: str
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    table: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,65}$")
    columns: list[Annotated[str, StringConstraints(min_length=1)]] = Field(
        min_length=1, description="the columns of `table` hidden from Viewers")


class RuleRef(BaseModel):
    project: str
    id: str


def share_links(conn, project: str) -> list[dict]:
    """D-82: the project's working share links, which its first rule revokes (listed in that change's preview)."""
    return conn.execute("SELECT id, report, page FROM share_links WHERE project = %s AND revoked IS NULL"
                        " AND expires > now() ORDER BY id", [project]).fetchall()


def revoking(n: int) -> str:
    return f"It also revokes the project's {n} share link(s): share links are off in projects with row-level security rules."


def _save(conn, project: str, drop: str, add: dict | None, what: str) -> None:
    """D-42: every rule change needs the Owner (the action's role) and a confirmation showing old and new rules of
    both kinds (D-82: and the share links a first rule revokes). Rule `drop` goes and `add` (a row or column rule)
    comes: one ID namespace. Then D-47/D-82 for a first rule."""
    f, old = _file(project), rules(project)
    parts = {k: [r for r in f.get(k, []) if r["id"] != drop] for k in ("rules", "columns")}
    if add:
        parts["rules" if "column" in add else "columns"].append(add)
    new = parts["rules"] + parts["columns"]
    links = share_links(conn, project) if new and not old else []
    actions.require_confirmation(f"This {what} changes who sees which rows or columns." + (f" {revoking(len(links))}" if links else ""),
                                 {"rules": {"old": old, "new": new}} | ({"revoke_share_links": links} if links else {}))
    p = git.path(project) / RLS
    p.parent.mkdir(exist_ok=True)
    p.write_text(dumps({"$schema": SCHEMA + "rls.json", "rules": parts["rules"]} | ({"columns": parts["columns"]} if parts["columns"] else {})))
    if new and not old:
        first_rule(conn, project)


def first_rule(conn, project: str) -> None:
    """D-47: the project's first rule replaces stored TL-4 failure text (it may hold rows every member reads) now, not
    at the minute retest; the tools stay disabled until that retest (rls.json is in their stamp). D-82: it revokes
    every share link of the project (the confirmation listed them)."""
    conn.execute("UPDATE share_links SET revoked = coalesce(revoked, now()) WHERE project = %s", [project])
    conn.execute("UPDATE tool_state SET error = %s WHERE project = %s AND error IS NOT NULL",
                 [f"Tests failed before row-level security rules were added; re-testing ({HIDDEN}).", project])


@action(Role.owner, definition=True)
def set_rls_rule(conn, user: User, args: Rule) -> dict:
    """Adds or replaces a row-level security rule: Viewers see only rows of `table` whose `column` holds one of
    their listed values (SE-6)."""
    _save(conn, args.project, args.id, args.model_dump(exclude={"project"}), "rule")
    return {}


@action(Role.owner, definition=True)
def set_column_rule(conn, user: User, args: ColumnRule) -> dict:
    """Adds or replaces a column-level security rule: Viewers never see the values of these columns of `table` (no
    reads, exports or measures over them; D-110). Delete it with delete_rls_rule."""
    _save(conn, args.project, args.id, args.model_dump(exclude={"project"}), "column rule")
    return {}


def outsider(i: int) -> str:
    """D-106: the label of the i-th user named in one output who isn't a member: letters (A..Z, AA..), never an ID or a
    number, so an answer quoting it holds no figure."""
    return f"user not in this project ({chr(65 + i % 26) * (i // 26 + 1)})"


@action(Role.viewer)
def rls_rules(conn, user: User, args: git.ProjectRef) -> dict:
    """The project's row-level security rules (SC-3) and column rules (`columns`: columns hidden from Viewers, D-110).
    A restricted Viewer gets only their own values per row rule, never another user's; Owners and Editors get every
    rule with user names, and Owners the accounts to pick from."""
    rs, cols = row_rules(args.project), _file(args.project).get("columns", [])
    if restricted(args.project, user.id):
        return {"restricted": True, "rules": [{"table": r["table"], "column": r["column"], "values": r["users"].get(str(user.id), [])}
                                              for r in rs]} | ({"columns": [{"table": r["table"], "columns": r["columns"]} for r in cols]} if cols else {})
    names = {str(r["id"]): r["name"] for r in conn.execute(f"SELECT id, name FROM users WHERE {MEMBERS}", [args.project, args.project])}
    others = sorted({u for r in rs for u in r["users"]} - names.keys(), key=lambda u: (len(u), u))  # D-106: no ID shown
    names |= {u: outsider(i) for i, u in enumerate(others)}
    out = {"restricted": False, "rules": [r | {"users": [{"id": u, "name": names[u], "values": v} for u, v in r["users"].items()]}
                                          for r in rs]} | ({"columns": cols} if cols else {})
    if role_in(conn, user, args.project) == Role.owner:
        out["accounts"] = conn.execute("SELECT u.id::text AS id, u.name, CASE WHEN u.workspace_owner THEN 'owner' ELSE r.role END AS role"
                                       " FROM users u LEFT JOIN project_roles r ON r.user_id = u.id AND r.project = %s"
                                       f" WHERE {MEMBERS} ORDER BY u.name", [args.project] * 3).fetchall()
    return out


@action(Role.owner, definition=True)
def delete_rls_rule(conn, user: User, args: RuleRef) -> dict:
    """Deletes a row-level or column-level security rule."""
    if not any(r["id"] == args.id for r in rules(args.project)):
        raise ActionFailed(404, f"No security rule '{args.id}'.")
    _save(conn, args.project, args.id, None, "deletion")
    return {}
