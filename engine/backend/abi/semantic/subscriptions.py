"""Conditions and subscriptions. PRD SE-8, FL-1 (condition), AT-2, DECISIONS D-31.

A condition is `{query, measure, op, value}`: it holds when any result row's measure value satisfies `op value` (NULL
never). Code compares tool results (principle 7). A subscription is a user's semantic query that the worker re-runs
every minute as that user; a changed result goes to the subscriber's private inbox (SE-8). Subscriptions are
runtime state in Postgres, not Git (§6.6).
"""

import hashlib
import json
import operator
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import Field

from abi import db, semantic, storage
from abi.actions import ActionFailed, action
from abi.security import Role, User, role_in
from abi.semantic import SemanticQuery

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "=": operator.eq, "!=": operator.ne}


class Condition(storage.ProjectRef):
    query: SemanticQuery
    measure: str = Field(description="a measure ID in query.measures")
    op: Literal[">", ">=", "<", "<=", "=", "!="]
    value: float


def evaluate(project: str, c: dict, user_id: int | None) -> dict:
    """{holds, rows}: the condition over `semantic.query` rows read as `user_id`."""
    if c["measure"] not in c["query"]["measures"]:
        raise ActionFailed(400, f"Measure '{c['measure']}' is not in the condition's query measures {c['query']['measures']}.")
    r = semantic.query(project, c["query"], user_id)
    i = r["columns"].index(c["measure"])
    return {"holds": any(row[i] is not None and OPS[c["op"]](float(row[i]), c["value"]) for row in r["rows"]), "rows": r["rows"]}


@action(Role.viewer)
def check_condition(conn, user: User, args: Condition) -> dict:
    """Checks a condition: runs the semantic query as you and says whether any result row's `measure` value satisfies `op value` (NULL never does). Returns {holds, rows}."""
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
