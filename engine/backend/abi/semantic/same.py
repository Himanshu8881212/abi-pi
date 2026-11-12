"""1.2.6 M11 entity resolution ("is this the same customer?").

An object type declares `resolve` in model/ontology.json (schemas/v1/ontology.json): exact columns (a VAT number, an
email, a phone; normalised), fuzzy columns (a name, an address; each with a threshold) and block columns (a postcode).
`find_same` proposes groups of the type's rows that look like one thing, with each pair's evidence; it never compares
every pair: only rows sharing an exact value, a block value or the first letters of the first fuzzy column (`_sql`).
Nothing is ever merged by itself: `decide_same` is a person's confirmed decision (same, different, split), also when an
agent calls it (the confirmation bridge; the model never holds a token).

Decisions are rows of the input table `same_as_<table>` (a, b, same), written as one write-back batch each (M4:
logged with who and when, undone with undo_edits): same = true says record b is the thing a (a: the group's one stable
ID, the key of its first record); same = false says the two are different: they are not proposed again and can't be
merged, directly or through other records, until that decision is undone. A record's group is found by going up its
same-rows to the top (`_group`, `_roots`), so a chain left by undoing an older decision is still one group. The table is
hidden (staging): out of the semantic model and of people's SQL; a restricted Viewer reads none of it (rls._context:
its keys are the type's key values); edit_cells and propose_edit refuse it and undo_edits asks the person (writeback).

Everything reads the resolved objects (`schema`): once a type has decisions, `semantic.execute` puts a schema of views
in front of the search path: the type's table without the records merged into another one, and every table pointing
at the type's key with that key replaced by its group's. So queries, measures (counted once), object reads, flows, ML
and people's SQL use the resolved ID with no change of their own; only the Data grid, write-back and the row-level
security key reads read the source rows (`raw`). The views are built over the rows the reader may see: a restricted
Viewer's group is made of the records they see, shown under the first of those (the stable ID when they see that
record), with that record's values; a record hidden from them adds no value, no count and no key. A project without
`resolve`, or whose types have no record merged now (none yet, or every merge undone), gets no schema and reads
exactly as before (its query copies and shared partials too).
"""

import hashlib
import json
import weakref
from typing import Literal

import duckdb
from pydantic import Field

from abi import actions, semantic, storage
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import SCHEMA, git
from abi.security import Role, User
from abi.semantic import model, ontology, rls

BLOCK = 200  # rows sharing one block value past which they aren't compared with each other (an empty-like postcode, "unknown")
LETTERS = 4  # first letters of the first fuzzy column that make a block
PAIRS = 20_000  # proposed pairs read at most
DEPTH = 16  # same-rows gone up from a record to its group's ID (1 unless an older decision was undone under a newer one)
_active: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> {(snapshot, same-as table): it merges a record}
_built: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> {schema names built}
_made: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # lake connection -> {(snapshot, rls schema, digest): schema}
_q = storage._q


def table(t: str) -> str:
    """The table that keeps the decisions about table t's rows."""
    return f"same_as_{t}"


# ---------- everything reads the resolved objects: the views in front of the search path ----------

def _objects(project: str) -> list[dict]:
    """The ontology's object types; none when the file can't be read (the import checks report a broken file; a read
    of the data never fails on it)."""
    def read() -> list:
        try:
            return ontology.read(project).get("objects", [])
        except ValueError:
            return []
    return actions.memo(("same.ontology", project), read)


def _merges(con, snap: int, s: str) -> bool:
    """Same-as table s holds a same = true row at this snapshot (one read per snapshot)."""
    seen = _active.setdefault(con, {})
    if (snap, s) not in seen:
        cur = con.cursor()
        semantic._effective(cur, con, snap)
        for k in [k for k in seen if k[0] < snap]:  # older snapshots never come back
            del seen[k]
        seen[snap, s] = cur.execute(f'SELECT 1 FROM {_q(s)} WHERE "same" LIMIT 1').fetchone() is not None
    return seen[snap, s]


def types(project: str, cols: dict, con, snap: int) -> dict[str, tuple[dict, str]]:
    """{table: (its object type, its key column)} of the types the views resolve: those with `resolve` whose same-as
    table is loaded as decide_same makes it (a, b of the key's type, same) and merges a record now. {} for every
    project without `resolve`, before anything else is read; a type whose merges were all undone reads as before."""
    o = _objects(project)
    if not any("resolve" in x for x in o):
        return {}
    try:
        keys = ontology._keys(actions.memo(("same.model", project), lambda: model._read(project, "model.json")))
    except ValueError:
        return {}
    return {t: (x, keys[t][0]) for x in o if "resolve" in x and len(keys.get(t := x["table"]) or []) == 1 and t in cols
            and cols.get(table(t)) == {"a": cols[t].get(keys[t][0]), "b": cols[t].get(keys[t][0]), "same": "BOOLEAN"}
            and _merges(con, snap, table(t))}


def _group(s_src: str) -> str:
    """SELECT b, a: every record b of a group and the group's ID a (its own row too), going up the same-rows of
    `s_src` from each record to the top, at most DEPTH steps. One row per b."""
    return (f'WITH RECURSIVE p AS (SELECT b, min(a) AS a FROM {s_src} WHERE "same" AND a <> b GROUP BY b),'
            f" up AS (SELECT b, a, 1 AS d FROM p UNION ALL SELECT up.b, p.a, up.d + 1 FROM up JOIN p ON p.b = up.a WHERE up.d < {DEPTH}),"
            " top AS (SELECT b, arg_max(a, d) AS a FROM up GROUP BY b)"
            " SELECT b, min(a) AS a FROM (SELECT b, a FROM top UNION ALL SELECT a, a FROM top) GROUP BY b")


def _reps(t_src: str, s_src: str, k: str) -> str:
    """SELECT b, a: for each row of `t_src` (the type's rows the reader sees) that is in a group, its key b and the key
    a its group shows under for this reader: the group's stable ID when they see that record, else the first record of
    the group they see. One row per b, so a join on b never repeats a row."""
    return (f"SELECT __t.{_q(k)} AS b, first_value(__t.{_q(k)}) OVER (PARTITION BY __s.a ORDER BY __t.{_q(k)} <> __s.a, __t.{_q(k)}) AS a"
            f" FROM {t_src} __t JOIN ({_group(s_src)}) __s ON __s.b = __t.{_q(k)}")


def _maps(mj: dict, ts: dict, cols: dict, t: str) -> dict[str, str]:
    """{column of t: the resolved table whose key it holds}: the foreign keys of t's relationships onto a resolved
    type's key. The same-as tables themselves are never resolved."""
    if t.startswith("same_as_") and t[8:] in ts:
        return {}
    return {r["from"]["columns"][0]: r["to"]["table"] for r in mj.get("relationships", [])
            if r["from"]["table"] == t and r["to"]["table"] in ts and r["to"]["columns"] == [ts[r["to"]["table"]][1]]
            and len(r["from"]["columns"]) == 1 and r["from"]["columns"][0] in cols.get(t, {})}


def affected(project: str, mj: dict, cols: dict) -> set[str]:
    """The tables the views change: the resolved types' tables and every table pointing at one's key. Their query
    copies and shared partials (built from the source rows) are not used."""
    if not any("resolve" in x for x in _objects(project)):
        return set()
    con = storage.lake(project)
    ts = types(project, cols, con, semantic._snapshot(con))
    return {t for t in cols if t in ts or _maps(mj, ts, cols, t)} if ts else set()


def _views(project: str, con, ts: dict, cols: dict, edited: frozenset, ctx: str | None, user_id: int | None) -> dict[str, str]:
    mj = model._read(project, "model.json")
    keys, hid = ontology._keys(mj), rls.hidden(project, user_id) if ctx else {}
    base = lambda t: f"{'memory.effective' if t in edited else 'lake.model'}.{_q(t)}"  # noqa: E731
    wheres = rls._where[con][ctx] if ctx else {}  # {table: the row filter their view of it applies}; unknown: the read fails (closed)

    def under(t: str) -> tuple[str, list[str]]:
        """(t's rows the reader sees before resolution, its columns hidden from them): the base table through their
        own row filter (the one their row-level security view applies), every column still there to join on."""
        w = wheres.get(t)
        out = [c for c in cols[t] if c.lower() in hid.get(t, ())]
        return (f"(SELECT * FROM {base(t)} WHERE {w})" if w else base(t)), out if len(out) < len(cols[t]) else []

    reps = {t: _reps(under(t)[0], base(table(t)), k) for t, (_, k) in ts.items()}
    views = {}
    for t in cols:
        maps = _maps(mj, ts, cols, t)
        if not maps and t not in ts or t.startswith("same_as_") and t[8:] in ts:
            continue
        src, hide = under(t)
        low = {c.lower() for c in hide}
        joins = "".join(f" LEFT JOIN ({reps[d]}) __m{i} ON __m{i}.b = __t.{_q(c)}" for i, (c, d) in enumerate(maps.items()))
        inner = f"SELECT __t.*{''.join(f', __m{i}.a AS __a{i}' for i in range(len(maps)))} FROM {src} __t{joins}"
        # a record merged into another one the reader sees is not a row of its own
        conds = [f"({_q(ts[t][1])} IS NULL OR {_q(ts[t][1])} NOT IN (SELECT b FROM ({reps[t]}) WHERE b <> a))"] if t in ts else []
        # a table with one row per object (its key is the foreign key: predictions, an input table) keeps the row of
        # the record the group shows under. ponytail: a group whose shown record has no row there shows none
        conds += [f"(__a{i} IS NULL OR {_q(c)} = __a{i})" for i, c in enumerate(maps) if keys.get(t) == [c]]
        gone = [f"__a{i}" for i in range(len(maps))] + [_q(c) for c in hide]
        swap = ", ".join(f"CAST(coalesce(__a{i}, {_q(c)}) AS {cols[t][c]}) AS {_q(c)}" for i, c in enumerate(maps) if c.lower() not in low)
        views[t] = (f"SELECT *{f' EXCLUDE ({', '.join(gone)})' if gone else ''}{f' REPLACE ({swap})' if swap else ''} FROM ({inner})"
                    + (f" WHERE {' AND '.join(conds)}" if conds else ""))
    return views


def schema(project: str, con, snap: int, ctx: str | None, user_id: int | None) -> str | None:
    """The schema `semantic.execute` puts in front of the search path for this reader (`ctx`: their row-level security
    schema, None when they read unfiltered), built once per snapshot, definition files and reader's filters; None when
    the project resolves nothing (no `resolve`, or no decision yet)."""
    if not any("resolve" in x for x in _objects(project)):
        return None
    cols = semantic._model_columns(con, snap)
    if not (ts := types(project, cols, con, snap)):
        return None
    key = (snap, ctx, rls.digest(project))
    if hit := _made.get(con, {}).get(key):
        return hit
    views = _views(project, con, ts, cols, semantic._edited(con, snap), ctx, user_id)
    name = "same_" + hashlib.sha256(json.dumps(views, sort_keys=True).encode()).hexdigest()[:16]
    with storage.project_lock("rls", project):  # one catalog write of a schema at a time, as the row-level security views
        if name not in _built.setdefault(con, set()):
            cur = con.cursor()
            cur.begin()
            try:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS memory.{name}")
                for t, sql in views.items():
                    cur.execute(f"CREATE OR REPLACE VIEW memory.{name}.{_q(t)} AS {sql}")
                cur.commit()
            except BaseException:
                cur.rollback()
                raise
            _built[con].add(name)  # ponytail: never dropped (a few views each), as the row-level security schemas
        cache = _made.setdefault(con, {})
        for k in [k for k in cache if k[0] < snap]:  # older snapshots never come back
            del cache[k]
        cache[key] = name
    return name


def _order(v: str) -> tuple:
    # ponytail: the "first" key by length then text (non-negative whole numbers in order); typed order if other keys need it
    return len(v), v


def _state(project: str, s: str, cols: dict) -> dict[tuple[str, str], bool]:
    """The decisions in force, {(a, b): same} with keys as text; {} before the first one. A system read."""
    if s not in cols:
        return {}
    return {(a, b): yes for a, b, yes in _read(project, f'SELECT CAST(a AS VARCHAR), CAST(b AS VARCHAR), "same" FROM {_q(s)}', {"same_as": s}, None)}


def _roots(state: dict) -> dict[str, str]:
    """{record: its group's ID}, going up the same-rows to the top as `_group` does."""
    up: dict[str, str] = {}
    for (a, b), yes in state.items():
        if yes and a != b:
            up[b] = min(up.get(b, a), a, key=_order)

    def top(v: str) -> str:
        for _ in range(DEPTH):
            if v not in up:
                break
            v = up[v]
        return v
    return {b: top(b) for b in up}


def shown_as(project: str, x: dict, key, user_id: int | None) -> list | None:
    """The key the record `key` of type x shows under for this reader, when they see that record and it was merged
    into another one they see (an old link to it opens the object it became); None otherwise. As the views: the
    group's stable ID when they see that record, else the first record of the group they see."""
    cols, t, con = model.lake_columns(project), x["table"], storage.lake(project)
    if not (got := types(project, cols, con, semantic._snapshot(con)).get(t)):
        return None
    k = got[1]
    lit = lambda v: rls.cast_lit(v, cols[t][k])  # noqa: E731
    # the group's records: a system read, of which only a key the reader sees ever leaves
    roots = _roots(_state(project, table(t), cols))
    the = roots.get(str(key), str(key))
    if not (group := [the, *(b for b, a in roots.items() if a == the)])[1:]:
        return None
    seen = semantic.execute(project, f"SELECT {_q(k)}, CAST({_q(k)} AS VARCHAR) FROM {_q(t)} WHERE {_q(k)} IN ({', '.join(map(lit, group))})"
                                     f" ORDER BY {_q(k)} <> {lit(the)}, {_q(k)}", {"same_as": x["id"]}, user_id, raw=True)  # as the reader: their own source rows
    # they see the record itself, and it isn't the one the group shows under for them
    return [semantic.tidy(seen[0][0])] if str(key) in [text for _, text in seen[1:]] else None


# ---------- proposing: candidate pairs by blocking, then scoring ----------

def _norm(c: str, how: str) -> str:
    """Column c normalised for comparing, NULL when nothing is left (an empty value never matches)."""
    v = f"CAST({_q(c)} AS VARCHAR)"
    if how == "email":
        e = f"lower(trim({v}))"
    elif how == "phone":  # the block only: its last 9 digits; whether two phones are the same is `_same_phone`
        d = f"regexp_replace({v}, '[^0-9]', '', 'g')"
        e = f"CASE WHEN length({d}) >= 7 THEN substring({d}, greatest(length({d}) - 8, 1)) END"
    elif how == "digits":  # a phone as dialled: its digits without leading zeros (00 49 30 …, 0 30 …)
        e = f"regexp_replace(regexp_replace({v}, '[^0-9]', '', 'g'), '^0+', '')"
    elif how == "id":
        e = f"upper(regexp_replace(strip_accents({v}), '[^A-Za-z0-9]', '', 'g'))"
    else:  # text: lower-cased, accents and punctuation dropped, spaces single
        e = f"trim(regexp_replace(lower(strip_accents({v})), '[[:punct:][:space:]]+', ' ', 'g'))"
    return f"NULLIF({e}, '')"


def _label(x: dict, c: str) -> str:
    """Column c of type x in the words the type gives it, as they read inside a sentence ("same email", "same VAT number")."""
    s = next((p["name"] for p in x.get("properties", []) if p["column"] == c and p.get("name")), ontology._label(c))
    return s if s.split(" ")[0].isupper() and len(s.split(" ")[0]) > 1 else s[:1].lower() + s[1:]


def _sql(x: dict, k: str, typ: str, decided: bool, key) -> tuple[str, str]:
    """(the proposed pairs of type x: ka, kb, their titles, per exact column whether it is the same and whether it
    differs, one score per fuzzy column, the number of candidate pairs compared; how many block values are shared by
    more than BLOCK rows). Candidate pairs come from blocking only: two rows that share a normalised exact value (a
    phone: its last 9 digits), a block column's value or the first LETTERS letters of the first fuzzy column, in a block
    of at most BLOCK rows. A pair is proposed when an exact column is the same, or every fuzzy column reaches its
    threshold and no exact column differs; never one a person marked as different (`decided`: the same-as table is
    there). Two phones are the same when, without leading zeros, the shorter one (7 digits or more) ends the longer
    one: a number with and without its country code, never two numbers that differ in their first digits."""
    rs, t = x["resolve"], x["table"]
    exact = rs.get("exact", [])
    ex = [(f"e{i}", _norm(f["column"], f.get("as", "id"))) for i, f in enumerate(exact)]
    ph = [(f"p{i}", _norm(f["column"], "digits")) for i, f in enumerate(exact) if f.get("as") == "phone"]
    fz = [(f"f{i}", _norm(f["column"], "text")) for i, f in enumerate(rs.get("fuzzy", []))]
    bl = [(f"b{i}", _norm(c, "text")) for i, c in enumerate(rs.get("block", []))]
    bl += [("b_", f"substring({fz[0][1]}, 1, {LETTERS})")] if fz else []
    r = (f"SELECT {_q(k)} AS k, CAST({_q(x['title'])} AS VARCHAR) AS t, " + ", ".join(f"{e} AS {n}" for n, e in ex + ph + fz + bl)
         + f" FROM {_q(t)}")
    blocks = [n for n, _ in ex + bl]
    pairs = " UNION ".join(f"SELECT a.k AS ka, b.k AS kb FROM r a JOIN r b ON a.{n} = b.{n} AND a.k < b.k WHERE a.{n} IN"
                           f" (SELECT {n} FROM r GROUP BY 1 HAVING count(*) BETWEEN 2 AND {BLOCK})" for n in blocks)
    sim = lambda n, f: (f"jaro_winkler_similarity(a.{n}, b.{n})" if f.get("method", "jaro_winkler") == "jaro_winkler" else  # noqa: E731
                        f"1 - levenshtein(a.{n}, b.{n}) / greatest(length(a.{n}), length(b.{n}))")
    phone = lambda i: (f"(least(length(a.p{i}), length(b.p{i})) >= 7 AND (suffix(a.p{i}, b.p{i}) OR suffix(b.p{i}, a.p{i})))",  # noqa: E731
                       f"a.p{i} IS NOT NULL AND b.p{i} IS NOT NULL")
    same = [phone(i) if f.get("as") == "phone" else (f"a.e{i} = b.e{i}", f"a.e{i} IS NOT NULL AND b.e{i} IS NOT NULL") for i, f in enumerate(exact)]
    eq = [f"coalesce({yes}, FALSE) AS q{i}" for i, (yes, _) in enumerate(same)]
    df = [f"coalesce({both} AND NOT {yes}, FALSE) AS d{i}" for i, (yes, both) in enumerate(same)]
    sc = [f"coalesce({sim(n, f)}, 0) AS s{i}" for i, ((n, _), f) in enumerate(zip(fz, rs.get("fuzzy", [])))]
    scored = (f"SELECT p.ka, p.kb, a.t AS ta, b.t AS tb, {', '.join([*eq, *df, *sc])}, count(*) OVER () AS n"
              " FROM pairs p JOIN r a ON a.k = p.ka JOIN r b ON b.k = p.kb")
    alike = " AND ".join(f"s{i} >= {float(f['threshold'])!r}" for i, f in enumerate(rs.get("fuzzy", []))) or "FALSE"
    keep = f"({' OR '.join([f'q{i}' for i in range(len(ex))] + [' AND '.join([f'({alike})'] + [f'NOT d{i}' for i in range(len(ex))])])})"
    if decided:
        s = _q(table(t))
        keep += f' AND (ka, kb) NOT IN (SELECT a, b FROM {s} WHERE NOT "same" UNION ALL SELECT b, a FROM {s} WHERE NOT "same")'
    if key is not None:
        lit = rls.cast_lit(key, typ)
        keep += f" AND (ka = {lit} OR kb = {lit})"
    out = ", ".join(["CAST(ka AS VARCHAR)", "CAST(kb AS VARCHAR)", "ta", "tb", *(f"q{i}" for i in range(len(ex))), *(f"d{i}" for i in range(len(ex))),
                     *(f"s{i}" for i in range(len(fz))), "n"])
    big = " + ".join(f"(SELECT count(*) FROM (SELECT 1 FROM r WHERE {n} IS NOT NULL GROUP BY {n} HAVING count(*) > {BLOCK}))" for n in blocks)
    return (f"WITH r AS MATERIALIZED ({r}), pairs AS ({pairs}) SELECT {out} FROM ({scored}) WHERE {keep} ORDER BY ka, kb LIMIT {PAIRS + 1}",
            f"WITH r AS MATERIALIZED ({r}) SELECT {big}")


def _resolving(project: str, name: str, user: User) -> tuple[dict, str, dict]:
    """(object type `name`, its key column, the lake's columns) for a type that says how its records are told apart."""
    _, m, x, cols, _ = ontology._obj(project, name, user.id)
    if "resolve" not in x:
        raise ActionFailed(409, f"{x['name']} has no rules yet for telling which {x['plural']} are the same one. Add `resolve` to object type"
                                f" '{x['id']}' in the ontology (save_ontology): the exact columns (a VAT number, an email), the fuzzy"
                                " columns (a name, an address) with a threshold each, and a block column (a postcode).")
    keys = ontology._keys(m)[x["table"]]
    if len(keys) != 1:
        raise ActionFailed(409, f"model/ontology.json object type '{x['id']}' resolve: table '{x['table']}' is keyed by {', '.join(keys)};"
                                " `resolve` needs one key column (set_table).")
    ontology._live(project, [x["table"]])
    return x, keys[0], cols


def _read(project: str, sql: str, asked: dict, user_id: int | None, raw: bool = False) -> list[tuple]:
    try:
        return [tuple(map(semantic.tidy, r)) for r in semantic.execute(project, sql, asked, user_id, timeout=semantic.TIMEOUT, raw=raw)]
    except duckdb.InterruptException:
        raise ActionFailed(400, f"Comparing took longer than {semantic.TIMEOUT:g} s and was stopped: add a `block` column (a postcode) to"
                                " `resolve` so fewer rows are compared with each other.") from None
    except duckdb.Error as e:
        raise ActionFailed(400, f"The read failed: {e}") from None


def _titles(project: str, x: dict, k: str, typ: str, keys, user_id: int | None) -> dict[str, tuple]:
    """{key as text: (title, source)} of the records (source rows) with these keys that the reader sees."""
    keys = list(dict.fromkeys(str(v) for v in keys))
    if not keys:
        return {}
    src = _q(x["resolve"]["source"]) if "source" in x["resolve"] else "NULL"
    return {r[0]: r[1:] for r in _read(project, f"SELECT CAST({_q(k)} AS VARCHAR), CAST({_q(x['title'])} AS VARCHAR), CAST({src} AS VARCHAR)"
                                                f" FROM {_q(x['table'])} WHERE {_q(k)} IN ({', '.join(rls.cast_lit(v, typ) for v in keys)}) ORDER BY {_q(k)}",
                                       {"same_as": x["id"]}, user_id, raw=True)}


def _decisions(conn, user: User, project: str, x: dict, k: str, typ: str, only: set[str] | None, limit: int = 20) -> list[dict]:
    """The decisions about type x's records, newest first, from the same-as table's edit history (M4): who, when, what
    (same, different, split; `edit` for a change made by hand), the records it names and whether it was undone. `only`:
    those naming one of these keys."""
    from abi import writeback  # not at top: it imports the semantic package

    try:
        h = writeback.edit_history(conn, user, writeback.History(project=project, table=table(x["table"]), limit=1000))
    except ActionFailed:  # no same-as table yet
        return []
    out: dict[str, dict] = {}
    for row_key, _, _, _, _, who, at, comment, batch, status, *_ in h["entries"]:
        d = out.setdefault(batch, {"batch": batch, "at": at, "user": who, "decision": comment if comment in ("same", "different", "split") else "edit",
                                   "undone": status == "undone", "keys": []})
        d["keys"] += [str(v) for v in json.loads(row_key).values() if str(v) not in d["keys"]]
    kept = [d for d in out.values() if only is None or only & set(d["keys"])][:limit]
    names = _titles(project, x, k, typ, [v for d in kept for v in d["keys"]], user.id)
    return [{i: v for i, v in d.items() if i != "keys"} | {"objects": [{"key": v, "title": names[v][0]} for v in d["keys"] if v in names]}
            for d in kept]


class Find(storage.ProjectRef):
    type: str = Field(description="an object type ID that has `resolve` (`ontology`)")
    key: storage.Value | None = Field(None, description="one object's key: only what concerns it, and the records it is made of")
    limit: int = Field(20, ge=1, le=2000, description="groups at most")


def kept(project: str, t: str) -> dict | None:
    """The object type whose decisions table `t` keeps (its `resolve` type's same-as table), else None."""
    return next((x for x in _objects(project) if "resolve" in x and table(x["table"]) == t), None) if t.startswith("same_as_") else None


def undo_words(conn, user: User, project: str, t: str, batch: str) -> str | None:
    """What undoing batch `batch` of table `t` takes back, in the person's words, when `t` keeps a type's decisions
    (writeback.undo_edits asks with it); None for any other table."""
    if (x := kept(project, t)) is None:
        return None
    cols, one = model.lake_columns(project), x["name"].lower()
    k = (ontology._keys(model._read(project, "model.json")).get(x["table"]) or [None])[0]
    d = next((d for d in _decisions(conn, user, project, x, k, cols[x["table"]][k], None, 1000) if d["batch"] == batch), None) if k in cols.get(
        x["table"], {}) else None
    if d is None or not d["objects"]:
        return f"Undo a decision about which {x['plural'].lower()} are the same one"
    who = [o["title"] or f"{one} {o['key']}" for o in d["objects"]]
    who = ", ".join(who[:-1]) + (" and " if who[:-1] else "") + who[-1]
    return "Undo: " + {"same": f"{who} are the same {one}", "different": f"{who} are not the same {one}",
                       "split": f"{who} taken out of the {one} merged before"}.get(d["decision"], f"a change made by hand to {who}")


@action(Role.editor)
def find_same(conn, user: User, args: Find) -> dict:
    """Proposes which objects of a type look like the same one, by the type's `resolve` rules: `groups`, each with its objects and every pair's evidence (`why`); merges nothing. `decisions` lists what people decided (undo one with undo_edits on `table`). Detail: harness/notes/model.md."""
    x, k, cols = _resolving(args.project, args.type, user)
    t, s = x["table"], table(x["table"])
    typ, rs = cols[t][k], x["resolve"]
    state = _state(args.project, s, cols)
    roots = _roots(state)
    top = lambda v: roots.get(v, v)  # noqa: E731
    apart = {frozenset((top(a), top(b))) for (a, b), yes in state.items() if not yes}  # marked as different, by their groups
    sql, big = _sql(x, k, typ, s in cols, args.key)
    asked = {"find_same": x["id"], **({"key": args.key} if args.key is not None else {})}
    rows = _read(args.project, sql, asked, user.id)
    exact, fuzzy = [_label(x, f["column"]) for f in rs.get("exact", [])], [_label(x, f["column"]) for f in rs.get("fuzzy", [])]
    root: dict[str, str] = {}

    def find(v: str) -> str:
        while root.setdefault(v, v) != v:
            root[v] = v = root[root[v]]
        return v

    titles, pairs, members = {}, [], {}
    for ka, kb, ta, tb, *v, _ in rows[:PAIRS]:
        ra, rb = find(ka), find(kb)
        # never one group with two records a person marked as different, directly or through what each was merged with
        if apart and ra != rb and any(frozenset((top(i), top(j))) in apart for i in members.get(ra, {ka}) for j in members.get(rb, {kb})):
            continue
        titles |= {ka: ta, kb: tb}
        members[ra] = members.pop(ra, {ka}) | members.pop(rb, {kb})
        root[rb] = ra
        n = len(exact)
        why = [f"same {lab}" for lab, yes in zip(exact, v) if yes] + [f"different {lab}" for lab, no in zip(exact, v[n:]) if no]
        why += [f"{lab} {score:.0%} alike" for lab, score in zip(fuzzy, v[2 * n:]) if score]
        pairs.append({"a": ka, "b": kb, "why": why})
    groups: dict[str, dict] = {}
    for p in pairs:
        g = groups.setdefault(find(p["a"]), {"objects": {}, "pairs": []})
        g["objects"] |= {v: {"key": v, "title": titles[v]} for v in (p["a"], p["b"])}
        g["pairs"].append(p)
    # a group in which something speaks against it (the same email under two VAT numbers) comes after the clear ones
    doubt = lambda g: any(w.startswith("different ") for p in g["pairs"] for w in p["why"])  # noqa: E731
    shown = [{"objects": list(g["objects"].values()), "pairs": g["pairs"]} for g in sorted(groups.values(), key=doubt)[:args.limit]]
    out = {"type": x["id"], "name": x["plural"], "table": s, "groups": shown, "more": len(groups) > args.limit or len(rows) > PAIRS,
           "compared": rows[0][-1] if rows else None}  # candidate pairs scored (not known when none of them is proposed)
    only = None
    if args.key is not None:  # the records this object is made of
        the = top(str(args.key))
        only = {the, *(b for b, a in roots.items() if a == the)}
        if len(only) > 1:
            out["records"] = [{"key": v, "title": n[0], "source": n[1]} for v, n in _titles(args.project, x, k, typ, only, user.id).items()]
    elif n := _read(args.project, big, asked, user.id)[0][0]:
        out["note"] = (f"{n} value(s) are shared by more than {BLOCK} {x['plural']} each (an empty-like postcode, a very common start of a name):"
                       " those weren't compared with each other through that value. A more telling `block` column in `resolve` compares them.")
    out["decisions"] = _decisions(conn, user, args.project, x, k, typ, only)
    # D-22: the names and each pair's evidence are quotable rows
    return out | {"columns": ["group", "name", "other name", "why"],
                  "rows": [[i + 1, titles[p["a"]], titles[p["b"]], ", ".join(p["why"])] for i, g in enumerate(shown) for p in g["pairs"]]}


# ---------- deciding: a person's confirmed decision, one undoable write-back batch ----------

def _ensure(project: str, x: dict, k: str, author: str) -> None:
    """The same-as table of type x, made at its first decision as an input table is (tables/<t>.json, model.json, an
    empty model table): keyed by (a, b), hidden, with a relationship from each onto the type's key (so row-level
    security filters it with the type)."""
    from abi import transform, writeback  # not at top: they import the semantic package
    from abi.project import imports

    t, s = x["table"], table(x["table"])
    m, cols = model.load(project), model.lake_columns(project)
    if (m["tables"].get(s) or {}).get("key") != ["a", "b"]:
        typ = transform._type(cols[t][k])
        imports.write(project, {f"tables/{s}.json": {"$schema": SCHEMA + "table.json", "id": s, "key": ["a", "b"], "steps": [], "columns": [
            {"name": "a", "type": typ}, {"name": "b", "type": typ}, {"name": "same", "type": "BOOLEAN"}]}})
        m["tables"][s] = {"kind": "fact", "key": ["a", "b"], "hidden": True, "description": (
            f"Which {x['plural']} are the same one: record b counts as a (same = true), or the two are different (same = false)."
            " Written by decide_same; undo a decision with undo_edits.")}
        m["relationships"] = [r for r in m["relationships"] if r["from"]["table"] != s] + [model._rel(f"{s}.{c}", f"{t}.{k}") for c in ("a", "b")]
        model.save(project, m)
    if s not in cols:
        columns = json.loads((git.path(project) / "tables" / f"{s}.json").read_text())["columns"]
        writeback.create_tables(project, {s: columns}, author)


class Decide(storage.ProjectRef):
    type: str = Field(description="an object type ID that has `resolve` (`ontology`)")
    keys: list[storage.Value] = Field(min_length=1, max_length=50, description="the objects' keys (find_same's `key`s): two or more for same and"
                                                                                  " different; for split, the record(s) to take out of their object")
    decision: Literal["same", "different", "split"] = Field(description="same: they are one object from now on; different: never propose them"
                                                                         " together again; split: take these records out of the object they were merged into")


@action(Role.editor, definition=True, confirms=True)
def decide_same(conn, user: User, args: Decide) -> dict:
    """Records a person's decision about objects that look the same (find_same): same (they count as one, under one stable ID), different, or split (take records out again). Always asks the person to confirm; one undoable change (undo_edits with `table` and `batch`). Detail: harness/notes/model.md."""
    from abi import writeback  # not at top: it imports the semantic package

    p = args.project
    x, k, cols = _resolving(p, args.type, user)
    t, s = x["table"], table(x["table"])
    typ, one, many = cols[t][k], x["name"].lower(), x["plural"].lower()
    if git.production(p) and (model.load(p)["tables"].get(s) or {}).get("key") != ["a", "b"]:  # D-56: no design change here
        raise ActionFailed(409, f"This production environment has no table yet to keep decisions about {many}: make the first decision in"
                                " the development project and promote it (pull), which brings the table. Decisions themselves are"
                                " made here: they are data of this environment, and a promote carries none.")
    keys = list(dict.fromkeys(str(v) for v in args.keys))
    state = _state(p, s, cols)
    roots = _roots(state)  # record -> its group's ID
    ids = set(roots.values())
    top = lambda v: roots.get(v, v)  # noqa: E731
    group = lambda the: [the, *(b for b, a in roots.items() if a == the)]  # noqa: E731
    parents = lambda v: [a for (a, b), yes in state.items() if yes and b == v and a != b]  # noqa: E731
    tops = list(dict.fromkeys(map(top, keys)))
    names = {v: n[0] for v, n in _titles(p, x, k, typ, [*keys, *(v for the in tops for v in group(the))], user.id).items()}
    if gone := [v for v in keys if v not in names]:
        raise ActionFailed(404, f"No {one} with key {', '.join(gone)}: use the keys find_same lists.")
    said = lambda vs: ", ".join(f"“{names.get(v) or f'{one} {v}'}”" for v in vs)  # noqa: E731
    want: dict[tuple[str, str], bool | None] = {}  # the rows this decision changes: same, different, or None (gone)

    def star(the: str, records: list[str]) -> None:
        """`records` are one group under `the`: one same-row from it to each, and no other row above any of them."""
        want.update({(a, the): None for a in parents(the)})
        for v in records:
            if v != the:
                want.update({(a, v): None for a in parents(v) if a != the})
                want[the, v] = True

    if args.decision == "same":
        if len(tops) < 2:
            raise ActionFailed(409, f"{'These are' if len(keys) > 1 else 'This is'} already one {one}: give two or more {many} that are still apart.")
        # never against an earlier "not the same": between any two records that would end in one group
        if no := next(((a, b) for (a, b), yes in state.items() if not yes and top(a) != top(b) and {top(a), top(b)} <= set(tops)), None):
            names |= {v: n[0] for v, n in _titles(p, x, k, typ, no, user.id).items()}
            raise ActionFailed(409, f"{said(no[:1])} and {said(no[1:])} were marked as not the same {one}, and this would make them one."
                                    " Undo that decision first (under Decided; undo_edits), then say they are the same.")
        the = min([v for v in tops if v in ids] or tops, key=_order)  # an ID there already stays: the stable one
        star(the, [v for r in tops for v in group(r)])
        why = f"Treat these {len(tops)} {many} as the same one: {said(tops)}. Every number then counts them once, under {said([the])}."
    elif args.decision == "different":
        if len(tops) < len(keys) or len(keys) < 2:
            raise ActionFailed(409, f"Give two or more {many} that are apart. {many.capitalize()} that count as one now are taken apart with"
                                    " decision 'split' first.")
        want.update({tuple(sorted((a, b), key=_order)): False for i, a in enumerate(tops) for b in tops[i + 1:]
                     if (a, b) not in state and (b, a) not in state})
        if not want:
            raise ActionFailed(409, f"These {many} are marked as different already.")
        why = f"Mark these {len(tops)} {many} as different: {said(tops)}. They won't be proposed as the same one again."
    else:
        if lone := [v for v in keys if v not in roots and v not in ids]:
            raise ActionFailed(409, f"{said(lone)} isn't merged with any other {one}, so there is nothing to take out.")
        for the in tops:
            records = group(the)
            rest = [v for v in records if v not in keys] or [min(records, key=_order)]  # all of them named: all but the first leave
            new = the if the in rest else min(rest, key=_order)  # the record that gives the group its ID leaves: the first of the rest does
            for v in [v for v in records if v not in rest]:
                want.update({(a, v): None for a in parents(v)} | {(v, c): None for c in records if state.get((v, c))})
                want[new, v] = False  # not this one: never proposed for it again, nor merged into it until this is undone
            star(new, rest)
        why = f"Take {said(keys)} out of the {one} {'it was' if len(keys) == 1 else 'they were'} merged into: each is its own {one} again."
    add = [{"a": a, "b": b, "same": yes} for (a, b), yes in want.items() if yes is not None and (a, b) not in state]
    edits = [writeback.CellEdit(key=[a, b], column="same", value=yes, old=state[a, b]) for (a, b), yes in want.items()
             if yes is not None and (a, b) in state and state[a, b] != yes]
    drop = [[a, b] for (a, b), yes in want.items() if yes is None and (a, b) in state]
    if not (add or edits or drop):
        raise ActionFailed(409, f"Nothing to change: these {many} are like that already.")
    # always the person's click, an agent's call too (never automatic). The token signs the rows this changes: a
    # decision made in between asks again
    require_confirmation(why + " You can undo this.", {"change": actions.sha(json.dumps([add, [[e.key, e.value] for e in edits], drop], sort_keys=True))[:16]})
    _ensure(p, x, k, user.name)
    done = writeback._batch(user, writeback.Edits(project=p, table=s, add=add, edits=edits, delete=drop, comment=args.decision), "active")
    return {"type": x["id"], "decision": args.decision, "table": s, "batch": done["batch"], "snapshot": done["snapshot"],
            "objects": [{"key": v, "title": names.get(v)} for v in tops], **({"id": the} if args.decision == "same" else {}),
            "note": f"Done. Undo it with undo_edits {{table: {s}, batch: {done['batch']}}}.",
            "columns": ["decision", "name"], "rows": [[args.decision, names[v]] for v in keys]}
