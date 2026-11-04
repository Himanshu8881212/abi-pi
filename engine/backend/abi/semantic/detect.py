"""Auto-detect types, keys, relationships, date table; classify fact/dimension. PRD IN-5, SE-2, §2 principle 5.

Everything here is a deterministic query over the project's lake (principle 7). The result is a
proposal: M-03 stores it in the semantic model, where the user can override it.
"""


from abi import storage
from abi.actions import action
from abi.security import Role, User
from abi.storage import _lit, _q

# First format that parses every non-null value wins.
# ponytail: day-first before month-first when both fit (e.g. only days <= 12); override with set_type (TR-1)
FORMATS = {
    "DATE": ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"],
    "TIMESTAMP": ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"],
}
_CANDIDATES = [(t, f) for t, fs in FORMATS.items() for f in fs]
_INT = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}
OVERLAP = 0.95  # IN-5: share of the column's distinct values found in the key


def _family(data_type: str) -> str | None:
    """Key-able type family; floats, booleans, timestamps etc. are never keys."""
    return "int" if data_type in _INT else {"VARCHAR": "text", "DATE": "date"}.get(data_type)


def _columns(cur, schema: str, table: str | None = None) -> list[tuple[str, str, str]]:
    return cur.execute(
        "SELECT table_name, column_name, data_type FROM duckdb_columns()"
        " WHERE database_name = 'lake' AND schema_name = ? AND (? IS NULL OR table_name = ?)"
        " ORDER BY table_name, column_index", [schema, table, table]).fetchall()


def typed_sql(cur, table: str, read=None) -> str:
    """IN-5 "dates stored as text": SELECT over raw.<table> that casts text date/timestamp columns. `read(sql)` returns
    the one result row: `semantic.execute` for committed data; ingest's default reads its uncommitted raw.<table>."""
    read = read or (lambda sql: cur.execute(sql).fetchone())
    src = f"lake.raw.{_q(table)}"
    casts = []
    for _, col, data_type in _columns(cur, "raw", table):
        if data_type != "VARCHAR":
            continue
        c = _q(col)
        n, *parsed = read(
            f"SELECT count({c}), " + ", ".join(f"count(try_strptime({c}, '{_lit(f)}'))" for _, f in _CANDIDATES)
            + f" FROM {src}")
        for (typ, fmt), ok in zip(_CANDIDATES, parsed):
            if n and ok == n:
                casts.append(f"strptime({c}, '{_lit(fmt)}')::{typ} AS {c}")
                break
    return f"SELECT * REPLACE ({', '.join(casts)}) FROM {src}" if casts else f"SELECT * FROM {src}"


def _name_match(col: str, dim: str, key: str) -> bool:
    if key.lower() == "id":  # `id` alone says nothing; need <table>_id
        # ponytail: naive singular (strip one trailing s); declare the relationship when a name doesn't fit
        return col.lower() in (f"{dim.lower()}_id", f"{dim.lower().removesuffix('s')}_id")
    return col.lower() == key.lower() or col.lower().endswith("_" + key.lower())


def detect(project: str, user_id: int | None = None) -> dict:
    """Types, row count, keys, date ranges, many-to-one relationships, date table and fact/dimension class of every model table.
    Catalog metadata comes from a cursor; every read of table data goes through `semantic.execute` (principle 4, OB-1)."""
    from abi import semantic  # not at top: the semantic package imports this module

    cur = storage.lake(project).cursor()

    def read(sql: str) -> tuple:
        return semantic.execute(project, sql, {"detect": project}, user_id)[0]

    tables: dict[str, dict] = {}
    for t, col, data_type in _columns(cur, "model"):
        tables.setdefault(t, {"columns": {}})["columns"][col] = data_type

    for t, info in tables.items():
        cols = [c for c, dt in info["columns"].items() if _family(dt)]
        dates = [c for c, dt in info["columns"].items() if dt == "DATE" or dt.startswith("TIMESTAMP")]
        # min/max cast to text: JSON-ready, and timestamptz needs no pytz on fetch. M3.2: one exact DISTINCT per read,
        # which DuckDB spills to disk; several in one read hold their memory at once (100M orders ran out of 1500MB
        # here). HyperLogLog (a few % off) only rules out columns with under half as many values as rows: no key
        n, *stats = read(
            "SELECT count(*)" + "".join(f", approx_count_distinct({_q(c)}), count({_q(c)})" for c in cols)
            + "".join(f", min({_q(c)})::VARCHAR, max({_q(c)})::VARCHAR" for c in dates)
            + f" FROM lake.model.{_q(t)}")
        stats, ranges = stats[:2 * len(cols)], stats[2 * len(cols):]
        info["date_range"] = {c: {"min": lo, "max": hi} for c, lo, hi in zip(dates, ranges[::2], ranges[1::2])}
        unique = [c for c, d, nn in zip(cols, stats[::2], stats[1::2]) if n and nn == n and d >= n / 2
                  and read(f"SELECT count(DISTINCT {_q(c)}) FROM lake.model.{_q(t)}")[0] == n]
        # prefer a name that says "key": id, <table>_id, then *_id / *key / *code, then the first unique column
        rank = [c for c in unique if _name_match(c, t, "id") or c.lower() == "id"] + \
               [c for c in unique if c.lower().endswith(("id", "key", "code"))] + unique
        info.update(rows=n, key=rank[0] if rank else None)

    date_table = None
    for t, info in tables.items():
        k = info["key"]
        if k and info["columns"][k] == "DATE":
            (days,) = read(f"SELECT date_diff('day', min({_q(k)}), max({_q(k)})) + 1 FROM lake.model.{_q(t)}")
            if days == info["rows"]:  # unique + no gaps = a calendar
                date_table = {"table": t, "column": k}
                break

    relationships = []
    for a, ai in tables.items():
        for col, data_type in ai["columns"].items():
            if col == ai["key"] or not _family(data_type):
                continue
            best = None
            for b, bi in tables.items():
                k = bi["key"]
                if b == a or not k or _family(bi["columns"][k]) != _family(data_type):
                    continue
                if not (_name_match(col, b, k) or (date_table and date_table["table"] == b)):
                    continue
                total, found = read(
                    f"SELECT count(*), count(d.{_q(k)}) FROM (SELECT DISTINCT {_q(col)} AS v FROM lake.model.{_q(a)}"
                    f" WHERE {_q(col)} IS NOT NULL) f LEFT JOIN lake.model.{_q(b)} d ON f.v = d.{_q(k)}")
                overlap = found / total if total else 0
                if overlap >= OVERLAP and (best is None or overlap > best["overlap"]):
                    best = {"from": f"{a}.{col}", "to": f"{b}.{k}", "overlap": round(overlap, 4)}
            if best:
                relationships.append(best)

    dims = {r["to"].split(".")[0] for r in relationships}
    for t, info in tables.items():
        info["kind"] = "dimension" if t in dims else "fact"  # SE-2: the "one" side of a relationship is a dimension
    return {"tables": tables, "relationships": relationships, "date_table": date_table}


@action(Role.editor)
def detect_model(conn, user: User, args: storage.ProjectRef) -> dict:
    """Detects column types, row counts, keys, the min/max of every date/timestamp column, many-to-one relationships (name/type match + >=95% value overlap), the date table, and whether each table is a fact or a dimension."""
    out = detect(args.project, user.id)
    # LLM-2-fix-3: the same facts as result rows, one per column, so an answer quoting them is grounded (D-22, X-01.5)
    # GATE-9-fix-8.7: and each column's relationship overlap (the share of its values found in the other table's key)
    overlap = {r["from"]: r["overlap"] for r in out["relationships"]}
    out["columns"] = ["table", "column", "type", "key", "rows", "min", "max", "overlap"]
    out["rows"] = [[t, c, dt, c == info["key"], info["rows"], *info["date_range"].get(c, {"min": None, "max": None}).values(),
                    overlap.get(f"{t}.{c}")] for t, info in out["tables"].items() for c, dt in info["columns"].items()]
    return out
