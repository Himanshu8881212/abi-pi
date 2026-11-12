"""Conditions and subscriptions. PRD SE-8, FL-1 (condition), AT-2, DECISIONS D-31.

A condition is `{query, measure, op, value}`: it holds when any result row's measure value satisfies `op value` (NULL
never). Code compares tool results (principle 7). ABI-PI (Monitor's checks): `other` compares with another measure of
the row (a target), `period` reads only the latest period with data, and `change` the % change from the period before,
all computed here, so an alert's figures are these rows. A subscription is a user's semantic query that the worker re-runs
every minute as that user; a changed result goes to the subscriber's private inbox (SE-8). Subscriptions are
runtime state in Postgres, not Git (§6.6).
"""

import hashlib
import json
import operator
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from abi import db, semantic, storage
from abi.actions import ActionFailed, action
from abi.security import Role, User, role_in
from abi.semantic import SemanticQuery

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "=": operator.eq, "!=": operator.ne}
YEAR = {"month": "year", "quarter": "year", "week": "week_year"}  # a period's year column: month 1 follows month 12


class Check(BaseModel):
    query: SemanticQuery
    measure: str = Field(description="a measure ID in query.measures")
    op: Literal[">", ">=", "<", "<=", "=", "!="]
    value: float
    other: str | None = Field(None, description="another measure ID in query.measures: holds where measure op other + value (a target; value 0)")
    period: str | None = Field(None, pattern=storage.COLUMN, description="a date-table column (day, week, month, quarter, year): only the latest period with data counts")
    skip_latest: bool = Field(False, description="with period: the period before the latest one (which may still be filling)")
    change: bool = Field(False, description="with period: `op value` compares the % change of measure from the period before (the total: no other, no dimensions)")


class Condition(Check, storage.ProjectRef):
    pass


def problem(c: dict) -> str | None:
    """Why a condition can't be checked (the import check and `evaluate` say the same), else None."""
    ms = c["query"]["measures"]
    if bad := next((x for x in (c["measure"], c.get("other")) if x and x not in ms), None):
        return f"Measure '{bad}' is not in the condition's query measures {ms}."
    if c.get("change") and not c.get("period"):
        return "A change compares periods: give `period`, a date-table column such as calendar.month."
    if c.get("change") and (c.get("other") or c["query"].get("dimensions")):
        return "A change compares the total of one measure: leave out `other` and the query's dimensions."
    return None


def periods(c: dict) -> tuple[dict, list[str]]:
    """The condition's query with its period first (its year column before a month, quarter or week), and those keys."""
    if not (p := c.get("period")):
        return c["query"], []
    t, col = p.split(".", 1)
    keys = [f"{t}.{YEAR[col]}", p] if col in YEAR else [p]
    return c["query"] | {"dimensions": keys + [d for d in c["query"].get("dimensions", []) if d not in keys]}, keys


def evaluate(project: str, c: dict, user_id: int | None) -> dict:
    """{holds, columns, rows}: the condition over `semantic.query` rows read as `user_id`. With `period`, the rows of the
    latest period (`skip_latest`: the one before); with `change`, one row: that period, the measure, the period before,
    its value and the % change (`change_pct`), which `op value` compares."""
    if err := problem(c):
        raise ActionFailed(400, err)
    q, keys = periods(c)
    r = semantic.query(project, q, user_id)
    cols, rows, m, n = r["columns"], r["rows"], c["measure"], len(keys)
    if keys:
        seen = sorted({tuple(row[:n]) for row in rows if None not in row[:n]}, reverse=True)[int(bool(c.get("skip_latest"))):]
        now, before = (seen + [None, None])[:2]
        if c.get("change"):
            v = {tuple(row[:n]): row[cols.index(m)] for row in rows}
            a, b = v.get(now), v.get(before)
            pct = round((a - b) / abs(b) * 100, 4) if a is not None and b else None
            cols = [*keys, m, *(f"previous_{k.split('.', 1)[1]}" for k in keys), f"previous_{m}", "change_pct"]
            rows = [[*now, a, *before, b, pct]] if before else []
        else:
            rows = [row for row in rows if tuple(row[:n]) == now]
    return {"holds": bool(held(c, cols, rows)), "columns": cols, "rows": rows, "measures": r["measures"], "filters": r["filters"]}


def held(c: dict, cols: list, rows: list) -> list:
    """The rows of a condition's result (`evaluate`'s columns and rows) that hold."""
    i, j = cols.index("change_pct" if c.get("change") else c["measure"]), cols.index(c["other"]) if c.get("other") else None
    to = lambda row: (0 if j is None else float(row[j])) + c["value"]  # noqa: E731
    return [row for row in rows if row[i] is not None and (j is None or row[j] is not None) and OPS[c["op"]](float(row[i]), to(row))]


@action(Role.viewer)
def check_condition(conn, user: User, args: Condition) -> dict:
    """Checks a condition: runs the semantic query as you and says whether any result row's `measure` value satisfies `op value` (NULL never does); with `other`, measure op other + value; with `period`, only the latest period counts (`skip_latest`: the one before); with `change`, the % change from the period before. Returns {holds, columns, rows}."""
    return evaluate(args.project, args.model_dump(mode="json"), user.id)


def fingerprint(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


class Subscribe(storage.ProjectRef):
    name: str = Field(min_length=1, max_length=200)
    query: SemanticQuery


@action(Role.viewer)
def subscribe(conn, user: User, args: Subscribe) -> dict:
    """Subscribes you to a semantic query: the worker re-runs it as you every minute and, when its result changes, posts the new result to your private inbox message list (action inbox)."""
    q = args.query.model_dump(mode="json")
    semantic.plan(args.project, q)  # 400 with the planner's reason
    s = conn.execute("INSERT INTO subscriptions (project, user_id, name, query) VALUES (%s, %s, %s, %s) RETURNING id",
                     [args.project, user.id, args.name, Jsonb(q)]).fetchone()
    return s | {"columns": ["id"], "rows": [[s["id"]]]}  # GATE-9-fix-8.6 (D-22): the handle unsubscribe takes


class SubscriptionRef(storage.ProjectRef):
    id: int


@action(Role.viewer)
def unsubscribe(conn, user: User, args: SubscriptionRef) -> dict:
    """Deletes one of your subscriptions."""
    if not conn.execute("DELETE FROM subscriptions WHERE id = %s AND project = %s AND user_id = %s RETURNING id",
                        [args.id, args.project, user.id]).fetchone():
        raise ActionFailed(404, f"You have no subscription {args.id} in project '{args.project}'.")
    return {}


@action(Role.viewer)
def list_subscriptions(conn, user: User, args: storage.ProjectRef) -> dict:
    """Your subscriptions in the project: name, query, the last check time and its error, if any."""
    subs = conn.execute("SELECT id, name, query, error, checked FROM subscriptions WHERE project = %s"
                        " AND user_id = %s ORDER BY id", [args.project, user.id]).fetchall()
    # D-22: the id is the handle unsubscribe takes, so it is quotable; the query (an input) never grounds a figure
    cols = ["id", "name", "checked", "error"]
    return {"subscriptions": subs, "columns": cols, "rows": [[s[c] for c in cols] for s in subs]}


def _post(conn, s: dict, text: str, data: dict | None = None) -> None:
    conn.execute("INSERT INTO inbox (project, sender, text, data, recipient) VALUES (%s, 'subscriptions', %s, %s, %s)",
                 [s["project"], text, data and Jsonb(data, dumps=lambda o: json.dumps(o, default=str)), s["user_id"]])


def check() -> None:
    """SE-8, every minute: re-run each subscription as its subscriber. The first check stores the baseline; a changed
    result posts one private inbox message with the new rows; an error posts once per distinct error and the next
    success says it works again (D-10)."""
    # ponytail: one query per subscription per minute; imported tables hit the SE-7 cache, live tables re-query
    # ClickHouse each time. Upgrade: skip when the D-02.4 change stamp is unchanged.
    with db.connect() as conn:
        subs = conn.execute("SELECT s.*, u.name AS user_name, u.workspace_owner FROM subscriptions s"
                            " JOIN users u ON u.id = s.user_id ORDER BY s.id").fetchall()
    for s in subs:
        error = fp = r = None
        try:
            with db.connect() as conn:
                if role_in(conn, User(s["user_id"], s["user_name"], s["workspace_owner"]), s["project"]) is None:
                    raise ActionFailed(403, f"you no longer have a role in project '{s['project']}', so it is skipped.")
            r = semantic.query(s["project"], s["query"], s["user_id"])
            fp = fingerprint([r["columns"], r["rows"]])
        except ActionFailed as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001 - one broken subscription doesn't stop the others; no raw text (SC-4)
            error = f"internal error ({type(e).__name__})."
        head = f"Subscription '{s['name']}' "
        with db.connect() as conn:
            if error and error != s["error"]:
                _post(conn, s, head + f"can't be checked: {error}")
            elif not error and s["error"]:
                _post(conn, s, head + "works again.")
            if fp and s["fingerprint"] and fp != s["fingerprint"]:
                _post(conn, s, head + "changed: the new result is attached.", {"columns": r["columns"], "rows": r["rows"]})
            conn.execute("UPDATE subscriptions SET fingerprint = coalesce(%s, fingerprint), error = %s, checked = now() WHERE id = %s",
                         [fp, error, s["id"]])
