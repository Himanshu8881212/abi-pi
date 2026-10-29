"""SC-11 usage metering (E-07.1, work/validation/E-07.md decisions 1-9): one content-free ledger row per query, LLM
completion, refresh, send and export, and one storage sample per project per UTC day. Counts only: never query text,
filter values, recipients or message text. A metering failure never fails the work it meters (logged instead)."""

import csv
import datetime
import io
import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from abi import db, security
from abi.actions import ActionFailed, action, current_user, run
from abi.project import git
from abi.security import Role, User

log = logging.getLogger(__name__)
# E-05 decision 7: a project row bills the project's tenant (none = platform), a workspace row the user's tenant
_TENANT = ("CASE WHEN %(project)s::text IS NULL THEN (SELECT tenant FROM users WHERE id = %(user)s)"
           " ELSE (SELECT tenant FROM project_tenants WHERE project = %(project)s) END")


def record(conn, kind: str, project: str | None, user_id: int | None = None, **counts) -> None:
    """One ledger row; `counts`: rows_scanned, prompt_tokens, completion_tokens, cost, effort, reasoning_tokens,
    cached_tokens. `conn`
    None: its own connection
    (a send or refresh that happened stays counted even if the caller's transaction rolls back)."""
    try:
        if conn is None:
            with db.connect() as own:
                return record(own, kind, project, user_id, **counts)
        cols = ", ".join(counts)
        with conn.transaction():  # a savepoint inside the caller's transaction: a failure here leaves it usable
            conn.execute(f"INSERT INTO usage (tenant, project, user_id, kind{', ' + cols if cols else ''})"
                         f" VALUES ({_TENANT}, %(project)s, %(user)s, %(kind)s{''.join(f', %({c})s' for c in counts)})",
                         {"project": project, "user": user_id, "kind": kind, **counts})
    except Exception as e:  # noqa: BLE001 - metering must never fail the action it meters
        log.error("SC-11: usage row (%s, %s) not written: %s", kind, project, type(e).__name__)


def _bytes(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file() and not f.is_symlink()) if folder.is_dir() else 0


def sample_storage() -> None:
    """Decision 8: today's (UTC) sample for each project that has none yet: lake bytes from the DuckLake catalog (every
    data and delete file still on disk, time travel included) and the repo + artifacts folders. Run every minute by
    the worker, so a missed day is caught up on the first tick after downtime."""
    from abi import storage  # not at top: storage imports abi.security
    from abi.tools import builtin

    with db.connect() as conn:
        done = {r["project"] for r in conn.execute("SELECT project FROM usage_storage WHERE day = (now() AT TIME ZONE 'UTC')::date")}
    for p in git.projects():
        if p in done:
            continue
        try:
            meta = f"{storage._META}.{p}"
            [(lake,)] = storage.lake(p).cursor().execute(
                f"SELECT (SELECT coalesce(sum(file_size_bytes), 0) FROM {meta}.ducklake_data_file)"
                f" + (SELECT coalesce(sum(file_size_bytes), 0) FROM {meta}.ducklake_delete_file)").fetchall()
            files = _bytes(git.path(p)) + _bytes(builtin.artifacts_root() / p)
            with db.connect() as conn:
                conn.execute(f"INSERT INTO usage_storage (day, tenant, project, lake_bytes, file_bytes)"
                             f" VALUES ((now() AT TIME ZONE 'UTC')::date, {_TENANT}, %(project)s, %(lake)s, %(files)s)"
                             " ON CONFLICT DO NOTHING", {"project": p, "user": None, "lake": int(lake), "files": files})
        except Exception as e:  # noqa: BLE001 - one broken project must not stop the others' samples
            log.error("SC-11: storage sample of %s failed: %s", p, type(e).__name__)


COLUMNS = ["tenant", "project", "queries", "rows_scanned", "llm_calls", "prompt_tokens", "cached_tokens", "completion_tokens",
           "llm_cost", "refreshes", "sends", "exports", "avg_lake_bytes", "avg_file_bytes", "latest_bytes"]
_FILTER = "(%(every)s OR tenant IS NOT DISTINCT FROM %(tenant)s)"
_UTC = "%({})s::timestamp AT TIME ZONE 'UTC'"  # a date as its UTC midnight, whatever the session TimeZone
# Principle 7: every number is this aggregate over the ledger. Storage: the period's daily samples averaged, plus the latest.
_SQL = f"""SELECT tenant, project,
  count(*) FILTER (WHERE kind = 'query') AS queries, coalesce(sum(rows_scanned), 0) AS rows_scanned,
  count(*) FILTER (WHERE kind = 'llm') AS llm_calls, coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
  coalesce(sum(cached_tokens), 0) AS cached_tokens,
  coalesce(sum(completion_tokens), 0) AS completion_tokens, sum(cost) AS llm_cost,
  count(*) FILTER (WHERE kind = 'refresh') AS refreshes, count(*) FILTER (WHERE kind = 'send') AS sends,
  count(*) FILTER (WHERE kind = 'export') AS exports,
  round(avg(lake_bytes))::bigint AS avg_lake_bytes, round(avg(file_bytes))::bigint AS avg_file_bytes,
  (array_agg(lake_bytes + file_bytes ORDER BY day DESC) FILTER (WHERE day IS NOT NULL))[1] AS latest_bytes
FROM (SELECT tenant, project, kind, rows_scanned, prompt_tokens, cached_tokens, completion_tokens, cost,
             NULL::date AS day, NULL::bigint AS lake_bytes, NULL::bigint AS file_bytes
      FROM usage WHERE at >= {_UTC.format("from")} AND at < {_UTC.format("to")} AND {_FILTER}
      UNION ALL
      SELECT tenant, project, NULL, NULL, NULL, NULL, NULL, NULL, day, lake_bytes, file_bytes
      FROM usage_storage WHERE day >= %(from)s AND day < %(to)s AND {_FILTER}) AS u
GROUP BY tenant, project ORDER BY tenant NULLS FIRST, project NULLS FIRST"""
EFFORT_COLUMNS = ["tenant", "project", "effort", "llm_calls", "prompt_tokens", "cached_tokens", "completion_tokens",
                  "reasoning_tokens"]
# D-71: LLM tokens per reasoning effort (effort null = none sent)
_EFFORTS = f"""SELECT tenant, project, effort, count(*) AS llm_calls, coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
  coalesce(sum(cached_tokens), 0) AS cached_tokens,
  coalesce(sum(completion_tokens), 0) AS completion_tokens, coalesce(sum(reasoning_tokens), 0) AS reasoning_tokens
FROM usage WHERE kind = 'llm' AND at >= {_UTC.format("from")} AND at < {_UTC.format("to")} AND {_FILTER}
GROUP BY tenant, project, effort ORDER BY tenant NULLS FIRST, project NULLS FIRST, effort NULLS FIRST"""


class Period(BaseModel):
    from_: datetime.date | None = Field(None, alias="from", description="first UTC day; default the current UTC month's first")
    to: datetime.date | None = Field(None, description="the UTC day after the last; default the next month's first")
    tenant: str | None = Field(None, pattern=git.ID, description="one tenant; a tenant admin always gets its own")
    by: Literal["project", "effort"] = Field("project", description="project: figures per tenant and project and a Total"
                                             " row; effort: LLM calls and tokens per reasoning effort")


@action(Role.owner, tenant_admin=True)
def usage(conn, user: User, args: Period) -> dict:
    """SC-11 usage per tenant and project for [from, to) in UTC (default this month): queries, rows scanned, LLM calls,
    tokens (cached prompt tokens apart, D-117) and estimated cost, refreshes, sends, exports and storage bytes, ending in a Total row; with `by` "effort",
    LLM calls and tokens per reasoning effort instead. A workspace owner sees every tenant and the platform (tenant
    null), a tenant admin only its own."""
    first = datetime.datetime.now(datetime.UTC).date().replace(day=1)
    start = args.from_ or first
    end = args.to or (start.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    if start >= end:
        raise ActionFailed(400, "'from' must be before 'to'.")
    tenant = args.tenant
    if not user.workspace_owner:  # a tenant admin (actions.run let only those in): its own tenant, never another
        own = security.tenant_of(conn, user)
        if tenant not in (None, own):
            raise ActionFailed(403, "A tenant admin sees only its own tenant's usage.")
        tenant = own
    params = {"from": start, "to": end, "tenant": tenant, "every": user.workspace_owner and tenant is None}
    period = {"from": start.isoformat(), "to": end.isoformat()}
    if args.by == "effort":
        return period | {"columns": EFFORT_COLUMNS, "rows": [[r[c] for c in EFFORT_COLUMNS] for r in conn.execute(_EFFORTS, params)]}
    out = [[r[c] for c in COLUMNS] for r in conn.execute(_SQL, params)]
    # the Total row (principle 7: summed here, not in the browser; in `rows` so AG-2 grounds it, LLM-2.8.3-fix-3); tenant
    # and project IDs are lower case, so "Total" names no real one; a cost total only where a price existed
    total = ["Total", "Total", *(None if all(r[i] is None for r in out) else sum(r[i] or 0 for r in out) for i in range(2, len(COLUMNS)))]
    return period | {"columns": COLUMNS, "rows": [*out, total]}


router = APIRouter(prefix="/api")


@router.get("/usage.csv")
def usage_csv(user: Annotated[User, Depends(current_user)], from_: Annotated[str | None, Query(alias="from")] = None,
              to: str | None = None, tenant: str | None = None,
              by: Annotated[Literal["project", "effort"], Query()] = "project") -> Response:
    """SC-11 billing export: the `usage` action's rows as CSV without its Total row (spreadsheets sum the column; a total
    line would count everything twice), with its visibility and audit row; `by=effort`: its LLM tokens per reasoning
    effort (D-71)."""
    from abi.tools.builtin import _cell  # not at top: builtin imports this module

    try:
        d = run("usage", {"from": from_, "to": to, "tenant": tenant, "by": by}, user, "rest")
    except ActionFailed as e:
        raise HTTPException(e.status, e.body) from None
    out = io.StringIO()
    rows = d["rows"] if by == "effort" else d["rows"][:-1]
    csv.writer(out).writerows([[_cell(x) for x in r] for r in [d["columns"], *rows]])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="usage-{"effort-" if by == "effort" else ""}{d["from"]}-{d["to"]}.csv"'})

