"""SE-10 SQL endpoint (work/validation/E-04.md decision 4, E-04.6): action `sql_query` and a Postgres wire-protocol
v3 server in front of it. The server only adapts the protocol: every statement is `actions.run("sql_query", via="sql")`,
which runs through `semantic.execute` (RLS, SE-7 cache, query_log). Every caller's SQL passes `rls.check` (D-44)."""

import asyncio
import datetime
import ipaddress
import itertools
import json
import logging
import os
import re
import ssl
import struct
import uuid
from decimal import Decimal, localcontext
from functools import cache

import duckdb
import sqlglot
from pydantic import Field
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from abi import actions, db, security, storage
from abi.actions import ActionFailed, action
from abi.security import Role, User
from abi.semantic import detect, execute, limited, rls
from abi.storage import _lit, _q

CAP = 100_000  # rows per statement (D-17)
TIMEOUT = 60  # seconds per statement (R-8.2): a lake read or catalog query still running then is stopped


SCHEMA = "public"  # what SQL clients see the model tables under (Postgres' default schema)
CATALOGS = ("pg_catalog", "information_schema")
# Postgres type OID -> DuckDB type a text parameter is cast to (others stay untyped literals, like Postgres' unknown)
CASTS = {16: "BOOLEAN", 20: "BIGINT", 21: "SMALLINT", 23: "INTEGER", 700: "FLOAT", 701: "DOUBLE", 1082: "DATE",
         1083: "TIME", 1114: "TIMESTAMP", 1184: "TIMESTAMPTZ", 2950: "UUID"}
PARAMS = {"server_version": "16.0", "server_encoding": "UTF8", "client_encoding": "UTF8", "DateStyle": "ISO, MDY",
          "integer_datetimes": "on", "standard_conforming_strings": "on", "TimeZone": "UTC"}
# the server's settings as SHOW and pg_settings answer them (never DuckDB's own settings: they hold server paths)
SHOWN = {k.lower(): v for k, v in PARAMS.items()} | {"search_path": SCHEMA, "transaction_isolation": "read committed",
                                                      "transaction_read_only": "on", "max_identifier_length": "63"}


class SqlInput(storage.ProjectRef):
    sql: str = Field(min_length=1, max_length=100_000)
    params: list[str | None] = Field([], max_length=10_000, description="text values of $1, $2, ...")
    types: list[int] = Field([], max_length=10_000, description="Postgres type OIDs of the params, 0 = unknown")
    describe: bool = Field(False, description="column names and types only, no rows")


def _bind(sql: str, params: list, types: list[int], project: str, known: set[str]) -> str:
    """E-04.7: put each $n parameter into the parse tree as a constant (never spliced into the text), and read
    `public.<t>` / `<project>.public.<t>` (how BI tools name a table) as `<t>`. The text changes only if one applies."""
    if not params and not re.search(SCHEMA, sql, re.IGNORECASE):
        return sql
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        if params:
            raise ActionFailed(400, "The query could not be parsed to bind its parameters.") from None
        return sql
    changed = False

    def put(node):
        nonlocal changed
        if isinstance(node, exp.Placeholder) and str(node.this).isdigit():
            if not 1 <= (n := int(node.this)) <= len(params):
                raise ActionFailed(400, f"No value for parameter ${n}.")
            changed, v = True, params[n - 1]
            cast = CASTS.get(oid := types[n - 1] if n <= len(types) else 0)
            if oid == 1700 and v is not None and re.fullmatch(r"-?\d{1,38}(\.\d*)?", v) and len(v.lstrip("-")) <= 39:
                cast = f"DECIMAL(38, {len(v.partition('.')[2])})"  # numeric keeps its scale (wider ones stay text)
            return exp.null() if v is None else exp.cast(exp.Literal.string(v), cast) if cast else exp.Literal.string(v)
        if isinstance(node, exp.Table) and node.db.lower() == SCHEMA and node.catalog in ("", project) and \
                node.name.lower() in known:
            changed = True
            node.set("db", None)
            node.set("catalog", None)
        return node

    tree = tree.transform(put)
    return tree.sql("duckdb") if changed else sql


def _parsed(cur, sql: str) -> tuple[bool, set[tuple[str, str]]]:
    """DuckDB's own parser (reads no data; sqlglot reads `(DESCRIBE t)`, `(SHOW TABLES)` as subqueries): is `sql`
    exactly one plain SELECT, and which (schema, table) names does it read."""
    tree = json.loads(cur.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    names, kinds, todo = set(), set(), [tree]
    while todo:
        node = todo.pop()
        if isinstance(node, dict):
            kinds.add(kind := node.get("type") if isinstance(node.get("type"), str) else None)  # a cast's "type" is a dict
            if kind == "BASE_TABLE":
                names.add((node["schema_name"].lower(), node["table_name"].lower()))
            todo += node.values()
        elif isinstance(node, list):
            todo += node
    return not tree["error"] and len(tree["statements"]) == 1 and not kinds & {"SHOW_REF", "TABLE_FUNCTION"}, names


# E-04.7: the Postgres catalog functions BI tools call in introspection queries, on top of D-44's list. Allowed only
# in the catalog sandbox below (no data in it); each one is reviewed in work/validation/E-04.7.md.
CATALOG_CALLS = {
    "version", "current_version", "current_database", "current_catalog", "current_schema", "current_schemas",
    "session_user", "current_user", "pg_get_userbyid", "pg_table_is_visible", "pg_type_is_visible", "format_type",
    "pg_get_expr", "pg_get_constraintdef", "pg_get_indexdef", "pg_get_partkeydef", "pg_get_statisticsobjdef_columns",
    "obj_description", "col_description", "has_table_privilege", "has_schema_privilege", "has_any_column_privilege",
    "pg_encoding_to_char", "_pg_expandarray", "array_upper", "array_to_string", "replace", "quote_ident",
    "generate_series", "unnest", "explode", "string_agg", "group_concat", "pg_relation_is_publishable", "format",
    "pg_partition_ancestors"}
SANDBOX = {"threads": 1, "memory_limit": "64MB", "allow_persistent_secrets": False, "autoload_known_extensions": False,
           "autoinstall_known_extensions": False}
PG_ONLY = [(r"OPERATOR\s*\(\s*(?:pg_catalog\s*\.\s*)?([^)\s]+)\s*\)", r"\1"),  # psql: OPERATOR(pg_catalog.~)
           (r"\s+COLLATE\s+(?:pg_catalog\s*\.\s*)?\"?default\"?", ""), (r'(?:\bpg_catalog|"pg_catalog")\s*\.\s*(?=[\w"])', ""),
           (r"\binformation_schema\s*\.\s*(?=_pg_expandarray\b)", ""),  # pgjdbc getPrimaryKeys
           (r"\bAS\s+(select|insert|update|delete|references|trigger|truncate)\b", r'AS "\1"')]  # Metabase privileges
# Postgres functions and types DuckDB lacks, in the sandbox's `main` schema (no data behind any of them)
SETUP = """CREATE TYPE main.regclass AS BIGINT; CREATE TYPE main.regtype AS BIGINT; CREATE TYPE main.regnamespace AS BIGINT;
CREATE TYPE main.regrole AS BIGINT; CREATE TYPE main.regproc AS VARCHAR;
CREATE MACRO main.pg_get_indexdef(i) AS NULL, (i, c, p) AS NULL; CREATE MACRO main.pg_get_partkeydef(o) AS NULL;
CREATE MACRO main.pg_get_statisticsobjdef_columns(o) AS NULL; CREATE MACRO main.array_upper(a, d) AS len(a);
CREATE MACRO main.pg_encoding_to_char(e) AS 'UTF8'; CREATE MACRO main.quote_ident(s) AS '"' || replace(s, '"', '""') || '"';
CREATE MACRO main._pg_expandarray(a) AS unnest([{'x': a[i], 'n': i} for i in range(1, len(a) + 1)]);
CREATE MACRO main.pg_get_expr(e, r) AS NULL, (e, r, p) AS NULL; CREATE MACRO main.pg_relation_is_publishable(r) AS false;
CREATE MACRO main.pg_partition_ancestors(r) AS CAST(r AS BIGINT);
CREATE MACRO main.version() AS 'PostgreSQL 16.0 (ABI SQL endpoint)'"""
# Postgres catalog relations DuckDB lacks, as subqueries: empty ones (no inheritance, policies, publications, ranges,
# extended statistics in a lake), and columns DuckDB's own views lack
_EMPTY = {"pg_inherits": "inhrelid, inhparent, inhseqno, inhdetachpending",
          "pg_policy": "oid, polname, polrelid, polcmd, polpermissive, polroles, polqual, polwithcheck",
          "pg_roles": "oid, rolname", "pg_statistic_ext": "oid, stxrelid, stxnamespace, stxname, stxkeys, stxkind, stxstattarget",
          "pg_publication": "oid, pubname, puballtables, pubviaroot", "pg_publication_namespace": "pnpubid, pnnspid",
          "pg_publication_rel": "prpubid, prrelid, prqual, prattrs SMALLINT[]","pg_range": "rngtypid, rngsubtype, rngmultitypid",
          "pg_stat_user_tables": "relid, schemaname, relname, n_live_tup", "pg_matviews": "schemaname, matviewname",
          "pg_views": "schemaname, viewname, viewowner, definition"}
# pg_catalog and information_schema are namespaces too (DuckDB lists only the database's own schemas)
_NS = ("SELECT oid, schema_name AS nspname, 0 AS nspowner, NULL AS nspacl FROM duckdb_schemas() WHERE database_name ="
       " current_database() OR (database_name = 'system' AND schema_name IN ('pg_catalog', 'information_schema'))")
_PG = "(SELECT oid FROM duckdb_schemas() WHERE database_name = 'system' AND schema_name = 'pg_catalog')"
# the Postgres types the endpoint sends (OID), with a receive function: Npgsql loads only types that have one
_TYPES = [(16, "bool", 1, "B"), (17, "bytea", -1, "U"), (18, "char", 1, "S"), (19, "name", 64, "S"), (20, "int8", 8, "N"),
          (21, "int2", 2, "N"), (23, "int4", 4, "N"), (25, "text", -1, "S"), (26, "oid", 4, "N"), (700, "float4", 4, "N"),
          (701, "float8", 8, "N"), (1042, "bpchar", -1, "S"), (1043, "varchar", -1, "S"), (1082, "date", 4, "D"),
          (1083, "time", 8, "D"), (1114, "timestamp", 8, "D"), (1184, "timestamptz", 8, "D"), (1186, "interval", 16, "T"),
          (1700, "numeric", -1, "N"), (2950, "uuid", 16, "U")]
_PG_TYPE = ("SELECT *, " + _PG + " AS typnamespace, 10 AS typowner, typlen <> -1 AS typbyval, 'b' AS typtype, false AS"
            " typispreferred, true AS typisdefined, ',' AS typdelim, 0 AS typrelid, 0 AS typelem, 0 AS typarray, 2400 AS"
            " typreceive, 0 AS typbasetype, -1 AS typtypmod, false AS typnotnull, 0 AS typndims, 0 AS typcollation,"
            " NULL AS typdefault, NULL AS typinput, NULL AS typoutput, NULL AS typsend, 'i' AS typalign, 'p' AS typstorage"
            " FROM (VALUES " + ", ".join(f"({o}, '{n}', {s}, '{c}')" for o, n, s, c in _TYPES) + ") t(oid, typname, typlen,"
            " typcategory)")
EMULATED = {k: "SELECT * FROM (VALUES ({})) t({}) WHERE false".format(*(", ".join(x) for x in zip(*(
    (f"CAST(NULL AS {c[1]})" if len(c) > 1 else "NULL", c[0]) for c in (s.split(" ", 1) for s in v.split(", "))))))
    for k, v in _EMPTY.items()} | {
    "pg_namespace": _NS, "pg_type": _PG_TYPE,
    "pg_class": f"SELECT *, false AS relforcerowsecurity FROM system.pg_catalog.pg_class WHERE relnamespace IN (SELECT oid FROM ({_NS}))",
    "pg_proc": "SELECT * FROM system.pg_catalog.pg_proc UNION ALL BY NAME SELECT 2400 AS oid, 'base_recv' AS proname",
    "pg_settings": "SELECT *, setting AS reset_val, setting AS boot_val, NULL AS unit, '' AS category, '' AS short_desc,"
                   " 'user' AS context, 'string' AS vartype, 'default' AS source FROM (VALUES "
                   + ", ".join(f"('{k}', '{v}')" for k, v in SHOWN.items()) + ") t(name, setting)"}


@cache
def _relations() -> frozenset[tuple[str, str]]:
    """DuckDB's Postgres-compatible catalog views: (schema, name), schema "" for pg_catalog (searched by default)."""
    with duckdb.connect(config=SANDBOX) as d:
        return frozenset(("" if s == "pg_catalog" else s, v) for s, v in d.execute(
            "SELECT schema_name, view_name FROM duckdb_views() WHERE schema_name IN ('pg_catalog', 'information_schema')"
        ).fetchall())


def _catalog(user: User, project: str, sql: str, cols: list[tuple], describe: bool) -> dict:
    """E-04.7: answer a query that reads no model table (BI-tool introspection: information_schema, pg_catalog,
    version()) in a throwaway in-memory DuckDB that holds only empty copies of the project's model tables (names and
    column types, no rows), with file access off and its settings locked. The lake is never attached, so DuckDB's real
    catalog (raw, edits, query copies, other projects) can't be listed. The SQL may read only catalog views and call
    only D-44 + CATALOG_CALLS functions, like any other SQL here (D-44)."""
    for pattern, to in PG_ONLY:
        sql = re.sub(pattern, to, sql, flags=re.IGNORECASE)
    try:
        [tree] = sqlglot.parse(sql, read="duckdb")
        if not isinstance(tree, exp.Query):
            raise ValueError("not a SELECT")  # noqa: TRY004 refused below
        ctes = {id(t) for sc in traverse_scope(tree) for t in sc.tables if t.name in sc.cte_sources}
        if bad := [t.sql("duckdb") for t in tree.find_all(exp.Table) if id(t) not in ctes and (t.catalog or (
                t.db.lower(), t.name.lower()) not in _relations() and (t.db or t.name.lower() not in EMULATED)) and not (
                isinstance(t.this, exp.Func) and rls._name(t.this) in ("generate_series", "unnest", "explode"))]:
            raise ValueError(f"reads {', '.join(bad)}")
        aliases = {a.name.lower() for a in tree.find_all(exp.TableAlias) if a.columns}  # `AS s(r)` reads as a call
        if bad := rls.refused_calls(sql, tree) - CATALOG_CALLS - (aliases - {rls._name(f) for f in tree.find_all(exp.Func)}):
            raise ValueError(f"calls {', '.join(sorted(bad))}")
    except Exception as e:  # noqa: BLE001 anything we can't prove safe is refused
        raise ActionFailed(403, f"Only one SELECT over the model tables or the catalog is allowed ({e}).") from None

    def emulate(node):  # only after the check: the stand-ins read system.pg_catalog
        if isinstance(node, exp.Table) and id(node) not in ctes and not node.db and node.name.lower() in EMULATED:
            return sqlglot.parse_one(EMULATED[node.name.lower()], read="duckdb").subquery(node.alias_or_name)
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Func) and node.alias and not node.args["alias"].columns:
            node.args["alias"].set("columns", [exp.to_identifier(node.alias)])  # Postgres: `generate_series(..) s` names the value s
        if isinstance(node, exp.Cast) and node.to.sql().lower() == "regclass" and node.this.is_string:  # 'pg_class'::regclass
            if node.this.name.isdigit():  # psql: '16384'::regclass is that OID
                return exp.cast(node.this, "BIGINT")
            return sqlglot.parse_one(f"(SELECT oid FROM system.pg_catalog.pg_class WHERE relname = "
                                     f"'{_lit(node.this.name)}' LIMIT 1)", read="duckdb")
        return node

    sql = tree.transform(emulate).sql("duckdb")
    with duckdb.connect(config=SANDBOX) as d:
        d.execute(f"ATTACH ':memory:' AS {_q(project)}; USE {_q(project)}; DETACH memory; CREATE SCHEMA {SCHEMA};"
                  f" SET search_path = '{SCHEMA},main'; {SETUP}; CREATE MACRO main.pg_get_userbyid(x) AS '{_lit(user.name)}'")
        for t, group in itertools.groupby(cols, lambda c: c[0]):  # ENUM labels are data: their type stays hidden
            d.execute(f"CREATE TABLE {SCHEMA}.{_q(t)} ({', '.join(_q(c) + ' ' + ('VARCHAR' if 'ENUM(' in typ else typ) for _, c, typ in group)})")
        d.execute("SET enable_external_access = false; SET lock_configuration = true")
        try:
            with limited(d, TIMEOUT):
                cur = d.execute(sql)
                rows = [] if describe else cur.fetchmany(CAP + 1)
        except duckdb.InterruptException:
            raise ActionFailed(400, f"The query ran longer than the {TIMEOUT} s limit and was stopped.") from None
        except duckdb.Error as e:
            raise ActionFailed(400, f"The query failed: {e}") from None
        if len(rows) > CAP:
            raise ActionFailed(422, f"More than {CAP:,} rows.")
        return {"columns": [c[0] for c in cur.description], "types": [str(c[1]) for c in cur.description], "rows": rows}


@action(Role.viewer)
def sql_query(conn, user: User, args: SqlInput) -> dict:
    """Runs one read-only SELECT over the project's model tables for SQL clients (the SE-10 endpoint): unqualified table names, a fixed list of functions, row-level security applied, at most 100,000 rows. To answer data questions use semantic_query (measures)."""
    from abi.semantic import model  # not at top: model imports the semantic package

    con = storage.lake(args.project)
    # M4 review: hidden (staging) tables are out of the model here too, as in semantic_query: bronze and silver have
    # no rule of their own (rules go on gold), so a restricted Viewer would read their rows unfiltered
    hide = {t["table"] for t in model._read(args.project, "model.json").get("tables", []) if t.get("hidden")}
    cols = [c for c in detect._columns(con.cursor(), "model") if c[0] not in hide]
    known = {t for t, _, _ in cols}
    sql = _bind(args.sql, args.params, args.types, args.project, {k.lower() for k in known})
    if not any(t in {k.lower() for k in known} and s not in CATALOGS for s, t in _parsed(con.cursor(), sql)[1]):
        return _catalog(user, args.project, sql, cols, args.describe)  # names no model table: introspection
    sql = f"SELECT * FROM ({sql}\n) AS __sql LIMIT {CAP + 1}"  # \n ends a trailing -- comment
    rls.check(args.project, user.id, sql, known)  # D-44 for every caller: this exact text is what runs
    if not _parsed(con.cursor(), sql)[0]:
        raise ActionFailed(403, "Only one SELECT over the model tables is allowed.")
    asked = {"sql": args.sql} | ({"params": args.params} if args.params else {})
    try:
        cols = execute(args.project, f"DESCRIBE {sql}", asked, user.id)
        rows = [] if args.describe else execute(args.project, sql, asked, user.id, timeout=TIMEOUT)
    except duckdb.InterruptException:
        raise ActionFailed(400, f"The query ran longer than the {TIMEOUT} s limit and was stopped.") from None
    except duckdb.Error as e:
        raise ActionFailed(400, f"The query failed: {e}") from None
    if len(rows) > CAP:
        raise ActionFailed(422, f"More than {CAP:,} rows; add a LIMIT or use the Arrow/Parquet export (SE-9).")
    return {"columns": [c[0] for c in cols], "types": [c[1] for c in cols], "rows": rows}


# Postgres type OIDs for DuckDB result types; anything else is sent as text
OID = {"BOOLEAN": 16, "TINYINT": 21, "SMALLINT": 21, "UTINYINT": 21, "INTEGER": 23, "USMALLINT": 23, "BIGINT": 20,
       "UINTEGER": 20, "HUGEINT": 1700, "UBIGINT": 1700, "FLOAT": 700, "DOUBLE": 701, "DATE": 1082, "TIME": 1083,
       "TIMESTAMP": 1114, "TIMESTAMP WITH TIME ZONE": 1184, "BLOB": 17, "UUID": 2950}
SERVER: asyncio.Server | None = None


def _msg(t: bytes, body: bytes = b"") -> bytes:
    return t + struct.pack("!i", len(body) + 4) + body


def _error(code: str, text: str) -> bytes:
    return _msg(b"E", b"".join(k + v.encode() + b"\0" for k, v in
                                [(b"S", "ERROR"), (b"V", "ERROR"), (b"C", code), (b"M", text)]) + b"\0")


def _text(v) -> bytes | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return b"t" if v else b"f"
    if isinstance(v, bytes):
        return b"\\x" + v.hex().encode()
    return str(v).encode()


def _oids(res: dict) -> list[int]:
    return [1700 if t.startswith("DECIMAL") else OID.get(t, 25) for t in res["types"]]


def _formats(codes: tuple, n: int) -> list[int]:
    """Bind's format codes: none = all text, one = for every column (or parameter), else one each."""
    return list(codes) * n if len(codes) == 1 else list(codes) or [0] * n


def _columns(res: dict, formats: list[int] | None = None) -> bytes:
    """RowDescription, or NoData for a statement without a result (BEGIN, SET)."""
    if not res["columns"]:
        return _msg(b"n")
    formats = formats or [0] * len(res["columns"])
    cols = b"".join(c.encode() + b"\0" + struct.pack("!ihihih", 0, 0, o, -1, -1, f)
                    for c, o, f in zip(res["columns"], _oids(res), formats))
    return _msg(b"T", struct.pack("!h", len(res["columns"])) + cols)


Y2K = datetime.datetime(2000, 1, 1)  # noqa: DTZ001 Postgres' binary dates and times count from here (TIMESTAMP is naive)


def _binary(oid: int, v) -> bytes:
    """A value in Postgres' binary format (Npgsql asks for it, pgjdbc for prepared statements)."""
    if oid in (16, 21, 23, 20, 700, 701):
        return struct.pack({16: "!?", 21: "!h", 23: "!i", 20: "!q", 700: "!f", 701: "!d"}[oid], v)
    if oid == 1700:
        return _numeric(Decimal(v))
    if oid == 1082:
        return struct.pack("!i", (v - Y2K.date()).days)
    if oid in (1083, 1114, 1184):
        t = datetime.datetime.combine(Y2K.date(), v) if oid == 1083 else v
        base = Y2K.replace(tzinfo=datetime.UTC) if oid == 1184 else Y2K
        return struct.pack("!q", (t - base) // datetime.timedelta(microseconds=1))
    if oid == 17:
        return bytes(v)
    if oid == 2950:
        return uuid.UUID(str(v)).bytes
    return _text(v)  # text and anything sent as text


def _numeric(d: Decimal) -> bytes:
    """Postgres binary numeric: base-10000 digits, weight of the first, sign, display scale."""
    if not d.is_finite():
        return struct.pack("!hhHh", 0, 0, 0xC000, 0)
    sign, digits, e = d.as_tuple()
    scale = max(-e, 0)
    s = ("".join(map(str, digits)) + "0" * max(e, 0)).rjust(scale + 1, "0")
    whole, frac = s[:len(s) - scale].lstrip("0"), s[len(s) - scale:]
    whole, frac = whole.rjust(-(-len(whole) // 4) * 4, "0"), frac.ljust(-(-len(frac) // 4) * 4, "0")
    groups, weight = [int((whole + frac)[i:i + 4]) for i in range(0, len(whole + frac), 4)], len(whole) // 4 - 1
    while groups and groups[0] == 0:
        groups.pop(0)
        weight -= 1
    while groups and groups[-1] == 0:
        groups.pop()
    return struct.pack(f"!hhHh{len(groups)}H", len(groups), weight if groups else 0, 0x4000 if sign else 0, scale, *groups)


def _from_binary(oid: int, b: bytes) -> str:
    """A binary parameter as the text it stands for (then bound like a text parameter)."""
    if oid in (16, 21, 23, 20, 26, 700, 701):
        return str(struct.unpack({16: "!?", 21: "!h", 23: "!i", 20: "!q", 26: "!I", 700: "!f", 701: "!d"}[oid], b)[0]).lower()
    if oid == 1700:
        n, weight, sign, scale = struct.unpack_from("!hhHh", b)
        if sign == 0xC000:
            return "NaN"
        with localcontext(prec=1000):  # exact for any numeric(1000)
            v = sum(Decimal(g).scaleb(4 * (weight - i)) for i, g in enumerate(struct.unpack_from(f"!{n}H", b, 8)))
        return f"{'-' if sign == 0x4000 else ''}{v:.{scale}f}"
    if oid == 1082:
        return (Y2K.date() + datetime.timedelta(days=struct.unpack("!i", b)[0])).isoformat()
    if oid in (1083, 1114, 1184):
        t = Y2K + datetime.timedelta(microseconds=struct.unpack("!q", b)[0])
        return t.time().isoformat() if oid == 1083 else t.isoformat(" ") + ("+00" if oid == 1184 else "")
    if oid == 2950:
        return str(uuid.UUID(bytes=b))
    if oid in (0, 18, 19, 25, 1042, 1043):
        return b.decode()
    raise PgError("0A000", f"binary parameters of type {oid} are not supported; send them as text")


def _rows(rows: list, oids: list[int] | None = None, formats: list[int] | None = None) -> bytes:
    out = []
    for row in rows:
        cells = [None if v is None else _binary(o, v) if f else _text(v)
                 for v, o, f in zip(row, oids or [0] * len(row), formats or [0] * len(row))]
        out.append(_msg(b"D", struct.pack("!h", len(cells)) + b"".join(
            struct.pack("!i", -1) if c is None else struct.pack("!i", len(c)) + c for c in cells)))
    return b"".join(out)


def _done(res: dict) -> bytes:
    return _msg(b"I") if res.get("empty") else _msg(b"C", (res.get("tag") or f"SELECT {len(res['rows'])}").encode() + b"\0")


class PgError(Exception):
    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code


def _cstr(body: bytes, i: int) -> tuple[str, int]:
    j = body.index(b"\0", i)
    return body[i:j].decode(), j + 1


def _nparams(sql: str, oids: list[int]) -> int:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return len(oids)
    return max([len(oids)] + [int(p.this) for p in tree.find_all(exp.Placeholder) if str(p.this).isdigit()])


def _loopback(host: str) -> bool:
    ip = ipaddress.ip_address(host.split("%")[0])
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


# SC-10/D-52: the password alone signs in only accounts with no second factor (no TOTP, tenant doesn't require it)
ACCOUNT = ("SELECT u.id, u.name, u.workspace_owner, u.password_hash,"
           " u.totp_secret IS NULL AND NOT COALESCE(t.mfa_required, false) AS password_only"
           " FROM users u LEFT JOIN tenants t ON t.id = u.tenant WHERE u.{} = %s")
MFA = "mfa"  # a right password of an account with two-step sign-in: refused, not counted as a failure


def _current(conn, login: tuple) -> User | None:
    """The account behind a login credential now (H-01.1-fix-2): its session still open, or its password unchanged
    and still enough on its own. None once it was revoked."""
    kind, v = login
    if kind == "token":
        return security.user_for_token(conn, v)
    if kind == "personal":  # M4.5: a personal token, until it is revoked or expires
        return security.user_for_personal(conn, v)
    row = conn.execute(ACCOUNT.format("id"), [v[0]]).fetchone()
    ok = row and row["password_only"] and row["password_hash"] == v[1]
    return User(row["id"], row["name"], row["workspace_owner"]) if ok else None


def _login(name: str, password: str, project: str, ip: str) -> tuple | str:
    """The account password, a personal token or a session token of `name` (SC-10 throttle shared with /api/login: security.attempt);
    the user needs a role in `project`. Returns the login credential (for `_current`) or the refusal text (the same
    for every bad credential). An account with two-step sign-in uses a session token: the app sign-in asked the code."""
    def check(conn):
        if (user := security.user_for_personal(conn, password)) and user.name == name:
            return "personal", password
        if (user := security.user_for_token(conn, password)) and user.name == name:
            return "token", password
        row = conn.execute(ACCOUNT.format("name"), [name]).fetchone()
        if not security.verify_password(password, row["password_hash"] if row else security._dummy_hash()) or not row:
            return None
        return ("password", (row["id"], row["password_hash"])) if row["password_only"] else MFA

    try:
        login = security.attempt(name, ip, check)
    except security.Throttled:
        actions.audit_login(name, "sql", 429)
        return "too many failed logins; try again in a few minutes"
    with db.connect() as conn:
        if login in (None, MFA) or not (user := _current(conn, login)):
            actions.audit_login(name, "sql", 401, MFA if login == MFA else None)
            return (f'password authentication failed for user "{name}"; use a personal token from the app as the'
                    " password (your name, then Personal tokens)")
        if security.role_in(conn, user, project) is None:
            actions.audit_login(name, "sql", 403, "no role")
            return f'user "{name}" has no role in project "{project}"'
    actions.audit_login(name, "sql", 200, login[0])
    return login


def _tls() -> ssl.SSLContext | None:
    if not (cert := os.environ.get("ABI_SQL_TLS_CERT")):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, os.environ["ABI_SQL_TLS_KEY"])
    return ctx


async def _read(r: asyncio.StreamReader) -> tuple[bytes, bytes]:
    head = await r.readexactly(5)
    n = struct.unpack("!i", head[1:])[0]
    if not 4 <= n <= 1_000_000:
        raise ConnectionError("bad message length")
    return head[:1], await r.readexactly(n - 4)


LOGIN_TIMEOUT = 30  # D-48: seconds a connection may take to log in
IDLE = 30 * 86400  # D-48: a logged-in connection idle this long is closed (the session lifetime, user_for_token)


async def _startup(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> tuple[tuple, str] | None:
    tls = False
    while True:  # SSLRequest / GSSENCRequest, then the StartupMessage
        n, code = struct.unpack("!ii", await r.readexactly(8))
        if not 8 <= n <= 10_000:
            return
        body = await r.readexactly(n - 8)
        if code == 80877103 and not tls and (ctx := _tls()):  # SSLRequest
            w.write(b"S")
            await w.drain()
            await w.start_tls(ctx)
            tls = True
        elif code in (80877103, 80877104):  # no TLS configured (or already on), no GSSAPI
            w.write(b"N")
        else:
            break
    if code != 196608:  # protocol 3.0
        w.write(_error("08P01", "unsupported frontend protocol"))
        return
    p = body.split(b"\0")
    params = {k.decode(): v.decode() for k, v in zip(p[0::2], p[1::2], strict=False) if k}
    name, project = params.get("user", ""), params.get("database", "")
    if not tls and not _plain(w.get_extra_info("peername")[0]):  # before the client sends a password
        w.write(_error("28000", "TLS is required from this address: connect with sslmode=require"))
        return
    w.write(_msg(b"R", struct.pack("!i", 3)))  # AuthenticationCleartextPassword
    await w.drain()
    t, body = await _read(r)
    if t != b"p":
        return
    login = await asyncio.to_thread(_login, name, body.rstrip(b"\0").decode(errors="replace"), project,
                                    w.get_extra_info("peername")[0])
    if isinstance(login, str):
        w.write(_error("28P01", login))
        return
    w.write(_msg(b"R", struct.pack("!i", 0)) + b"".join(
        _msg(b"S", f"{k}\0{v}\0".encode()) for k, v in PARAMS.items()) + _msg(b"Z", b"I"))
    return login, project


TX = {"BEGIN": "BEGIN", "START": "START TRANSACTION", "COMMIT": "COMMIT", "END": "COMMIT", "ROLLBACK": "ROLLBACK",
      "ABORT": "ROLLBACK"}


def _command(sql: str) -> dict | None:
    """Session statements clients send around their queries. The endpoint is read-only and has no session state, so
    transactions, SET, RESET and DISCARD run nothing (whatever follows them in the text is not run either)."""
    words = sql.split(None, 1)
    first = words[0].upper()
    if first in TX or first in ("SET", "RESET", "DISCARD", "DEALLOCATE", "SAVEPOINT", "RELEASE", "UNLISTEN"):
        return {"columns": [], "types": [], "rows": [], "tag": TX.get(first, first)}
    if first == "SHOW":
        name = " ".join(words[1:]).strip().strip('"').lower()
        name = "transaction_isolation" if " ".join(name.split()) == "transaction isolation level" else name
        if name not in SHOWN:
            raise PgError("42704", f'unrecognized configuration parameter "{name}"')
        return {"columns": [name], "types": ["VARCHAR"], "rows": [(SHOWN[name],)], "tag": "SHOW"}
    return None


def _err(e: Exception) -> bytes:
    if isinstance(e, PgError):
        return _error(e.code, str(e))
    if isinstance(e, ActionFailed):  # audited by actions.run
        return _error({403: "42501", 422: "54000"}.get(e.status, "42000"), str(e))
    return _error("XX000", "internal error")  # a crash shows no internals


class Revoked(PgError):
    """The login credential no longer holds (H-01.1-fix-2): the connection ends."""


def _now(login: tuple) -> User:
    with db.connect() as conn:
        if user := _current(conn, login):
            return user
    raise Revoked("28000", "your sign-in was revoked (signed out, password changed or account removed): connect again")


async def _serve(r: asyncio.StreamReader, w: asyncio.StreamWriter, login: tuple, project: str) -> None:
    """Simple and extended query protocol. Every statement is `actions.run("sql_query", via="sql")` as the account
    reloaded from its login credential (`_now`); the extended protocol only carries its text, parameter values (text
    format) and how many rows the client fetches at a time."""
    stmts: dict[str, tuple[str, list[int]]] = {}
    portals: dict[str, dict] = {}
    s = {"tx": False, "failed": False}  # in a transaction block; an extended-protocol error skips to Sync

    async def run(sql: str, params=(), oids=(), describe=False) -> dict:
        if not (sql := sql.strip().rstrip(";").strip()):
            return {"empty": True, "columns": [], "types": [], "rows": []}
        if res := _command(sql):
            s["tx"] = {"BEGIN": True, "START TRANSACTION": True, "COMMIT": False, "ROLLBACK": False}.get(res["tag"], s["tx"])
            return res
        user = await asyncio.to_thread(_now, login)
        return await asyncio.to_thread(actions.run, "sql_query", {
            "project": project, "sql": sql, "params": list(params), "types": list(oids), "describe": describe}, user, "sql")

    async def result(p: dict) -> dict:
        if p["res"] is None:
            p["res"] = await run(p["sql"], p["params"], p["oids"])
        return p["res"]

    while True:
        await w.drain()
        async with asyncio.timeout(IDLE):
            t, body = await _read(r)
        if t == b"X":
            return
        if t == b"S":
            s["failed"] = False
            if not s["tx"]:  # portals end with their transaction
                portals.clear()
            w.write(_msg(b"Z", b"T" if s["tx"] else b"I"))
            continue
        if t == b"H" or (s["failed"] and t != b"Q"):
            continue
        try:
            if t == b"Q":
                stmts.pop("", None)
                portals.pop("", None)
                res = await run(body.rstrip(b"\0").decode(errors="replace"))
                out = (_columns(res) if res["columns"] else b"") + _rows(res["rows"]) + _done(res)
            else:
                out = await _extended(t, body, stmts, portals, run, result)
        except Revoked as e:
            w.write(_error(e.code, str(e)))
            return
        except Exception as e:  # noqa: BLE001 reported to the client, see _err
            out, s["failed"] = _err(e), t != b"Q"
        w.write(out + (_msg(b"Z", b"T" if s["tx"] else b"I") if t == b"Q" else b""))


async def _extended(t: bytes, body: bytes, stmts: dict, portals: dict, run, result) -> bytes:
    """One Parse/Bind/Describe/Execute/Close message (protocol v3, 'Extended Query')."""
    if t == b"P":
        name, i = _cstr(body, 0)
        sql, i = _cstr(body, i)
        n = struct.unpack_from("!h", body, i)[0]
        if name not in stmts and len(stmts) >= 200:
            raise PgError("53400", "too many prepared statements; close some")
        stmts[name] = (sql, list(struct.unpack_from(f"!{n}i", body, i + 2)))
        return _msg(b"1")
    if t in (b"D", b"C"):
        kind, (name, _) = body[:1], _cstr(body, 1)
        if t == b"C":
            (stmts if kind == b"S" else portals).pop(name, None)
            return _msg(b"3")
        if kind == b"P":
            if name not in portals:
                raise PgError("34000", f'portal "{name}" does not exist')
            res = await result(p := portals[name])
            return _columns(res, _formats(p["formats"], len(res["columns"])))
        if name not in stmts:
            raise PgError("26000", f'prepared statement "{name}" does not exist')
        sql, oids = stmts[name]
        n = _nparams(sql, oids)
        types = [oids[k] if k < len(oids) and oids[k] else 25 for k in range(n)]  # unknown: sent as text
        return _msg(b"t", struct.pack(f"!h{n}i", n, *types)) + _columns(await run(sql, [None] * n, oids, True))
    if t == b"B":
        portal, i = _cstr(body, 0)
        name, i = _cstr(body, i)
        if name not in stmts:
            raise PgError("26000", f'prepared statement "{name}" does not exist')
        sql, oids = stmts[name]
        n = struct.unpack_from("!h", body, i)[0]
        codes, i = struct.unpack_from(f"!{n}h", body, i + 2), i + 2 + 2 * n
        n, i, values = struct.unpack_from("!h", body, i)[0], i + 2, []
        for k, f in enumerate(_formats(codes, n)):
            size = struct.unpack_from("!i", body, i)[0]
            v = None if size < 0 else body[i + 4:i + 4 + size]
            values.append(v if v is None else _from_binary(oids[k] if k < len(oids) else 0, v) if f else v.decode())
            i += 4 + max(size, 0)
        n = struct.unpack_from("!h", body, i)[0]
        if portal not in portals and len(portals) >= 16:
            raise PgError("53400", "too many open portals; close some")
        portals[portal] = {"sql": sql, "params": values, "oids": oids, "res": None, "sent": 0,
                           "formats": struct.unpack_from(f"!{n}h", body, i + 2)}
        return _msg(b"2")
    if t == b"E":
        name, i = _cstr(body, 0)
        if name not in portals:
            raise PgError("34000", f'portal "{name}" does not exist')
        p, limit = portals[name], struct.unpack_from("!i", body, i)[0]
        rows = (res := await result(p))["rows"][p["sent"]:]
        oids, formats = _oids(res), _formats(p["formats"], len(res["columns"]))
        if 0 < limit < len(rows):  # the client fetches in batches (JDBC fetchSize): PortalSuspended
            p["sent"] += limit
            return _rows(rows[:limit], oids, formats) + _msg(b"s")
        p["sent"] += len(rows)
        return _rows(rows, oids, formats) + _done(res | {"rows": rows})
    raise PgError("08P01", f"unexpected message type {t.decode(errors='replace')}")


async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        async with asyncio.timeout(LOGIN_TIMEOUT):
            login = await _startup(r, w)
        if login:
            await _serve(r, w, *login)
        await w.drain()
    except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, ssl.SSLError, OSError, UnicodeDecodeError):
        pass  # the client went away or spoke garbage: drop it
    finally:
        w.close()


def _local() -> bool:
    """M4.5: ABI_SQL_BIND=127.0.0.1, so only this machine reaches the port (in Docker: compose publishes it there, and
    its clients arrive from the bridge's gateway), and a plain connection is allowed (`_plain`)."""
    return os.environ.get("ABI_SQL_BIND") == "127.0.0.1"


def _gateway() -> str | None:
    """The default route's gateway (Linux): in Docker, the address this machine's clients arrive from."""
    try:
        with open("/proc/net/route") as f:
            for line in f.read().splitlines()[1:]:
                x = line.split()
                if x[1] == "00000000" and int(x[3], 16) & 2:  # destination 0.0.0.0, RTF_GATEWAY
                    return str(ipaddress.IPv4Address(struct.pack("<I", int(x[2], 16))))
    except (OSError, IndexError, ValueError):
        pass
    return None


def _plain(host: str) -> bool:
    """A connection without TLS: from loopback; with `_local`, also from the gateway (M4 review: not from another
    container on the compose network, which reaches the listener at 0.0.0.0 inside the container)."""
    if _loopback(host):
        return True
    ip = ipaddress.ip_address(host.split("%")[0])
    return _local() and str(getattr(ip, "ipv4_mapped", None) or ip) == _gateway()


async def start() -> None:
    """App lifespan: listen on ABI_SQL_PORT when it is set, at ABI_SQL_BIND (default every interface; ABI_SQL_LISTEN,
    which compose sets inside the container, wins). M4.5: never without TLS (ABI_SQL_TLS_CERT/KEY) unless ABI_SQL_BIND
    is 127.0.0.1: the port stays closed and the log says why; the engine serves on."""
    global SERVER
    if not (port := os.environ.get("ABI_SQL_PORT")):
        return
    try:
        tls = _tls()
    except (OSError, ssl.SSLError, KeyError) as e:  # a missing or unreadable file, no key named: say which kind
        tls, why = None, f" (its certificate didn't load: {type(e).__name__})"
    else:
        why = ""
    if not tls and not _local():
        logging.getLogger("abi.sql").error(
            "The SQL endpoint (ABI_SQL_PORT) is not started: it needs TLS (ABI_SQL_TLS_CERT and ABI_SQL_TLS_KEY) unless"
            f" ABI_SQL_BIND=127.0.0.1 keeps it on this machine{why}.")
        return
    host = os.environ.get("ABI_SQL_LISTEN") or os.environ.get("ABI_SQL_BIND") or None
    SERVER = await asyncio.start_server(_handle, host, int(port))


async def stop() -> None:
    global SERVER
    if SERVER:
        SERVER.close()
        SERVER = None
