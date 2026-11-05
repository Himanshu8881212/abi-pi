"""Ordered JSON steps compiled to one SQL query via SQLGlot. PRD §6.4 TR-1, TR-2.

`compile(table, steps)` turns a table's step list (tables/<t>.json `steps`, schema in
schemas/v1/table.json) into ONE DuckDB query: a CTE per step, each reading the previous one, so
DuckDB runs the whole pipeline in one pass (`storage.refresh(..., model_sql=compile(t, steps))`).
`source` reads lake.raw.<t>, where loading the step's source puts the data (ST-1), or with `table` (M4.2: silver
feeds gold) lake.model.<that table>, which the project builds first, so no source is read again; `merge` and
`append` read other tables' lake.model.<t>, never their effective tables (W-01): edits are the last layer over the
table they were made on (§11 #9), and nothing refreshes this table when that one is edited. Row expressions (filter, add_column) and the `sql`
step are DuckDB SQL (V-02 C-23), parsed and checked by SQLGlot: they may only see the previous
step's rows, never files, other tables or settings (trust boundary: steps come from users).
"""

import contextlib
import json
from typing import Annotated, Literal

import duckdb
import sqlglot
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_serializer, model_validator
from sqlglot import exp

from abi.project import git
from abi.storage import _q

Name = Annotated[str, Field(min_length=1)]
Scalar = str | int | float | bool
Agg = Literal["sum", "avg", "min", "max", "count", "median", "count_distinct"]
DENY = {"current_setting", "getenv", "getvariable"}  # scalar functions that read server/session state (duckdb 1.5)
INPUT = "input"  # what a `sql` step calls the previous step


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Step):
    type: Literal["source"]
    source: Name | None = None  # sources/<id>.json; its load lands in raw.<this table>
    table: Name | None = None  # M4.2: or a table the project builds: its model.<table>, no raw copy, no source read

    @model_validator(mode="after")
    def _one(self):
        if (self.source is None) == (self.table is None):
            raise ValueError("A `source` step names either a `source` (sources/<id>.json) or a `table` the project builds.")
        return self

    @model_serializer(mode="wrap")
    def _dump(self, handler):  # the one it names only: a 1.1 step's file and fingerprint stay byte for byte
        return {k: v for k, v in handler(self).items() if v is not None}

    @property
    def reads(self) -> str:
        return self.source or self.table


class Select(_Step):
    type: Literal["select"]
    columns: list[Name] = Field(min_length=1)


class Rename(_Step):
    type: Literal["rename"]
    columns: dict[Name, Name] = Field(min_length=1)  # old -> new


class SetType(_Step):
    type: Literal["set_type"]
    columns: dict[Name, Name] = Field(min_length=1)  # column -> DuckDB type; CAST, so a bad value fails the refresh (ST-2)


class SortKey(_Step):
    column: Name
    descending: bool = False


class Sort(_Step):
    type: Literal["sort"]
    by: list[SortKey] = Field(min_length=1)


class Deduplicate(_Step):
    type: Literal["deduplicate"]
    columns: list[Name] = []  # empty: whole rows


class Filter(_Step):
    type: Literal["filter"]
    condition: Name  # DuckDB SQL expression, rows where it is true are kept


class FillNulls(_Step):
    type: Literal["fill_nulls"]
    columns: dict[Name, Scalar] = Field(min_length=1)


class AddColumn(_Step):
    type: Literal["add_column"]
    name: Name
    expression: Name


class SplitColumn(_Step):
    type: Literal["split_column"]
    column: Name  # replaced by the `into` columns
    delimiter: Name
    into: list[Name] = Field(min_length=1)


class Merge(_Step):
    type: Literal["merge"]
    table: Name  # model.<table>
    on: dict[Name, Name] = Field(min_length=1)  # this column -> that column
    columns: list[Name] = Field(min_length=1)  # taken from `table`
    how: Literal["inner", "left", "right", "full"] = "left"


class Append(_Step):
    type: Literal["append"]
    tables: list[Name] = Field(min_length=1)  # model.<table>, matched by column name


class Aggregation(_Step):
    name: Name
    function: Agg
    column: Name | None = None  # None only for count: count(*)


class Group(_Step):
    type: Literal["group"]
    by: list[Name] = []
    aggregations: list[Aggregation] = Field(min_length=1)


class Pivot(_Step):
    type: Literal["pivot"]
    column: Name  # its values become columns
    values: Name  # aggregated into them
    function: Agg
    into: list[Scalar] = Field(min_length=1)  # explicit, so the output columns are known without reading data


class Unpivot(_Step):
    type: Literal["unpivot"]
    columns: list[Name] = Field(min_length=1)
    name: Name
    value: Name


class Sql(_Step):
    type: Literal["sql"]
    query: Name  # one SELECT over `input`


Step = Annotated[Source | Select | Rename | SetType | Sort | Deduplicate | Filter | FillNulls | AddColumn
                 | SplitColumn | Merge | Append | Group | Pivot | Unpivot | Sql, Field(discriminator="type")]
Steps = TypeAdapter(list[Step])


def compile(table: str, steps: list, source_sql: str | None = None) -> str:
    """TR-2: the table's steps as one DuckDB query, a CTE per step. Raises ValueError on invalid steps.

    `source_sql` replaces what the `source` step reads (default: bare lake.raw.<table>); ingest passes the
    IN-5 typed raw (`detect.typed_sql`)."""
    steps = validate(steps)
    query = exp.select("*").from_(f"step{len(steps)}")
    for i, step in enumerate(steps):
        query = query.with_(f"step{i + 1}", as_=sqlglot.parse_one(_step_sql(step, f"step{i}", table, source_sql), read="duckdb"))
    return query.sql("duckdb")


def _step_sql(s, prev: str, table: str, source_sql: str | None) -> str:
    q = _q
    frm = f"FROM {prev}"
    match s.type:
        case "source":
            return source_sql or f"SELECT * FROM lake.{'model' if s.table else 'raw'}.{q(s.table or table)}"
        case "select":
            return f"SELECT {', '.join(map(q, s.columns))} {frm}"
        case "rename":
            return f"SELECT * RENAME ({', '.join(f'{q(a)} AS {q(b)}' for a, b in s.columns.items())}) {frm}"
        case "set_type":
            return f"SELECT * REPLACE ({', '.join(f'CAST({q(c)} AS {_type(t)}) AS {q(c)}' for c, t in s.columns.items())}) {frm}"
        case "sort":
            return f"SELECT * {frm} ORDER BY " + ", ".join(q(k.column) + (" DESC" if k.descending else "") for k in s.by)
        case "deduplicate":
            if not s.columns:
                return f"SELECT DISTINCT * {frm}"
            # ponytail: keeps an arbitrary row per key (no row order in SQL); add a keep-first-by-sort option if users need it
            return f"SELECT * {frm} QUALIFY row_number() OVER (PARTITION BY {', '.join(map(q, s.columns))}) = 1"
        case "filter":
            return f"SELECT * {frm} WHERE {_expr(s.condition)}"
        case "fill_nulls":
            fills = ", ".join(f"coalesce({q(c)}, {exp.convert(v).sql('duckdb')}) AS {q(c)}" for c, v in s.columns.items())
            return f"SELECT * REPLACE ({fills}) {frm}"
        case "add_column":
            return f"SELECT *, {_expr(s.expression)} AS {q(s.name)} {frm}"
        case "split_column":
            parts = ", ".join(f"split_part({q(s.column)}, {exp.convert(s.delimiter).sql('duckdb')}, {n}) AS {q(c)}"
                              for n, c in enumerate(s.into, 1))
            return f"SELECT * EXCLUDE ({q(s.column)}), {parts} {frm}"
        case "merge":
            on = " AND ".join(f"l.{q(a)} = r.{q(b)}" for a, b in s.on.items())
            cols = ", ".join(f"r.{q(c)}" for c in s.columns)
            return f"SELECT l.*, {cols} FROM {prev} AS l {s.how.upper()} JOIN lake.model.{q(s.table)} AS r ON {on}"
        case "append":
            return f"SELECT * {frm}" + "".join(f" UNION ALL BY NAME SELECT * FROM lake.model.{q(t)}" for t in s.tables)
        case "group":
            aggs = [_agg(a.function, a.column) + f" AS {q(a.name)}" for a in s.aggregations]
            by = ", ".join(map(q, s.by))
            return f"SELECT {', '.join([*map(q, s.by), *aggs])} {frm}" + (f" GROUP BY {by}" if by else "")
        case "pivot":
            into = ", ".join(exp.convert(v).sql("duckdb") for v in s.into)
            return f"SELECT * {frm} PIVOT ({_agg(s.function, s.values)} FOR {q(s.column)} IN ({into}))"
        case "unpivot":
            return f"SELECT * {frm} UNPIVOT ({q(s.value)} FOR {q(s.name)} IN ({', '.join(map(q, s.columns))}))"
        case "sql":
            return _sql(s.query, prev)


def validate(steps: list) -> list:
    """TR-1 steps as models; ValueError unless they start with exactly one `source` step."""
    steps = Steps.validate_python(steps)
    if not steps or steps[0].type != "source" or any(s.type == "source" for s in steps[1:]):
        raise ValueError("A table's steps start with exactly one `source` step.")
    return steps


def tables(project: str) -> dict[str, list]:
    """Every table's validated steps, from the project's tables/<id>.json (TR-1). ValueError names a bad file.
    An input table (WB-2, `steps: []`) has no steps and isn't loaded, so it isn't here."""
    out = {}
    for f in (git.path(project) / "tables").glob("*.json"):
        try:
            if (steps := json.loads(f.read_text()).get("steps")) != []:
                out[f.stem] = validate(steps)
        except (ValueError, AttributeError) as e:  # AttributeError: the file isn't a JSON object
            raise ValueError(f"tables/{f.name}: {e}") from None
    return out


def inputs(project: str) -> set[str]:
    """WB-2 input tables: tables/<id>.json with `steps: []`. They have no `sources` row, so every list of a project's
    tables is the `sources` registry plus these. A broken file is left out (the import checks report it)."""
    out = set()
    for f in (git.path(project) / "tables").glob("*.json"):
        with contextlib.suppress(ValueError, AttributeError, OSError):
            if json.loads(f.read_text()).get("steps") == []:
                out.add(f.stem)
    return out


def reads(steps: list) -> set[str]:
    """TR-4 DAG edges: the source a table's steps load (or the table they start from), and the tables they merge or append."""
    return {steps[0].reads} | {s.table for s in steps if s.type == "merge"} | {t for s in steps if s.type == "append" for t in s.tables}


def _agg(function: str, column: str | None) -> str:
    if column is None:
        if function != "count":
            raise ValueError(f"`{function}` needs a column.")
        return "count(*)"
    return f"count(DISTINCT {_q(column)})" if function == "count_distinct" else f"{function}({_q(column)})"


def _type(t: str) -> str:
    try:
        return exp.DataType.build(t, dialect="duckdb").sql("duckdb")
    except sqlglot.errors.SqlglotError:
        raise ValueError(f"Unknown type '{t}'.") from None


def _parse(text: str) -> exp.Expression:
    try:
        tree = sqlglot.parse(text, read="duckdb")
    except sqlglot.errors.SqlglotError as e:
        raise ValueError(f"Invalid SQL '{text}': {e}") from None
    if len(tree) != 1 or tree[0] is None:
        raise ValueError(f"Expected one SQL expression or query, got '{text}'.")
    tree = tree[0]
    for f in tree.find_all(exp.Func):
        if (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower() in DENY:
            raise ValueError(f"Function not allowed in steps: '{f.sql('duckdb')}'.")
    return tree


def _expr(text: str) -> str:
    """A row expression over the previous step's columns: no subqueries, so no other tables or files."""
    tree = _parse(text)
    if isinstance(tree, exp.Query) or tree.find(exp.Query, exp.Table):
        raise ValueError(f"Expressions can't contain queries or tables: '{text}'.")
    return tree.sql("duckdb")


def _sql(text: str, prev: str) -> str:
    """A `sql` step: one query whose only table is `input` (the previous step).

    No WITH inside: a CTE name is only visible in its own scope, and elsewhere DuckDB resolves the same
    name to a file or table (M-01 verifier). Subqueries cover the same needs.
    """
    tree = _parse(text)
    if not isinstance(tree, exp.Query):
        raise ValueError(f"A `sql` step must be one SELECT query, got '{text}'.")  # noqa: TRY004 (bad user input, not a bad type)
    if tree.find(exp.With):
        raise ValueError("A `sql` step can't use WITH; use subqueries over `input`.")
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier) or t.args.get("db") or t.args.get("catalog") or t.name.lower() != INPUT:
            raise ValueError(f"A `sql` step may only read `{INPUT}` (the previous step), not '{t.sql('duckdb')}'.")
        t.set("this", exp.to_identifier(prev))
        if not t.alias:  # keep `input.col` references working
            t.set("alias", exp.TableAlias(this=exp.to_identifier(INPUT)))
    sql = tree.sql("duckdb")
    # H-01.5-fix-8: sqlglot reads `LATERAL read_csv('https://…')` as no table, DuckDB as a TABLE_FUNCTION that fetches
    # any URL (an outside contact with no click, §5 #3); DuckDB's own parser (reads no data) has the last word
    from abi.semantic.sql import _parsed  # not at top: semantic imports this module

    with duckdb.connect() as con:
        if not _parsed(con, sql)[0]:
            raise ValueError(f"A `sql` step may only read `{INPUT}` (the previous step), no table functions: '{text}'.")
    return sql
