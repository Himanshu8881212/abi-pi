"""Custom agent runs, traces, evaluation, publish. PRD §7.2 AG-1..AG-4, §5 #3, NFR Observability (OB-1), FL-1.

`run_agent` asks an agent a question: a stub-able LLM tool loop that offers only the agent's tools (exploratory mode
adds `semantic_query`), every call through `actions.run(..., user, via="agent")` as the requesting user, reads limited
to the agent's scope. With grounding on (always in flows) the answer passes `grounding.check` or gets one retry, then
the template answer (AG-2). Each run is traced in Postgres `agent_runs` (AG-3) and its tokens go to `llm_usage` under
the agent's ID (OB-1). `test_agent` runs the evaluation set; `publish_agent` needs every case passing and a click.
AG-4: `retest` (in the worker's TL-4 job) re-runs published agents' sets after a model, tool or agent change and
disables a failing agent (`agent_state`, not the file).
"""

import contextvars
import hashlib
import json
import uuid
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb
from pydantic import AliasChoices, Field, ValidationError

from abi import actions, db, flows, security, semantic, storage, tools
from abi.actions import ACTIONS, ActionFailed, action, require_confirmation
from abi.agent import (
    COMPACTED,
    MAX_STEPS,
    _cap,
    compact,
    llm,
    notify,
    notify_change,
    sampled,
)
from abi.agents import grounding, studio
from abi.flows import inbound
from abi.project import ID, git, imports
from abi.project.bundle import bundle, dumps
from abi.security import MEMBERS, Role, User, role_in
from abi.semantic import SemanticQuery, lineage, uses
from abi.tools import builtin

MAX_DEPTH = 2  # AGENT-AS-TOOL: an agent may ask agents that ask agents, no further
NEST = contextvars.ContextVar("nest", default=(0, False))  # (depth, dry) of a run that ask_agent started


def _load(files: dict, project: str, agent: str) -> dict:
    if (a := files.get(f"agents/{agent}.json")) is None:
        raise ActionFailed(404, f"No agent '{agent}' in project '{project}'.")
    return a


def _snapshot(files: dict, s) -> dict:
    """D-24: a report snapshot request as a query of every measure on its page or visual."""
    if not isinstance(s, dict):
        return {}
    r = files.get(f"reports/{s.get('report')}.json") or {"pages": []}
    pages = s.get("pages") if isinstance(s.get("pages"), list) else [s.get("page")]  # MULTI-EXPORT.1
    return {"measures": [m for p in r["pages"] if s.get("pages") == "all" or p["id"] in pages for v in p["visuals"]
                         if s.get("visual") in (None, v["id"]) for m in (v.get("query") or {}).get("measures", [])]}


def _refused(name: str, payload: dict, allowed: list[str], files: dict, g: dict, measures: set[str], tables: set[str],
             agents: list[str]) -> str | None:
    """Why the agent may not make this call: a tool it doesn't hold, or a read/write outside its scope (D-24: custom
    tools' queries, measures used in MEL, report snapshots too). An asked agent reads within its own scope."""
    if name not in allowed:
        return f"'{name}' is not one of your tools ({', '.join(allowed)})."
    if name == studio.ASK:
        return None if payload.get("agent") in agents else f"'{payload.get('agent')}' is not one of the agents you may ask ({', '.join(agents)})."
    q = payload if name == "semantic_query" else payload.get("data")  # send_email/post_message attach a query or tool
    if isinstance(q, dict) and "measures" in q:
        try:
            q = SemanticQuery.model_validate(q).model_dump(mode="json")
        except ValidationError:
            q = None  # actions.run refuses it with the details
    if name == "semantic_query" and isinstance(q, dict) and isinstance(payload.get("expressions"), list):
        ms, cols = uses(payload["expressions"])  # M2.1: what its inline expressions read is in the query too
        q = q | {"measures": q["measures"] + ms, "dimensions": q["dimensions"] + cols}
    if isinstance(q, dict) and isinstance(q.get("tool"), str):
        if q["tool"] not in allowed:
            return f"Tool '{q['tool']}' is not one of your tools."
        q = (files.get(f"tools/{q['tool']}.json") or {}).get("query")  # schema-checked: placeholders only in values
    if name not in studio.BUILTIN:  # a custom tool reads its own query
        q = (files.get(f"tools/{name}.json") or {}).get("query")
    # the request's page/visual; render_snapshot checks what is drawn again (`_call` sets builtin.SCOPE)
    snap = _snapshot(files, payload if name == "report_snapshot" else payload.get("report"))
    if out := list(dict.fromkeys(x for r in [q if isinstance(q, dict) else {}, snap] for x in studio.outside(r, measures, tables, g))):
        return f"Outside your scope: {', '.join(out)}."
    if name in ("refresh", "propose_edit") and payload.get("table") not in tables:
        return f"Table '{payload.get('table')}' is outside your scope."
    return None


def _defs(names: list[str], files: dict, agents: list[str]) -> list[dict]:
    out = []
    for n in names:
        if n == studio.ASK:  # AGENT-AS-TOOL: one function for every agent it may ask (names can't hold "agent:<id>")
            s = {"type": "object", "required": ["agent", "question"], "properties": {
                "agent": {"enum": agents}, "question": {"type": "string", "minLength": 1, "maxLength": 20_000}}}
            desc = "Asks another agent of this project, as the user; it answers from its own tools and scope. " + " ".join(
                f"{x}: {o.get('purpose', '')}" for x in agents if (o := files.get(f"agents/{x}.json")))
        elif n in studio.BUILTIN:
            s = ACTIONS[n].input.model_json_schema()
            s = s | {"properties": {k: v for k, v in s["properties"].items() if k != "project"},
                     "required": [r for r in s.get("required", []) if r != "project"]}
            desc = ACTIONS[n].fn.__doc__ or ""
        else:  # a custom tool: its own typed inputs
            t = files.get(f"tools/{n}.json") or {}
            s, desc = t.get("inputs", {"type": "object"}), t.get("description", "")
        out.append({"type": "function", "function": {"name": n, "description": desc.strip(), "parameters": s}})
    return out


def _call(project: str, name: str, payload: dict, user: User, dry: bool, outside, files: dict,
          inbound_run: tuple | None = None, shared: bool = False) -> dict:
    t = None if name in (*studio.BUILTIN, studio.ASK) else files.get(f"tools/{name}.json") or {}
    sends = tools.sends(t) if t is not None else name in tools.AGENT_ACTION_TOOLS  # an API tool may change something outside (TL-5)
    if dry and sends:  # H-01.6-fix-8: an evaluation run never sends, through an action or an API tool
        target = urlsplit(t["request"]["url"]).hostname if t else payload.get("to") or payload.get("channel") or payload.get("table")
        target = ", ".join(map(str, target)) if isinstance(target, list) else target  # LLM-2-fix-12: not a failure
        return {"status": f"Evaluation run: not run; in a real run this {name} would have gone to {target}."}
    if t is not None:  # a custom tool
        name, payload = "run_tool", {"tool": name, "inputs": payload}
    if inbound_run and sends:  # AG-5, D-32: an inbound message never runs an action or an outside write
        return inbound.defer(project, *inbound_run, name, payload, user)
    ctx = contextvars.copy_context()  # own context: the nested run must not overwrite this action's VIA / confirmation state
    ctx.run(builtin.SCOPE.set, outside)  # D-24: a snapshot's drawn measures stay in scope
    if name == studio.ASK:  # AGENT-AS-TOOL: run_agent as the user; depth and dry ride along (an evaluation never sends)
        ctx.run(NEST.set, (NEST.get()[0] + 1, dry))
        ctx.run(llm.UNATTENDED.set, llm.UNATTENDED.get() or actions.VIA.get() == "worker")  # D-53: run() sets VIA agent
        name, payload = "run_agent", {"agent": payload["agent"], "question": payload.get("question")}
    if shared:
        ctx.run(actions.SHARE.set, (project, user.id))  # E-03.2: only this call's read tool, after the scope check
    try:
        return {"result": ctx.run(actions.run, name, payload | {"project": project}, user, "agent")}
    except ActionFailed as e:
        body = {k: v for k, v in e.body.items() if k != "confirm_token"}  # F-02: a token never reaches the LLM
        return {"error": body}
    except Exception:  # noqa: BLE001 (run() audited it; the LLM gets no internals)
        return {"error": "The tool failed with an internal error."}


def _system(a: dict, explore: bool, today: str) -> str:
    return "\n".join([  # D-117: today, the only part that moves between runs, last (providers cache the prefix)
        f"You are {a['name']}: {a['purpose']}", a["instructions"],
        "Answer only through your tools. Every number and name you state must come from a tool result; never compute or guess."
        " You may round a value or show a ratio as a percent (0.683 as 68.3%), and name the period or member the query was"
        " filtered on; always answer in words, never with nothing. Send an email, post a message or change data only when"
        " the person asks for exactly that; a question is answered in your reply.",  # ABI-PI: what the check accepts (31 Oct)
        COMPACTED,
        "Your knowledge scope (IDs): " + json.dumps(a["scope"]),
        *(["You may run read-only semantic_query calls within this scope."] if explore else []), today,
    ])


def _answer(project: str, a: dict, question: str, user: User, via: str, files: dict, ground: bool, dry: bool = False,
            shared: bool = False) -> dict:
    """One run of the tool loop; traced in agent_runs. Returns {run, answer, grounded, retried, template, tools, data, snapshot}."""
    explore = a["mode"] == "exploratory"
    asks = a.get("agents") and NEST.get()[0] < MAX_DEPTH and not shared  # E-03.2: a callee isn't shared with the recipient
    allowed = [t for t in dict.fromkeys([*a["tools"], *(["semantic_query"] if explore else []), *([studio.ASK] if asks else [])])
               # E-03.2: a share recipient gets only the read tools (no sends, changes or snapshots, nothing queued)
               if not shared or t in actions.SHARE_TOOLS
               or (t not in studio.BUILTIN and not tools.sends(files.get(f"tools/{t}.json") or {}))]
    g = lineage.graph(files)
    measures, tables = studio.scope(a, files, g)
    today, day = grounding.today(project)  # GATE-9-fix-9: stated in the instructions, grounded by `check`
    messages = [{"role": "system", "content": _system(a, explore, today)}, {"role": "user", "content": question}]
    trace, outputs, notes, seen, tokens, effort = [], [], [], [], [0, 0, 0, 0], None  # prompt, completion, reasoning, cached
    retried = template = nudged = False
    grounded, scope = None, None
    run_id = str(uuid.uuid4())
    stubbed, keep = frozenset(), len(messages)  # D-117: tool messages in `stubbed` go to the LLM as stubs; from `keep` on never
    for _ in range(MAX_STEPS):
        sent, stubbed = compact(messages, stubbed, keep)
        try:
            # OB-1/AG-6: usage per agent and provider; the agent file's model (alias or auto) and effort (D-71)
            resp = llm.complete(user, a["id"], a.get("model", "auto"), question, project=project, effort=a.get("effort"),
                                messages=sent, tools=_defs(allowed, files, a.get("agents", [])))
        except llm.Declined:  # ABI-PI: the provider's safety filter refused the request: the agent declines it
            text, grounded = llm.DECLINED, True if ground else None
            break
        except llm.Shown:  # no model to try (LLM-2-fix-13) or a provider error said plainly (LLM-2.7-fix-1)
            raise
        except Exception as e:  # noqa: BLE001 (the message may carry the endpoint URL)
            raise ActionFailed(502, f"LLM call failed: {type(e).__name__}") from None
        effort = resp.effort
        tokens = [t + s for t, s in zip(tokens, resp.spent, strict=True)]  # every attempt, failovers too (OB-1)
        msg = resp.choices[0].message
        if not msg.tool_calls and not msg.content:  # every provider empty (LLM-2-fix-6): one nudge, then the template
            trace.append({"tool": "empty answer", "input": {}, "output": {"finish_reason": resp.choices[0].finish_reason,  # ABI-PI
                                                                              "refusal": getattr(msg, "refusal", None)}})
            if not nudged:  # LLM-2-fix-12: never a blank answer, and the run (an eval case) keeps its trace
                nudged = True
                messages.append({"role": "user", "content": "Answer the question now from the tool results."})
                continue
            text, template, grounded = grounding.template(outputs), True, False if ground else None
            break
        if not msg.tool_calls:
            text = msg.content
            if ground:
                # ponytail: grounding.scope re-reads the repo files and one DISTINCT query per scoped text column on every
                # run (cached only within the run); cache it per snapshot + model hash if agent runs get frequent.
                scope = scope or grounding.scope(project, user.id, tables)
                bad = grounding.check(text, outputs + sampled(seen) + grounding.filtered(project, user.id, outputs), *scope,
                                       notes=notes, question=question, spans=True, today=day)
                grounded = not (bad["numbers"] or bad["names"])
                if not grounded:  # ABI-PI: the trace keeps what the check rejected (the draft masked, the figures and names)
                    trace.append({"tool": "grounding", "input": {"draft": grounding.mask(text, bad)[:4000]},
                                  "output": {"numbers": bad["numbers"], "names": bad["names"]}})
                if not grounded and not retried:
                    retried = True
                    messages += [{"role": "assistant", "content": grounding.mask(text, bad)},
                                 {"role": "user", "content": grounding.RETRY}]  # GATE-9-fix-4: nothing to echo
                    continue
                if not grounded:
                    text, template = grounding.template(outputs), True  # AG-2: never the ungrounded text
            break
        keep = len(messages)  # this step's call and results stay whole until the next step
        messages.append({"role": "assistant", "content": msg.content,
                         "tool_calls": [c.model_dump(include={"id", "type", "function"}) for c in msg.tool_calls]})
        for c in msg.tool_calls:
            name = c.function.name
            try:
                payload = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                result = {"error": "Arguments are not a JSON object."}
            elif why := _refused(name, payload, allowed, files, g, measures, tables, a.get("agents", [])):
                result = {"error": why}
            else:
                result = _call(project, name, payload, user, dry, lambda ms: studio.outside({"measures": ms}, measures, tables, g), files,
                               # D-32: also an agent step of a message-triggered flow (its prompt may hold the text)
                               (a["id"], run_id) if via == "inbound" or actions.INBOUND.get() else None, shared)
            trace.append({"tool": name, "input": payload, "output": result})
            if name == studio.ASK and "result" in result:  # AG-3: the asked agent's own trace and tokens, nested
                with db.connect() as conn:
                    trace[-1]["run"] = conn.execute(
                        "SELECT id::text, agent, trace, prompt_tokens, completion_tokens, reasoning_tokens FROM agent_runs"
                        " WHERE id = %s", [result["result"]["run"]]).fetchone()
            (outputs if "result" in result else notes).append(result.get("result", result))  # LLM-2-fix-7
            messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(_cap(result, seen), default=str)})
    else:  # no final answer within MAX_STEPS: only the tool outputs
        text, template, grounded = grounding.template(outputs), True, False if ground else None
    with db.connect() as conn:  # own transaction: the trace survives a caller that fails afterwards (AG-3)
        conn.execute(
            "INSERT INTO agent_runs (id, project, agent, version, user_id, via, question, trace, answer, grounded, retried,"
            " template, prompt_tokens, completion_tokens, reasoning_tokens, effort, flow_run)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [run_id, project, a["id"], a["version"], user.id, via, question, Jsonb(trace, dumps=lambda o: json.dumps(o, default=str)),
             text, grounded, retried, template, *tokens[:3], effort,
             # 1.2.6 M4: flows.STEP is set around a flow step's action and inherited by the agents it asks (same context)
             (flows.STEP.get() or (None,))[0]])  # cached: per call in llm_usage
    # the last tabular tool result, as the tool returned it (principle 7): inbound replies show it (X-02.4)
    data = next((o for o in reversed(outputs) if isinstance(o, dict) and {"columns", "rows"} <= o.keys()
                 and "snapshot" not in o), None)  # GATE-9-fix-3: a load's snapshot row is not the data
    return {"run": run_id, "answer": text, "grounded": grounded, "retried": retried, "template": template,
            "tools": [t["tool"] for t in trace if t["tool"] not in ("grounding", "empty answer")], "data": data,
            **({"columns": data["columns"], "rows": data["rows"]} if data else {}),  # D-22: its figures ground the Main Agent
            # the last report_snapshot it took (`artifact:<project>/<file>`): an email reply attaches it (X-02.6)
            "snapshot": next((o["snapshot"] for o in reversed(outputs) if isinstance(o, dict) and isinstance(o.get("snapshot"), str)), None)}


def _contains(answer: str, x) -> bool:
    if isinstance(x, str):
        return grounding._has(x, answer)
    return any(abs(v - x) <= tol or (not neg and abs(v - abs(x)) <= tol) for _, v, tol, _, neg in grounding.numbers(answer))


def _evaluate(project: str, a: dict, files: dict, user: User) -> list[dict]:
    """AG-4: each case passes when its answer is grounded without the template, contains every `expect.contains`
    value (numbers as written or rounded) and called every `expect.tools` tool. Action tools don't run (dry)."""
    out = []
    for c in (files.get(a["evaluation"]) or {}).get("cases", []):
        r = _answer(project, a, c["question"], user, "eval", files, ground=True, dry=True)
        missing = (["a grounded answer"] if r["template"] else []) + \
            [json.dumps(x) for x in c["expect"]["contains"] if not _contains(r["answer"], x)] + \
            [f"a call to {t}" for t in c["expect"].get("tools", []) if t not in r["tools"]]
        out.append({"question": c["question"], "passed": not missing, "missing": missing, "answer": r["answer"], "run": r["run"]})
    return out


class Ask(studio.AgentRef):
    question: str = Field(min_length=1, max_length=20_000, validation_alias=AliasChoices("question", "prompt"),
                          description="the question (a flow agent step's `prompt`)")


@action(Role.viewer)
def run_agent(conn, user: User, args: Ask) -> dict:
    """Asks a custom agent a question, as you: it answers only through its own tools within its scope, every number and name grounded in tool results (or a template answer from them). Drafts run only for editors testing them; disabled agents (failing evaluation set) don't run. Returns the answer and the run ID of its trace."""
    files = bundle(git.path(args.project))
    a = _load(files, args.project, args.agent)
    studio.require_ready(args.project)
    via = actions.VIA.get()
    if via == "mcp" and not any(c["type"] == "mcp" for c in a.get("channels", [])):  # §7.2 channels, D-33
        raise ActionFailed(403, f"Agent '{args.agent}' doesn't answer over MCP; its owner can add the MCP channel.")
    role = role_in(conn, user, args.project)  # None: a share recipient (actions.run checked the share, E-03.2)
    depth, dry = NEST.get()
    if depth and a["status"] != "published":  # AGENT-AS-TOOL
        raise ActionFailed(403, f"Agent '{args.agent}' is a draft; agents ask only published agents.")
    if a["status"] != "published" and (via in ("flow", "mcp") or (role or 0) < Role.editor):  # AG-4, §5 #3
        raise ActionFailed(403, f"Agent '{args.agent}' is a draft; only editors can run it to test it in the app, and flows"
                                " and MCP need it published.")
    state = conn.execute("SELECT error FROM agent_state WHERE project = %s AND agent = %s", [args.project, args.agent]).fetchone()
    if state and state["error"]:
        raise ActionFailed(409, f"Agent '{args.agent}' is disabled because its evaluation set fails: {state['error']}")
    # §7.2; an asked agent always grounds: its text goes to another LLM, not to a person who sees it is unchecked
    return _answer(args.project, a, args.question, user, via, files, ground=a["grounding"] or bool(depth) or via in ("flow", "inbound", "mcp"),
                   dry=dry, shared=role is None)


@action(Role.editor)
def test_agent(conn, user: User, args: studio.AgentRef) -> dict:
    """Runs a custom agent's evaluation set (draft or published) as you: each case passed or failed, with what is missing and the answer. Its send and change tools don't run."""
    files = bundle(git.path(args.project))
    a = _load(files, args.project, args.agent)
    studio.require_ready(args.project)
    cases = _evaluate(args.project, a, files, user)
    return {"passed": bool(cases) and all(c["passed"] for c in cases), "cases": cases,  # D-22: each case's run ID
            "columns": ["case", "run", "passed"], "rows": [[i, c["run"], c["passed"]] for i, c in enumerate(cases, 1)]}


def _stamp(files: dict, rel: str) -> str:
    """AG-4: the model files, the tools, the agent with its evaluation set and the agents it asks."""
    a = files[rel]
    return hashlib.sha256(dumps([{k: v for k, v in files.items() if k.startswith((*tools.MODEL, "tools/"))}, a,
                                 files.get(a["evaluation"]),
                                 # AGENT-AS-TOOL: the agents it asks (absent otherwise: existing stamps stay, no paid re-run)
                                 *([[files.get(f"agents/{x}.json") for x in a["agents"]]] if a.get("agents") else [])]).encode()).hexdigest()


def _record(conn, project: str, a: dict, stamp: str, error: str | None) -> None:
    """Store the agent's evaluation outcome (a failure disables it) and post a change of it to the feed (AG-4, D-10)."""
    conn.execute("INSERT INTO agent_state VALUES (%s, %s, %s, %s) ON CONFLICT (project, agent)"
                 " DO UPDATE SET stamp = EXCLUDED.stamp, error = EXCLUDED.error", [project, a["id"], stamp, error])
    owner = conn.execute(f"SELECT name FROM users WHERE id = %s AND {MEMBERS}", [int(a["owner"]), project, project]).fetchone()
    notify_change("agent", project, f"Agent '{a['id']}' ", error and f"is disabled; owner {owner['name'] if owner else a['owner']},"
                  f" please fix it: {error}", "passes its evaluation set again and is enabled.", a["id"])


@action(Role.editor, definition=True)
def publish_agent(conn, user: User, args: studio.AgentRef) -> dict:
    """Publishes a custom agent for everyone in the project (asks the user to confirm): runs its evaluation set and, if every case passes, sets it published with the next version. Refused with the failures otherwise."""
    files = bundle(git.path(args.project))
    a = _load(files, args.project, args.agent)
    studio.require_ready(args.project)
    if not (files.get(a["evaluation"]) or {}).get("cases"):
        raise ActionFailed(400, f"Agent '{args.agent}' has no evaluation cases; add them with save_agent.")
    ask = imports.grant_check(args.project, {f"agents/{a['id']}.json": {**a, "status": "published", "version": a["version"] + 1}})
    require_confirmation(f"Publishing agent '{a['id']}' makes it available to everyone in project '{args.project}'."  # §5 #3
                         + (f" {ask[0]}" if ask else ""), ask and ask[1])  # 1.2.6 M4: an Owner-granted flow runs it
    if failed := [c for c in _evaluate(args.project, a, files, user) if not c["passed"]]:
        raise ActionFailed(400, f"Agent '{args.agent}' isn't published: {len(failed)} evaluation case(s) fail.", failures=failed)
    a = {**a, "status": "published", "version": a["version"] + 1}
    rel = f"agents/{a['id']}.json"
    imports.check(conn, args.project, files | {rel: a})
    imports.write(args.project, {rel: a})
    _record(conn, args.project, a, _stamp(files | {rel: a}, rel), None)
    return tools.saved(a)


def retest(project: str) -> None:
    """AG-4 (TL-4 path): re-run the evaluation set of each published agent whose model files, tools or definition
    changed since its last run, as its owner; a failing agent is disabled and the feed says so once. A worker job
    (audited via worker): never on the owner's ChatGPT plan (D-53), and with no workspace model the change is posted
    as not re-tested and the agent keeps its state (LLM-2-fix-16)."""
    # ponytail: a disabled agent re-runs only on such a change (each run costs LLM tokens), unlike tools, which re-run
    # every minute; re-run disabled agents after each refresh too if data loads alone should re-enable them.
    files = bundle(git.path(project))
    via = actions.VIA.set("worker")
    try:
        with db.connect() as conn:
            state = {r["agent"]: r["stamp"] for r in conn.execute("SELECT agent, stamp FROM agent_state WHERE project = %s", [project])}
            for rel, a in files.items():
                if imports._kind(rel) != "agent" or a.get("status") != "published" or state.get(a["id"]) == (stamp := _stamp(files, rel)):
                    continue
                u = security.user(conn, int(a["owner"]))
                skipped = None
                try:
                    if u is None:
                        raise ActionFailed(400, f"its owner (user {a['owner']}) no longer exists.")
                    if bad := studio.tools_outside(a, files):  # D-24: a tool or measure edited to read outside the scope
                        raise ActionFailed(400, "; ".join(f"tool '{t}' reads outside its knowledge scope: {', '.join(o)}" for t, o in bad.items()))
                    studio.require_ready(project)
                    failed = [f"'{c['question']}' misses {', '.join(c['missing'])}" for c in
                              _evaluate(project, a, files, u) if not c["passed"]]
                    error = "evaluation fails: " + "; ".join(failed) if failed else None
                except llm.NoModel as e:  # a ChatGPT-only workspace must not disable every published agent
                    skipped = f"Agent '{a['id']}' was not re-tested after a change: {e}"
                except ActionFailed as e:
                    error = str(e)
                if skipped:  # this stamp counts as run (posted once); the agent keeps its enabled/disabled state
                    conn.execute("INSERT INTO agent_state VALUES (%s, %s, %s, NULL) ON CONFLICT (project, agent)"
                                 " DO UPDATE SET stamp = EXCLUDED.stamp", [project, a["id"], stamp])
                    notify("agent", skipped, project, a["id"], len(skipped))
                else:
                    _record(conn, project, a, stamp, error)
                actions.audit(conn, u, "worker", "test_agent", project, {"agent": a["id"]}, 400 if skipped or error else 200,
                              skipped or error)  # SC-6
                conn.commit()  # one agent at a time: a later failure keeps the earlier outcomes
    finally:
        actions.VIA.reset(via)


class Runs(storage.ProjectRef):
    agent: str | None = Field(None, pattern=ID, description="omit for every agent")
    limit: int = Field(20, ge=1, le=200)


@action(Role.viewer)
def agent_runs(conn, user: User, args: Runs) -> dict:
    """Custom agent run history, newest first, with each run's trace (tools called, inputs, outputs), final answer and grounding result; `tokens` = LLM token totals per agent. Under row-level security rules a Viewer sees only their own runs."""
    # SE-6: other people's runs (and flow/evaluation runs as the owner) carry their unfiltered rows in trace and answer
    # E-03.2: a share recipient (no role; actions.run checked the agent's share) sees only its own runs and tokens of it
    guest = role_in(conn, user, args.project) is None
    mine = user.id if guest or semantic.rls.restricted(args.project, user.id) else None
    runs = conn.execute(
        "SELECT id::text, at, agent, version, user_id, via, question, trace, answer, grounded, retried, template, prompt_tokens,"
        " completion_tokens, reasoning_tokens, effort, flow_run FROM agent_runs WHERE project = %s AND agent = coalesce(%s, agent) AND (%s::bigint IS NULL OR user_id = %s)"
        " ORDER BY at DESC, id LIMIT %s", [args.project, args.agent, mine, mine, args.limit]).fetchall()
    tokens = conn.execute("SELECT agent, count(*) AS runs, sum(prompt_tokens)::int AS prompt_tokens, sum(completion_tokens)::int"
                          " AS completion_tokens FROM agent_runs WHERE project = %s AND (%s OR agent = %s AND user_id = %s)"
                          " GROUP BY agent ORDER BY agent", [args.project, not guest, args.agent, user.id]).fetchall()
    # 1.2.6 M4: an agent run inside a flow run an object action started carries its runner's typed inputs
    editor = (security.role_in(conn, user, args.project) or 0) >= Role.editor
    acted = {r["id"]: r["started_by"] for r in conn.execute("SELECT id, started_by FROM flow_runs WHERE id = ANY(%s) AND trigger ? 'action'",
                                                           [[r["flow_run"] for r in runs if r["flow_run"]]])}
    runs = [r | {"question": None, "trace": None, "answer": None} if r["flow_run"] in acted and not editor and acted[r["flow_run"]] != user.id
            else r for r in runs]
    # D-22: quotable; D-114: never the agent ID or the run's question (author/LLM-written, a digit there would ground),
    # and the answer only when AG-2 checked it (grounding off: LLM text nobody verified)
    return {"runs": runs, "tokens": tokens,
            "columns": ["run", "at", "version", "answer", "prompt_tokens", "completion_tokens", "reasoning_tokens", "tokens"],
            "rows": [[r["id"], r["at"], r["version"], r["answer"] if r["grounded"] is True else None, r["prompt_tokens"],
                      r["completion_tokens"], r["reasoning_tokens"], r["prompt_tokens"] + r["completion_tokens"]] for r in runs]}
