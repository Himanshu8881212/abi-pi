"""Add data: upload, server path, S3/MinIO, HTTP URL, Postgres/MySQL table → DuckLake. PRD §6.2 IN-1, IN-2,
IN-6 (files on change, databases daily), SC-4, §6.4 TR-4.

Every load is `storage.refresh` (one atomic snapshot) inside the `add_data` action; uploads are just a
transport that stores the file and then calls that action. The worker re-runs a load when the
source's files change (`refresh_changed`, IN-6). Credentials only enter as `${secret:NAME}`.

Sources are project files (D-9): `add_data` writes `sources/<table>.json`, and that file ID is the
source ID a `source` step names. Every load reads the definition from the repo (`_source`); the
Postgres `sources` table keeps only runtime state per loaded table (the files' fingerprint). A table
with steps (tables/<t>.json, `set_steps`) loads its source's location into raw.<t> and builds
model.<t> with `transform.compile`; steps that start from a table the project builds (M4.2) build model.<t> from that
table's model.<table> (`_build`), so silver feeds gold without the source read again. Every load refreshes what reads the table, in dependency order
(`refresh`, TR-4).

M3.2: a load past ABI_INLINE_ROWS rows (`_big`, checked as the load checks its source) answers at once with a run ID;
the heavy worker runs the same action inline (`job`) and `load_runs` tells its caller how far it got.
"""

import contextlib
import fcntl
import functools
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from graphlib import CycleError, TopologicalSorter
from pathlib import Path as FsPath
from typing import Annotated, Literal

import duckdb
import psycopg
import pyarrow as pa
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.concurrency import run_in_threadpool
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from psycopg.types.json import Jsonb
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
)

from abi import actions, db, flows, ml, security, semantic, storage, transform, writeback
from abi.actions import ActionFailed, action, audit, current_user, production_refusal
from abi.agent import notify_change
from abi.project import SCHEMA, SCHEMAS, git, imports
from abi.project.bundle import bundle, loads
from abi.security import Role, User, credentials, role_in, usage
from abi.semantic import detect, lineage, live, model
from abi.storage import TABLE, _lit, _q

from . import fetch, quality

READERS = {"parquet": "read_parquet", "csv": "read_csv", "json": "read_json", "xlsx": "read_xlsx", "avro": "read_avro",
           "arrow": "read_arrow",  # IN-4 (nanoarrow; uncompressed IPC only, so server paths use pyarrow: `_arrow`)
           "delta": "delta_scan", "iceberg": "iceberg_scan", "sqlite": "sqlite_scan"}  # IN-4, see `_reader`
ALIASES = {"tsv": "csv", "ndjson": "json", "jsonl": "json", "feather": "arrow", "ipc": "arrow", "sqlite3": "sqlite",
           "db": "sqlite"}  # read_csv/read_json auto-detect the rest
FOLDERS = {"_delta_log": "delta", "metadata": "iceberg"}  # IN-4: a server folder holding this subfolder
SECRET_ONLY = rf"^{credentials.REF}$"
# M3.2: a load of more rows than this (about 20 s at the 220k rows/s pve1 loaded at, M0) runs as a heavy job. A text
# file's rows are guessed from its bytes; a guess too high only makes a job of a load that could have run inline
INLINE_ROWS = int(os.environ.get("ABI_INLINE_ROWS", "4400000"))
ROW_BYTES = 32
RUN: ContextVar[dict | None] = ContextVar("load_run", default=None)  # the load job this thread runs (`job`): its table, cursor
SOURCE_SCHEMA = Draft202012Validator(json.loads((SCHEMAS / "source.json").read_text()))


class S3(BaseModel):
    """S3/MinIO options (DuckDB `TYPE s3` secret); credential fields accept only ${secret:NAME}."""

    key_id: str | None = Field(None, pattern=SECRET_ONLY)
    secret: str | None = Field(None, pattern=SECRET_ONLY)
    region: str | None = Field(None, pattern=r"^[a-z0-9-]{1,50}$")  # it becomes part of the host (D-2)
    endpoint: str | None = Field(None, pattern=r"^[A-Za-z0-9.-]+(:[0-9]+)?$")  # host[:port] only, no scheme (D-2)
    url_style: Literal["path", "vhost"] | None = None
    use_ssl: bool | None = None


class Database(BaseModel):
    """IN-2 database source: the table to read, and the login only as ${secret:NAME}, both required (D-27)."""

    table: str = Field(pattern=r"^([A-Za-z_][A-Za-z0-9_]{0,62}\.)?[A-Za-z_][A-Za-z0-9_]{0,62}$", description="schema.table or table")
    user: str = Field(pattern=SECRET_ONLY)
    password: str = Field(pattern=SECRET_ONLY)
    incremental: str | None = Field(None, pattern=TABLE, description="IN-6 monotonic column; default: the auto-increment key")
    sync: bool = Field(False, description="WB-10: Owners may write edits back to this table (sync_edits); the login then"
                                          " needs UPDATE, INSERT and DELETE rights on it")


class SQLite(BaseModel):
    """IN-4 SQLite file source: the table to read (sqlite_scan, read-only, never ATTACH)."""

    table: str = Field(pattern=TABLE)


class Csv(BaseModel):
    """IN-2 CSV dialect for files DuckDB's sniffer rejects; only what is set overrides auto-detect (IN-5).
    Whitelisted values only: they become read_csv options, never free text in SQL."""

    model_config = ConfigDict(extra="forbid")
    quote: Literal['"', "'"] | None = None
    escape: Literal['"', "'", "\\"] | None = None
    delimiter: Literal[",", ";", "|", "\t"] | None = None
    header: bool | None = None
    strict_mode: bool | None = None  # false: read rows that break RFC 4180 (e.g. `"a"|"b"` in one field)


class Xlsx(BaseModel):
    """IN-4 Excel options: the sheet to read (default: the first). A sheet name as Excel allows it, without quotes."""

    model_config = ConfigDict(extra="forbid")
    sheet: str | None = Field(None, pattern=r"^[^\x00-\x1f'\[\]*?/\\:]{1,31}$")


# SC-4: a login typed into a location (user:password@ after any '://', or a '?' anywhere, even past '#' or a newline,
# in a location that doesn't start with http(s)://: DuckDB reads s3_secret_access_key and friends from an s3:// query)
# is refused at input validation, before audit_log, Git or a log sees it. Unanchored, so leading whitespace doesn't
# hide it. The same pattern is the `not` of source.json's location
LOGIN = r"://[^/?#]*@|^(?![hH][tT][tT][pP][sS]?://)[\s\S]*://[\s\S]*\?"


def _no_login(loc: str) -> str:
    if re.search(LOGIN, loc):
        raise ValueError("A location can't carry a login (user:password@) or, except http(s)://, a '?' query; put"
                         " logins in `headers`, `s3` or `database` as ${secret:NAME} and endpoint/region in `s3`.")
    return loc


class Source(storage.ProjectRef):
    table: str = Field(pattern=TABLE)
    location: Annotated[str, AfterValidator(_no_login)] = Field(min_length=1, description="Server path (under the files root), s3://…, http(s)://…,"
                                                    " or postgres|mysql|clickhouse(s)://host[:port]/database")
    format: Literal[*READERS] | None = None  # default: from the file extension, or a server folder's _delta_log/ or metadata/
    s3: S3 | None = None
    sqlite: SQLite | None = None  # IN-4: with format sqlite
    # HTTP; each value must use a secret; no CR/LF, so no injected header lines (D-2)
    headers: dict[Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,100}$")],
                  Annotated[str, Field(pattern=rf"^[^\r\n]*{credentials.REF}[^\r\n]*$")]] = Field({})
    database: Database | None = None  # IN-2: with a postgres:// or mysql:// location
    csv: Csv | None = None  # IN-2: CSV dialect, with format csv
    xlsx: Xlsx | None = None  # IN-4: the sheet, with format xlsx
    api: fetch.Api | None = None  # API-SRC: an https location that is a REST/JSON or CSV API
    _sid: str | None = PrivateAttr(None)  # sources/<id>.json when it isn't `table` (a stepped table's source)

    @property
    def sid(self) -> str:
        return self._sid or self.table

    def spec(self) -> dict:
        """The definition; without `database`/`sqlite` exactly as before D-01.1, so file sources keep their fingerprints."""
        # `sync` off (the default) leaves it out, so database sources from before WB-10 keep their fingerprints too
        d = self.model_dump(mode="json", exclude={"database": {"sync"}} if self.database and not self.database.sync else None)
        return {k: v for k, v in d.items() if v is not None or k not in ("database", "sqlite", "csv", "xlsx", "api")}


DATABASES = {"postgres": 5432, "mysql": 3306}  # IN-2, D-27
MYSQL_MAX_PORT = 65353  # DuckDB's mysql extension refuses higher ports ("port must be between 0 and 65353")
# IN-1 connection string: no userinfo, query or secret (the login is in `database`); host[:port] as plain as F-09 `_host`
DB_LOCATION = r"(postgres|mysql)://([A-Za-z0-9.-]+)(?::(\d{1,5}))?/([A-Za-z0-9_-]{1,63})"
ORDERABLE = r"U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?|DECIMAL\(.*\)|FLOAT|DOUBLE|DATE|TIME.*|VARCHAR"  # IN-6 incremental column types


def files_root() -> FsPath:
    return FsPath(os.environ.get("ABI_FILES_PATH", "/data/files")).resolve()


UPLOADED = r"uploads/([0-9a-f]{32})/\w[\w .-]{0,199}"  # where an IN-7 upload lands: files/uploads/<id>/<its file name>


def locations(files: dict) -> set[str]:
    """Every location a project's files (a bundle) load, as any environment would (PC-9 overrides)."""
    out = set()
    for rel, f in files.items():
        if isinstance(f, dict) and rel.startswith("sources/"):
            out.add(f.get("location"))
        elif isinstance(f, dict) and rel.startswith("environments/"):
            out |= {s.get("location") for s in (f.get("sources") or {}).values() if isinstance(s, dict)}
    return {x for x in out if isinstance(x, str)}


def readable(conn, user: User, project: str, location: str) -> None:
    """A server path loads only what the caller may read: an upload made into a project where they read every row
    (Editor or Owner: row-level security restricts only roles below Editor), or a path such a project already loads.
    Any other path in the files folder is the workspace owner's. 1.1 took any path there, so a restricted Viewer of one
    project could load its uploaded file into a project they edit, past its row-level security. Existing sources keep
    refreshing: the worker and `refresh` load what the project's files already say."""
    if "://" in location or user.workspace_owner:
        return
    mine = {r["project"] for r in conn.execute(
        "SELECT project FROM project_roles WHERE user_id = %s AND role IN ('editor', 'owner')", [user.id])}
    if m := re.fullmatch(UPLOADED, location):  # the upload's record: in progress (.upload.json), then loaded (.owner.json)
        for f in (".upload.json", ".owner.json"):
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                if json.loads((files_root() / "uploads" / m[1] / f).read_text())["project"] in mine:
                    return
    for p in mine:
        root = git.path(p)
        with contextlib.suppress(OSError, ValueError):
            if location in locations({f"{f.parent.name}/{f.name}": json.loads(f.read_text())
                                      for f in [*root.glob("sources/*.json"), *root.glob("environments/*.json")]}):
                return
    raise ActionFailed(403, "That server path isn't yours to load: upload the file into this project, or ask the"
                            " workspace owner (who reads every path in the files folder).")


@action(Role.editor, definition=True)
def add_data(conn, user: User, args: Source) -> dict:
    """Loads a file, folder, database or HTTP API into the project's table as a new snapshot, replacing that table's data, saves the source as sources/<table>.json, then refreshes the tables that read it. A REST/JSON or CSV API is an https `location` (the endpoint, fixed query parameters in `api.query`) with an `api` object: `records` is the JSON pointer to the array of records (e.g. /data/items), `pagination` how to get the next page (page or offset number in `param`, a cursor at `pointer` sent as `param`, or a next link at `pointer` or in the Link header), `auth` takes only ${secret:NAME} references (names from list_secrets, never a typed key), `columns` maps and types fields, `incremental` {field, param} sends the largest loaded value of `field` to load only new records, and `every_hours` sets the schedule (default daily). Loading from a host the project hasn't loaded from yet waits for the person's Confirm click. A big load (millions of rows) answers at once with a run ID and loads in the background: load_runs says when it is in."""
    if args.table in transform.inputs(args.project):  # D-20: an input table's name is reserved; a load would drop its edits
        raise ActionFailed(409, f"'{args.table}' is an input table (its rows are edits), so no data loads into it. Load"
                                " under another name, or delete the input table first.")
    try:
        steps = transform.tables(args.project).get(args.table)
    except ValueError as e:
        raise ActionFailed(400, str(e))
    if steps and steps[0].source != args.table:
        raise ActionFailed(409, f"Table '{args.table}' is built by steps from {'table' if steps[0].table else 'source'}"
                                f" '{steps[0].reads}'; add data to that source or change the table's source step.")
    readable(conn, user, args.project, args.location)
    if actions.needs_click() and (targets := hosts(args.spec())):  # §5 #3: the load (and every refresh) connects there
        from abi.tools import builtin  # not at top: builtin imports this module

        builtin._first_send(conn, args.project, targets, {"hosts": sorted(t[7:] for t in targets), "source": args.spec()})
    _lock(conn, args.project, args.table)  # before the file: a worker check seeing it waits for this load (IN-6)
    if rows := _big(conn, args):  # M3.2: no file yet; the job writes it as this call would
        return queue(conn, user, "add_data", args, rows)
    _write(args)  # a failed load rolls the file back (git.change)
    snap = _load(conn, args, user.name)
    conn.commit()  # the load is in; downstream loads use their own connections and must see this row (and its secret binding)
    refresh(conn, args.project, {args.table}, user.name)
    return storage.made(snap)


class TableSteps(storage.ProjectRef):
    table: str = Field(pattern=TABLE)
    steps: list[transform.Step] = Field(min_length=1)


@action(Role.editor, definition=True)
def set_steps(conn, user: User, args: TableSteps) -> dict:
    """Saves a table's transform steps (the first one names a loaded `source`, or a `table` the project builds: silver reads bronze's table and gold silver's, with no source read again), loads the table through them, then refreshes the tables that read it. On an input table (schema-first, declared columns) the steps map a source onto it: they must output exactly its declared column names with values that cast to the declared types; its hand-entered rows stay as edits. A big table answers at once with a run ID and loads in the background (load_runs)."""
    t, first = args.table, args.steps[0]
    try:
        transform.compile(t, args.steps)
        graph = _graph({**transform.tables(args.project), t: args.steps}, {f.stem for f in (git.path(args.project) / "sources").glob("*.json")})
        TopologicalSorter(graph).prepare()
    except CycleError as e:  # nothing written yet: Git stays as it was
        raise ActionFailed(400, "Tables would read each other in a cycle: " + " reads ".join(reversed(e.args[1])) + ". Start"
                                " one of them from a source, or from a table outside the cycle.")
    except ValueError as e:
        raise ActionFailed(400, str(e))
    if first.table and (first.table == t or first.table not in graph):  # M4.2: a loaded or stepped table, never itself
        raise ActionFailed(400, f"Table '{t}' can't start from table '{first.table}': " + (
            "a table can't read itself." if first.table == t else "the project builds no such table (a loaded source or"
                                                                   f" a table with steps: {', '.join(sorted(graph.keys() - {t}))})."))
    for x in {t} | transform.reads(args.steps):  # IN-3: steps run in DuckDB over loaded data
        live.refuse(args.project, x)
    src = None if first.table else _source(args.project, first.source, t)
    f = git.path(args.project) / "tables" / f"{t}.json"
    old = json.loads(f.read_text()) if f.exists() else {}
    steps = args.steps
    # PC-15: mapping a source onto an input table (D-20: its edits stay). A final set_type keeps the declared types on
    # every later refresh; `_model_sql` checks the steps before it
    if declared := old["columns"] if old.get("steps") == [] else None:
        steps = steps + [transform.SetType(type="set_type", columns={c["name"]: c["type"] for c in declared})]

    def write(columns):
        imports.write(args.project, {f"tables/{t}.json": {"$schema": SCHEMA + "table.json", **old, "id": t, "columns": columns,
                                                          "steps": [s.model_dump(mode="json") for s in steps]}})

    # ponytail: the load and downstream refresh run inside git.change's repo lock, so a long load blocks the project's
    # other definition actions; refresh after the change (and roll back the file on failure) if that starts to hurt
    _lock(conn, args.project, t)  # as add_data
    if rows := _big(conn, src) if src else _big_built(args.project, first.table):
        return queue(conn, user, "set_steps", args, rows)
    write(old.get("columns", []))  # _load builds model.<t> from the file's steps
    snap = _load(conn, src, user.name, declared=declared) if src else _build(conn, args.project, t, user.name, declared=declared)
    write(semantic.columns(args.project, t))
    conn.commit()  # as in add_data
    refresh(conn, args.project, {t}, user.name)
    return storage.made(snap)


class TableRef(storage.ProjectRef):
    table: str = Field(pattern=TABLE)


class SourceRef(storage.ProjectRef):
    source: str = Field(pattern=TABLE, description="source ID: the file sources/<id>.json")


def _without(files: dict, t: str | None, *paths: str) -> dict:
    """`files` less `paths` and, when `t` is given, the model.json entries naming table `t`."""
    out = {k: v for k, v in files.items() if k not in paths}
    if t and (m := out.get("model/model.json")):
        m = {k: v for k, v in m.items() if not (k == "date_table" and v["table"] == t)}
        for k in ("tables", "entities", "relationships"):
            if k in m:
                m[k] = [x for x in m[k] if t not in (x.get("table"), x.get("from", {}).get("table"), x.get("to", {}).get("table"))]
        out["model/model.json"] = m
    return out


def _delete(conn, user: User, project: str, old: dict, files: dict, node: str) -> dict:
    """E-14: `files` (the project without `node`) through the import checks, so a dangling use is refused 422 with its
    file and fix, after one confirmation (§5 #3) whose signed preview lists what reads `node` (SE-11, from the
    definitions only). Removed tables leave IN-6 refresh and go to the 30-day archive (PC-7)."""
    impact = lineage.reach(lineage.graph(old), {node}, "downstream")
    why = f"It deletes {node.replace(':', ' ', 1)}." + (f" Objects that read it: {', '.join(impact)}." if impact else "")
    return git.apply_tree(conn, user, project, files, f"Deleting {node}", [why], {"delete": node, "impact": impact})


@action(Role.editor, definition=True, confirms=True)
def delete_table(conn, user: User, args: TableRef) -> dict:
    """Deletes a table (also an input table): its table and source files and the model entries naming it (relationships, entities, date table). Refused while a measure, report, dataset, tool or another table uses it; otherwise asks a confirmation listing everything that reads it. Its data is archived for 30 days; undo brings the definition back."""
    t, old = args.table, bundle(git.path(args.project))
    files = _without(old, t, f"tables/{t}.json", f"sources/{t}.json")
    if files.keys() == old.keys():
        raise ActionFailed(404, f"No table '{t}' in project '{args.project}'.")
    return _delete(conn, user, args.project, old, files, f"table:{t}")


@action(Role.editor, definition=True, confirms=True)
def delete_source(conn, user: User, args: SourceRef) -> dict:
    """Deletes a source (sources/<id>.json). Refused while a table's steps load it; the table of the same name that no steps build goes with it (data archived for 30 days). Asks a confirmation listing everything that reads it."""
    s, old = args.source, bundle(git.path(args.project))
    if f"sources/{s}.json" not in old:
        raise ActionFailed(404, f"No source '{s}' in project '{args.project}'.")
    try:
        users = sorted(t for t, steps in transform.tables(args.project).items() if steps[0].source == s)
    except ValueError as e:
        raise ActionFailed(400, str(e))
    if users:
        raise ActionFailed(409, f"Source '{s}' is loaded by table(s) {', '.join(users)}; change their source step or"
                                " delete them first.", tables=users)
    files = _without(old, None if f"tables/{s}.json" in old else s, f"sources/{s}.json")
    return _delete(conn, user, args.project, old, files, f"source:{s}")


def _graph(steps: dict, sources) -> dict[str, set[str]]:
    """TR-4: every source or stepped table -> what it reads (a table with no steps reads only its own source's files)."""
    return {t: set() for t in sources} | {t: transform.reads(s) - {t} for t, s in steps.items()}


def _no_pgservice(kind: str) -> None:
    if kind == "postgres" and "PGSERVICE" in os.environ:  # a service file outranks the env and can set hostaddr (D-27)
        raise ActionFailed(500, "PGSERVICE is set on the server, so a Postgres source could connect elsewhere; unset it.")


def _write(src: Source) -> None:
    """sources/<table>.json (D-9): credentials stay `${secret:NAME}` references, never values (SC-4)."""
    f = git.path(src.project) / "sources" / f"{src.table}.json"
    doc = src.model_dump(mode="json", exclude_defaults=True, exclude={"project", "table"})
    # IN-6: databases and APIs daily, files on change, ClickHouse live (IN-3)
    refresh = "live" if re.match(live.LOCATION, src.location) else "daily" if src.database or src.api else "on_change"
    with contextlib.suppress(ValueError):  # IN-8: a person's freshness setting stays (a broken old file is replaced)
        if f.is_file() and (q := loads(f.read_text()).get("quality")):
            doc["quality"] = q
    imports.write(src.project, {f"sources/{src.table}.json": {"$schema": SCHEMA + "source.json", "id": src.table,
                                                               "refresh": refresh, **doc}})


def _source(project: str, sid: str, table: str) -> Source:
    """D-9: sources/<sid>.json as the spec that loads `table`, checked on every load: schemas/v1/source.json, then the
    `Source` model (secret-only credentials, F-09 host rules). Messages never echo a value from the file (SC-4)."""
    f = git.path(project) / "sources" / f"{sid}.json"
    if not re.fullmatch(TABLE, sid) or not f.is_file():  # a step's source is free text: never build a path from it unchecked
        raise ActionFailed(404, f"No source '{sid}' (sources/<id>.json) in project '{project}'; add data first.")
    try:
        doc = loads(f.read_text())
        env = git.path(project) / "environments" / f"{git.environment(project)}.json"
        if env.is_file():  # PC-9: this environment's connection settings replace the file's top-level keys
            doc |= loads(env.read_text()).get("sources", {}).get(sid, {})
        if err := best_match(SOURCE_SCHEMA.iter_errors(doc)):  # err.message would echo the value
            raise ActionFailed(400, f"sources/{sid}.json: {imports.json_path(err, 'source')} fails '{err.validator}' in schemas/v1/source.json.")
        if doc["id"] != sid:
            raise ActionFailed(400, f"sources/{sid}.json: its id must be '{sid}', the file name.")
        src = Source.model_validate({**doc, "project": project, "table": table})
        src._sid = sid
        return src
    except ValidationError as e:
        raise ActionFailed(400, f"sources/{sid}.json: " + "; ".join(
            f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in actions.invalid(e)))
    except ValueError as e:  # not JSON, duplicate key
        raise ActionFailed(400, f"sources/{sid}.json: {e}")


def _migrate(conn, project: str, steps: dict) -> None:
    """D-9: write pre-M-10 definitions (the Postgres `spec`) to sources/<id>.json; `refresh` NULLs each `spec` once
    that table's replay passed (so a failed first refresh can still bind a legacy secret later, F-09).

    Worker only (`refresh_changed`, never `refresh`, which revert/add_data call inside git.change), as plain file writes
    (uncommitted changes, PC-6): inside an action's change, `undo` would delete a definition whose Postgres copy is gone."""
    rows = conn.execute("SELECT table_name, spec FROM sources WHERE project = %s AND spec IS NOT NULL", [project]).fetchall()
    if rows:
        git.repo(project)  # 404 when the project has no repo: nowhere to put the files
    for r in rows:
        t = r["table_name"]
        if (t in steps and steps[t][0].source != t) or (git.path(project) / "sources" / f"{t}.json").exists():
            continue  # a stepped table's copy of its source's spec, or already a file
        _write(Source.model_validate(r["spec"]))


# ponytail: per process, so a worker restart retries at once; keep it in the `sources` row if several workers refresh
BACKOFF: dict[tuple[str, str], tuple[int, float]] = {}  # (project, table) -> (failures in a row, next try, unix time)
CHECK: ContextVar[bool] = ContextVar("check_only", default=False)  # M4.1: refresh_changed(check=True) loads nothing
DUE = "due"  # refresh's word for a table the check found to load


class Due(Exception):  # noqa: N818 - not an error: `_load` under CHECK reached a load that would run
    pass


def refresh(conn, project: str, after: set[str] | None, author: str, reload: set[str] = frozenset()) -> dict[str, str]:
    """TR-4: reload tables in dependency order, each as its own snapshot. Returns each failed table's error.

    `after`: tables just loaded (or removed); reload everything that reads them, directly or not, plus the
    tables in `reload` (a revert changed their steps, PC-7). None (IN-6 worker):
    reload every table whose files changed, and everything downstream of a reloaded table. A failure is
    audited and notified (MA-7) and skips the table's dependents; their previous snapshots stay live (ST-2).
    """
    steps = transform.tables(project)
    prints = {r["table_name"]: r["fingerprint"] for r in conn.execute(
        "SELECT table_name, fingerprint FROM sources WHERE project = %s", [project]).fetchall()}
    graph = _graph(steps, {f.stem for f in (git.path(project) / "sources").glob("*.json")})
    done, failed = set(after or ()), {}
    for t in TopologicalSorter(graph).static_order():
        if t in done or t not in graph:  # just loaded, or a name only read (e.g. a missing table)
            continue
        up = graph[t]
        if bad := sorted(up & failed.keys()):
            failed[t] = f"It reads {bad}, which failed."
            continue
        forced = bool(up & done) or t in reload
        if after is not None and not forced:
            continue
        built = steps[t][0].table if t in steps else None  # M4.2: from another table's model, no source read
        source = steps[t][0].source if t in steps else t
        spec = {"project": project, "table": t, **({"from": built} if built else {"source": source})}  # audited; the definition itself is in Git
        used: dict = {}  # the secrets this load resolved, to scrub the log (SC-4)
        is_db, key, now = False, (project, t), datetime.now(UTC).timestamp()
        try:
            src = None if built else _source(project, source, t)
            is_db = src is not None and (src.database is not None or src.api is not None)
            if is_db and not forced and now < BACKOFF.get(key, (0, 0))[1]:
                continue  # IN-6: a failing database load waits, so a wrong login doesn't hit the source every minute
            with db.connect() as c:
                if not forced:  # a load already running (add_data, set_steps, a sync) is this one; re-read what it wrote
                    if not _lock(c, project, t, wait=False):
                        continue
                    prints[t] = (c.execute("SELECT fingerprint FROM sources WHERE project = %s AND table_name = %s",
                                           [project, t]).fetchone() or {}).get("fingerprint")
                unless = None if forced else prints.get(t)
                snap = _build(c, project, t, author, unless) if built else _load(c, src, author, unless=unless, used=used)
                # replayed (legacy secrets bound in `allow`, same transaction): drop the pre-M-10 spec, the file holds it now
                c.execute("UPDATE sources SET spec = NULL WHERE project = %s AND table_name = %s", [project, t])
                BACKOFF.pop(key, None)
                if snap is not None:
                    done.add(t)
                    audit(c, None, "worker", "refresh_source", project, spec, 200, f"snapshot {snap} by {author}")
        except (ActionFailed, duckdb.Error, psycopg.Error, ValueError) as e:  # ValueError: invalid steps (TR-1)
            if isinstance(e, (duckdb.Error, psycopg.Error)):  # T-03.5: raw text (paths, SQL) to the server log only
                logging.getLogger(__name__).error("Refreshing %s/%s failed: %s", project, t, credentials.log_text(e, used))
                e = ActionFailed(500, f"Loading failed ({type(e).__name__}); the details are in the server log.")
            failed[t] = str(e)
            if is_db:  # 1 min, doubling to 1 h
                n = BACKOFF.get(key, (0, 0))[0] + 1
                BACKOFF[key] = (n, now + min(60 * 2 ** (n - 1), 3600))
            with db.connect() as c:
                audit(c, None, "worker", "refresh_source", project, spec, getattr(e, "status", 500), str(e))
            notify_change("refresh", project, f"Refreshing table '{t}': ", f"{e} The previous snapshot stays live.",
                          "works again.", t)  # §5, MA-7, D-10: once per distinct error
        except Due:  # M4.1: refresh_changed(check=True): this one loads in the heavy worker
            failed[t] = DUE
    return failed


def probe(conn, project: str, sid: str) -> None:
    """PC-3 import of sources/<sid>.json: refuse a hand-set incremental column the source table lacks (IN-6), now."""
    if (src := _source(project, sid, sid)).database and src.database.incremental:
        _load(conn, src, "import", probe=True)


def _lock(conn, project: str, table: str, wait: bool = True) -> bool:
    """IN-6, API-SRC.4-fix-1: one load of a table at a time, held until `conn` ends its transaction (re-entrant)."""
    f = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    return conn.execute(f"SELECT {f}(hashtext(%s)) AS ok", [f"load:{project}/{table}"]).fetchone()["ok"]


def _big(conn, src: Source) -> int | None:
    """M3.2: the rows a load of `src` copies when they are more than ABI_INLINE_ROWS, else None (it loads inline, as in
    1.1; so do API and live sources, and every load in the worker). The probe checks the source as the load would, so a
    wrong path, login or format fails now."""
    if RUN.get() or flows.IN_FLOW.get() or src.api or src.location.split("://", 1)[0].lower() in live.SCHEMES:
        return None
    rows = _load(conn, src, "probe", probe=True)
    return rows if rows and rows > INLINE_ROWS else None


def _big_built(project: str, table: str) -> int | None:
    """M4.2: as `_big`, for steps that start from `table`: its rows (DuckLake's count) when more than ABI_INLINE_ROWS.
    Not built yet: None, and the build says so."""
    if RUN.get() or flows.IN_FLOW.get():
        return None
    with contextlib.suppress(duckdb.Error):
        rows = storage.lake(project).cursor().execute(f"SELECT count(*) FROM lake.model.{_q(table)}").fetchone()[0]
        return rows if rows > INLINE_ROWS else None
    return None


def queue(conn, user: User, name: str, args: storage.ProjectRef, rows: int) -> dict:
    """M3.2: action `name` with `args` as a heavy job (`job`) for `user`, through the same door (VIA); the run ID now."""
    run = str(uuid.uuid4())
    conn.execute("INSERT INTO load_runs (id, project, table_name, action, payload, via, user_id, estimate)"
                 " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                 [run, args.project, args.table, name, Jsonb(args.model_dump(mode="json")), actions.VIA.get(), user.id, rows])
    conn.commit()  # before the worker picks it up; the table's load lock goes with it (the job takes its own)
    from abi import worker  # not at top: the worker imports every action module

    try:
        worker.defer(worker.load, run=run)
    except Exception:
        conn.execute("DELETE FROM load_runs WHERE id = %s", [run])
        conn.commit()  # the action rolls back on the error: without this the run stays queued with no job
        raise
    return {"run": run, "table": args.table, "status": "queued", "columns": ["run", "table", "status"],
            "rows": [[run, args.table, "queued"]], "message": f"Loading '{args.table}' in the background: load_runs with"
            " this run says how far it got and when it is in. Don't load it again meanwhile."}


def job(run: str) -> None:
    """M3.2, the heavy worker's load (abi.worker.load): the queued action, inline, as the person who asked (their role
    checked again) and through their door, so it lands as the inline call would: one Git change, one snapshot, the
    quality scan and copies, then what reads the table. Rows so far, the outcome and its error go to load_runs."""
    with db.connect() as conn:
        r = conn.execute("UPDATE load_runs SET status = 'running', started = now() WHERE id = %s AND status = 'queued'"
                         " RETURNING project, table_name, action, payload, via, user_id, estimate", [run]).fetchone()
        u = r and security.user(conn, r["user_id"])
    if r is None:
        return
    box, done = {"table": r["table_name"], "cur": None}, threading.Event()
    threading.Thread(target=_progress, args=(run, box, r["estimate"], done), daemon=True).start()
    held, snap, error, rows = RUN.set(box), None, None, None
    try:
        if u is None:
            raise ActionFailed(400, "The person who asked for this load no longer has an account.")
        snap = actions.run(r["action"], r["payload"], u, r["via"]).get("snapshot")
    except ActionFailed as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001 - any failure is the run's; its text (paths, SQL) to the server log only
        logging.getLogger(__name__).error("Load run %s failed: %s", run, credentials.log_text(e))
        error = f"Loading failed ({type(e).__name__}); the details are in the server log."
    finally:
        RUN.reset(held)
        done.set()
    with contextlib.suppress(Exception):  # the outcome stands either way
        if error is None:
            rows = semantic.execute(r["project"], f"SELECT count(*) FROM {_q(r['table_name'])}", {"load_run": run})[0][0]
    with contextlib.suppress(OSError):
        if r["action"] == "add_data":
            _settled(r["payload"]["location"], error is None)
    with db.connect() as conn:  # the definition (it may name secrets) isn't kept past the run
        conn.execute("UPDATE load_runs SET status = %s, error = %s, snapshot = %s, rows = coalesce(%s, rows),"
                     " finished = now(), payload = NULL WHERE id = %s", ["failed" if error else "done", error, snap, rows, run])


INTERRUPTED = "The load was interrupted (ABI restarted or its worker stopped); load it again."


def interrupted(run: str) -> None:
    """M3 review: a load job whose heavy worker died (kill -9, out of memory, a restart's KILL) ends failed, as its own
    failure would (worker.retry_stalled_runs, once its worker's heartbeat is 30 s old); 1.2 before kept it running."""
    with db.connect() as conn:
        r = conn.execute("UPDATE load_runs SET status = 'failed', error = %s, finished = now(), payload = NULL WHERE id = %s"
                         " AND status IN ('queued', 'running') RETURNING action, (SELECT payload FROM load_runs WHERE id = %s) AS payload",
                         [INTERRUPTED, run, run]).fetchone()
    with contextlib.suppress(OSError, TypeError, KeyError):
        if r and r["action"] == "add_data":
            _settled(r["payload"]["location"], False)


PROGRESS_S = 2  # how often a load job writes its rows so far


def _progress(run: str, box: dict, estimate: int, done: threading.Event) -> None:
    """M3.2: a running load's rows so far, every PROGRESS_S: its query's progress (DuckDB) of the probe's estimate."""
    best = 0
    while not done.wait(PROGRESS_S):
        with contextlib.suppress(duckdb.Error):
            if box["cur"] and (n := int(estimate * min(box["cur"].query_progress(), 100) / 100)) > best:
                best = n
                with db.connect() as conn:
                    conn.execute("UPDATE load_runs SET rows = %s WHERE id = %s AND status = 'running'", [n, run])


def _watched(box: dict, hook, cur) -> None:
    """`storage.refresh`'s hook in a load job: the job reads this load's progress (DuckDB prints none)."""
    cur.execute("SET enable_progress_bar = true")
    cur.execute("SET enable_progress_bar_print = false")
    box["cur"] = cur
    if hook:
        hook(cur)


class RunsOf(storage.ProjectRef):
    run: str | None = Field(None, pattern=r"^[0-9a-f-]{36}$", description="the run ID add_data, set_steps or refresh"
                                                                          " answered; omitted: your latest runs")


@action(Role.viewer)
def load_runs(conn, user: User, args: RunsOf) -> dict:
    """Your loads that run in the background: add_data, set_steps or refresh of a big table answers at once with a run ID and status queued, and the server loads it. Each run: table, status (queued, running, done or failed), rows (while it runs: about how many so far, from the source's size; when done: the table's rows), the snapshot it made, its error, when it was asked and finished; newest first. Wait a few seconds between calls. Only your own runs."""
    cols = ["run", "table", "action", "status", "rows", "snapshot", "error", "created", "finished"]
    runs = conn.execute(
        'SELECT id::text AS run, table_name AS "table", action, status, rows, snapshot, error, created, finished'
        " FROM load_runs WHERE project = %s AND user_id = %s AND (%s::uuid IS NULL OR id = %s::uuid)"
        " ORDER BY created DESC LIMIT 20", [args.project, user.id, args.run, args.run]).fetchall()
    if args.run and not runs:  # someone else's run doesn't exist for you
        raise ActionFailed(404, f"No load run '{args.run}' of yours in project '{args.project}'.")
    return {"runs": runs, "columns": cols, "rows": [[r[c] for c in cols] for r in runs]}


def _load(conn, src: Source, author: str, unless: str | None = None, used: dict | None = None, probe: bool = False,
          hook=None, declared: list[dict] | None = None) -> int | None:
    """Load `src`; skip (return None) when its files' fingerprint equals `unless`. `used` collects the resolved secrets.
    `probe`: connect (a database also checks `incremental`) or list the files and return the rows a full load would
    copy (M3.2), loading nothing. `hook`: `storage.refresh`'s."""
    if not probe:
        _lock(conn, src.project, src.table)
    used = {} if used is None else used
    # the scheme comes from the unresolved location, so no message can echo a secret value (SC-4)
    kind = src.location.split("://", 1)[0].lower() if "://" in src.location else "path"
    if re.search(credentials.REF, kind):
        raise ActionFailed(403, "A secret can't be the location's scheme.")
    if kind in live.SCHEMES:  # IN-3: never copied, only its columns are recorded
        return live.register(conn, src, unless)
    name = f"abi_{uuid.uuid4().hex}"  # the temporary secret, and the attached database's alias
    if kind in DATABASES or src.database:
        m = re.fullmatch(DB_LOCATION, src.location)
        if not m or not src.database:
            raise ActionFailed(400, "A database source needs location postgres://host[:port]/database or"
                                    " mysql://host[:port]/database (no user, password or options) and a `database` object.")
        if kind == "mysql" and int(m[3] or 0) > MYSQL_MAX_PORT:
            raise ActionFailed(400, f"A MySQL source's port must be at most {MYSQL_MAX_PORT}; DuckDB's MySQL driver"
                                    " can't connect to a higher one.")
        _no_pgservice(kind)
        loc, host = None, m[2].lower()
        # D-27: every setting explicit, never the PG*/MYSQL_* env (the catalog's own PGPASSWORD is there)
        opts = {"TYPE": kind, "HOST": m[2], "PORT": m[3] or str(DATABASES[kind]), "DATABASE": m[4],
                "USER": credentials.resolve(conn, src.database.user, used, src.project),
                "PASSWORD": credentials.resolve(conn, src.database.password, used, src.project)}
        schema, _, table = src.database.table.rpartition(".")
        raw_sql = f"SELECT * FROM {name}.{_q(schema) + '.' if schema else ''}{_q(table)}"
    elif src.api:  # API-SRC.1: fetched page by page in the load, after the D-2 check below
        loc, opts, raw_sql = credentials.resolve(conn, src.location, used, src.project), {}, None
        if kind != "https" or src.format or (src.csv and src.api.format != "csv"):
            raise ActionFailed(400, "An API source needs an https:// location, its format in `api.format` and `csv`"
                                    " options only with api.format csv.")
        headers, query, hide = fetch.request(conn, src, used)
        host = _host(kind, loc, opts)
    else:
        loc = credentials.resolve(conn, src.location, used, src.project)
        if kind == "path":
            loc = str(files_root() / loc)
            if not FsPath(loc).resolve().is_relative_to(files_root()):
                raise ActionFailed(400, "Server paths must be inside the files folder.")
        elif kind not in ("s3", "http", "https"):
            raise ActionFailed(400, f"Unsupported location '{kind}://'; use a server path, s3://, http(s)://,"
                                    " postgres://, mysql:// or clickhouse(s)://.")
        elif kind == "s3" and "?" in loc:  # DuckDB reads ?s3_endpoint=, ?s3_region=, … from it past the `s3` options (D-2)
            raise ActionFailed(400, "An s3:// location can't have a '?' query; put endpoint, region and keys in `s3`.")
        ext = re.sub(r"[?#].*", "", loc).rsplit(".", 1)[-1].lower()
        folder = kind == "path" and FsPath(loc).is_dir()
        fmt = src.format or next((f for d, f in FOLDERS.items() if folder and (FsPath(loc) / d).is_dir()), None) or ALIASES.get(ext, ext)
        if fmt not in READERS:
            raise ActionFailed(400, f"Can't tell the format of '{src.location}'; set format to one of {list(READERS)}.")
        if fmt == "sqlite" and (kind != "path" or not src.sqlite):
            raise ActionFailed(400, "A SQLite source needs a server path and the table to read (`sqlite`: {\"table\": …}).")
        if src.csv and fmt != "csv" or src.xlsx and fmt != "xlsx":
            k = "csv" if src.csv and fmt != "csv" else "xlsx"
            raise ActionFailed(400, f"`{k}` options apply to {k.upper()} files only; '{src.location}' is read as {fmt}.")
        if fmt in ("delta", "iceberg") and (kind not in ("path", "s3") or fmt == "delta" and kind == "s3" and not (
                src.s3 and src.s3.endpoint and src.s3.url_style == "path")):
            # D-2: Delta's own S3 client sends vhost requests to the bare endpoint, not <bucket>.<endpoint> as `_host` says
            raise ActionFailed(400, f"A {fmt} table must be a server folder or an s3:// folder"
                                    + (" (Delta on S3: set s3.endpoint and url_style 'path')." if fmt == "delta" else "."))
        opts = {}
        if kind == "s3" and src.s3:
            opts = {"TYPE": "s3", **{k.upper(): credentials.resolve(conn, str(v).lower() if isinstance(v, bool) else v, used, src.project)
                                     for k, v in src.s3.model_dump(exclude_none=True).items()}}
        if kind.startswith("http") and src.headers:
            opts = {"TYPE": "http", "EXTRA_HTTP_HEADERS": {k: credentials.resolve(conn, v, used, src.project)
                                                           for k, v in src.headers.items()}}
        host = _host(kind, loc, opts)
        raw_sql = _reader(fmt, loc, src)
        if fmt == "arrow" and kind == "path":  # pyarrow on the load's own cursor (also LZ4/ZSTD Feather, which nanoarrow can't)
            raw_sql, before = f"SELECT * FROM {name}", hook
            hook = lambda cur: (before and before(cur), cur.register(name, _arrow(loc)))
        elif fmt == "arrow":
            storage.lake(src.project).execute("LOAD nanoarrow")  # a community extension: never autoloaded
    con = storage.lake(src.project)
    # F-09 replay: only a pre-M-10 spec still in Postgres (NULL once migrated), so no source file picks a legacy secret's host
    stored = conn.execute("SELECT spec FROM sources WHERE project = %s AND table_name = %s", [src.project, src.table]).fetchone()
    credentials.allow(conn, used, host, replay=stored is not None and stored["spec"] == src.spec())  # before any connection
    tmp = None
    try:
        if opts:
            if loc:
                opts["SCOPE"] = loc.split("*", 1)[0]  # this source only
            con.execute(f"CREATE TEMPORARY SECRET {name} ({', '.join(f'{k} {_sql(v)}' for k, v in opts.items())})")
        # the definition too (source with only ${secret:NAME} refs, and the table's steps): an undo, revert or edit of
        # sources/<id>.json or tables/<t>.json reloads the table (no steps: same print as before M-15, no mass reload)
        steps = transform.tables(src.project).get(src.table)
        defn = json.dumps(src.spec() | ({"steps": [s.model_dump(mode="json") for s in steps]} if steps else {}), sort_keys=True)
        same = bool(unless and unless.startswith(defn))  # same definition as the last load, a later day (or hours)
        if src.api and src.api.every_hours:  # API-SRC.2: the load time; due again `every_hours` after it
            fingerprint = defn + datetime.now(UTC).isoformat()
            if same and not _due(unless[len(defn):], src.api.every_hours):
                return None
        else:
            fingerprint = defn + (
                datetime.now(UTC).date().isoformat() if src.database or src.api  # IN-6 databases daily, checked without connecting
                else credentials.scrub(con.cursor().execute(
                    "SELECT string_agg(concat_ws('|', filename, size, last_modified::varchar), ',' ORDER BY filename)"
                    # IN-4 folders: every file under them. Filenames can hold a secret from the URL: scrubbed
                    f" FROM read_blob('{_lit(loc.rstrip('/') + '/**' if fmt in FOLDERS.values() else loc)}')"
                ).fetchone()[0] or "", used))
            # ponytail: size + Last-Modified; an HTTP server without Last-Modified that keeps the size hides a change
            if unless is not None and fingerprint == unless:
                return None
        if CHECK.get():  # M4.1: what changed is all the minute's check wants; the heavy worker loads it
            raise Due
        append = truncated = None
        if src.api:  # pages go to files in this lake's own temp folder (LOAD-MEMORY), read in one snapshot below
            tmp = f"{con.execute("SELECT current_setting('temp_directory')").fetchone()[0]}/api-{uuid.uuid4().hex}"
            inc = src.api.incremental
            since = _since(con.cursor(), src.table, inc.field) if same and inc else None  # IN-6: past the stored max
            raw_sql, append, truncated = _api(con, src, loc, (headers, query, hide), tmp, since)
        if src.database:  # hostaddr='' stops libpq taking PGHOSTADDR from the env and connecting there (D-27)
            con.execute(f"ATTACH '{_lit("hostaddr=''") if kind == "postgres" else ""}' AS {name} (TYPE {kind}, SECRET {name}, READ_ONLY)")
            have = {r[0]: r[1] for r in con.execute(f"DESCRIBE {raw_sql}").fetchall()}
            if enums := {c: "VARCHAR[]" if t.endswith("[]") else "VARCHAR" for c, t in have.items() if t.startswith("ENUM(")}:
                # DuckLake has no ENUM type (a Postgres status column, often), so it refused the whole load: its labels as text
                raw_sql = raw_sql.replace("SELECT *", f"SELECT * REPLACE ({', '.join(f'CAST({_q(c)} AS {t}) AS {_q(c)}' for c, t in enums.items())})", 1)
                have |= enums
            col, bad = src.database.incremental, None
            if col:  # principle 7: a hand-set column that isn't there is an error, or (gone since) a visible full reload
                if not re.fullmatch(ORDERABLE, have.get(col, "")):
                    bad = f"incremental column {col} {f'is {have[col]}, not orderable,' if col in have else 'missing'} in {src.database.table}"
                if same:  # posted once per change (D-10), and once more when the column is back
                    notify_change("refresh", src.project, f"Incremental refresh of '{src.table}': ", bad and bad + "; full reload.",
                                  "appends again.", src.table)
                elif bad:
                    sid = f"sources/{steps[0].source if steps else src.table}.json"
                    raise ActionFailed(400, f"{sid}: $.database.incremental: {bad}.", errors=[{
                        "file": sid, "path": "$.database.incremental", "error": bad + ".",
                        "fix": f"Name a numeric, date/time or text column of {src.database.table}, or remove `incremental`."}])
            if probe:
                return _db_rows(con, kind, name, m[4], schema, table)
            # IN-6: append past the monotonic column, if there is one
            if same and not bad and (col := col or _monotonic(con, kind, name, m[4], schema, table)):
                append = lambda cur: _append(cur, raw_sql, src.table, col)
        if probe:  # M3.2: what a full load copies (`_big`); nothing loaded
            return None if src.api else _file_rows(con, fmt, loc)
        if (box := RUN.get()) and box["table"] == src.table:  # a load job reports this load's progress
            hook = functools.partial(_watched, box, hook)
        once = not (steps or declared or src.database or src.api)  # M4.2: files with no steps are stored once (no raw copy)
        snap = raw_sql and storage.refresh(con, src.table, raw_sql, author=author, append=append, hook=hook, once=once,
                                           model_sql=lambda cur: _model_sql(cur, src.project, src.table, declared,
                                                                            "model" if once else "raw"))
    except duckdb.OutOfMemoryException:  # actions.run says so (503); the worker's `refresh` logs it like any load error
        raise
    except (duckdb.Error, pa.ArrowException) as e:  # T-03.5: DuckDB's text holds server paths and SQL; server log only
        logging.getLogger(__name__).warning("Loading %s into %s/%s failed: %s", src.location, src.project, src.table,
                                            credentials.scrub(str(e), used))
        raise ActionFailed(400, f"Loading '{src.location}' failed ({type(e).__name__}); the details are in the server log.") from None
    except ValueError as e:  # the table's steps don't compile (TR-1): our message about the user's own steps
        raise ActionFailed(400, f"Loading '{src.location}' failed: {credentials.scrub(str(e), used)}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        if src.database:
            con.execute(f"DETACH DATABASE IF EXISTS {name}")
        if opts:
            con.execute(f"DROP SECRET IF EXISTS {name}")
    if src.api:  # API-SRC.2: a load cut off at max_pages says so once (D-10), and once when it is whole again
        notify_change("refresh", src.project, f"API source of table '{src.table}': ", truncated and (
            f"the load stopped at max_pages ({src.api.pagination.max_pages}) with pages left, so the table is partial."),
            "loads every page again.", src.table)
    return _landed(conn, src.project, src.table, author, fingerprint, snap)


def _build(conn, project: str, table: str, author: str, unless: str | None = None,
           declared: list[dict] | None = None) -> int | None:
    """M4.2: model.<table> from the model.<t> its steps start from (`transform.Source.table`), in one snapshot: no
    source read, no raw.<table> (gone if it had one). Its fingerprint is its steps (an undo or edit rebuilds it); what
    it reads changing forces it (`refresh`). Skipped (None) when the fingerprint equals `unless`."""
    _lock(conn, project, table)
    steps = transform.tables(project)[table]
    fingerprint = json.dumps({"steps": [s.model_dump(mode="json") for s in steps]}, sort_keys=True)
    if unless is not None and fingerprint == unless:
        return None
    if CHECK.get():  # M4.1: as `_load`'s
        raise Due
    box = RUN.get()  # a load job reports this build's progress
    try:
        snap = storage.refresh(storage.lake(project), table, None, author=author,
                               hook=functools.partial(_watched, box, None) if box and box["table"] == table else None,
                               model_sql=lambda cur: _model_sql(cur, project, table, declared))
    except duckdb.OutOfMemoryException:
        raise
    except duckdb.Error as e:  # T-03.5: server log only
        logging.getLogger(__name__).warning("Building %s/%s from table %s failed: %s", project, table, steps[0].table, e)
        raise ActionFailed(400, f"Building '{table}' from table '{steps[0].table}' failed ({type(e).__name__}); the details"
                                " are in the server log. Is that table loaded?") from None
    except ValueError as e:  # the steps don't compile (TR-1)
        raise ActionFailed(400, f"Building '{table}' failed: {e}")
    return _landed(conn, project, table, author, fingerprint, snap)


def _landed(conn, project: str, table: str, author: str, fingerprint: str, snap: int | None) -> int | None:
    """A load or build is in: its fingerprint, then what follows a new snapshot of `table`."""
    if not (git._root() / project / ".git").is_dir():  # deleted while it loaded (the worker's IN-6): no row brings it back
        raise ActionFailed(404, f"Project '{project}' does not exist.")
    conn.execute(
        "INSERT INTO sources (project, table_name, fingerprint) VALUES (%s, %s, %s) ON CONFLICT (project, table_name)"
        " DO UPDATE SET fingerprint = excluded.fingerprint",
        [project, table, fingerprint],
    )
    notify_change("refresh", project, f"Refreshing table '{table}': ", None, "works again.", table)  # D-10 recovery
    if snap is None:  # incremental, nothing new: a successful load for IN-8 freshness, no snapshot, not metered
        quality.capture(project, table)
        return None
    usage.record(None, "refresh", project)  # SC-11: one committed load (own connection: the snapshot is committed)
    model.extend_calendar(project, table, author)  # IN-5: new dates get calendar rows
    writeback.review(project, table)  # WB-6: the refresh may orphan edits or conflict with them
    flows.after_refresh(project, table, snap)  # FL-2
    ml.after_refresh(project, table, snap)  # ML-6: score the models reading it
    quality.capture(project, table)  # IN-8: this load's stats (never raises)
    return snap


def _reader(fmt: str, loc: str, src: Source) -> str:
    """The raw SELECT of a file source. IN-4 Delta/Iceberg: their log/manifests name the data files and may name any
    file (absolute or ../ paths, measured), so a data file outside the table's location fails the load before commit."""
    if fmt == "sqlite":  # the table name is identifier-validated (`SQLite.table`)
        return f"SELECT * FROM sqlite_scan('{_lit(loc)}', '{src.sqlite.table}')"
    if fmt not in FOLDERS.values():
        given = src.csv if fmt == "csv" else src.xlsx if fmt == "xlsx" else None
        csv = given.model_dump(exclude_none=True) if given else {}  # whitelisted values only (`Csv`, `Xlsx`)
        opts = "".join(f", {'delim' if k == 'delimiter' else k} = {str(v).lower() if isinstance(v, bool) else f"'{_lit(v)}'"}"
                       for k, v in csv.items())
        if fmt == "xlsx":  # read_xlsx stops at the first empty row: a blank separator row would cut the rest off unseen
            return f"SELECT * FROM read_xlsx('{_lit(loc)}'{opts}, stop_at_empty = false) WHERE concat_ws('', *COLUMNS(*)) <> ''"
        return f"SELECT * FROM {READERS[fmt]}('{_lit(loc)}'{opts})"
    moved = ", allow_moved_paths = true" if fmt == "iceberg" else ""  # a copied table reads under its new location
    return (f"SELECT * EXCLUDE (__abi_file) FROM {READERS[fmt]}('{_lit(loc)}'{moved}, filename = '__abi_file')"
            f" WHERE CASE WHEN starts_with(__abi_file, '{_lit(loc.rstrip('/'))}/') AND NOT contains(__abi_file, '/..')"
            f" THEN true ELSE error('the {fmt} table names a data file outside its location') END")


def _arrow(path: str) -> pa.RecordBatchReader:
    """IN-4 Arrow IPC file (Feather v2, any compression) or IPC stream, batch by batch from a memory map (D-17)."""
    try:
        f = pa.ipc.open_file(pa.memory_map(path))
    except pa.ArrowInvalid:
        return pa.ipc.open_stream(pa.memory_map(path))
    return pa.RecordBatchReader.from_batches(f.schema, (f.get_batch(i) for i in range(f.num_record_batches)))


def _file_rows(con, fmt: str, loc: str) -> int:
    """M3.2: the rows a load of these files copies: Parquet's own count from its footers (a Delta or Iceberg table's
    data files too, older versions' included), else their bytes over ROW_BYTES."""
    if fmt in ("parquet", "delta", "iceberg"):
        files = loc if fmt == "parquet" else loc.rstrip("/") + "/**/*.parquet"
        return con.cursor().execute(f"SELECT sum(num_rows) FROM parquet_file_metadata('{_lit(files)}')").fetchone()[0] or 0
    return (con.cursor().execute(f"SELECT sum(size) FROM read_blob('{_lit(loc)}')").fetchone()[0] or 0) // ROW_BYTES


def _db_rows(con, kind: str, name: str, database: str, schema: str, table: str) -> int:
    """M3.2: the source table's rows as its database's statistics count them (Postgres reltuples, MySQL table_rows), 0
    before it has any. The names are identifier-validated, as in `_monotonic`."""
    sql = (f"SELECT c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
           f" WHERE n.nspname = '{schema or 'public'}' AND c.relname = '{table}'" if kind == "postgres"
           else f"SELECT table_rows FROM information_schema.tables WHERE table_schema = '{schema or database}' AND table_name = '{table}'")
    row = con.execute(f"SELECT * FROM {kind}_query('{name}', '{_lit(sql)}')").fetchone()
    return max(int(row[0] or 0), 0) if row else 0


def _monotonic(con, kind: str, name: str, database: str, schema: str, table: str) -> str | None:
    """IN-6: the source table's auto-increment column from its own catalog (Postgres identity/serial, MySQL AUTO_INCREMENT).
    The names are identifier-validated (`DB_LOCATION`, `Database.table`), so they can't break out of the quotes."""
    where = (f"table_schema = '{schema or 'public'}' AND (is_identity = 'YES' OR column_default LIKE 'nextval(%')"
             if kind == "postgres" else f"table_schema = '{schema or database}' AND extra LIKE '%auto_increment%'")
    sql = (f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}' AND {where}"
           " ORDER BY ordinal_position LIMIT 1")
    row = con.execute(f"SELECT * FROM {kind}_query('{name}', '{_lit(sql)}')").fetchone()
    return row and row[0]


def _append(cur, src_sql: str, table: str, col: str) -> str | None:
    """IN-6 incremental: a SELECT of the source rows with `col` past raw.<table>'s max, or None for a full reload (no
    stored rows, a changed column set, or a source whose max fell below the stored max: truncated or reset).
    Runs inside the load's transaction, so a retried commit recomputes it."""
    # ponytail: append-only; rows updated or deleted in place at the source, or a lower key committed after a higher one
    # was loaded, stay as loaded until a full reload (a
    # definition change or the fallbacks below); add a periodic full reload or CDC if sources start editing old rows
    have = _raw_columns(cur, table)
    if col not in dict(have) or have != [r[:2] for r in cur.execute(f"DESCRIBE {src_sql}").fetchall()]:
        return None
    c = _q(col)
    top, typ = cur.execute(f"SELECT max({c})::varchar, typeof(max({c})) FROM lake.raw.{_q(table)}").fetchone()
    if top is None:
        return None
    bound = f"'{_lit(top)}'::{typ}"  # a constant, so the scanner pushes the filter to the source
    if not cur.execute(f"SELECT 1 FROM ({src_sql}) WHERE {c} >= {bound} LIMIT 1").fetchone():
        return None
    return f"{src_sql} WHERE {c} > {bound}"


def _raw_columns(cur, table: str) -> list[tuple[str, str]]:
    return cur.execute("SELECT column_name, data_type FROM duckdb_columns() WHERE database_name = 'lake'"
                       " AND schema_name = 'raw' AND table_name = ? ORDER BY column_index", [table]).fetchall()


def _due(last: str, hours: int) -> bool:
    """API-SRC.2 `every_hours`: the last load (ISO time from the fingerprint) is at least `hours` ago."""
    try:
        return datetime.now(UTC) - datetime.fromisoformat(last) >= timedelta(hours=hours)
    except (ValueError, TypeError):  # a print without a time (it was daily): due
        return True


def _since(cur, table: str, col: str) -> tuple[str, str] | None:
    """IN-6 incremental API: raw.<table>'s max of `col` as (the request parameter, a typed SQL constant), or None
    (no such column or no rows: a full load). Times go out as ISO 8601 to the second, UTC with Z for TIMESTAMPTZ;
    cut-off fractions only re-fetch records the `> constant` filter drops."""
    if col not in dict(_raw_columns(cur, table)):
        return None
    c = _q(col)
    top, typ = cur.execute(f"SELECT max({c})::varchar, typeof(max({c})) FROM lake.raw.{_q(table)}").fetchone()
    if top is None:
        return None
    bound = f"'{_lit(top)}'::{typ}"
    iso = ("strftime(timezone('UTC', {b}), '%Y-%m-%dT%H:%M:%SZ')" if typ == "TIMESTAMP WITH TIME ZONE"
           else "strftime({b}, '%Y-%m-%dT%H:%M:%S')" if typ.startswith("TIMESTAMP") else None)
    return (cur.execute(f"SELECT {iso.format(b=bound)}").fetchone()[0] if iso else top), bound


def _api(con, src: Source, loc: str, request: tuple, tmp: str, since: tuple | None) -> tuple[str | None, object, bool]:
    """API-SRC: fetch the pages into `tmp` and return (the raw SELECT, or None when an incremental load found nothing
    new; `storage.refresh`'s append callback or None; whether it stopped at max_pages). With `since` (IN-6) only the
    records past it are fetched and appended; if their columns differ from the stored ones, everything is fetched again."""
    headers, query, hide = request
    inc = src.api.incremental
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    files, truncated = fetch.pages(src, loc, headers, query | ({inc.param: since[0]} if since else {}), hide, tmp)
    if os.path.getsize(files[0]) == 0:
        if since:
            return None, None, truncated
        raise ActionFailed(400, f"API source '{src.table}' returned no records.")
    reader = (_reader("csv", f"{tmp}/*.csv", src) if src.api.format == "csv"
              else f"SELECT * FROM read_json('{_lit(files[0])}', format = 'newline_delimited')")
    casts = ", ".join(f"CAST({_q(c)} AS {col.type}) AS {_q(c)}" for c, col in src.api.columns.items())
    raw_sql = reader.replace("SELECT *", f"SELECT * REPLACE ({casts})", 1) if casts else reader
    if not since:
        return raw_sql, None, truncated
    cur = con.cursor()
    stored = _raw_columns(cur, src.table)
    if {c for c, _ in stored} != {r[0] for r in cur.execute(f"DESCRIBE {raw_sql}").fetchall()}:
        return _api(con, src, loc, request, tmp, None)
    new = (f"SELECT * FROM (SELECT {', '.join(f'CAST({_q(c)} AS {t}) AS {_q(c)}' for c, t in stored)}"
           f" FROM ({raw_sql})) WHERE {_q(inc.field)} > {since[1]}")
    return raw_sql, lambda _cur: new, truncated


def _model_sql(cur, project: str, table: str, declared: list[dict] | None = None, layer: str = "raw") -> str:
    """model.<table>: the IN-5 typed raw (text dates), through the table's steps (TR-2) when it has any.
    `declared` (PC-15 mapping onto an input table; set_steps added a final set_type to them): the steps before it must
    output exactly these column names, with every value castable to its declared type; else 400, and the load's
    transaction rolls back."""
    steps = transform.tables(project).get(table)
    typed = None if steps and steps[0].table else detect.typed_sql(cur, table, layer=layer)  # M4.2: that table's model is typed
    if declared:
        sql = transform.compile(table, steps[:-1], source_sql=typed)
        have, want = [r[0] for r in cur.execute(f"DESCRIBE {sql}").fetchall()], [c["name"] for c in declared]
        if set(have) != set(want):
            raise ActionFailed(400, f"The steps must output exactly the declared columns of '{table}' ({', '.join(want)});"
                                    f" missing: {sorted(set(want) - set(have))}, extra: {sorted(set(have) - set(want))}."
                                    " Add select/rename steps.")
        bad = cur.execute("SELECT " + ", ".join(
            f"count(*) FILTER ({_q(c['name'])} IS NOT NULL AND TRY_CAST({_q(c['name'])} AS {transform._type(c['type'])}) IS NULL)"
            for c in declared) + f" FROM ({sql})").fetchone()
        if bad := [f"{c['name']} ({n} value(s) not {c['type']})" for c, n in zip(declared, bad) if n]:
            raise ActionFailed(400, f"Values don't cast to the declared types of '{table}': {', '.join(bad)}. Fix them with"
                                    " steps (e.g. filter or add_column).")
    return transform.compile(table, steps, source_sql=typed) if steps else typed


def refresh_changed(only: str | None = None, check: bool = False) -> dict[str, frozenset[str]]:
    """IN-6: reload every table whose files changed or whose database source last loaded before today (UTC; incremental
    past a monotonic column when there is one), and what reads it (TR-4); `only`: that project's. M4.1: the default
    worker checks every minute (`check`: nothing loads; each project's tables that would) and the heavy worker loads."""
    due, held = {}, CHECK.set(check)
    try:
        with db.connect() as conn:
            projects = {r["project"] for r in conn.execute("SELECT DISTINCT project FROM sources")}
            for p in sorted(projects | {g.parent.name for g in git._root().glob("*/.git")}):  # D-9: never loaded yet too
                if not (git._root() / p / ".git").is_dir() or only and p != only:  # deleted since: a load would bring it back
                    continue
                error = None
                try:
                    _migrate(conn, p, transform.tables(p))
                    due[p] = frozenset(t for t, e in refresh(conn, p, None, "worker").items() if e is DUE)
                except (ValueError, ActionFailed) as e:  # invalid tables/*.json, a cycle, legacy rows without a repo
                    error = f"{e} The previous snapshots stay live."
                notify_change("refresh", p, "Refreshing tables: ", error, "works again.")  # D-10
    finally:
        CHECK.reset(held)
    return due


router = APIRouter(prefix="/api")


# IN-1 file uploads are IN-7 sessions (the single-request PUT wrote before its role check: H-01.2-fix-2).
# IN-7 resumable chunked upload, tus 1.0 core semantics (work/validation/D-05.md): POST starts, HEAD tells the offset,
# PATCH appends at it. A session is files/uploads/<id>/ with `.part` (the bytes) and `.upload.json` (who, where, size);
# once loaded, the file under its own name and the record as `.owner.json`.
UPLOAD_MAX = 500 * 1024**3  # §9 Scale: hundreds of GB; the free-disk check is the real bound
CHUNK_MAX = 256 * 1024**2  # one PATCH body
STALE = 24 * 3600  # a session with no write for this long is deleted when another upload starts
UID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
OCTETS = "application/offset+octet-stream"


def _fail(status: int, message: str, **headers) -> HTTPException:
    return HTTPException(status, {"message": message}, headers=headers or None)


@router.post("/uploads/{project}/{table}/{filename}", status_code=201)
def start_upload(
    project: Annotated[str, Path(pattern=storage.PROJECT)],
    table: Annotated[str, Path(pattern=TABLE)],
    filename: Annotated[str, Path(pattern=r"^\w[\w .-]{0,199}$")],  # no '/', no leading dot, fits a file name
    length: Annotated[int, Header(alias="Upload-Length", ge=1, le=UPLOAD_MAX)],
    user: Annotated[User, Depends(current_user)],
    response: Response,
):
    """IN-7: starts a resumable upload of `length` bytes (Editor, as add_data); returns its id and Location."""
    with db.connect() as conn:
        have = role_in(conn, user, project)
        refused = have is None or have < Role.editor  # checked now, so a Viewer can't fill the disk before add_data refuses
        message = f"Uploading data needs the editor role in project '{project}'."
        if not refused and (why := production_refusal("add_data", project)):  # D-56: the upload ends in add_data
            refused, message = True, why
        if refused:  # SC-6; raised after the block, which would roll the audit row back
            audit(conn, user, "rest", "upload", project, {"table": table, "filename": filename, "length": length}, 403, message)
    if refused:
        raise _fail(403, message)
    root = files_root() / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    for meta in root.glob("*/.upload.json"):  # abandoned sessions; `.part` exists while `.upload.json` does
        with contextlib.suppress(OSError):  # finished meanwhile, or the folder holds a loaded file (crash after add_data)
            if time.time() - (meta.parent / ".part").stat().st_mtime > STALE:
                meta.unlink()
                (meta.parent / ".part").unlink()
                meta.parent.rmdir()  # only if empty: never a source's file
    if shutil.disk_usage(root).free < length:
        raise _fail(507, f"Not enough disk space for {length} bytes.")
    uid = uuid.uuid4().hex
    (root / uid).mkdir()
    (root / uid / ".part").touch()
    (root / uid / ".upload.json").write_text(json.dumps(
        {"user": user.id, "project": project, "table": table, "filename": filename, "length": length}))
    response.headers.update({"Location": f"/api/uploads/{uid}", "Upload-Offset": "0"})
    return {"id": uid}


def _session(uid: str, user: User) -> tuple[dict, FsPath]:
    folder = files_root() / "uploads" / uid
    try:
        meta = json.loads((folder / ".upload.json").read_text())
    except FileNotFoundError:
        meta = None
    if not meta or meta["user"] != user.id:  # someone else's upload doesn't exist for you
        raise _fail(404, "No such upload.")
    return meta, folder


@router.head("/uploads/{uid}")
def upload_offset(uid: UID, user: Annotated[User, Depends(current_user)]):
    """IN-7: the bytes already stored (durable), where a resumed PATCH starts."""
    meta, folder = _session(uid, user)
    return Response(status_code=204, headers={"Upload-Offset": str((folder / ".part").stat().st_size),
                                              "Upload-Length": str(meta["length"]), "Cache-Control": "no-store"})


@router.patch("/uploads/{uid}")
async def upload_chunk(
    uid: UID,
    offset: Annotated[int, Header(alias="Upload-Offset", ge=0)],
    content_type: Annotated[str, Header()],
    request: Request,
    user: Annotated[User, Depends(current_user)],
    csv: str | None = None,  # query parameter: E-16.1 CSV options as JSON, for the load
    xlsx: str | None = None,  # query parameter: Excel options as JSON ({"sheet": …}), for the load
):
    """IN-7: appends the body at `offset`. When the file is complete, loads it with add_data (with `csv` or `xlsx`,
    UI-2); if that fails the bytes stay, and an empty PATCH at the final offset retries the load, also with other options."""
    if content_type != OCTETS:
        raise _fail(415, f"Content-Type must be {OCTETS}.")
    try:  # add_data validates them (422/400)
        options = {k: json.loads(v) for k, v in (("csv", csv), ("xlsx", xlsx)) if v is not None}
    except json.JSONDecodeError:
        raise _fail(422, "csv and xlsx must be JSON objects of options.")
    meta, folder = _session(uid, user)
    with (folder / ".part").open("r+b") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held through the load: one writer per upload
        except BlockingIOError:
            raise _fail(409, "Another request is writing this upload; retry after it ends.")
        size = f.seek(0, os.SEEK_END)
        if offset != size:
            raise _fail(409, f"The upload is at byte {size}, not {offset}.", **{"Upload-Offset": str(size)})
        room = min(CHUNK_MAX, meta["length"] - size)
        too_big = _fail(413, f"At most {room} bytes fit in this request.", **{"Upload-Offset": str(size)})
        if int(request.headers.get("content-length") or 0) > room:
            raise too_big
        try:
            async for chunk in request.stream():  # streamed to disk, never held whole (D-17)
                if f.tell() + len(chunk) > size + room:
                    f.truncate(size)
                    raise too_big
                f.write(chunk)
        finally:  # a dropped connection keeps what arrived: the resume starts after it
            f.flush()
            os.fsync(f.fileno())
        if f.tell() < meta["length"]:
            return Response(status_code=204, headers={"Upload-Offset": str(f.tell())})
        final = folder / meta["filename"]
        final.unlink(missing_ok=True)  # left by a crash during an earlier load
        os.link(folder / ".part", final)  # the real name (format from the extension), no copy
        payload = {"project": meta["project"], "table": meta["table"], "location": str(final.relative_to(files_root())), **options}
        try:
            result = await run_in_threadpool(actions.run, "add_data", payload, user, "rest")
        except ActionFailed as e:
            final.unlink()
            raise HTTPException(e.status, e.body)
    if result.get("status") != "queued":  # M3.2: a big file's load job settles its session (`_settled`)
        _settled(payload["location"], True)
    return result


def _settled(location: str, loaded: bool) -> None:
    """The end of an upload's load (IN-7): its record stays as .owner.json, whose project the file is (`readable`), after
    `.part` goes (in that order, so `.part` exists whenever `.upload.json` does); a failed load (M3.2: a job's, as
    upload_chunk's) takes the file's name away and leaves the session for a retry or the stale sweep. Only a session
    still open counts: a location loaded before is another source's file."""
    folder = files_root() / FsPath(location).parent
    if not re.fullmatch(UPLOADED, location) or not (folder / ".upload.json").exists():
        return
    if loaded:
        os.replace(folder / ".upload.json", folder / ".owner.json")
        (folder / ".part").unlink()
    else:
        (files_root() / location).unlink(missing_ok=True)


NAMED = re.compile(r"uploads/([0-9a-f]{32})(?![0-9a-f])")  # M3.5: an upload a file names (a location, a pattern's start)
PATTERN = re.compile(r'"location"\s*:\s*"([^"]*[*?[][^"]*)"')  # a location with a wildcard, read through DuckDB's glob


def _uploads(texts) -> set[str]:
    """M3.5: the uploads these project files or load payloads name, also through a server-path glob."""
    out, patterns = set(), set()
    for t in texts:
        out.update(NAMED.findall(t))
        patterns.update(p for p in PATTERN.findall(t) if "://" not in p)
    up = files_root() / "uploads"
    with duckdb.connect() as con:
        for p in patterns:
            with contextlib.suppress(duckdb.Error):  # a pattern DuckDB can't expand loads nothing
                for (f,) in con.execute("SELECT file FROM glob(?)", [str(files_root() / p)]).fetchall():
                    if (f := FsPath(f).resolve()).is_relative_to(up) and f != up:
                        out.add(f.relative_to(up).parts[0])
    return out


def retain(folder: FsPath, gone: str) -> None:
    """M3.5, as delete_project removes a project and its Git history: the uploads it named, now or in any version (what
    a restore loads), wait for its lake `gone` to be purged, listed in uploads/<gone>.json."""
    root = files_root() / "uploads"
    try:
        ids = _uploads(git.texts(folder))
    except Exception as e:  # noqa: BLE001 - a repo it can't read still deletes; with no list, its lake holds back every unlisted upload
        logging.getLogger(__name__).error("Listing the uploads of %s failed: %s", folder.name, e)
        return
    root.mkdir(parents=True, exist_ok=True)
    (root / f".{gone}").write_text(json.dumps(sorted(ids)))
    os.replace(root / f".{gone}", root / f"{gone}.json")  # whole: purge_uploads may be reading it


def purge_uploads(conn, now: float | None = None) -> None:
    """M3.5, daily after storage.purge_deleted: delete each upload no live project reads once every deleted project that
    named it is past its lake's retention (30 days after its deletion). Never one that a live project's files name,
    now or in their history (another project, a published copy), a queued or running load reads, an upload in progress
    holds, or that changed in the last day. 1.1 kept no record of an upload's project: the files decide, not the record."""
    now, root = now or time.time(), files_root() / "uploads"
    if not root.is_dir():
        return
    cutoff = now - storage.ARCHIVE_DAYS * 86400
    # live projects first: one deleted meanwhile wrote its list before its folder went
    keep = _uploads(t for g in git._root().glob("*/.git") for t in git.texts(g.parent))
    if conn.execute("SELECT to_regclass('load_runs') AS t").fetchone()["t"]:  # M3.2's background loads, when in
        keep |= _uploads(r["p"] for r in conn.execute("SELECT payload::text AS p FROM load_runs WHERE payload IS NOT NULL"
                                                      " AND status IN ('queued', 'running')"))
    lists = {f: (int(f.name.split("_")[2]), json.loads(f.read_text())) for f in root.glob(f"{storage.DELETED}*.json")}
    keep |= {u for t, ids in lists.values() if t >= cutoff for u in ids}
    listed = {u for _, ids in lists.values() for u in ids}
    # a project deleted before 1.2 left no list: while its lake is kept, an upload no list names may be one of its
    unknown = any(int(r["nspname"].split("_")[2]) >= cutoff and not (root / f"{r['nspname']}.json").exists() for r in
                  conn.execute("SELECT nspname FROM pg_namespace WHERE starts_with(nspname, %s)", [storage.DELETED]))
    for d in root.iterdir():
        if re.fullmatch(r"[0-9a-f]{32}", d.name) and d.name not in keep and (d.name in listed or not unknown) \
                and not (d / ".upload.json").exists() and now - d.stat().st_mtime > STALE:
            shutil.rmtree(d, ignore_errors=True)
    for f, (t, _) in lists.items():
        if t < cutoff:
            f.unlink()


def _host(kind: str, loc: str, opts: dict) -> str | None:
    """The host a load's requests go to (D-2), from the resolved location; None for a server path or an unclear URL.

    Only plain `scheme://host[:port]/…` counts: userinfo, backslashes, IPv6 or no `/` after the host give None
    (refused with secrets), so a parser difference with DuckDB can't send a secret elsewhere, and the secret's
    SCOPE (the location prefix) can't match another host's URL."""
    host = r"([A-Za-z0-9.-]+)(?::\d+)?"
    m = re.match(rf"\w+://{host}/", loc)
    if not m:  # includes every server path (no "://")
        return None
    if kind != "s3":
        return m[1].lower()
    if "?" in loc:  # DuckDB reads ?s3_endpoint=, ?s3_url_style=, … from an s3:// URL and lets them override the options
        return None
    # DuckDB 1.5.6 without ENDPOINT uses s3.<region, default us-east-1>.amazonaws.com (checked via a proxy)
    # an endpoint with a scheme is refused: DuckDB keeps it as part of the host ("x://a.com" goes to host "x")
    ep = re.fullmatch(host, opts.get("ENDPOINT", f"s3.{opts.get('REGION', 'us-east-1')}.amazonaws.com"))
    if not ep:
        return None
    return ep[1].lower() if opts.get("URL_STYLE") == "path" else f"{m[1]}.{ep[1]}".lower()  # vhost: bucket in the host


def hosts(doc: dict) -> set[str]:
    """H-01.5-fix-8: the outside hosts a load of source definition `doc` contacts (FL-6 targets `source:<host>`), read
    from its unresolved text (a ${secret:NAME} host counts as written): the location's host (S3 as `_host` derives it,
    database and ClickHouse hosts too) and an OAuth token endpoint's. None for a server path."""
    loc = doc.get("location") or ""
    if "://" not in loc:
        return set()
    if m := re.fullmatch(DB_LOCATION, loc) or re.fullmatch(live.LOCATION, loc):
        out = {m[2].lower()}
    else:
        kind = loc.split("://", 1)[0].lower()
        s3 = {k.upper(): v for k, v in (doc.get("s3") or {}).items() if k in ("region", "endpoint", "url_style")}
        out = {_host(kind, loc, s3) or re.split(r"[/?#]", loc.split("://", 1)[1])[0].lower()}
    if url := ((doc.get("api") or {}).get("auth") or {}).get("token_url"):
        out.add(re.split(r"[/?#]", url.split("://", 1)[-1])[0].lower())
    return {"source:" + h for h in out}


def _sql(v) -> str:
    if isinstance(v, dict):
        return "MAP {" + ", ".join(f"'{_lit(k)}': '{_lit(x)}'" for k, x in v.items()) + "}"
    return f"'{_lit(v)}'"
