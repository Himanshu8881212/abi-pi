"""Write-back: edit log, effective tables, input tables, agent proposals. PRD §6.8 WB-1..WB-5, WB-9; ST-1, ST-2 (V-02 C-3).

The edit log of model table <t> is the DuckLake table lake.edits.<t>: one row per changed cell (WB-3). A row op has
no column: adding a row logs its JSON as the new value (plus one cell per column), deleting it logs its JSON as the
old value (WB-1). Each edit batch is one `storage._commit`, so one snapshot (ST-2), and the SE-7 cache and time travel
follow edits (ST-3). Edits apply to the model table by row key; raw data is never edited (§11 #9). The effective table
(WB-4) is `effective_sql`: lake.model.<t> without deleted rows, with each edited cell replaced by its latest active
edit, plus the added rows. `semantic._effective` puts it in front of lake.model for every query.

An input table (WB-2) is tables/<t>.json with columns, key and no steps, over an empty lake.model.<t>: its rows are
only edits. The Main Agent's edits are stored as a proposal (status `proposed`, WB-9) with old and new values, and
apply only when the user approves them (`approve_edit`) or never (`reject_edit`, status `rejected`); the log keeps
the proposal's time and author and adds the reviewer and review time (D-20).

A refresh replaces lake.model.<t> but never the edits, so they apply again (WB-6); `flags_sql` flags an applied
edit 'orphaned' when its row (not one an edit added, even if later deleted) or column is gone and 'conflict' when the
model value differs from the one it was made on (`base_value`); a conflict still applies. `review` posts each table's flags to the feed
on change (§5, D-10). Undo (WB-8) sets a batch's active rows to status 'undone': they stay in the history but no
longer apply. Proposals are never applied, flagged or undone.
"""

import contextlib
import json
import uuid
from datetime import UTC, datetime

import duckdb
from pydantic import BaseModel, Field

from abi import actions, storage, transform
from abi.actions import ActionFailed, action
from abi.agent import notify_change
from abi.project import SCHEMA, git
from abi.security import Role, User
from abi.semantic.detect import _columns
from abi.storage import TABLE, Value, _lit, _q

LOG = ('row_key VARCHAR, "column" VARCHAR, old_value VARCHAR, new_value VARCHAR, "user" VARCHAR,'
       ' "timestamp" TIMESTAMP, comment VARCHAR, batch_id VARCHAR, status VARCHAR,'  # WB-3; row_key: JSON {key column: value}
       ' base_value VARCHAR,'  # WB-6: the model table's cell value when edited; old_value is what the user saw (maybe an edit)
       ' reviewed_by VARCHAR, reviewed_at TIMESTAMP')  # D-20: who approved or rejected a proposal, and when


def _logs(cur) -> set[str]:
    """The tables that have an edit log."""
    return {t for (t,) in cur.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 'edits'").fetchall()}


def _key_sets(cur, table: str) -> list[list[str]]:
    return [k for (k,) in cur.execute(
        f"SELECT DISTINCT json_keys(row_key::JSON) FROM lake.edits.{_q(table)} WHERE status = 'active'").fetchall()]


def _latest(table: str) -> str:
    """The applied change of each cell or row: its latest active one (proposed, rejected and undone ones never apply).
    An approved proposal counts from its approval."""
    return (f'SELECT * FROM lake.edits.{_q(table)} WHERE status = \'active\' QUALIFY row_number() OVER'
            f' (PARTITION BY row_key, "column" ORDER BY coalesce(reviewed_at, "timestamp") DESC, batch_id DESC) = 1')


def _shape(cur, table: str) -> tuple[dict[str, str], list[str] | None, list[str]]:
    """(model column types, the active edits' key if it still fits the model table, edited non-key columns in it)."""
    types = {c: dt for _, c, dt in _columns(cur, "model", table)}
    keys = _key_sets(cur, table)
    keys = keys[0] if len(keys) == 1 and set(keys[0]) <= set(types) else None
    cols = [c for (c,) in cur.execute(f"""SELECT DISTINCT "column" FROM lake.edits.{_q(table)}
                                          WHERE status = 'active' ORDER BY 1""").fetchall() if c in types and c not in (keys or [])]
    return types, keys, cols


def _typed_key(keys: list[str], types: dict[str, str], col: str = "row_key") -> str:
    """An edit's row_key as a struct of the model table's key types (compares with `_row_key`)."""
    return "struct_pack(" + ", ".join(f"__k{i} := CAST({col}::JSON->>'{_lit(k)}' AS {types[k]})"
                                      for i, k in enumerate(keys)) + ")"


def _row_key(keys: list[str]) -> str:
    """A model row's key, as a struct."""
    return "struct_pack(" + ", ".join(f"__k{i} := {_q(k)}" for i, k in enumerate(keys)) + ")"


def flags_sql(cur, table: str) -> str:
    """WB-6: each cell's or row's applied change (lake.edits columns) plus `flag`: 'orphaned' (its row is not in the
    model table any more and no active edit ever added it, or its column is gone; a deleted row that's gone too and
    wasn't added by an edit), 'conflict' (the model value is no longer `base_value`; the edit still applies), or NULL;
    and `source_value`, the model cell's value now (NULL for a whole row or a row that's gone)."""
    types, keys, cols = _shape(cur, table)
    latest = _latest(table)
    if not keys:
        return f"SELECT *, 'orphaned' AS flag, NULL AS source_value FROM ({latest})"
    now = ("CASE e.\"column\" " + " ".join(f"WHEN '{_lit(c)}' THEN CAST(m.{_q(c)} AS VARCHAR)" for c in cols) + " END"
           if cols else "NULL")
    kept = f"e.\"column\" IN ({', '.join(f"'{_lit(c)}'" for c in cols)})" if cols else "false"
    # rows an active edit ever added (even if a later edit deleted them): their edits never orphan (GATE-4-fix-2);
    # this covers every input table row, whose model table is always empty
    added = (f'SELECT row_key FROM lake.edits.{_q(table)} WHERE status = \'active\' AND "column" IS NULL'
             f' AND new_value IS NOT NULL')
    gone = f"m.__k IS NULL AND e.row_key NOT IN ({added})"
    flag = (f"CASE WHEN e.\"column\" IS NULL THEN CASE WHEN e.new_value IS NULL AND {gone} THEN 'orphaned' END"
            f" WHEN m.__k IS NULL THEN CASE WHEN {gone} THEN 'orphaned' END"
            f" WHEN NOT {kept} THEN 'orphaned' WHEN {now} IS DISTINCT FROM e.base_value THEN 'conflict' END")
    # the model side is cut to the edited rows first: DuckDB builds the join's hash table on the right side
    return (f"SELECT e.*, {flag} AS flag, {now} AS source_value FROM ({latest}) e LEFT JOIN (SELECT {_row_key(keys)} AS __k, *"
            f" FROM lake.model.{_q(table)} WHERE {_row_key(keys)} IN (SELECT {_typed_key(keys, types)}"
            f" FROM lake.edits.{_q(table)})) m ON m.__k = {_typed_key(keys, types, 'e.row_key')}")


def _seen(project: str, t: str, user_id: int | None, col: str = "row_key") -> str | None:
    """SE-6 (D-42): None when the user reads unfiltered, else a condition on edit-log column `col` keeping the log rows
    of `t` whose row the user may see now: its key is a row of the user's RLS view of the effective table (an edit read
    by the same rule). A row deleted or not yet added, or a log keyed differently from the table, is hidden."""
    from abi import semantic  # not at top: the semantic package imports this module
    from abi.semantic import model, rls

    if not rls.restricted(project, user_id):
        return None
    cur = storage.lake(project).cursor()
    types = {c: dt for _, c, dt in _columns(cur, "model", t)}
    key = (model.load(project)["tables"].get(t) or {}).get("key") or []
    hid = rls.hidden(project, user_id).get(t, set())
    if not key or not set(key) <= set(types) or hid & {k.lower() for k in key}:  # D-110: a row key shows its values
        return "false"
    lit = lambda x: "NULL" if x is None else f"'{_lit(x)}'"
    logged = [r for r in semantic.execute(project, "SELECT DISTINCT row_key, " + ", ".join(
        f"row_key::JSON->>'{_lit(k)}'" for k in key) + f" FROM lake.edits.{_q(t)}", {"edit_keys": t})
        if set(json.loads(r[0])) == set(key)]
    if not logged:
        return "false"
    # ponytail: the log's keys go in as constants (one VALUES row per edited row); a lake table of keys if logs of
    # hundreds of thousands of rows get slow
    vals = ", ".join("(" + ", ".join([lit(rk)] + [f"TRY_CAST({lit(x)} AS {types[k]})" for k, x in zip(key, xs)]) + ")"
                     for rk, *xs in logged)  # VALUES columns: col0 the log's key text, col1.. the typed key
    typed = ", ".join(f"col{i + 1}" for i in range(len(key)))
    seen = semantic.execute(project, f"SELECT col0 FROM (VALUES {vals}) WHERE ({typed}) IN"
                                     f" (SELECT {', '.join(map(_q, key))} FROM {_q(t)})", {"edit_rows_seen": t}, user_id, raw=True)  # 1.2.6 M11: the source rows
    if not seen:
        return "false"
    # D-110: no edit of a hidden column, nor a whole-row add/delete (its values hold every column)
    c = col.removesuffix("row_key") + '"column"'
    return f"{col} IN ({', '.join(lit(rk) for (rk,) in seen)})" + (
        f" AND lower({c}) NOT IN ({', '.join(map(lit, sorted(hid)))})" if hid else "")


def review(project: str, t: str) -> None:
    """Post the table's conflicts and orphaned edits to the notification feed when they change (§5, MA-7, D-10).
    1.2.6 M11: never for a decisions table (its rows are decisions, read under Decided; a feed line would name a table)."""
    from abi import semantic  # not at top: the semantic package imports this module
    from abi.semantic import rls

    cur = storage.lake(project).cursor()
    if t not in _logs(cur) or rls.same_as(t, [c for _, c, _ in _columns(cur, "model", t)]):
        return
    [(c, o)] = semantic.execute(project, f"SELECT count(*) FILTER (flag = 'conflict'), count(*) FILTER"
                                         f" (flag = 'orphaned') FROM ({flags_sql(cur, t)})", {"edit_flags": t})
    conflicts, orphans = "conflicting edit(s) (source value changed, still applied)", "orphaned edit(s) (row gone)"
    problem = " and ".join(f"{n} {what}" for n, what in [(c, conflicts), (o, orphans)] if n)
    notify_change("writeback", project, f"Edits of table '{t}': ", problem and problem + " to review.",
                  "no conflicts or orphaned edits left.", t)


def effective_sql(cur, table: str) -> str | None:
    """SELECT of table's effective rows (WB-4), or None when it has no active edits that still fit its model table."""
    types, keys, cols = _shape(cur, table)
    if not keys:  # orphaned edits (flags_sql) change nothing
        return None
    latest, k = _latest(table), _typed_key(keys, types)
    v = "".join(f"""__v{i} := any_value(new_value) FILTER ("column" = '{_lit(c)}'),"""
                f""" __s{i} := bool_or("column" IS NOT DISTINCT FROM '{_lit(c)}'), """ for i, c in enumerate(cols))
    v += ('__add := bool_or("column" IS NULL AND new_value IS NOT NULL),'
          ' __del := bool_or("column" IS NULL AND new_value IS NULL)')  # never NULL: typed literals below
    val = {c: f"CAST(__e.__v{i} AS {types[c]})" for i, c in enumerate(cols)}
    swap = ", ".join(f"CASE WHEN __e.__s{i} THEN {val[c]} ELSE {_q(c)} END AS {_q(c)}" for i, c in enumerate(cols))
    # The grid pages in insertion order (UI-2), which DuckDB keeps only through order-preserving operators (scan,
    # WHERE, SELECT). Any subquery or join (a scalar subquery is a cross product, a filter on it a nested-loop join)
    # is NO_ORDER and scrambles OFFSET pages past one row group (GATE-4). So the edits go in as a constant MAP literal.
    # ponytail: the literal is re-bound on every query (~40 ms per 1,500 edited rows); a per-cursor
    # `SET VARIABLE` + getvariable() if tables with tens of thousands of edited rows get slow
    lit = lambda x: "NULL::VARCHAR" if x is None else str(x).upper() if isinstance(x, bool) else f"'{_lit(x)}'::VARCHAR"
    keytext = ", ".join(f"row_key::JSON->>'{_lit(k)}'" for k in keys)
    entries = ", ".join(
        "struct_pack(" + ", ".join(f"__k{i} := CAST({lit(x)} AS {types[k]})" for i, (k, x) in enumerate(zip(keys, key)))
        + "): {" + ", ".join(f"'{f}': {lit(x)}" for f, x in e.items()) + "}"
        for key, e in cur.execute(f"SELECT [{keytext}], struct_pack({v}) FROM ({latest}) GROUP BY row_key").fetchall())
    model = (f"SELECT * EXCLUDE (__e){f' REPLACE ({swap})' if swap else ''} FROM (SELECT *, MAP {{{entries}}}"
             f"[{_row_key(keys)}] AS __e FROM lake.model.{_q(table)}) WHERE __e.__del IS NOT TRUE")
    # added rows: a key the model table doesn't have (a refresh that brings the row makes the edits apply to it)
    new = ", ".join(f"__g.k.__k{keys.index(c)}" if c in keys else f"CASE WHEN __e.__s{cols.index(c)} THEN {val[c]} END"
                    if c in val else f"CAST(NULL AS {types[c]})" for c in types)
    on = " AND ".join(f"__m.{_q(k)} = __g.k.__k{i}" for i, k in enumerate(keys))
    # UNION ALL keeps order (DuckDB order preservation), so the model rows page as before and added rows come last
    return (f"WITH __g AS (SELECT {k} AS k, struct_pack({v}) AS __e FROM ({latest}) GROUP BY row_key)"
            f" {model} UNION ALL SELECT * FROM (SELECT {new} FROM __g ANTI JOIN lake.model.{_q(table)} __m ON {on}"
            f" WHERE __g.__e.__add ORDER BY __g.k)")


def views(cur) -> dict[str, str]:
    """{table: effective SELECT} for every table with active edits."""
    return {t: sql for t in _logs(cur) if (sql := effective_sql(cur, t))}


class CellEdit(BaseModel):
    key: list[Value] = Field(min_length=1, description="the row's key value(s), in key column order")
    column: str = Field(min_length=1)
    value: Value | None = Field(description="the new value; null clears the cell")
    old: Value | None = Field(None, description="the value the user saw; if the cell changed since, the edit is"
                                                " refused with 'value changed, reload?' (WB-7)")


class Edits(storage.ProjectRef):
    table: str = Field(min_length=1)
    edits: list[CellEdit] = Field([], description="cells to change")
    add: list[dict[str, Value | None]] = Field([], description="rows to add, {column: value}; key columns required")
    delete: list[list[Value]] = Field([], description="rows to delete, by key value(s) in key column order")
    comment: str | None = Field(None, max_length=2000)


def _table(project: str, t: str) -> tuple:
    """(lake connection, {column: type}, key) of an editable model table."""
    # not at top: the semantic package imports this module
    from abi.semantic import live, model

    live.refuse(project, t)  # IN-3
    con = storage.lake(project)
    types = {c: dt for _, c, dt in _columns(con.cursor(), "model", t)}
    if not types:
        raise ActionFailed(404, f"No table '{t}' in project '{project}'.")
    key = (model.load(project)["tables"].get(t) or {}).get("key")
    if not key:  # WB-5
        raise ActionFailed(409, f"Table '{t}' has no key, so it is read-only. Offer the user to declare its key column(s)"
                                " (the columns that identify a row) with set_table; then its cells can be edited.")
    return con, types, key


def _src(cur, t: str) -> str:
    """Table t's effective rows: its model table, under its active edits once it has an edit log (a table nothing has
    edited yet has none: an object action's first write to it reads here before any log exists, 1.2.6 M7)."""
    return f"({sql})" if t in _logs(cur) and (sql := effective_sql(cur, t)) else f"lake.model.{_q(t)}"


def _row(cur, src: str, t: str, key: list, types: dict, vals: list) -> dict | None:
    """The effective row with key `vals` as {column: text}, or None."""
    where = " AND ".join(f"{_q(k)} = CAST(? AS {types[k]})" for k in key)
    found = cur.execute(f"SELECT COLUMNS(*)::VARCHAR FROM {src} WHERE {where} LIMIT 2", vals).fetchall()
    if len(found) > 1:
        raise ActionFailed(409, f"Key {vals} is not unique in '{t}'; fix the key with set_table.")
    return dict(zip(types, found[0])) if found else None


def _text(cur, value, dt: str) -> str | None:
    """A value as its column type's text (how the log stores values)."""
    return cur.execute(f"SELECT CAST(CAST(? AS {dt}) AS VARCHAR)", [value]).fetchone()[0]


def reserved(project: str, t: str) -> None:
    """1.2.6 M11: tables named same_as_… are decide_same's (semantic/same.py): 409 for any other writer."""
    from abi.semantic import same  # not at top: the semantic package imports this module

    if not t.startswith("same_as_"):
        return
    if x := same.kept(project, t):
        raise ActionFailed(409, f"Table '{t}' keeps the decisions about which {x['plural']} are the same one. Decide with decide_same"
                                f" (in the app: Possible duplicates on the {x['plural']} list, or a {x['name']}'s page), which asks the"
                                " person; take a decision back with undo_edits.")
    raise ActionFailed(409, "Tables named same_as_… are kept for decisions about which records are the same one. Decide with decide_same"
                            " on a type that has `resolve`; for anything else, rename the table.")


BATCH = ["batch", "time", "status", "column", "old"]


def _batch(user: User, args: Edits, status: str, decision: bool = False) -> dict:
    """WB-1: one batch of cell edits, added and deleted rows = one snapshot; `status` proposed (WB-9) or active.
    1.2.6 M11 `decision`: decide_same's own write, marked as such in the log (reviewed_by, which no input carries).
    Nothing else writes a table named same_as_…, whatever its columns and whether or not a type resolves through it:
    not by hand, as a proposal, from an agent, an object action or a flow's step, so a merge is always a person's
    confirmed decision and no row waits there to become one."""
    # not at top: the semantic package imports this module
    from abi.semantic import model, same

    t = args.table
    if not decision:
        reserved(args.project, t)
    if t == model.CALENDAR:  # D-21: derived, regenerated by build_model; edits would be orphaned on every rebuild
        raise ActionFailed(409, f"'{t}' is the generated date table, so it is read-only (derived). Change its range or use"
                                " your own date table in the date table settings (set_date_table).")
    con, types, key = _table(args.project, t)
    if not (args.edits or args.add or args.delete):
        raise ActionFailed(400, "Nothing to change: give edits, add or delete.")
    for e in args.edits:
        if e.column not in types or e.column in key:
            raise ActionFailed(400, f"'{e.column}' is not an editable column of '{t}' (key columns: {key}).")
    for vals in [e.key for e in args.edits] + args.delete:
        if len(vals) != len(key):
            raise ActionFailed(400, f"A row key of '{t}' has {len(key)} value(s): {key}.")
    for r in args.add:
        if bad := set(r) - set(types):
            raise ActionFailed(400, f"'{t}' has no column {sorted(bad)}.")
        if any(r.get(k) is None for k in key):
            raise ActionFailed(400, f"A new row of '{t}' needs its key column(s) {key}.")
    batch, now = uuid.uuid4().hex, datetime.now(UTC).replace(tzinfo=None)
    rid = "json_object(" + ", ".join(f"'{_lit(k)}', CAST(? AS {types[k]})" for k in key) + ")::VARCHAR"
    log: list[list] = []

    def run(cur):
        cur.execute(f"CREATE TABLE IF NOT EXISTS lake.edits.{_q(t)} ({LOG})")
        if (have := _key_sets(cur, t)) and have != [key]:
            raise ActionFailed(409, f"'{t}' has edits keyed by {have[0]}, but its key is now {key}; set the key back"
                                    " with set_table to edit it.")
        src = _src(cur, t)

        def row(vals):
            if (r := _row(cur, src, t, key, types, vals)) is None:
                raise ActionFailed(404, f"No row of '{t}' has key {vals}.")
            return cur.execute(f"SELECT {rid}", vals).fetchone()[0], r

        for e in args.edits:
            k, r = row(e.key)
            # WB-7: checked inside the write transaction (storage._commit serializes writers and retries conflicts)
            seen, has = ("old" in e.model_fields_set and _text(cur, e.old, types[e.column])), r[e.column]
            if types[e.column] == "DOUBLE" and seen and has:  # query rows show 15 digits (semantic.tidy, LLM-2-fix-8)
                seen, has = f"{float(seen):.15g}", f"{float(has):.15g}"
            if "old" in e.model_fields_set and seen != has:
                raise ActionFailed(409, f"The value of {e.column} at key {e.key} changed to {r[e.column]!r} since you"
                                        " read it. Reload?")
            base = (_row(cur, f"lake.model.{_q(t)}", t, key, types, e.key) or {}).get(e.column)  # WB-6; none: added row
            log.append([k, e.column, r[e.column], _text(cur, e.value, types[e.column]), base])
        for vals in args.delete:
            k, r = row(vals)
            log.append([k, None, json.dumps(r), None, None])
        for new in args.add:
            vals = [new[c] for c in key]
            if _row(cur, src, t, key, types, vals):
                raise ActionFailed(409, f"A row of '{t}' with key {vals} already exists.")
            k, r = cur.execute(f"SELECT {rid}", vals).fetchone()[0], {c: _text(cur, new.get(c), dt) for c, dt in types.items()}
            log.extend([[k, None, None, json.dumps(r), None], *([k, c, None, r[c], None] for c in types if c not in key)])
        if len({(k, c) for k, c, *_ in log}) < len(log):
            raise ActionFailed(400, "Each cell or row may be changed once per batch.")
        cur.executemany(f"INSERT INTO lake.edits.{_q(t)} VALUES ({', '.join(['?'] * 12)})",
                        [[*r[:4], user.name, now, args.comment, batch, status, r[4], same.MARK if decision else None, None] for r in log])

    try:
        snap = storage._commit(con, run, user.name, f"{'propose' if status == 'proposed' else 'edit'} {t} ({len(log)} changes)")
    except (duckdb.ConversionException, duckdb.InvalidInputException) as e:
        raise ActionFailed(400, f"A value doesn't fit its column: {e}")
    # D-22, GATE-9-fix-8: the batch ID, its time and the old values the server read are quotable rows; D-114: the
    # caller's own row keys and new values stay out (an agent's edit is a proposal: its values are not data yet)
    out = {"batch": batch, "snapshot": snap, "status": status, "changes": _changes(log), "columns": BATCH,
           "rows": [[batch, str(now), status, c, o] for _, c, o, *_ in log]}
    if status == "proposed":
        out["note"] = "Stored as a proposal; it applies only when the user approves it (they see old and new values)."
    else:
        review(args.project, t)  # a new edit on a flagged cell resolves its flag
    return out


def _changes(log) -> list[dict]:
    """Preview (WB-9): old and new value per changed cell; a row op (column null) shows the whole row as JSON."""
    return [{"row": json.loads(k), "column": c, "old": o, "new": n} for k, c, o, n, *_ in log]


@action(Role.editor)  # SC-2: Editor = build, write-back
def edit_cells(conn, user: User, args: Edits) -> dict:
    """Changes a keyed table as one batch (one data snapshot): edits cells (found by row key), adds rows and deletes rows. Each change is logged with old and new value, user, time, comment and batch ID, and every query then reads the result. Tables without a key are read-only. When the Main Agent calls it, the batch is stored as a proposal the user approves (like propose_edit)."""
    out = _batch(user, args, "proposed" if actions.by_agent() else "active")
    if out["status"] == "active":
        _fire(args.project, args.table, out["snapshot"])
    return out


def _fire(project: str, t: str, snap: int) -> None:
    """FL-2 write-back event: a batch that changes what queries read starts the flows on `t` (never from a flow, D-31)."""
    # not at top: flows imports the semantic package, which imports this module
    from abi import flows

    flows.after_refresh(project, t, snap, "writeback")


@action(Role.editor)
def propose_edit(conn, user: User, args: Edits) -> dict:
    """Proposes cell edits, added and deleted rows for a keyed table without applying them: the proposal is stored with old and new values for the user to review, and applies only when they approve it (approve_edit)."""
    from abi import flows  # not at top: flows imports the semantic package, which imports this module

    if flows.OBJECT.get():  # 1.2.6 M8: also an agent step's tool call
        raise ActionFailed(403, "An alert for one object can't propose an edit: a proposal shows its values to everyone who reviews"
                                " edits, and the alert reaches only project members who see that object. Post to the Inbox instead"
                                " (post_message), and let the person propose the edit.")
    return _batch(user, args, "proposed")


class Proposal(storage.ProjectRef):
    table: str = Field(min_length=1)
    batch: str = Field(pattern=r"^[0-9a-f]{32}$")


@action(Role.editor)
def approve_edit(conn, user: User, args: Proposal) -> dict:
    """Applies a proposed edit batch once the user confirms its preview (old and new values), after checking that its old values are still current (otherwise propose again)."""
    from abi import semantic  # not at top: the semantic package imports this module

    t = args.table
    reserved(args.project, t)  # 1.2.6 M11: no proposal becomes a decision
    con, types, key = _table(args.project, t)
    sql = (f"""SELECT row_key, "column", old_value, new_value FROM lake.edits.{_q(t)}"""
           f" WHERE batch_id = '{args.batch}' AND status = 'proposed'")  # batch: 32 hex digits (Proposal pattern)
    try:
        log = semantic.execute(args.project, sql, {"proposal": args.batch}, user.id)
    except duckdb.CatalogException:
        log = []
    if not log:
        raise ActionFailed(404, f"No open proposal '{args.batch}' for '{t}'.")
    # WB-9: always a person's Confirm click on this preview (the UI's Approve, or the Chat's button for the agent)
    actions.require_confirmation(f"Apply {len(log)} proposed change(s) to '{t}': " + "; ".join(
        f"{json.loads(k)} {c or 'row'}: {o!r} → {n!r}" for k, c, o, n in log) + ".")

    def run(cur):
        src, stale = _src(cur, t), []
        for k, c, old, _ in log:
            row = json.loads(k)
            if set(row) != set(key):
                raise ActionFailed(409, f"The proposal is keyed by {list(row)}, but '{t}' is keyed by {key}; propose again.")
            r = _row(cur, src, t, key, types, [row[x] for x in key])
            if (r and (json.dumps(r) if c is None else r.get(c))) != old:
                stale.append(f"{row} {c or 'row'}")
        if stale:
            raise ActionFailed(409, f"Values changed since the proposal ({'; '.join(stale)}); reload and propose again.")
        if not _decide(cur, t, args.batch, "active", user):  # rejected or approved meanwhile
            raise ActionFailed(404, f"No open proposal '{args.batch}' for '{t}'.")

    snap = storage._commit(con, run, user.name, f"approve edit {t} ({len(log)} changes)")
    review(args.project, t)
    _fire(args.project, t, snap)
    return {"batch": args.batch, "snapshot": snap, "changes": len(log), "columns": ["changes"], "rows": [[len(log)]]}  # D-22


def _decide(cur, t: str, batch: str, status: str, user: User) -> int:
    """D-20: a proposal becomes `status`; the log keeps its time and author and adds the reviewer and review time."""
    return cur.execute(f"UPDATE lake.edits.{_q(t)} SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE batch_id = ?"
                       " AND status = 'proposed'", [status, user.name, datetime.now(UTC).replace(tzinfo=None), batch]).fetchone()[0]


@action(Role.editor)
def reject_edit(conn, user: User, args: Proposal) -> dict:
    """Rejects a proposed edit batch: it never applies and stays in the history as 'rejected'."""
    t, con = args.table, storage.lake(args.project)

    def run(cur):
        if t not in _logs(cur) or not _decide(cur, t, args.batch, "rejected", user):
            raise ActionFailed(404, f"No open proposal '{args.batch}' for '{t}'.")

    return {"batch": args.batch, "snapshot": storage._commit(con, run, user.name, f"reject edit {t}")}


@action(Role.viewer)
def edit_proposals(conn, user: User, args: storage.ProjectRef) -> dict:
    """Lists the open edit proposals of a project (`proposals`), each with its changes' old and new values (WB-9 preview)."""
    from abi import semantic  # not at top: the semantic package imports this module

    out = {}
    for t in sorted(_logs(storage.lake(args.project).cursor())):
        seen = _seen(args.project, t, user.id)  # SE-6: then a system read of the rows the user may see
        for k, c, o, n, u, ts, cm, b in semantic.execute(args.project,
                f"""SELECT row_key, "column", old_value, new_value, "user", "timestamp"::VARCHAR, comment, batch_id
                    FROM lake.edits.{_q(t)} WHERE status = 'proposed' AND {seen or 'true'} ORDER BY "timestamp", batch_id""",
                {"edit_proposals": t}, None if seen else user.id):
            p = out.setdefault(b, {"table": t, "batch": b, "user": u, "timestamp": ts, "comment": cm, "changes": []})
            p["changes"] += _changes([(k, c, o, n)])
    # D-22: batch IDs, times and old values are quotable rows; D-114: keys and proposed values (a caller's own) stay out
    return {"proposals": list(out.values()), "columns": ["batch", "time", "column", "old"],
            "rows": [[p["batch"], p["timestamp"], c["column"], c["old"]] for p in out.values() for c in p["changes"]]}


class Column(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    type: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_ ]*(\([0-9, ]+\))?$", description="DuckDB type, e.g. VARCHAR, INTEGER, DATE, DECIMAL(18,2)")


class InputTable(storage.ProjectRef):
    table: str = Field(pattern=TABLE)
    columns: list[Column] = Field(min_length=1)
    key: list[str] = Field(min_length=1, description="key column(s): input tables are always keyed")
    description: str | None = Field(None, min_length=1, max_length=2000)


@action(Role.editor, definition=True)
def create_input_table(conn, user: User, args: InputTable) -> dict:
    """Creates an empty keyed input table (e.g. a budget to enter by hand): tables/<table>.json with its columns and key, and a model table. Its rows are then added and edited with edit_cells."""
    # not at top: imports and the semantic package import this module
    from abi.project import imports
    from abi.semantic import live, model

    t, names = args.table, [c.name for c in args.columns]
    live.refuse(args.project, t)  # IN-3
    from abi.semantic import same

    if x := same.kept(args.project, t):  # 1.2.6 M11: decide_same makes it at the first decision
        raise ActionFailed(409, f"Table '{t}' is kept for the decisions about which {x['plural']} are the same one: decide_same"
                                " makes it at the first decision. Use another name.")
    if len(set(names)) < len(names) or not set(args.key) <= set(names):
        raise ActionFailed(400, "Column names must be unique and the key must name columns of the table.")
    repo = git.path(args.project)
    if t in model.lake_columns(args.project) or (repo / "tables" / f"{t}.json").exists() \
            or (repo / "sources" / f"{t}.json").exists():
        raise ActionFailed(409, f"Table '{t}' already exists in project '{args.project}'.")
    try:
        cols = [{"name": c.name, "type": transform._type(c.type)} for c in args.columns]  # a type is SQL: rebuilt by sqlglot
    except ValueError as e:
        raise ActionFailed(400, str(e))
    imports.write(args.project, {f"tables/{t}.json": {"$schema": SCHEMA + "table.json", "id": t, "columns": cols,
                                                      "key": args.key, "steps": []}})
    m = model.load(args.project)
    m["tables"][t] = {"kind": "fact", "key": args.key, "description": args.description}
    model.save(args.project, m)
    return {"table": t, "snapshot": create_tables(args.project, {t: cols}, user.name)}


def create_tables(project: str, tables: dict[str, list[dict]], author: str) -> int:
    """WB-2/PC-15: the empty lake.model.<t> of each input table {t: columns}, all in one snapshot; a loaded table of
    that name (D-36: undoing a mapping) is replaced and its raw layer dropped. Never touches edits.<t>."""
    def run(cur):
        for t, cols in tables.items():
            cur.execute(f"DROP TABLE IF EXISTS lake.raw.{_q(t)}")
            cur.execute(f"CREATE OR REPLACE TABLE lake.model.{_q(t)} ({', '.join(f'{_q(c['name'])} {c['type']}' for c in cols)})")

    try:
        return storage._commit(storage.lake(project), run, author, f"create input table {', '.join(tables)}")
    except duckdb.Error as e:  # the action's Git change rolls the files back
        raise ActionFailed(400, f"Can't create {', '.join(tables)}: {e}")


@action(Role.editor)
def undo_edits(conn, user: User, args: Proposal) -> dict:
    """Undoes one applied edit batch as one data snapshot: its changes stop applying, and each cell or row shows its previous edit or the source value again. The batch stays in the history as 'undone'. Proposals are not undone (they never applied)."""
    from abi import flows  # not at top: flows imports the semantic package, which imports this module
    from abi.semantic import same

    t, con = args.table, storage.lake(args.project)
    decisions = same.undo_words(conn, user, args.project, t, args.batch)  # 1.2.6 M11: (what it takes back, the newer decision on it)
    if decisions:  # a decision is a person's, its undo too (undoing a "not the same" or a take-out merges)
        if actions.VIA.get() == "flow" or flows.IN_FLOW.get():
            raise ActionFailed(403, "This is a decision about which records are the same one: a flow can't take a decision back."
                                    " A person does, under Decided.")
        if decisions[1]:  # newest first: undoing under a newer decision would change nothing, or leave that one half done
            raise ActionFailed(409, f"A newer decision builds on this one: undo “{decisions[1]}” first.")
        if actions.needs_click():  # anyone but the person's own click asks them, in their words: an agent's call the
            actions.require_confirmation(f"Undo: {decisions[0]}.")  # harness replays over REST (X-Abi-Confirm) too
        if decisions[2]:  # it can't be checked against the newer decisions (the type's rules are gone): nothing is undone
            raise ActionFailed(409, decisions[2])
    elif actions.by_agent() or actions._call.get((None,) * 4)[3]:  # WB-9: an agent changes data only after the user's click
        actions.require_confirmation(f"Undo edit batch {args.batch} of '{t}'.")  # (also when the harness replays its call over REST)

    def run(cur):
        if t not in _logs(cur) or not cur.execute(f"UPDATE lake.edits.{_q(t)} SET status = 'undone' WHERE batch_id = ?"
                                                  " AND status = 'active'", [args.batch]).fetchone()[0]:
            raise ActionFailed(404, f"No applied edit batch {args.batch} on '{t}'.")
        # the last defence: never a record of two objects, never a chain deeper than the views follow (the transaction rolls back)
        why = None
        if decisions and (sql := effective_sql(cur, t)):
            with contextlib.suppress(duckdb.BinderException):  # a table of that name with other columns: nothing to check
                why = same.unsound({(a, b): yes for a, b, yes in cur.execute(
                    f'SELECT CAST(a AS VARCHAR), CAST(b AS VARCHAR), "same" FROM ({sql})').fetchall()})
        if why:
            raise ActionFailed(409, f"Undoing this isn't possible now: {why}. Undo the newer decisions about these records first.")

    snap = storage._commit(con, run, user.name, f"undo edit batch {args.batch} of {t}")
    review(args.project, t)
    if not decisions:
        _fire(args.project, t, snap)
    return {"batch": args.batch, "snapshot": snap}


class History(storage.ProjectRef):
    table: str = Field(min_length=1)
    key: list[Value] | None = Field(None, description="one row's key value(s): that row's history only")
    column: str | None = Field(None, description="that column's history only; with key, that cell's")
    flagged: bool = Field(False, description="only applied edits flagged 'conflict' or 'orphaned' (WB-6 review)")
    offset: int = Field(0, ge=0, description="rows to skip, to page through the full history")
    limit: int = Field(100, ge=1, le=1000)


@action(Role.viewer)
def edit_history(conn, user: User, args: History) -> dict:
    """The edit history of a table, row or cell, newest first (page with offset): row key, column (null: a whole row added or deleted), old and new value, user, time, comment, batch, status ('active', 'undone', 'proposed', 'rejected' or 'synced': written to the source table, WB-10), who approved or rejected a proposal and when, and for each cell's applied edit its flag: 'conflict' (the source value changed since; the edit still applies) or 'orphaned' (the row or column is gone after a refresh), and the source (model) value now."""
    from abi import semantic  # not at top: the semantic package imports this module

    t = args.table
    con, types, key = _table(args.project, t)
    cur = con.cursor()
    cols = ["row_key", "column", "old_value", "new_value", "base_value", "user", "timestamp", "comment", "batch_id",
            "status", "reviewed_by", "reviewed_at", "flag", "source_value"]
    if t not in _logs(cur):
        return {"columns": cols, "rows": [], "entries": []}
    seen = _seen(args.project, t, user.id, "h.row_key")  # SE-6: then a system read of the rows the user may see
    where = [seen or "true"]
    if args.key is not None:
        if len(key) != len(args.key):
            raise ActionFailed(400, f"A row key of '{t}' has {len(key)} value(s): {key}.")
        where.append(f"{_typed_key(key, types, 'h.row_key')} = struct_pack(" + ", ".join(
            f"__k{i} := CAST('{_lit(str(v))}' AS {types[k]})" for i, (k, v) in enumerate(zip(key, args.key))) + ")")
    if args.column is not None:
        where.append(f"h.\"column\" = '{_lit(args.column)}'")
    if args.flagged:
        where.append("f.flag IS NOT NULL")
    # D-122: a row the same batch adds (a whole-row entry with no old value)
    added = (f"h.row_key IN (SELECT a.row_key FROM lake.edits.{_q(t)} a WHERE a.batch_id = h.batch_id"
             " AND a.\"column\" IS NULL AND a.old_value IS NULL)")
    sql = (f"SELECT {', '.join(f'h.{_q(c)}::VARCHAR' for c in cols[:-2])}, f.flag, f.source_value, {added}"
           f" FROM lake.edits.{_q(t)} h"
           f" LEFT JOIN ({flags_sql(cur, t)}) f ON f.batch_id = h.batch_id AND f.row_key = h.row_key"
           f" AND f.\"column\" IS NOT DISTINCT FROM h.\"column\" WHERE {' AND '.join(where)}"
           f" ORDER BY coalesce(h.reviewed_at, h.\"timestamp\") DESC, h.batch_id DESC, h.row_key, h.\"column\" NULLS FIRST"
           f" LIMIT {args.limit} OFFSET {args.offset}")
    try:
        rows = semantic.execute(args.project, sql, args.model_dump(mode="json"), None if seen else user.id)
    except duckdb.ConversionException as e:
        raise ActionFailed(400, f"A key value doesn't fit its column: {e}")
    entries = [list(r[:-1]) for r in rows]
    # D-122, D-114: a proposed or rejected value never applied, so it is not data to quote: blanked in `rows` (with the
    # key of a row it would add), and every comment is caller-written text (as in edit_proposals); the UI's history
    # panel reads the full `entries`
    out = [[None if i == 7 or ((i == 3 or (i == 0 and r[-1])) and r[9] in ("proposed", "rejected")) else v
            for i, v in enumerate(r[:-1])] for r in rows]
    return {"columns": cols, "rows": out, "entries": entries}
