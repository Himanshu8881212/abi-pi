"""Row-level security (PRD SE-6, SC-3, DECISIONS D-42, work/validation/E-02.md).

Rules live in the repo as security/rls.json (schemas/v1/rls.json). They restrict users whose project role is below
Editor; a restricted user missing from a rule on T sees no rows of T (default deny), and a fact related to a filtered
table sees only rows whose key the user may see. `semantic.execute` applies the context: unqualified table names
resolve to `memory.rls_<hash>.<t>` views (filters over the effective tables that are constant lists or, for the keys a
follow link gives, a cast to an ENUM type of those keys, `memory.rls_keys.k_<hash>`: never a join or subquery, so row
order holds per GATE-4-fix-1; 1.2.7 M0 measured that a semi-join and a list read from a table both lose it,
bench/RESULTS.md), and `check` refuses SQL that could name anything else, a type too."""

import contextlib
import functools
import hashlib
import json
import math
import re
import secrets
import threading
import time
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
_keys: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> its key types (`_key_type`)
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
    if out:  # 1.2.6 M9: a predictions table's copy of a hidden key (ml_<id>.<table>_<key>) is hidden with it
        from abi.semantic import model  # not at top: model imports the semantic package

        for r in model._read(project, "model.json").get("relationships", []):
            t = r["from"]["table"]
            if t.startswith("ml_"):  # with its model or after it (a deleted model's table stays)
                out.setdefault(t, set()).update(f.lower() for f, c in zip(r["from"]["columns"], r["to"]["columns"])
                                                if c.lower() in out.get(r["to"]["table"], ()))
        out = {t: cs for t, cs in out.items() if cs}
    return out


def same_as(t: str, cols) -> bool:
    """1.2.6 M11: table `t` with columns `cols` is a decisions table (semantic/same.py): named same_as_<table>, with
    exactly the columns a, b and same, in any order. The one test of it: such a table is written by decide_same only
    (writeback._batch), shows restricted readers no rows (`_context`) and is the only shape the views resolve from."""
    return t.startswith("same_as_") and set(cols) == {"a", "b", "same"}


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


# Without a key type (`conditions` called with no `keep`: the tests' oracle, never a person's views since 1.2.7 M0):
IN_SQL = True  # a follow link's key list is written by DuckDB (`conditions.listed`); False: by `_in`, key by key (tests compare the two)
SETTLE = True  # `conditions` settles rules and relationships before the follow links join; False: all in one iteration


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


def _list_sql(col: str, src: str) -> str:
    """The read that writes `_in`'s list for one column itself: one row of (every value's type is a plain type name,
    the distinct non-NULL `col` values of DuckDB `FROM src` as `CAST('<text>' AS <type>), …`); (NULL, NULL) when there
    is none. The type's name goes into the text unquoted, so only names made of capitals, digits, spaces and a
    (precision[, scale]) count as plain; `conditions.listed` takes any other column the earlier way."""
    return ("SELECT bool_and(regexp_full_match(typeof(c), '[A-Z0-9_ ]+(\\(\\d+(,\\s*\\d+)?\\))?')),"
            " string_agg('CAST(''' || replace(CAST(c AS VARCHAR), '''', '''''') || ''' AS ' || typeof(c) || ')', ', '"
            f" ORDER BY CAST(c AS VARCHAR), typeof(c)) FROM (SELECT DISTINCT {storage._q(col)} AS c FROM {src}) WHERE c IS NOT NULL")


KEEP_S = 600  # a key type of an older snapshot stays this long after a newer snapshot's build: a read that began before it ends
_SALT = secrets.token_hex(16)  # in every key type's name: not to be worked out from the keys or the rules


def _key_text(cols) -> str:
    """One text per key of `cols`, the same for the same key and never for two: a column's own text as DuckDB renders its
    type (an ENUM column's label: no type name is ever written), several columns each with its length before it. NULL
    when a column is NULL."""
    parts = [f"CAST({storage._q(c)} AS VARCHAR)" for c in cols]
    return parts[0] if len(parts) == 1 else " || ".join(f"CAST(length({p}) AS VARCHAR) || ':' || {p}" for p in parts)


def _key_test(cols, name: str) -> str:
    """The filter keeping rows whose `cols` are a key of ENUM type `name`: a cast inside the scan (a hash lookup, no
    join, so row order holds). A key the type lacks, a NULL and an empty type keep no row, and none of them raises."""
    return f"TRY_CAST({_key_text(cols)} AS {name}) IS NOT NULL"


def _key_type(project: str, con, snap: int, src: str, cols) -> str:
    """1.2.7 M0: the ENUM type in `memory.rls_keys` of the distinct non-NULL keys `cols` of DuckDB `FROM src` (a system
    read of the source rows at `con`, logged as the key reads were), for `_key_test`. Named by its keys and a salt of
    this process: the same keys are one type (two people with the same condition share it, and the fixpoint of
    `conditions` settles when the keys do), and the name can't be guessed. Read once per snapshot and read; raises
    duckdb.Error on a missing table or column (the caller denies)."""
    from abi import semantic  # not at top: it imports this module

    sql = f"SELECT DISTINCT k FROM (SELECT {_key_text(cols)} AS k FROM {src}) WHERE k IS NOT NULL"
    # ponytail: 16 locks per project, so two people's different key reads may wait for each other; one per read if it shows
    with storage.project_lock(f"rls keys {int(hashlib.sha256(sql.encode()).hexdigest()[:2], 16) % 16}", project):
        with storage.project_lock("rls", project):
            held = _keys.setdefault(con, {"names": {}, "types": {}})
            name = held["names"].get((snap, sql))
        if name is None or name not in held["types"]:
            cur, start, rows, error = con.cursor(), time.perf_counter(), None, None
            try:
                semantic._effective(cur, con, snap)
                cur.execute(f"CREATE OR REPLACE TEMP TABLE __rls_k AS {sql}")  # the cursor's own: no other read sees it
                rows, digest = cur.execute("SELECT count(*), sha256(coalesce(string_agg(CAST(length(k) AS VARCHAR) || ':' || k, ''"
                                           " ORDER BY k), '')) FROM __rls_k").fetchone()
                name = "k_" + hashlib.sha256(f"{_SALT}{digest}".encode()).hexdigest()[:32]
                with storage.project_lock("rls", project):  # one catalog write of the schema at a time
                    if name not in held["types"]:
                        cur.execute("CREATE SCHEMA IF NOT EXISTS memory.rls_keys")
                        cur.execute(f"DROP TYPE IF EXISTS memory.rls_keys.{name}")
                        cur.execute(f"CREATE TYPE memory.rls_keys.{name} AS ENUM (SELECT k FROM __rls_k)")
                        held["types"][name] = [snap, None]
                    held["names"][snap, sql] = name
            except duckdb.Error as e:
                error = str(e)
                raise
            finally:
                with contextlib.suppress(duckdb.Error):
                    cur.execute("DROP TABLE IF EXISTS __rls_k")
                logged = actions.redacted(sql) if actions.inbound() else sql  # D-32, as `semantic._partials`
                semantic._log(None, project, {"rls": "keys"}, logged, snap, False, rows,
                              start, error and f"<sha256:{actions.sha(error)}>" if actions.inbound() else error)
        with storage.project_lock("rls", project):
            if (at := held["types"].get(name)) and snap >= at[0]:
                at[:] = [snap, None]  # in use at this snapshot
    return f"memory.rls_keys.{name}"


def _sweep_keys(con, snap: int) -> None:
    """Key types no build of snapshot `snap` or newer used go, KEEP_S after the first such build (under the "rls" lock).
    A view of an older snapshot that still names one then fails, closed."""
    held, now = _keys.get(con), time.monotonic()
    if not held:
        return
    held["names"] = {k: v for k, v in held["names"].items() if k[0] >= snap}
    for name, at in list(held["types"].items()):
        if at[0] < snap:
            at[1] = at[1] or now
            if at[1] <= now - KEEP_S:
                with contextlib.suppress(duckdb.Error):
                    con.cursor().execute(f"DROP TYPE IF EXISTS memory.rls_keys.{name}")
                del held["types"][name]


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


def conditions(project: str, user_id: int, read, types: dict | None = None, keep=None) -> dict[str, str]:
    """{table: the filter of every row this user may see}: its own rules ANDed, plus `fk IN (<keys>)` for each
    relationship to a filtered table (SE-2 direction, any depth). DuckDB SQL for lake tables, ClickHouse SQL for live
    ones (E-02.6). `read(sql, reads)` runs an unfiltered system read (`reads`: ClickHouse reads, as `execute`).
    `types`: {lake table: {column: DuckDB type}} of tables read as they are (no edits); M3.6: where a relationship's
    foreign key has its key's types, `fk IN (<keys>)` takes the dimension's visible keys, never a scan of t (a key
    no row of t holds matches nothing; a NULL key is left out by `_in`, as a NULL fk joins nothing).
    1.2.6 M3: an ontology link with `rls: follow` gives its `from` table F `key IN (<keys of F linked to visible rows
    of its `to` table T>)` once T is filtered, read over the link's `via` through the visible rows of every table on
    the way (a system read; only those keys are inlined, so views stay constant lists). That filter travels on to
    tables pointing at F, but never back along the link's own `via` relationships (a NULL foreign key there, as an
    anonymous order, would hide the row).
    1.2.7 M0: with `keep(src, cols)` (`_context`'s: `_key_type` on the reader's lake connection) every filter part made
    from a follow link, on F and on the tables pointing at it, tests its columns against a key type instead of holding
    the keys (25,000 customers were 684 KB of text in every view of them): the same rows, in the same order. Without
    `keep` the keys are inlined as before: what the tests compare with."""
    from abi.semantic import live, model, ontology  # not at top: circular imports

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
    mj = model._read(project, "model.json")
    rels, fl, ent = mj.get("relationships", []), follows(project, mj), ontology._keys(mj)
    back = {f["id"]: f["back"] for f in fl}
    vis = lambda x, conds: " AND ".join(c for _, c in conds.get(x, []))  # noqa: E731
    denied = unfiltered_outputs(project)  # once per build: it hashes every file of the project
    blank, hid = (_blanks(project, user_id, types, denied), _hidden(project, user_id)) if fl else (None, {})

    @functools.cache  # fixpoint rounds repeat unchanged reads: run each once
    def held(src: str, cols: tuple[str, ...], on: tuple[str, ...]) -> str:
        """The filter keeping rows whose `on` columns are a `cols` row of DuckDB `FROM src`, by a key type; a missing
        table or column denies."""
        try:
            return _key_test(on, keep(src, cols))
        except (duckdb.Error, ActionFailed):
            return "FALSE"

    def groups(r: dict, conds: dict) -> list[tuple[frozenset, list[str]]]:
        """The parts of r's target's filter that cross r (`crosses`), one group per tag (the follow link a part is
        made from, or none). Onto the target's key, `fk IN (keys of d WHERE A AND B)` keeps the rows `fk IN (A's) AND
        fk IN (B's)` do, so each group is its own filter; onto other columns the parts that cross go as one, untagged
        (filtered further on, never less)."""
        parts = conds.get(d := r["to"]["table"], [])
        if fl:
            parts = [(tag, c) for tag, c in parts if crosses(back, r, tag)]
        if not parts:
            return []
        if not fl or sorted(r["to"]["columns"]) != sorted(ent.get(d) or [None]):
            return [(frozenset(), [c for _, c in parts])]
        out: dict[frozenset, list] = {}
        for tag, c in parts:
            out.setdefault(tag, []).append(c)
        return list(out.items())

    def linked(f: dict, conds: dict) -> str:
        """The filter keeping F's rows linked to a visible row of T along the link's `via`: the values of F's columns
        the last hop joins on, read by a system read through the visible rows of every table on the way."""
        at, where = f["to"], vis(f["to"], conds)
        for r, along in f["hops"]:
            mine, theirs = (r["from"], r["to"]) if along else (r["to"], r["from"])
            sub = f"SELECT {', '.join(map(storage._q, mine['columns']))} FROM {storage._q(at)}" + (f" WHERE {where}" if where else "")
            at = theirs["table"]
            where = " AND ".join(x for x in (f"{ontology._tuple(theirs['columns'])} IN ({sub})", vis(at, conds) if at != f["from"] else "") if x)
        if keep:
            return held(f"{storage._q(at)} WHERE {where}", tuple(f["cols"]), tuple(f["cols"]))
        return listed(at, f["cols"][0], where) if IN_SQL and len(f["cols"]) == 1 else allowed(at, tuple(f["cols"]), where)

    @functools.cache  # fixpoint rounds repeat unchanged reads: run each once
    def listed(t: str, col: str, where: str) -> str:
        """`allowed` for one column, the list's text made by DuckDB itself: the same values as `_in` gives (each as
        CAST('<text>' AS <type>), a NULL left out; the same rows, ordered by their text), without a Python step per key.
        Tens of thousands of keys per person, for many people at once, kept every other reader waiting on that step
        (M10: 50 Viewers, 25,000 customers each: page p95 15 s). Raises nothing: a missing table or column denies."""
        try:
            got = read(_list_sql(col, f"{storage._q(t)} WHERE {where}"), {})
        except (duckdb.Error, ActionFailed):
            return "FALSE"
        if got and got[0][0] is False:  # a type whose name is no plain word (an ENUM's holds its values): the earlier way
            return allowed(t, (col,), where)
        return f"list_contains([{got[0][1]}], {storage._q(col)})" if got and got[0][1] else "FALSE"

    def step(t: str, conds: dict, follow: bool = True) -> list:
        """t's filter as (tag: the follow link it is made from, or none; SQL) parts; without `follow`: the rules and
        relationships alone."""
        cs = [(frozenset(), c) for c in direct.get(t, [])]
        for r in rels:
            if r["from"]["table"] != t:
                continue
            for tag, dc in groups(r, conds):
                d = r["to"]["table"]
                # ponytail: the dimension's visible keys are inlined as constants; a key list of millions makes
                # a long view text, push the rule into a lake table if dimensions that large get rules
                fk, key = tuple(r["from"]["columns"]), tuple(r["to"]["columns"])
                if d in lv:  # its visible keys: a ClickHouse read (no snapshot: `context` doesn't cache these)
                    q = exp.select(*(exp.column(k, quoted=True) for k in key)).from_(exp.to_table(
                        live.table_sql(project, d), dialect="clickhouse")).where(" AND ".join(dc), dialect="clickhouse")
                    src, reads = '"__d" __d', (("__d", (d, q.sql("clickhouse"))),)
                else:
                    src, reads = f"(SELECT * FROM {storage._q(d)} WHERE {' AND '.join(dc)}) __d", ()
                same = d not in lv and all((types or {}).get(t, {}).get(f, 1) == (types or {}).get(d, {}).get(k, 2) for f, k in zip(fk, key))
                if t in lv:
                    cs.append((tag, ch(t, fk, src, key, reads)))
                elif tag and keep and d not in lv:  # made from a follow link: d's visible keys are as many as the link's
                    on = " AND ".join(f"__d.{storage._q(k)} = {storage._q(t)}.{storage._q(f)}" for f, k in zip(fk, key))
                    cs.append((tag, held(src, key, fk) if same else held(f"{storage._q(t)} WHERE EXISTS (SELECT 1 FROM {src} WHERE {on})", fk, fk)))
                elif d not in lv and all((types or {}).get(t, {}).get(f, 1) == (types or {}).get(d, {}).get(k, 2)
                                         for f, k in zip(fk, key)):
                    try:
                        cs.append((tag, _in(fk, keys(src, key))))
                    except (duckdb.Error, ActionFailed):
                        cs.append((tag, "FALSE"))
                else:
                    on = " AND ".join(f"__d.{storage._q(k)} = {storage._q(t)}.{storage._q(f)}" for f, k in zip(fk, key))
                    cs.append((tag, allowed(t, fk, f"EXISTS (SELECT 1 FROM {src} WHERE {on})", reads)))
        for f in fl if follow else ():  # after the relationships, so a project without follow links keeps its filters' text
            if f["from"] != t:
                continue
            tag = frozenset({f["id"]})
            if f["hops"] is None or f["live"] or any(map(blank, f["on"])) or any(  # fail closed: a link that doesn't hold,
                    c.lower() in hid.get(r[s]["table"], ()) for r, _ in f["hops"] for s in ("from", "to") for c in r[s]["columns"]):
                cs.append((tag, "FALSE"))  # a table on the way that shows no rows anyway or a join column hidden on it.
                # ponytail: follow over live tables denies
            elif f["to"] in conds:  # T is restricted for this user
                cs.append((tag, linked(f, conds)))
        return cs

    # A row is visible when it passes its own rules and every row it points to is visible: the greatest fixpoint,
    # so relationship cycles and self-loops (E-02.2-fix-1) stay complete. Visible sets only shrink, so it ends.
    # ponytail: one round per hop of the longest chain of hidden rows (a deep self-referencing hierarchy means many
    # rounds); a recursive CTE per cycle if that gets slow
    tables = sorted(set(direct) | {r["from"]["table"] for r in rels} | {f["from"] for f in fl})
    conds, last, grew = {t: [(frozenset(), c) for c in cs] for t in tables if (cs := direct.get(t))}, None, 0
    skeleton = lambda c: {t: [tag for tag, _ in cs] for t, cs in c.items()}  # noqa: E731
    # M10: with follow links the rules and relationships are settled first, then the follow parts join and the whole is
    # iterated again. It ends at the same fixpoint (the first phase's is above it, and filters only shrink from there),
    # and a link's keys are then read through tables that already have their filters: once, not once per round in
    # which a table on the way was still getting its own (SETTLE False: all at once, as before; tests compare the two)
    for follow in (False, True) if fl and SETTLE else (True,):
        last = None
        while conds != last:
            last, conds = conds, {t: cs for t in tables if (cs := step(t, conds, follow))}
            grew += skeleton(conds) != skeleton(last)  # parts only ever join (tags never change), so this stays small
            if fl and grew > CAP + 10 * len(tables):  # never: fail closed if it ever does
                return {t: "FALSE" for t in {*tables, *denied}}
    return {t: " AND ".join(c for _, c in cs) for t, cs in conds.items()} | {t: "FALSE" for t in denied}


def where(con, schema: str, t: str) -> str | None:
    """M3.6: the row filter user schema `schema`'s view of t applies (None: no filter), as `context` built it; raises
    KeyError when that schema has no view of t."""
    return _where[con][schema][t]


def crosses(back: dict, r: dict, tag: frozenset) -> bool:
    """1.2.6 M3, the one crossing rule (`conditions`, `_sources`): a filter part made from follow links `tag` crosses
    relationship r (always into its many side) unless one of them walks r there from its F's side back toward its T
    (`back`: {link: {(relationship ID, table)}}, `resolve`). So a follow filter never goes back
    along its own via (an anonymous order keeps its NULL customer), but crosses its via the other way (orders at a
    hidden café stay hidden), and a part made from rules alone (no tag) always crosses. The target's key plays no part."""
    return not any((r["id"], r["from"]["table"]) in back.get(x, ()) for x in tag)


def _blanks(project: str, user_id: int | None, types: dict | None = None, denied: list | None = None):
    """t -> t shows this user no rows whatever its filter: made from filtered rows (`unfiltered_outputs`) or every
    column hidden (`context`). A follow link reading such a table denies its F."""
    from abi.semantic import model  # not at top: model imports the semantic package

    denied, hid = set(unfiltered_outputs(project) if denied is None else denied), _hidden(project, user_id)

    def blank(t: str) -> bool:
        if t in denied:
            return True
        cols = t in hid and ((types or {}).get(t) or model.lake_columns(project).get(t, {}))
        return bool(cols) and {c.lower() for c in cols} <= hid[t]
    return blank


def key_columns(project: str, t: str) -> list[str]:
    """M3.6: the columns of t that any user's filter of t reads (`conditions`): its row rules' columns, the foreign
    keys of its relationships into filtered tables, and the columns a follow link's filter tests. The same for every user."""
    src, rels, fl = _sources(project)
    by = src.get(t, {})
    return list(dict.fromkeys([r["column"] for r in row_rules(project) if r["table"] == t] + [
        c for r in rels if r["from"]["table"] == t and ("rel", r["id"]) in by for c in r["from"]["columns"]] + [
        c for f in fl if f["from"] == t and ("link", f["id"]) in by and f["hops"] is not None and not f["live"] for c in f["cols"]]))


def filtered(project: str) -> set[str]:
    """Tables a rule filters: ruled ones plus every table with a relationship into one (SE-2 direction, any depth),
    and the `from` table of a follow link into one (1.2.6 M3)."""
    return set(_sources(project)[0])


CAP = 100  # fixpoint rounds past which follow links' filters count as never settling (fail closed)


def follows(project: str, mj: dict | None = None) -> list[dict]:
    """1.2.6 M3: `resolve` of the project's ontology and model, with its live tables."""
    from abi.semantic import live, model  # not at top: both import the semantic package

    return resolve(model._read(project, "ontology.json"), model._read(project, "model.json") if mj is None else mj,
                   live.columns(project))


def resolve(o: dict, mj: dict, lv=()) -> list[dict]:
    """1.2.6 M3: ontology `o`'s `rls: follow` links over model.json `mj`, each as {id, from (table F), to (table T),
    via (relationship IDs), hops (walking `via` back from T to F: (relationship, along?), ontology.walk), on (T and the
    tables between), cols (F's columns the last hop joins on: the ones the filter tests), live (a live table on the
    way)}: what the link means, all of it (the import gate compares this). A link that doesn't hold (a type,
    relationship or column gone, a hidden table) has hops None and shows restricted users no rows of `from`: F, or
    every table it may mean when its from type is unknown (fail closed). `back`: the (relationship, table) crossings
    its filter never makes (`crosses`). [] without an ontology. Data written into a table on the way (an input table,
    write-back) changes which F rows are linked, so who sees what: M4 must gate Viewer actions that write there."""
    from abi.semantic import ontology  # not at top: it imports this module

    objs, rels, out = {x["id"]: x["table"] for x in o.get("objects", [])}, {r["id"]: r for r in mj.get("relationships", [])}, []
    for ln in o.get("links", []):
        if ln.get("rls") != "follow":
            continue
        f, t = objs.get(ln.get("from")), objs.get(ln.get("to"))
        hops = ontology.walk(mj, t, ln["via"][::-1]) if f and t else None
        if isinstance(hops, list) and hops and (end := hops[-1][0]["to" if hops[-1][1] else "from"])["table"] == f:
            on = [t, *(r["to" if a else "from"]["table"] for r, a in hops[:-1])]
            # walked from T, a hop along its many-to-one direction has T's side as its many side: a filter crossing it
            # (always into the many side) goes from F's side toward T there
            out.append({"id": ln["id"], "from": f, "to": t, "via": list(ln["via"]), "hops": hops, "on": on, "cols": list(end["columns"]),
                        "live": bool({f, *on} & set(lv)), "back": {(r["id"], r["from"]["table"]) for r, a in hops if a}})
            continue
        deny = [f] if f else sorted({t, *(x[s]["table"] for i in ln["via"] if (x := rels.get(i)) for s in ("from", "to"))} - {None})
        both = {(i, x[s]["table"]) for i in ln["via"] if (x := rels.get(i)) for s in ("from", "to")}  # every via relationship, both ways
        out += [{"id": ln["id"], "from": d, "to": t, "via": list(ln["via"]), "hops": None, "on": [], "cols": [], "live": False,
                 "back": both} for d in deny]
    return out


def _sources(project: str) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """({filtered table: {what filters it ("rule", ("rel", relationship ID), ("link", follow link ID)): the tags of
    its parts (the follow link each is made from, or none)}}, the relationships, `follows`), as `conditions` builds
    every user's filters: a relationship carries each part of its target's filter that `crosses` it (onto columns
    other than the target's key, those as one untagged part). A follow link reading a table that shows no rows by
    default (`unfiltered_outputs`, every column hidden) denies F in `conditions` without being listed here: F's
    filter is then FALSE, which reads no column. Past CAP rounds (never: tags never change), every table is filtered."""
    from abi.semantic import model, ontology  # not at top: both import the semantic package

    mj = model._read(project, "model.json")
    rels, fl, ent = mj.get("relationships", []), follows(project, mj), ontology._keys(mj)
    back, ruled, src = {f["id"]: f["back"] for f in fl}, {r["table"] for r in row_rules(project)}, {}
    for _ in range(CAP + 10 * (len(rels) + len(fl) + 1)):
        new: dict[str, dict] = {t: {"rule": {frozenset()}} for t in ruled}
        for f in fl:
            if f["hops"] is None or f["live"]:
                new.setdefault(f["from"], {})[("link", f["id"])] = {frozenset({f["id"]})}
        for r in rels:
            d = r["to"]["table"]
            tags = {g for gs in src.get(d, {}).values() for g in gs if crosses(back, r, g)}
            if tags and (not fl or sorted(r["to"]["columns"]) != sorted(ent.get(d) or [None])):
                tags = {frozenset()}
            if tags:
                new.setdefault(r["from"]["table"], {})[("rel", r["id"])] = tags
        for f in fl:
            if f["hops"] is not None and not f["live"] and f["to"] in src:
                new.setdefault(f["from"], {})[("link", f["id"])] = {frozenset({f["id"]})}
        if new == src:
            return src, rels, fl
        src = new
    src = {t: {"rule": {frozenset()}} for t in {*ruled, *(r[s]["table"] for r in rels for s in ("from", "to")), *(f["from"] for f in fl)}}
    for r in rels:  # and every column a filter may read (key_columns)
        src[r["from"]["table"]][("rel", r["id"])] = {frozenset()}
    for f in fl:
        src[f["from"]][("link", f["id"])] = {frozenset({f["id"]})}
    return src, rels, fl


def unfiltered_outputs(project: str) -> list[str]:
    """Tables built from rows a rule filters but not filtered by those same rules: TR-1 tables whose merge/append steps
    read a filtered table or whose source step loads a filtered table's source (`source:<t>`, any depth,
    H-01.4-fix-1) and ML predictions tables (ml_<id>, ML-5; one whose model file is gone too, 1.2.6 M9). A table with no rule of its own counts as filtered only
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
    from abi.semantic import lineage  # not at top: it imports the semantic package

    ruled, cols = {r["table"] for r in row_rules(project)}, {r["table"] for r in _file(project).get("columns", [])}
    if not ruled | cols:
        return []
    _, rels, fl = _sources(project)
    g = lineage.graph(bundle(git.path(project)))

    def roots(t: str, follow: bool) -> set[str]:
        """The ruled tables whose rules filter t: t itself if ruled, and every one its relationships reach; with
        `follow`, also on through every follow link from its F to T and the tables between (whose rows its filter
        reads). Rows read from upstream follow, t's own (its own source too) don't: a follow link only adds denials, never makes a
        table built from filtered rows (F derived, ML predictions on F) visible (owner, 1.2.6 M3 round 5)."""
        seen, todo = {t}, [t]
        while todo:
            x = todo.pop()
            todo += (new := {r["to"]["table"] for r in rels if r["from"]["table"] == x} | {
                y for f in fl if follow and f["from"] == x for y in f["on"]}) - seen
            seen |= new
        return seen & ruled

    ups = lambda n: {u.split(":", 1)[1] for u in lineage.reach(g, {n}, "upstream") if u.startswith(("table:", "source:"))}
    # 1.2.6 M9: a predictions table whose model was deleted has no lineage left to say what it was made from: denied
    # (fail closed), as is any other model table named ml_<x> with no ml/<x>.json, until it gets a row rule of its own
    orphan = lambda t: t.startswith("ml_") and t not in ruled and not (git.path(project) / "ml" / f"{t[3:]}.json").is_file()  # noqa: E731
    return sorted(t for n in g if n.startswith("table:") and (
        (t := n[6:]) not in ruled and set().union(*(roots(u, u != t) for u in ups(n))) - roots(t, False) or t not in cols and ups(n) & cols
        or orphan(t)))


def explain(project: str, user_id: int) -> str:
    """RP-5 for a restricted user: SQL comment lines naming the rows each filtered table keeps for them (their own
    rule values only, never another user's), prefixed to the SQL "How is this calculated?" shows."""
    lines = [f"-- {r['table']}: columns {', '.join(r['columns'])} are hidden from you (D-110)"
             for r in _file(project).get("columns", [])]
    for r in row_rules(project):
        vals = r["users"].get(str(user_id))
        cond = (exp.column(r["column"], quoted=True).isin(*map(exp.convert, vals)).sql("duckdb") if vals
                else "no rows (you are not in this rule)")
        lines.append(f"-- {r['table']}: {cond}")
    src, rels, fl = _sources(project)
    # every relationship into a filtered table is ANDed (`conditions`), also onto a table with its own rule or a cycle
    lines += [f"-- {t}: only rows whose {fk} is a row of {d} you may see" for t, d, fk in sorted(
        {(r["from"]["table"], r["to"]["table"], ", ".join(r["from"]["columns"])) for r in rels if ("rel", r["id"]) in src.get(r["from"]["table"], ())})]
    # a follow link applies once its `to` table is restricted for the person (`conditions`)
    blank = _blanks(project, user_id) if fl else None
    why = lambda f: ("no longer holds" if f["hops"] is None else "crosses a live table" if f["live"]  # noqa: E731
                     else "reads a table that shows you no rows" if any(map(blank, f["on"])) else None)
    lines += [f"-- {f['from']}: only rows linked by '{f['id']}' ({', '.join(f['via'])}) to a row of {f['to']} you may see"
              f" ({f['to']} is restricted for you)" if not why(f) else
              f"-- {f['from']}: no rows (link '{f['id']}', which row-level security follows, {why(f)})"
              for f in sorted(fl, key=lambda f: (f["id"], f["from"])) if ("link", f["id"]) in src.get(f["from"], ())]
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
    conds = conditions(project, user_id, read, {t: c for t, c in model_columns.items() if t not in edited},
                       lambda src, cols: _key_type(project, con, snap, src, cols))
    # 1.2.6 M11: a same-as table (same.py) holds the type's key values of the records people decided about, also where
    # the key column is hidden from this reader, and no restricted reader needs it (the views read the base table, proposals
    # and decisions are an Editor's): it shows them no rows, whatever else filters it
    conds |= {t: "FALSE" for t, c in model_columns.items() if same_as(t, c)}
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
        _sweep_keys(con, snap)
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
        # 1.2.7 M0: no type but DuckDB's own. A cast to a key type (`_key_type`) would tell which keys another person
        # sees, also by a guessed name; and the key types' schema is never named, as anything
        if any(d.this == exp.DataType.Type.USERDEFINED for d in tree.find_all(exp.DataType)) or any(
                t.text.lower() == "rls_keys" for t in DuckDB().tokenize(sql) if t.token_type in (TokenType.VAR, TokenType.IDENTIFIER)):
            raise ValueError("names a type outside DuckDB's own")
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
    touched = {r["table"] for r in old if r not in new} | {r["table"] for r in new if r not in old}
    follow = sorted({f"{f['from']} (link '{f['id']}')" for f in follows(project) if f["hops"] is not None and touched & set(f["on"])})
    also = f" Tables that follow its table through links (as their target or a table between) see the same change:" \
           f" {', '.join(follow)}; a rule removed or widened shows them more rows too." if follow else ""
    actions.require_confirmation(f"This {what} changes who sees which rows or columns." + also + (f" {revoking(len(links))}" if links else ""),
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
