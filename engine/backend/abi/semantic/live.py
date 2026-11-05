"""ClickHouse live tables: never copied, read per query. PRD IN-3, IN-6 (ClickHouse live), SE-5 (live read path), SC-4.

A live source is `sources/<t>.json` with a `clickhouse(s)://host[:port]/database` location and `refresh: "live"`.
`add_data` (and `refresh` of the table) records only its columns (tables/<t>.json); every read is one ClickHouse
query through `semantic.execute(..., live=t)` (principle 4), whose Parquet result DuckDB reads, so types are the
ones queries see. The client is built per read from the source file with every connection value explicit (D-27),
after the secrets' host check (D-2), and ClickHouse error text goes to the server log only, scrubbed.
"""

import json
import logging
import re
import tempfile

import clickhouse_connect
import sqlglot
from clickhouse_connect.driver.exceptions import ClickHouseError
from sqlglot import exp

from abi import db, storage
from abi.actions import ActionFailed, memo
from abi.project import SCHEMA, git
from abi.security import credentials

SCHEMES = {"clickhouse": (8123, False), "clickhouses": (8443, True)}  # HTTP, HTTPS
# IN-1 connection string: no userinfo, query or secret (the login is in `database`); host[:port] as plain as F-09 `_host`
LOCATION = r"(clickhouses?)://([A-Za-z0-9.-]+)(?::(\d{1,5}))?/([A-Za-z0-9_]{1,63})"
READ_ONLY = "live ClickHouse table is read-only (IN-3)"
STAMP_TIMEOUT = 5  # s, connect and read: a hung server must not hold a project's live-tile poller (RP-7)


def _sources(project: str) -> dict[str, dict]:
    """{live table: its source file}; M3.6 round 2: read once per read action (`actions.memo`)."""
    return memo(("live.sources", project), lambda: _read_sources(project))


def _read_sources(project: str) -> dict[str, dict]:
    out = {}
    for f in (git.path(project) / "sources").glob("*.json"):
        try:
            doc = json.loads(f.read_text())
        except ValueError:
            continue  # a broken file fails its own load (ingest._source)
        if isinstance(doc, dict) and doc.get("refresh") == "live":
            out[f.stem] = doc
    return out


def is_live(project: str, table: str) -> bool:
    return table in _sources(project)


def refuse(project: str, table: str) -> None:
    """IN-3: write-back, steps and input tables work on DuckLake data only."""
    if is_live(project, table):
        raise ActionFailed(400, f"Table '{table}': {READ_ONLY}; its data stays in ClickHouse.")


def columns(project: str) -> dict[str, dict[str, str]]:
    """{live table: {column: DuckDB type}} as recorded by `register`."""
    out = {}
    for t in _sources(project):
        f = git.path(project) / "tables" / f"{t}.json"
        if f.is_file():
            out[t] = {c["name"]: c["type"] for c in json.loads(f.read_text()).get("columns", [])}
    return out


def _parse(src) -> tuple[re.Match, str]:
    m = re.fullmatch(LOCATION, src.location)
    if not m or not src.database or "." in src.database.table:
        raise ActionFailed(400, "A ClickHouse source needs location clickhouse://host[:port]/database or"
                                " clickhouses://host[:port]/database (no user, password or options) and a `database`"
                                " object whose table is a plain table name.")
    return m, src.database.table


def _src(project: str, table: str):
    """(source, location match, ClickHouse table) of a live table."""
    from abi import ingest  # not at top: ingest imports this package

    src = ingest._source(project, table, table)
    return src, *_parse(src)


def table_sql(project: str, table: str) -> str:
    """`database`.`table` of a live table, for ClickHouse SQL."""
    _, m, t = _src(project, table)
    return exp.table_(t, db=m[4], quoted=True).sql("clickhouse")


def filtered(sql: str, name: str, cond: str | None, hide: list[str] = ()) -> str:
    """SE-6 (E-02.6): ClickHouse `sql` (one live table's read) with every read of table `name` (its `table_sql`)
    replaced by `(SELECT * EXCEPT (<hide>) FROM <table> WHERE <cond>) AS <its alias>`, via sqlglot, so the filter
    applies before any aggregate, grouping set, ORDER BY or OFFSET of the query, and hidden columns (D-110) are gone.
    Per read, never one map for all reads: two live tables may read the same ClickHouse table under different rules
    (verifier round 2)."""
    star = exp.Star(except_=[exp.column(c, quoted=True) for c in hide]) if hide else exp.Star()

    def wrap(n):
        if isinstance(n, exp.Table) and exp.table_(n.name, db=n.db, quoted=True).sql("clickhouse") == name:
            inner = exp.select(star).from_(exp.table_(n.name, db=n.db, quoted=True))
            inner = inner.where(cond, dialect="clickhouse") if cond else inner
            return inner.subquery(exp.to_identifier(n.alias or n.name, quoted=True))
        return n  # transform doesn't descend into a replaced node, so the inner read stays as it is
    return sqlglot.parse_one(sql, read="clickhouse").transform(wrap).sql("clickhouse")


def run(project: str, reads: dict[str, tuple[str, str]], sql: str, prepare=None, after=None):
    """Run each ClickHouse read {name: (live table, ClickHouse SQL)} on its table's source, then DuckDB `sql` on the
    project's lake, which reads each result as table `name` (a temporary view of this cursor only). `prepare(cur)`
    runs first (the WB-4 search path for the imported tables `sql` reads); with `after(cur)`, returns (rows, its
    result) (SC-11: the lake rows read)."""
    # ponytail: one temp file per live read (no pyarrow); pyarrow + query_arrow if it shows up in profiles
    with tempfile.TemporaryDirectory() as d, storage.lake(project).cursor() as cur:  # closing drops the views
        for i, (name, (table, ch)) in enumerate(reads.items()):
            f = f"{d}/{i}.parquet"
            with open(f, "wb") as out:
                out.write(_fetch(project, table, ch))
            cur.execute(f"CREATE TEMP VIEW {storage._q(name)} AS SELECT * FROM read_parquet('{storage._lit(f)}')")
        if prepare:
            prepare(cur)
        rows = cur.execute(sql).fetchall()
        return (rows, after(cur)) if after else rows


def stamp(project: str, table: str) -> str | None:
    """RP-7 change stamp of a live table: rows and last modification of its active parts (an insert, mutation or
    merge changes it); None for a table without parts (View, Distributed), which counts as changed on every tick."""
    _, m, t = _src(project, table)
    n, rows, at = _fetch(project, table, "SELECT count(), sum(rows), max(modification_time) FROM system.parts WHERE active"
                         f" AND database = {exp.convert(m[4]).sql('clickhouse')} AND table = {exp.convert(t).sql('clickhouse')}",
                         "TabSeparated", STAMP_TIMEOUT).decode().split("\t")
    return None if n == "0" else f"{rows} {at.strip()}"


def _fetch(project: str, table: str, sql: str, fmt: str = "Parquet", timeout: int = 300) -> bytes:
    """ClickHouse `sql` on the live table's source, as Parquet (or `fmt`); `timeout` s to connect (at most 10) and
    to read."""
    src, m, _ = _src(project, table)
    port, secure = SCHEMES[m[1]]
    used: dict = {}
    with db.connect() as conn:
        user = credentials.resolve(conn, src.database.user, used, project)
        password = credentials.resolve(conn, src.database.password, used, project)
        credentials.allow(conn, used, m[2].lower(), replay=False)  # D-2: before any connection
    try:  # D-27: every connection value explicit
        client = clickhouse_connect.get_client(
            host=m[2], port=int(m[3] or port), database=m[4], username=user, password=password, secure=secure,
            interface="https" if secure else "http", connect_timeout=min(timeout, 10), send_receive_timeout=timeout, query_limit=0)
        try:
            return client.raw_query(sql, fmt=fmt)
        finally:
            client.close()
    except ClickHouseError as e:  # T-03.5: the text (SQL, server details) to the server log only, scrubbed (SC-4)
        text = credentials.scrub(str(e), used)
        logging.getLogger(__name__).warning("ClickHouse read of %s/%s failed: %s", project, table, text)
        code = re.search(r"\(([A-Z][A-Z_]+)\)", text)
        raise ActionFailed(502, f"Reading live table '{table}' from ClickHouse failed"
                                f" ({code[1] if code else type(e).__name__}); the details are in the server log.") from None


def register(conn, src, unless: str | None) -> None:
    """add_data / refresh of a live source: nothing is copied (IN-3); records its columns in tables/<t>.json and the
    table in the `sources` registry. Skipped (no connection) while the definition is unchanged (`unless`)."""
    from abi import semantic  # not at top: the semantic package imports this module
    from abi.project import imports  # same (imports imports the semantic package)

    _parse(src)
    t = src.table
    fingerprint = json.dumps(src.spec(), sort_keys=True)
    if unless is not None and fingerprint == unless:
        return
    if t in {r[0] for r in storage.lake(src.project).cursor().execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 'model'").fetchall()}:
        raise ActionFailed(409, f"Table '{t}' holds imported data; add the live ClickHouse table under another name.")
    cols = semantic.execute(src.project, f"SELECT * FROM {table_sql(src.project, t)} LIMIT 0", {"live_columns": t},
                            live=t, over='DESCRIBE SELECT * FROM "__live"')
    f = git.path(src.project) / "tables" / f"{t}.json"
    old = json.loads(f.read_text()) if f.exists() else {}
    imports.write(src.project, {f"tables/{t}.json": {"$schema": SCHEMA + "table.json", **old, "id": t, "columns": [
        {"name": c[0], "type": c[1]} for c in cols], "steps": [{"type": "source", "source": t}]}})
    conn.execute("INSERT INTO sources (project, table_name, fingerprint) VALUES (%s, %s, %s) ON CONFLICT"
                 " (project, table_name) DO UPDATE SET fingerprint = excluded.fingerprint", [src.project, t, fingerprint])
