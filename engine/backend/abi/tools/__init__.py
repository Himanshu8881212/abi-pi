"""Named, typed, tested tools. PRD §7.1 TL-1, TL-3, TL-4.

A tool is `tools/<id>.json` (schemas/v1/tool.json): typed `inputs`, a semantic `query` with `{"$input": name}`
placeholders, typed `outputs`, `tests` and a minimum role. Its rows come only from `semantic.query` (principles 4, 7).
Lifecycle (TL-3): `save_tool` (always a draft) → `test_tool` on the project's current data → `publish_tool`
(every test passes, version + 1). Only published tools run. TL-4: after a change to the model files or the tool,
`retest` runs published tools' tests again; a failing tool is disabled (`tool_state` in Postgres, not the file) and
the feed names its owner, once per distinct failure, with a recovery post when it passes again (D-10).
TL-5: a calculation tool (`kind: "calculation"`) also has `tools/<id>.py` defining `calculate(rows, inputs)`; its query's
rows go through that code in `sandbox` (never imported here) and the code's rows are the tool's result. An API tool
(`kind: "api"`) has no query: `api.call` sends its `request` to an outside host and its rows come from the response;
publishing one needs the Owner role and a confirmation naming the host (D-35).
"""

import hashlib
import json
import logging
from urllib.parse import urlsplit

import referencing
from jsonschema import Draft202012Validator
from pydantic import Field

from abi import actions, db, semantic, storage
from abi.actions import ACTIONS, ActionFailed, action, by_share, require_confirmation
from abi.agent import notify_change
from abi.project import ID, SCHEMA, git, imports
from abi.project.bundle import bundle, dumps
from abi.security import MEMBERS, Role, User, role_in

MODEL = ("model/", "tables/", "sources/")  # TL-4: a change under these re-runs the tests
log = logging.getLogger(__name__)


def validate(schema: dict, value) -> list[dict]:
    """JSON Schema errors as {path, error}, naming the rule broken, never the value (it may be anything a caller sent).
    No registry: a `$ref` to a URL is unresolvable, never fetched."""
    try:
        return [{"path": e.json_path, "error": f"fails `{e.validator}`: {json.dumps(e.validator_value)}"}
                for e in Draft202012Validator(schema, registry=referencing.Registry()).iter_errors(value)]
    except referencing.exceptions.Unresolvable:
        return [{"path": "$", "error": "the schema has a `$ref` that can't be resolved; inline it"}]


def _inputs(t: dict, inputs: dict) -> dict:
    """`inputs` plus the input defaults; 422 on inputs outside the `inputs` schema."""
    vals = {k: p["default"] for k, p in t["inputs"]["properties"].items() if "default" in p} | inputs
    if errs := validate(t["inputs"], vals):
        raise ActionFailed(422, "Inputs don't match the tool's `inputs` schema.", errors=errs)
    return vals


def query_for(t: dict, inputs: dict) -> dict:
    """The tool's semantic query (an API tool: its request) with its placeholders filled from `inputs` plus the input defaults."""
    vals = _inputs(t, inputs)

    def fill(x):
        if isinstance(x, dict) and x.keys() == {"$input"}:
            if x["$input"] not in vals:
                raise ActionFailed(422, f"Input '{x['$input']}' has no value; pass it or give it a default.")
            return vals[x["$input"]]
        if isinstance(x, dict):
            return {k: fill(v) for k, v in x.items()}
        return [fill(v) for v in x] if isinstance(x, list) else x

    return fill(t["request"] if t.get("kind") == "api" else t["query"])


def _code(project: str, tool: str):
    return git.path(project) / "tools" / f"{tool}.py"


def _run(project: str, t: dict, inputs: dict, user_id: int | None) -> dict:
    if t.get("kind") == "api":  # TL-5: rows from an outside system, never from the model
        rows = api.call(project, t["id"], t["request"], query_for(t, inputs), t["rows"])
        out = {"columns": list(dict.fromkeys(k for r in rows for k in r)), "measures": {}}
    else:
        out = semantic.query(project, query_for(t, inputs), user_id)  # principle 7: the only source of numbers
        rows = [dict(zip(out["columns"], map(semantic.norm, r))) for r in out["rows"]]
    if t.get("kind") == "calculation":  # TL-5: deterministic code on the tool's own rows, in the sandbox only
        try:
            rows, peak = sandbox.run(_code(project, t["id"]).read_text(), rows, _inputs(t, inputs))
        except sandbox.SandboxError as e:
            raise ActionFailed(400, f"Tool '{t['id']}': {e}") from None
        rows = [{k: semantic.tidy(v) for k, v in r.items()} for r in rows]  # principle 7: no binary noise (LLM-2-fix-8)
        log.info("calculation tool %s/%s: %d rows, sandbox peak RSS %d MB", project, t["id"], len(rows), peak >> 20)  # D-17
        out["columns"] = list(dict.fromkeys(k for r in rows for k in r))
    if errs := validate(t["outputs"], rows):
        raise ActionFailed(400, f"The result of tool '{t['id']}' doesn't match its `outputs` schema.", errors=errs)
    return {"columns": out["columns"], "measures": out["measures"], "rows": rows}


def _key(rows: list[dict]) -> list[str]:
    return sorted(json.dumps({k: semantic.norm(v) for k, v in r.items()}, sort_keys=True) for r in rows)


def _tests(project: str, t: dict) -> list[dict]:
    out = []
    for i, x in enumerate(t["tests"], 1):
        try:
            got = _run(project, t, x["inputs"], None)
        except ActionFailed as e:
            out.append({"test": i, "passed": False, "error": f"Test {i} can't run: {e} {json.dumps(e.body.get('errors', ''))}".strip()})
            continue
        res = {"test": i, "passed": "expect" not in x or _key(got["rows"]) == _key(x["expect"]), "rows": got["rows"]}
        if not res["passed"]:  # SE-6: a system read of every row; the text reaches every member (list_tools, feed)
            res["error"] = f"Test {i} fails: expected {json.dumps(x['expect'])}, got " + (
                f"other rows ({semantic.rls.HIDDEN})." if semantic.rls.rules(project) else f"{json.dumps(got['rows'])}.")
        out.append(res)
    return out


def _stamp(files: dict, rel: str) -> str:
    code = files.get(f"{rel[:-5]}.py")  # TL-4: a calculation's code is part of the tool
    # rls.json too: a first rule re-tests the tool, so stored failure text loses its rows (SE-6)
    return hashlib.sha256(dumps([{k: v for k, v in files.items() if k.startswith(MODEL)}, files[rel], code,
                                 files.get(semantic.rls.RLS)]).encode()).hexdigest()


def _record(conn, project: str, t: dict, stamp: str, error: str | None) -> None:
    """Store the tool's test outcome (a failure disables it) and post a change of it to the feed (TL-4, D-10)."""
    conn.execute("INSERT INTO tool_state VALUES (%s, %s, %s, %s) ON CONFLICT (project, tool)"
                 " DO UPDATE SET stamp = EXCLUDED.stamp, error = EXCLUDED.error", [project, t["id"], stamp, error])
    owner = conn.execute(f"SELECT name FROM users WHERE id = %s AND {MEMBERS}", [int(t["owner"]), project, project]).fetchone()
    owner = owner["name"] if owner else f"user {t['owner']}"
    notify_change("tool", project, f"Tool '{t['id']}' ", error and f"is disabled; owner {owner}, please fix it: {error}",
                  "passes its tests again and is enabled.", t["id"])


def retest(project: str) -> None:
    """TL-4: re-run the tests of each published tool whose model files or definition changed since its last run, or
    that is disabled (a load may have been pending)."""
    files = bundle(git.path(project))
    with db.connect() as conn:
        state = {r["tool"]: r for r in conn.execute("SELECT tool, stamp, error FROM tool_state WHERE project = %s", [project])}
        for rel, t in files.items():
            if imports._kind(rel) != "tool" or t.get("status") != "published":
                continue
            stamp, old = _stamp(files, rel), state.get(t["id"])
            if not old or old["stamp"] != stamp or old["error"]:
                _record(conn, project, t, stamp, " ".join(r["error"] for r in _tests(project, t) if not r["passed"]) or None)
                conn.commit()  # one tool at a time: a later failure keeps the earlier outcomes


def _write(conn, project: str, files: dict, t: dict, code: str | None = None) -> None:
    py, calc = f"tools/{t['id']}.py", t.get("kind") == "calculation"
    new = {f"tools/{t['id']}.json": t} | ({py: code} if calc and code is not None else {})
    if not calc:  # a tool that stops being a calculation drops its code
        files = {k: v for k, v in files.items() if k != py}
    imports.check(conn, project, files | new)  # schema, references, planner (PC-3)
    if not calc:
        _code(project, t["id"]).unlink(missing_ok=True)
    imports.write(project, new)


class ToolRef(storage.ProjectRef):
    tool: str = Field(pattern=ID, description="tool ID (file name in tools/)")


class SaveTool(storage.ProjectRef):
    tool: dict = Field(description=(
        "The tool (schemas/v1/tool.json): {id, name, description, inputs: JSON Schema {type: object, properties:"
        " {name: {type, description, default?, minimum+maximum (numbers), maxLength or enum (strings), items+maxItems"
        " (arrays)}}, required?, additionalProperties: false}, query: semantic query (measure IDs, table.column"
        " dimensions, filters, order_by, limit) where a filter value or limit may be {\"$input\": name}, outputs: JSON"
        " Schema of the result rows (array of {column: value}; columns are the dimensions then the measure IDs), tests:"
        " [{inputs, expect? (exact rows)}], permissions: {role: viewer|editor}, kind?: query (default) | calculation | api}."
        " An api tool has no query but request {method: GET|POST, url: https://host[:port]/path, query?, headers?, body?}"
        " (a query/body value may be {\"$input\": name}, a header/query value exactly ${secret:NAME}) and rows: JSON"
        " pointer to the rows array in the response."
        " version, owner and status are set for you."))
    code: str | None = Field(None, max_length=100_000, description=(
        "kind calculation only: Python (stdlib only; no files, network or secrets) defining calculate(rows, inputs) that"
        " returns the result rows ([{column: value}] matching `outputs`) from the query's rows; omit to keep the saved code."))


@action(Role.editor, definition=True)
def save_tool(conn, user: User, args: SaveTool) -> dict:
    """Creates or replaces a tool (tools/<id>.json) as a draft; a published tool goes back to draft until published again. Checked like an import (schema, measure IDs, model tables, the planner with each test's inputs); rejected with file, JSON path and fix if invalid. Next: test_tool, then publish_tool."""
    t, files = args.tool, bundle(git.path(args.project))
    old = files.get(f"tools/{imports.object_id(t, 'A tool')}.json") or {}
    t = {**t, "$schema": SCHEMA + "tool.json", "status": "draft", "version": old.get("version", 0),
         "owner": old.get("owner", str(user.id))}
    _write(conn, args.project, files, t, args.code)
    return saved(t)


def saved(x: dict) -> dict:
    """A saved or published tool or agent: its server-set status and version as rows (D-22: quotable; its ID is the
    author's name and stays out, D-114)."""
    return {"id": x["id"], "status": x["status"], "version": x["version"], "columns": ["status", "version"],
            "rows": [[x["status"], x["version"]]]}


@action(Role.editor, definition=True, confirms=True)
def delete_tool(conn, user: User, args: ToolRef) -> dict:
    """Deletes a tool."""
    imports.remove(args.project, "tool", args.tool)
    _code(args.project, args.tool).unlink(missing_ok=True)
    conn.execute("DELETE FROM tool_state WHERE project = %s AND tool = %s", [args.project, args.tool])
    return {}


class TestTool(ToolRef):
    inputs: dict | None = Field(None, description="run once with these inputs instead of the tool's tests")


@action(Role.editor)
def test_tool(conn, user: User, args: TestTool) -> dict:
    """Runs a tool (draft or published) on the project's current data: its tests (each passed or failed, with the rows), or once with the given inputs."""
    t = imports.load(args.project, "tool", args.tool)
    # H-01.5-fix-1, §5 #3: an agent's (or inbound) draft run is an outside send. A person's own Test click is one step
    # (like PERSON_CLICK), but a click carrying a token (the agent's Confirm, sent over REST) must match what it showed.
    if t.get("kind") == "api" and actions.needs_click():
        host = urlsplit(t["request"]["url"]).hostname
        builtin._first_send(conn, args.project, {"api:" + host}, {"host": host} | imports.api_shape(t))
    if args.inputs is not None:
        return _run(args.project, t, args.inputs, user.id)
    tests = _tests(args.project, t)
    return {"passed": bool(tests) and all(r["passed"] for r in tests), "tests": tests,  # D-22: each test's result rows
            "columns": ["test", "passed", "row"], "rows": [[x["test"], x["passed"], r] for x in tests for r in x.get("rows") or [None]]}


@action(Role.editor, definition=True)
def publish_tool(conn, user: User, args: ToolRef) -> dict:
    """Publishes a tool: runs its tests on the current data and, if every one passes, sets it published with the next version. Refused with the failures otherwise."""
    t = imports.load(args.project, "tool", args.tool)
    if t.get("kind") == "api":  # D-35: it sends requests out of the workspace, for any caller with its role
        if role_in(conn, user, args.project) != Role.owner:
            raise ActionFailed(403, f"Publishing API tool '{args.tool}' needs the owner role in project '{args.project}'.")
        host = urlsplit(t["request"]["url"]).hostname
        ask = imports.grant_check(args.project, {f"tools/{t['id']}.json": {**t, "status": "published", "version": t["version"] + 1}})
        require_confirmation(f"Publishing tool '{args.tool}' lets everyone with the {t['permissions']['role']} role send"
                             f" requests to the outside host '{host}' through it." + (f" {ask[0]}" if ask else ""),
                             {"host": host} | imports.api_shape(t) | (ask[1] if ask else {}))
        conn.execute("INSERT INTO send_targets VALUES (%s, %s) ON CONFLICT DO NOTHING", [args.project, "api:" + host])
    if not t["tests"]:
        raise ActionFailed(400, f"Tool '{args.tool}' has no tests; add at least one with save_tool.")
    if failed := [r for r in _tests(args.project, t) if not r["passed"]]:
        raise ActionFailed(400, f"Tool '{args.tool}' isn't published: {len(failed)} test(s) fail.", failures=[r["error"] for r in failed])
    t, files = {**t, "status": "published", "version": t["version"] + 1}, bundle(git.path(args.project))
    _write(conn, args.project, files, t)
    rel = f"tools/{t['id']}.json"
    _record(conn, args.project, t, _stamp(files | {rel: t}, rel), None)
    return saved(t)


def sends(t: dict) -> bool:
    """An API tool whose request may change something outside (method not GET/HEAD): never for share recipients (SH-1)."""
    return t.get("kind") == "api" and t["request"]["method"] not in ("GET", "HEAD")


class RunTool(ToolRef):
    inputs: dict = Field({}, description="the tool's inputs; missing ones take their defaults")


@action(Role.viewer)
def run_tool(conn, user: User, args: RunTool) -> dict:
    """Runs a published tool with these inputs, as you: returns its result columns, measure names and rows (every number comes from the semantic engine). Disabled tools (failing tests) don't run."""
    t = imports.load(args.project, "tool", args.tool)
    if t["status"] != "published":
        raise ActionFailed(409, f"Tool '{args.tool}' is a draft; publish_tool it first.")
    state = conn.execute("SELECT error FROM tool_state WHERE project = %s AND tool = %s", [args.project, args.tool]).fetchone()
    if state and state["error"]:
        raise ActionFailed(409, f"Tool '{args.tool}' is disabled because its tests fail: {state['error']}")
    need, have = Role[t["permissions"]["role"]], role_in(conn, user, args.project)
    if have is None and by_share(args.project, user):  # E-03.2: a shared agent's recipient, view-only
        if sends(t):
            raise ActionFailed(403, f"Tool '{args.tool}' sends a {t['request']['method']} request; a share is view-only.")
        have = Role.viewer
    if (have or 0) < need:
        raise ActionFailed(403, f"Tool '{args.tool}' needs the {need.name} role in project '{args.project}'.")
    if sends(t) and (test := builtin.flows.TEST_RUN.get()) is not None:  # AT-4: a test run's outside write reaches only the owner's inbox
        method, host = t["request"]["method"], urlsplit(t["request"]["url"]).hostname
        builtin._inbox(conn, args.project, user, f"Test run: {method} request to {host} through tool {t['id']} (not sent)", None, None, test)
        return {"tool": t["id"], "version": t["version"], "columns": [], "measures": {}, "rows": []}
    return {"tool": t["id"], "version": t["version"], **_run(args.project, t, args.inputs, user.id)}


@action(Role.viewer)
def list_tools(conn, user: User, args: storage.ProjectRef) -> dict:
    """The project's tools: each one's definition (a calculation tool's Python as `code`), plus `disabled` and the last test failure when its tests fail (TL-4), and the built-in tools (TL-2) with their input and output JSON Schemas."""
    state = {r["tool"]: r["error"] for r in conn.execute("SELECT tool, error FROM tool_state WHERE project = %s", [args.project])}
    listed = imports.objects(args.project, "tools")
    return {"tools": [{**t, "disabled": bool(state.get(t["id"])), "error": state.get(t["id"]),  # code never holds a secret (import check)
                       **({"code": _code(args.project, t["id"]).read_text()} if t.get("kind") == "calculation" else {})}
                      for t in listed],
            # LLM-2-fix-5: the figures the agent may quote (D-22: only `rows` ground a number), e.g. a tool's version
            # D-114: never the author-written id/name (a digit in them would ground a figure)
            "columns": ["version", "status", "disabled"],
            "rows": [[t.get("version"), t.get("status"), bool(state.get(t["id"]))] for t in listed],
            "builtins": [{"id": n, "description": ACTIONS[n].fn.__doc__, "role": ACTIONS[n].role.name,
                          "inputs": ACTIONS[n].input.model_json_schema(), "outputs": out}
                         for n, out in builtin.BUILTIN_TOOLS.items()]}


AGENT_ACTION_TOOLS = ("send_email", "post_message", "refresh", "propose_edit")  # D-3: the only actions custom agents get (T-02)

# last: builtin (the TL-2 actions) imports this module
from abi.tools import api, builtin, sandbox
