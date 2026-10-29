"""DuckLake raw/model/edits, snapshots. PRD §6.3 ST-1..3, NFR Reliability "restore any snapshot".

One lake per project (glossary "Snapshot": a version of the project's data), so snapshots and
restores never cross projects. One DuckDB connection per project per process (`lake(project)`);
readers take `con.cursor()` each (they run in
parallel, see test_ST_cursor_reads_overlap); writes go through `_commit`, which serializes them
per process and retries DuckLake commit conflicts with the other process (app vs worker, V-01).
Every committed write is exactly one DuckLake snapshot; its id is the data version (ST-3).
"""

import os
import re
import shutil
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from contextlib import nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import psycopg
from pydantic import BaseModel, Field

from abi.actions import ActionFailed, action
from abi.security import Role, User

LAYERS = ("raw", "model", "edits")  # ST-1; edits.<table> is created and owned by write-back (W-01)
# lake.query holds derived query copies of facts (semantic.build_copies, S-09-fix-3), never a source of truth
PROJECT = r"^[a-z][a-z0-9_]{0,62}$"  # also the lake's catalog schema and data folder name
TABLE = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
COLUMN = r"^[^.\s][^.]*\.[^.]+$"  # table.column
Value = str | int | float | bool  # a cell value
_META = "__ducklake_metadata_lake"  # DuckLake attaches its catalog under this name
_project: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # connection -> its project
_write_lock = threading.Lock()  # ponytail: the per-process serialized write queue (§9); one writer at a time
RETRIES = 5


def connect(project: str, catalog: str | None = None, data_path: str | None = None) -> duckdb.DuckDBPyConnection:
    """Attach the project's lake as `lake`: catalog schema `<project>`, files in `<data_path>/<project>`.

    Defaults: Postgres catalog via the libpq env vars, files on the /data volume.
    """
    if not re.fullmatch(PROJECT, project):
        raise ValueError(f"invalid project name {project!r}")
    catalog = catalog or os.environ.get("ABI_LAKE_CATALOG", "postgres:")
    data_path = data_path or os.environ.get("ABI_DATA_PATH", "/data/lake")
    con = duckdb.connect()
    # Spills go to this instance's own folder: DuckDB's default `.tmp` in the cwd is shared by every lake of the process
    # and by the worker, and instances spilling into one folder delete and overwrite each other's files (LOAD-MEMORY).
    # DuckDB creates it on the first spill and removes it on close.
    # ponytail: a killed process leaves its folder behind; sweep <data_path>/.tmp at startup if that piles up
    os.makedirs(f"{data_path}/.tmp", exist_ok=True)
    con.execute(f"SET temp_directory = '{_lit(data_path)}/.tmp/{project}-{uuid.uuid4().hex}'")
    # D-27 (H-01.7-fix-1): httpfs copies AWS_*/DUCKDB_S3_* env into its global s3_* settings when it loads, and an
    # S3 source without its own key would be signed with the server's identity; RESET GLOBAL restores the no-env defaults.
    con.execute("LOAD httpfs")
    for (s,) in con.execute("SELECT name FROM duckdb_settings() WHERE starts_with(name, 's3_')").fetchall():
        con.execute(f"RESET GLOBAL {s}")
    if lim := os.environ.get("ABI_DUCKDB_MEMORY_LIMIT"):  # D-17 memory budget; DuckDB spills to disk above it
        con.execute(f"SET memory_limit = '{_lit(lim)}'")
    # Another process attaching the same new lake races DuckLake's `CREATE SCHEMA IF NOT EXISTS` in Postgres (duplicate
    # key, DEMO-1.11-fix-3), so a Postgres catalog attaches under a session advisory lock in that same database.
    with (
        psycopg.connect(catalog.removeprefix("postgres:"), autocommit=True) if catalog.startswith("postgres:")
        else nullcontext()
    ) as pg:
        if pg:
            pg.execute("SELECT pg_advisory_lock(hashtext(%s))", [f"lake-attach:{project}"])  # released on close
            # GATE-9-fix-7: the catalog's pool kept one idle Postgres connection per lake for good, so a process held
            # one per project (46 projects: max_connections hit). Unpooled, it holds only in-flight transactions'.
            # ponytail: ~6 ms connect per catalog transaction; keep an LRU of hot lakes pooled
            # (postgres_configure_pool) if that latency matters
            con.execute("SET pg_pool_max_connections = 0")
        con.execute(
            f"ATTACH 'ducklake:{_lit(catalog)}' AS lake"
            f" (DATA_PATH '{_lit(data_path)}/{project}', METADATA_SCHEMA '{project}', DATA_INLINING_ROW_LIMIT 0)"
        )
        for layer in LAYERS:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS lake.{layer}")  # no-op (no snapshot) once it exists
        # API-SRC.2-fix-1: DuckLake 1.5.6 stores inlined rows of a Postgres catalog as text and can't read a STRUCT
        # column back (DATE/TIMESTAMP/UUID fields fail, quotes and BLOBs come back changed), so user data is never
        # inlined (above, per attach); only the write-back log, small frequent writes of flat VARCHARs, keeps inlining.
        if not con.execute("SELECT 1 FROM lake.options() WHERE option_name = 'data_inlining_row_limit'"
                           " AND scope = 'SCHEMA' AND scope_entry = 'edits'").fetchone():
            con.execute("CALL lake.set_option('data_inlining_row_limit', 10, schema => 'edits')")
        if pg:
            _oid[con] = _schema_oid(pg, project)
    _project[con] = project
    return con


def _schema_oid(pg, project: str) -> int | None:
    """The catalog schema's Postgres OID: a rename (delete/rename_project) keeps it, a new lake under the name gets a new one."""
    r = pg.execute("SELECT oid FROM pg_namespace WHERE nspname = %s", [project]).fetchone()
    return r and r[0]


_lakes: dict[str, duckdb.DuckDBPyConnection] = {}
_lakes_lock = threading.Lock()
_oid: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # Postgres-catalog connection -> its schema's OID
_checked: dict[str, float] = {}  # project -> monotonic time its cached lake was last matched against the catalog
RECHECK_S = 1.0


def lake(project: str) -> duckdb.DuckDBPyConnection:
    """The process-wide connection to a project's lake, created once under a lock (other processes: `connect`).
    DEMO-BRIEF-fix-2: another process may delete or rename the project and a new lake take its name; DuckLake then
    reads the new catalog by name but answers from the old lake's cached catalog for the same snapshot IDs (missing
    tables and columns), so a cached lake whose catalog schema is no longer the one it attached is attached again."""
    # ponytail: one DuckDB instance per project; attach several lakes to one instance if projects get many
    # ponytail: matched at most once per RECHECK_S, so a lake recreated under the name is missed for up to that long;
    # LISTEN/NOTIFY from delete/rename_project if that window matters
    with _lakes_lock:
        con, now = _lakes.get(project), time.monotonic()
        if con in _oid and now - _checked.get(project, 0) > RECHECK_S:
            _checked[project] = now
            with psycopg.connect(os.environ.get("ABI_LAKE_CATALOG", "postgres:").removeprefix("postgres:")) as pg:
                if _schema_oid(pg, project) != _oid[con]:
                    del _lakes[project]  # not closed: other threads may be reading it (as move_lake)
        if project not in _lakes:
            _lakes[project] = connect(project)
            _checked[project] = now
        return _lakes[project]


def _close_lakes() -> None:
    """Close (not just forget) every lake: a cursor left holding an unfinished result keeps its Postgres catalog
    connection idle in transaction, and that blocks DROP/ALTER SCHEMA on the catalog (R-8.1). Closing the parent closes
    its cursors."""
    with _lakes_lock:
        for con in _lakes.values():
            con.close()
        _lakes.clear()


lake.cache_clear = _close_lakes


def snapshot_id(con: duckdb.DuckDBPyConnection) -> int:
    """ST-3: current data version, the cache and time-travel key."""
    return con.cursor().execute("SELECT id FROM lake.current_snapshot()").fetchone()[0]


def made(snap: int | None, **extra) -> dict:
    """An action's answer naming the snapshot it made, also as `columns`/`rows` so the agent may quote it (D-22,
    GATE-9-fix-3)."""
    return {**extra, "snapshot": snap, "columns": ["snapshot"], "rows": [[snap]]}


def refresh(con, table: str, raw_sql: str, *, author: str, model_sql: str | Callable | None = None,
            append: Callable | None = None, hook: Callable | None = None) -> int:
    """ST-2: replace raw.<table> and model.<table> in one snapshot. Never touches edits.<table>.

    `raw_sql`/`model_sql` are built by our own ingest/transform code, never taken from users. A callable
    `model_sql(cursor)` is called once raw.<table> exists, in the same transaction (type detection, IN-5).
    `append(cursor)` (IN-6 incremental) returns a SELECT of rows to add to raw.<table> instead, or None to replace it.
    `hook(cursor)` runs first in the same transaction (WB-10: marking synced edits).
    On any error the transaction rolls back and the previous snapshot stays live.
    """
    t = _q(table)
    model_sql = model_sql or f"SELECT * FROM lake.raw.{t}"

    def run(cur):
        if hook:
            hook(cur)
        rows = append(cur) if append else None
        cur.execute(f"INSERT INTO lake.raw.{t} {_ordered(rows)}" if rows
                    else f"CREATE OR REPLACE TABLE lake.raw.{t} AS {_ordered(raw_sql)}")
        cur.execute(f"CREATE OR REPLACE TABLE lake.model.{t} AS {_ordered(model_sql(cur) if callable(model_sql) else model_sql)}")

    return _commit(con, run, author, f"refresh {table}")  # failures reach the feed via the caller (ingest scrubs secrets)


def restore(con, version: int, *, author: str) -> int:
    """NFR "restore any snapshot" as a NEW snapshot holding the data of `version` (history is kept, V-01)."""
    version = int(version)
    meta = f"{_META}.{_project[con]}"

    def run(cur):
        if not cur.execute("SELECT 1 FROM lake.snapshots() WHERE snapshot_id = ?", [version]).fetchone():
            if version < cur.execute("SELECT id FROM lake.current_snapshot()").fetchone()[0]:  # ids only grow
                raise ValueError(f"snapshot {version} has expired: snapshots are kept {retention_days():g} days (D-12)")
            raise ValueError(f"unknown snapshot {version}")
        then = set(cur.execute(
            f"""SELECT s.schema_name, t.table_name FROM {meta}.ducklake_table t
                JOIN {meta}.ducklake_schema s USING (schema_id)
                WHERE $v >= t.begin_snapshot AND (t.end_snapshot IS NULL OR $v < t.end_snapshot)
                  AND $v >= s.begin_snapshot AND (s.end_snapshot IS NULL OR $v < s.end_snapshot)""",
            {"v": version}).fetchall())
        now = set(cur.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = 'lake'").fetchall())
        for s, t in now - then:
            cur.execute(f"DROP TABLE lake.{_q(s)}.{_q(t)}")
        for s, t in then:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS lake.{_q(s)}")
            # ponytail: copies every table, even unchanged ones; skip tables with no change since `version` if restores get slow
            name = f"lake.{_q(s)}.{_q(t)}"
            cur.execute(f"CREATE OR REPLACE TABLE {name} AS {_ordered(f'SELECT * FROM {name} AT (VERSION => {version})')}")

    return _commit(con, run, author, f"restore snapshot {version}")


ARCHIVE_DAYS = 30


def archive(con, tables: set[str], *, author: str) -> int:
    """PC-7: move removed tables' data (every layer) to lake.archive."<table>__<layer>__<unix time>", one snapshot."""
    stamp = int(time.time())

    def run(cur):
        cur.execute("CREATE SCHEMA IF NOT EXISTS lake.archive")
        for s, t in cur.execute("SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = 'lake'"
                                " AND schema_name IN ('raw', 'model', 'edits')").fetchall():
            if t in tables:
                cur.execute(f"CREATE TABLE lake.archive.{_q(f'{t}__{s}__{stamp}')} AS {_ordered(f'SELECT * FROM lake.{s}.{_q(t)}')}")
                cur.execute(f"DROP TABLE lake.{s}.{_q(t)}")

    return _commit(con, run, author, f"archive {', '.join(sorted(tables))}")


def purge_archive(con, *, author: str, now: float | None = None) -> None:
    """PC-7: drop archived table data older than 30 days (DuckLake frees the files once its snapshots expire)."""
    cutoff = (now or time.time()) - ARCHIVE_DAYS * 86400
    old = [t for (t,) in con.cursor().execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 'archive'").fetchall()
        if int(t.rsplit("__", 1)[1]) < cutoff]
    if old:
        _commit(con, lambda cur: [cur.execute(f"DROP TABLE lake.archive.{_q(t)}") for t in old], author, "purge archive")


DELETED = "_deleted_"  # an archived lake's name prefix: never a project ID (those start with a letter)


def lake_exists(conn, project: str) -> bool:
    """A lake catalog schema or data folder named `project` exists (also a leftover of an earlier project)."""
    folder = Path(os.environ.get("ABI_DATA_PATH", "/data/lake")) / project
    return folder.exists() or conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [project]).fetchone() is not None


def move_lake(conn, old: str, new: str) -> list[tuple[str, str]]:
    """E-14.2: rename a project's lake catalog schema inside `conn`'s transaction and return the data folder move
    [(from, to)] for the caller to make before it commits. DuckLake stores file paths relative to the catalog's
    `data_path`, so moving the folder and updating that one value keeps every file readable (checked on 1.5.6,
    work/validation/E-14.2.md). No lake: nothing to move."""
    if not re.fullmatch(PROJECT, old):
        return []  # X-04-fix-4: a folder with an invalid ID never got a lake (connect refuses it)
    if os.environ.get("ABI_LAKE_CATALOG", "postgres:") != "postgres:":  # only then the catalog is in `conn`'s database
        raise ActionFailed(409, "Moving a project's data needs the Postgres lake catalog (ABI_LAKE_CATALOG=postgres:).")
    with _write_lock, _lakes_lock:
        _lakes.pop(old, None)  # the SE-7 cache is keyed weakly by this connection, so it goes too
        if not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [old]).fetchone():
            return []
        conn.execute("SET LOCAL lock_timeout = '10s'")  # a worker mid-commit on this lake holds it briefly
        conn.execute(f'ALTER SCHEMA "{old}" RENAME TO "{new}"')  # both names are PROJECT or DELETED + PROJECT
        path = conn.execute(f"""SELECT value FROM "{new}".ducklake_metadata WHERE key = 'data_path'""").fetchone()["value"]
        to = str(Path(path).with_name(new)) + "/"
        conn.execute(f"""UPDATE "{new}".ducklake_metadata SET value = %s WHERE key = 'data_path'""", [to])
        return [(path, to)] if Path(path).is_dir() else []  # tiny tables are inlined in the catalog: maybe no folder yet


def purge_deleted(conn, now: float | None = None) -> None:
    """PC-7: drop the lakes of projects deleted over 30 days ago (`_deleted_<unix time>_<project>`)."""
    cutoff = (now or time.time()) - ARCHIVE_DAYS * 86400
    for s in [r["nspname"] for r in conn.execute("SELECT nspname FROM pg_namespace WHERE starts_with(nspname, %s)", [DELETED])]:
        if int(s.split("_")[2]) < cutoff:
            path = conn.execute(f"""SELECT value FROM "{s}".ducklake_metadata WHERE key = 'data_path'""").fetchone()["value"]
            conn.execute(f'DROP SCHEMA "{s}" CASCADE')
            shutil.rmtree(path, ignore_errors=True)
            conn.commit()


def retention_days() -> float:
    """D-12: snapshots are kept this many days (`ABI_SNAPSHOT_RETENTION_DAYS`, default 30)."""
    return float(os.environ.get("ABI_SNAPSHOT_RETENTION_DAYS", "30"))


def expire_snapshots(con, *, author: str) -> None:
    """D-12: expire snapshots older than the retention window and delete the files only they referenced.

    DuckLake never expires the current snapshot (checked at the V-01 pin, see work/validation/M-14.md).
    """
    def run(cur):
        cur.execute("SELECT count(*) FROM ducklake_expire_snapshots('lake', older_than => now() - to_seconds(?))",
                    [retention_days() * 86400])
        cur.execute("SELECT count(*) FROM ducklake_cleanup_old_files('lake', cleanup_all => true)")

    _commit(con, run, author, "expire snapshots")


class ProjectRef(BaseModel):  # the one-field project input (git.ProjectRef keeps no pattern)
    project: str = Field(pattern=PROJECT)


class Restore(ProjectRef):
    snapshot: int = Field(ge=0)


class Snapshots(ProjectRef):
    limit: int = Field(50, ge=1, le=1000)


@action(Role.editor)
def list_snapshots(conn, user: User, args: Snapshots) -> dict:
    """Lists the project's data snapshots, newest first: id (for restore_snapshot), time (UTC), author and what made it. Expired snapshots (older than the retention window) are gone."""
    from abi import semantic  # not at top: semantic imports this module
    # The cutoff is a literal: expiry (D-12) makes no snapshot, so a cached `lake.snapshots()` would keep expired ids
    # under the same snapshot key (SE-7); a moving cutoff is both a fresh key and the window itself. Current never expires.
    cutoff = datetime.now(UTC) - timedelta(days=retention_days())
    sql = ("SELECT snapshot_id, strftime(timezone('UTC', snapshot_time), '%Y-%m-%d %H:%M:%S UTC'), author, commit_message"
           f" FROM lake.snapshots() WHERE snapshot_time >= TIMESTAMPTZ '{cutoff.isoformat()}'"
           f" OR snapshot_id = (SELECT id FROM lake.current_snapshot()) ORDER BY snapshot_id DESC LIMIT {args.limit}")
    rows = semantic.execute(args.project, sql, args.model_dump())  # lake metadata, no rows: a system read (E-02.4)
    return {"columns": ["id", "time", "author", "message"], "rows": rows}


@action(Role.owner, destructive=True)
def restore_snapshot(conn, user: User, args: Restore) -> dict:
    """Restores the project's data to that snapshot as a new snapshot; write-back edits made after that snapshot are reverted too. Snapshots are kept 30 days by default (ABI_SNAPSHOT_RETENTION_DAYS); older ones have expired and can't be restored."""
    try:
        return made(restore(lake(args.project), args.snapshot, author=user.name))
    except ValueError as e:
        raise ActionFailed(400, str(e))


def _commit(con, run: Callable, author: str, message: str) -> int:
    """Run `run(cursor)` as one transaction = one snapshot; retry DuckLake commit conflicts with backoff."""
    with _write_lock:
        for attempt in range(RETRIES):
            cur = con.cursor()
            cur.begin()
            try:
                cur.execute("CALL lake.set_commit_message(?, ?)", [author, message])
                run(cur)
                cur.commit()
                return cur.execute("SELECT id FROM lake.current_snapshot()").fetchone()[0]
            except duckdb.TransactionException as e:
                with suppress(duckdb.Error):  # a failed DuckLake commit has already ended the transaction
                    cur.rollback()
                if "conflict" not in str(e).lower() or attempt == RETRIES - 1:
                    raise
                time.sleep(0.05 * 2**attempt)
            except BaseException:
                cur.rollback()
                raise


def _ordered(sql: str) -> str:
    """Writes keep source row order, so the grid's OFFSET paging shows file order (UI-2, GATE-1-fix-2).

    DuckLake's insert always plans a parallel COPY that interleaves row groups from several threads, ignoring
    preserve_insertion_order (duckdb/ducklake#1356; fix PR #1357 unmerged). `row_number() OVER ()` is a streaming
    window, which DuckDB never runs in parallel, so this one write pipeline runs on one thread. Never `SET threads`
    at runtime instead: it deadlocks the instance's concurrent queries in DuckDB 1.5.6 (D-04-fix-2).
    """
    # ponytail: the write pipeline is single-threaded (~3x slower loads); drop this once DuckLake writes in order.
    return f"SELECT * EXCLUDE (__abi_rn) FROM (SELECT *, row_number() OVER () AS __abi_rn FROM ({sql}\n))"


def _q(name: str) -> str:
    if not name:
        raise ValueError("empty identifier")
    return '"' + name.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return s.replace("'", "''")
