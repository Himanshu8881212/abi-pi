"""Source sync: write a table's applied edits back to its Postgres/MySQL source table. PRD §6.8 WB-10; DECISIONS D-29.

Opt-in per source (`database.sync`), Owner only, and every call is first a 409 preview the person confirms. The changes
are the effective table (WB-4) against the loaded model table, both read through the semantic engine: a model row the
effective table lacks is a DELETE (only while the source row still equals the loaded one), an effective row the model
lacks is an INSERT, and each latest active cell edit is `UPDATE … SET c = new WHERE <pk> AND (c = base OR c = new)`
("= new" makes a retry idempotent). All run in one source transaction with psycopg or PyMySQL, every value a driver
parameter, each row under a SAVEPOINT; any failing row rolls everything back and the 409 lists every failing row with
the source's current value (principle 7: a value changed in the source is never overwritten). After the COMMIT one
DuckLake snapshot marks the edits `synced` (in the history, never applied, not undoable) and fully reloads the table.
"""

import json
import re
from datetime import UTC

import psycopg
import pymysql
from pydantic import Field
from pymysql.constants import CLIENT

from abi import ingest, semantic, storage
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import git
from abi.security import Role, User, credentials
from abi.semantic import live, model
from abi.semantic.detect import _columns
from abi.storage import _lit, _q
from abi.writeback import TABLE, _latest, _logs, _row_key, _table, _typed_key

DRIVER_ERRORS = (psycopg.Error, pymysql.MySQLError)


class Target(storage.ProjectRef):
    table: str = Field(pattern=TABLE)


def _txt(v) -> str | None:
    return None if v is None else str(v)


def _row_txt(row: dict | None) -> str | None:
    return None if row is None else json.dumps({c: _txt(v) for c, v in row.items()})


@action(Role.owner)  # D-29: writes outside the app, so stricter than Editor (SC-2)
def sync_edits(conn, user: User, args: Target) -> dict:
    """Writes a table's applied edits (changed cells, added and deleted rows) to its Postgres or MySQL source table in one transaction, after the user confirms the preview (target and each change old → new). A value changed in the source since is reported per row and nothing is written. Synced edits show as 'synced' in the history and the table reloads from the source. Needs "sync": true in the source file and a source primary key equal to the table's key."""
    p, t = args.project, args.table
    live.refuse(p, t)  # IN-3: ClickHouse tables are read-only
    if t == model.CALENDAR:
        raise ActionFailed(409, f"'{t}' is the generated date table (derived); it has no source to sync to.")
    if (f := git.path(p) / "tables" / f"{t}.json").is_file():
        what = ("an input table (its rows are edits), so it has no source" if json.loads(f.read_text()).get("steps") == []
                else "built by transform steps, so its rows don't map to one source table")
        raise ActionFailed(409, f"'{t}' is {what}; only tables loaded straight from a database table sync.")
    m = None
    if (git.path(p) / "sources" / f"{t}.json").is_file():
        src = ingest._source(p, t, t)
        m = src.database and re.fullmatch(ingest.DB_LOCATION, src.location)
    if not m:
        raise ActionFailed(409, f"'{t}' isn't loaded from a Postgres or MySQL table, so there is no source to sync to.")
    if not src.database.sync:
        raise ActionFailed(409, f"Source sync is off for '{t}': set \"sync\": true under \"database\" in sources/{t}.json"
                                f" (its login then needs UPDATE, INSERT and DELETE rights on {src.database.table}).")
    ingest._no_pgservice(m[1])
    con, types, key = _table(p, t)
    cur = con.cursor()
    asked = {"sync_edits": t}
    read = lambda sql: semantic.execute(p, sql, asked, user.id)
    ids = t in _logs(cur) and read(f'SELECT DISTINCT {_typed_key(key, types)}, row_key, "column" FROM lake.edits.{_q(t)}'
                                   " WHERE status = 'active'")
    if not ids:
        raise ActionFailed(409, f"'{t}' has no applied edits to sync.")

    # the changes: effective vs model rows of the edited keys, and the latest active cell edits (principle 4)
    tz = {c for c, dt in types.items() if dt == "TIMESTAMP WITH TIME ZONE"}  # read as UTC: DuckDB's tz values need pytz
    val = lambda c, sql: f"timezone('UTC', {sql})" if c in tz else sql
    utc = lambda c, v: v.replace(tzinfo=UTC) if c in tz and v is not None else v
    edited = f"SELECT {_typed_key(key, types)} FROM lake.edits.{_q(t)} WHERE status = 'active'"
    rows = lambda frm: {tuple(r[0].values()): {c: utc(c, v) for c, v in zip(types, r[1:])} for r in read(
        f"SELECT {_row_key(key)}, {', '.join(val(c, _q(c)) for c in types)} FROM {frm} WHERE {_row_key(key)} IN ({edited})")}
    loaded, effective = rows(f"lake.model.{_q(t)}"), rows(_q(t))  # unqualified: the effective table (WB-4)
    changes = [("delete", k, None, r, None) for k, r in loaded.items() if k not in effective]
    changes += [("insert", k, None, None, r) for k, r in effective.items() if k not in loaded]
    latest = read(f'SELECT row_key, "column" FROM ({_latest(t)}) WHERE "column" IS NOT NULL ORDER BY 2, 1')
    skipped = [{"row": {k: _txt(v) for k, v in json.loads(rk).items()}, "column": c,  # gone from the source on a reload
                "error": f"not synced: column {c} missing"} for rk, c in latest if c not in types]
    for c in sorted({c for _, c in latest if c in types and c not in key}):
        for k, base in read(f"SELECT {_typed_key(key, types)}, {val(c, f'CAST(base_value AS {types[c]})')}"
                            f" FROM ({_latest(t)}) WHERE \"column\" = '{_lit(c)}' ORDER BY 1"):
            if (k := tuple(k.values())) in loaded and k in effective:
                changes.append(("update", k, c, utc(c, base), effective[k][c]))
    if not changes:
        why = ", ".join(sorted({s["error"] for s in skipped})) or "the edited rows are gone"
        raise ActionFailed(409, f"'{t}' has no applied edits that change its source rows ({why}).", skipped=skipped)
    raw = {c: dt for _, c, dt in _columns(cur, "raw", t)}
    written = types if any(op != "update" for op, *_ in changes) else {c for _, _, c, *_ in changes}  # row ops: every column
    if bad := [c for c in written if raw.get(c) != types[c]]:  # IN-5 retyped text dates: writing back changes the format
        raise ActionFailed(409, f"Column(s) {bad} of '{t}' have another type in the model than in the source (typed on"
                                " load), so writing them back would change their format; sync refuses.")

    kind, host, db = m[1], m[2].lower(), m[4]
    schema, _, name = src.database.table.rpartition(".")
    schema = schema or ("public" if kind == "postgres" else db)
    target = f"{host}/{db} {schema}.{name}"
    preview = {"target": target, "changes": [
        {"row": dict(zip(key, map(_txt, k))), "column": c, **({"old": _txt(o), "new": _txt(n)} if c else
                                                             {"old": _row_txt(o), "new": _row_txt(n)})}
        for _, k, c, o, n in changes], "skipped": skipped}
    used: dict = {}
    login = (credentials.resolve(conn, src.database.user, used, p), credentials.resolve(conn, src.database.password, used, p))
    credentials.allow(used, host)  # D-2, before any connection (403, audited by actions.run)
    sql = _Sql(kind, schema, name)
    try:
        with _connect(kind, m, login) as s:
            c = s.cursor()
            pk = sql.primary_key(c)
            if sorted(pk) != sorted(key):
                raise ActionFailed(409, f"{target} has primary key {pk or 'none'}, but '{t}' is keyed by {key}; sync needs"
                                        " a source primary key equal to the table's key.")
            require_confirmation(f"Write {len(changes)} change(s) of '{t}' to {target}.", preview)
            errors = []
            for op, k, col, old, new in changes:
                c.execute("SAVEPOINT abi_row")
                row = dict(zip(key, k))
                try:
                    error = getattr(sql, op)(c, row, col, old, new)
                except DRIVER_ERRORS as e:
                    error = credentials.scrub(str(e), used)
                if error:
                    c.execute("ROLLBACK TO SAVEPOINT abi_row")
                    now = sql.current(c, row, types)
                    errors.append({"row": dict(zip(key, map(_txt, k))), "column": col, "error": error,
                                   "source_value": _txt((now or {}).get(col)) if col else _row_txt(now)})
                else:
                    c.execute("RELEASE SAVEPOINT abi_row")
            if errors:
                s.rollback()
                raise ActionFailed(409, f"{len(errors)} row(s) can't be written to {target}; nothing was written. Reload"
                                        " the table to see the source's values.", rows=errors)
            s.commit()
    except DRIVER_ERRORS as e:  # connecting or the catalog read: the driver's text, never a secret (SC-4)
        raise ActionFailed(502, f"Source {target} failed: {credentials.scrub(str(e), used)}")

    done = {k for _, k, *_ in changes}  # only the edits it wrote: one to a missing column stays active
    synced = [[rk, c] for k, rk, c in ids if tuple(k.values()) in done and (c is None or c in types)]

    def mark(cur):  # the same snapshot as the reload, so values never jump (D-29)
        cur.executemany(f"UPDATE lake.edits.{_q(t)} SET status = 'synced' WHERE status = 'active' AND row_key = ?"
                        ' AND "column" IS NOT DISTINCT FROM ?', synced)

    out = {"target": target, "written": len(changes), "skipped": skipped,  # D-22: the counts are quotable rows
           "columns": ["written", "skipped"], "rows": [[len(changes), len(skipped)]]}
    try:
        out["snapshot"] = ingest._load(conn, src, user.name, hook=mark)  # full reload: unless=None never appends
    except ActionFailed as e:  # the source has the changes; the edits stay active and a retry is idempotent
        return out | {"reloaded": False, "note": f"The source has the changes, but reloading '{t}' failed"
                                                 f" ({e.body['message']}); sync again to reload. Retrying is safe."}
    conn.commit()  # as add_data: downstream loads use their own connections
    ingest.refresh(conn, p, {t}, user.name)  # TR-4
    return out | {"reloaded": True}


def _connect(kind: str, m, login: tuple[str, str]):
    """D-27: every setting explicit; hostaddr='' stops libpq taking PGHOSTADDR, PyMySQL reads no environment."""
    port = int(m[3] or ingest.DATABASES[kind])
    if kind == "postgres":
        return psycopg.connect(host=m[2], hostaddr="", port=port, dbname=m[4], user=login[0], password=login[1],
                               connect_timeout=10)
    # no time_zone: TIMESTAMP values compare in the server's default zone, the one DuckDB's mysql scanner loaded them in
    return pymysql.connect(host=m[2], port=port, database=m[4], user=login[0], password=login[1], connect_timeout=10,
                           client_flag=CLIENT.FOUND_ROWS, autocommit=False)  # rowcount = matched rows


class _Sql:
    """Parameterized statements for one source table; identifiers quoted from validated names, every value a %s."""

    def __init__(self, kind: str, schema: str, table: str):
        self.kind, self.names = kind, [schema, table]
        self.table = f"{self.q(schema)}.{self.q(table)}"

    def q(self, name: str) -> str:
        c = '"' if self.kind == "postgres" else "`"
        return c + name.replace(c, c * 2).replace("%", "%%") + c  # %% : both drivers %-format the query

    def where(self, row: dict) -> tuple[str, list]:
        return " AND ".join(f"{self.q(c)} IS NULL" if v is None else f"{self.q(c)} = %s" for c, v in row.items()), \
            [v for v in row.values() if v is not None]

    def primary_key(self, c) -> list[str]:
        sql = ("SELECT k.column_name FROM information_schema.table_constraints t JOIN information_schema.key_column_usage k"
               " ON k.constraint_name = t.constraint_name AND k.table_schema = t.table_schema AND k.table_name = t.table_name"
               " WHERE t.constraint_type = 'PRIMARY KEY' AND t.table_schema = %s AND t.table_name = %s ORDER BY k.ordinal_position"
               if self.kind == "postgres" else
               "SELECT column_name FROM information_schema.key_column_usage WHERE constraint_name = 'PRIMARY'"
               " AND table_schema = %s AND table_name = %s ORDER BY ordinal_position")
        c.execute(sql, self.names)
        return [r[0] for r in c.fetchall()]

    def current(self, c, key: dict, cols) -> dict | None:
        """The source row with `key` as {column: value} (of `cols`), or None."""
        where, args = self.where(key)
        c.execute(f"SELECT {', '.join(map(self.q, cols))} FROM {self.table} WHERE {where}", args)
        r = c.fetchone()
        return r and dict(zip(cols, r))

    def update(self, c, key, col, base, new) -> str | None:
        where, args = self.where(key)
        either, vals = zip(*(self.where({col: v}) for v in (base, new)))
        c.execute(f"UPDATE {self.table} SET {self.q(col)} = %s WHERE {where} AND ({' OR '.join(either)})",
                  [new, *args, *(x for v in vals for x in v)])
        if c.rowcount == 1:
            return None
        return "the source value changed since the edit" if self.current(c, key, [col]) else "the row is gone in the source"

    def delete(self, c, key, _col, old, _new) -> str | None:
        where, args = self.where(old)
        c.execute(f"DELETE FROM {self.table} WHERE {where}", args)
        if c.rowcount == 1 or not self.current(c, key, key):  # gone already: a retry (idempotent)
            return None
        return "the source row changed since it was loaded"

    def insert(self, c, key, _col, _old, new) -> str | None:
        if self.current(c, key, key):  # a retry finds its own row; the source compares in its column types
            where, args = self.where(new)
            c.execute(f"SELECT 1 FROM {self.table} WHERE {where}", args)
            return None if c.fetchone() else "a row with this key already exists in the source"
        c.execute(f"INSERT INTO {self.table} ({', '.join(map(self.q, new))}) VALUES ({', '.join(['%s'] * len(new))})",
                  list(new.values()))
        return None
