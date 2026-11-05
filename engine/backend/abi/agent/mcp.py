"""MCP server at /api/mcp (SE-9, §7.2 channel MCP, DECISIONS D-33).

An outside LLM gets exactly the Main Agent's powers: the tools are `agent.tools()` (no SecretStr action, SC-4), each
call runs in-process through `agent._call` as the bearer token's user with `via="mcp"`, so every agent rule applies
(`actions.by_agent()`: edits become proposals, WB-9; approvals and token rotation stay a person's click in the app).
A confirm token never leaves: a call that needs a confirmation returns an error result saying "do it in the app".
Transport: the official `mcp` SDK's Streamable HTTP session manager, stateless with JSON responses (both protocol
eras: 2026-07-28 per-request metadata and the initialize handshake); auth is the app's session token (no OAuth, D-33).
M4.5: /api/mcp/outside (the app's /mcp) is the same server for outside clients signed in with a personal token: only
OUTSIDE's reads are listed and run, each audited as the person with via "mcp:token".
"""

import contextlib
import json
from urllib.parse import urlsplit

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse

from abi import actions, agent, db
from abi.security import user_for_personal, user_for_token


# M4.5: what a personal token may call, chosen by what each does: reads of data, the model and its documentation as the
# person (row-level and column rules hold). Nothing that changes anything, confirms, runs an agent, tool or flow, renders
# a file, spends model tokens or lists secrets, rules, share links or people.
OUTSIDE = frozenset({"list_projects", "semantic_model", "semantic_query", "visual_data", "dataset_data", "list_reports",
                     "explain_change", "how_calculated", "lineage", "docs", "readiness", "data_quality", "project_locale"})


async def _list_tools(ctx, params) -> types.ListToolsResult:
    outside = ctx.request.scope.get("abi_outside")
    return types.ListToolsResult(tools=[types.Tool(name=f["name"], description=f["description"], input_schema=f["parameters"])
                                        for f in (t["function"] for t in agent.tools())
                                        if not outside or f["name"] in OUTSIDE])


def _outside(name: str, args: str, user):
    """A personal token's call: a read of OUTSIDE as the person, else refused; audited either way as an outside client."""
    actions.OUTSIDE.set(True)
    if name in OUTSIDE:
        return agent._call(name, args, user, "mcp")
    with db.connect() as conn:
        actions.audit(conn, user, "mcp", name, None, None, 403, "a personal token only reads")
    return {"error": f"'{name}' isn't available with a personal token: it only reads. Do it in the app."}, None


async def _call_tool(ctx, params) -> types.CallToolResult:
    user = ctx.request.scope["abi_user"]
    args = json.dumps(params.arguments or {})
    call = _outside if ctx.request.scope.get("abi_outside") else lambda n, a, u: agent._call(n, a, u, "mcp")
    result, confirm = await anyio.to_thread.run_sync(lambda: call(params.name, args, user))
    if confirm is not None:  # D-33: the token stays here; the person confirms in the app
        result = {"error": result["message"] + " It needs your confirmation, which is never given over MCP:"
                  " do it in the app."}
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(agent._cap(result), default=str))],
                                is_error="result" not in result)


server = Server("abi", on_list_tools=_list_tools, on_call_tool=_call_tool)
_manager: StreamableHTTPSessionManager | None = None


@contextlib.asynccontextmanager
async def running():
    """App lifespan: a manager runs only once, so each app start gets a fresh one."""
    global _manager
    _manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
    async with _manager.run():
        yield


def _user(header: str, outside: bool = False):
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    with db.connect() as conn:
        return (user_for_personal if outside else user_for_token)(conn, token)


class Endpoint:
    """ASGI endpoint: session auth (401) and the spec's Origin check (403: a browser page from another origin).
    outside: a personal token instead, for OUTSIDE's reads only (M4.5)."""

    def __init__(self, outside: bool = False):
        self.outside = outside

    async def __call__(self, scope, receive, send):
        h = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        if "origin" in h and urlsplit(h["origin"]).netloc != h.get("host"):
            return await JSONResponse({"detail": "Cross-origin MCP requests are refused."}, 403)(scope, receive, send)
        scope["abi_user"] = await anyio.to_thread.run_sync(_user, h.get("authorization", ""), self.outside)
        scope["abi_outside"] = self.outside
        if scope["abi_user"] is None:
            return await JSONResponse({"detail": "Invalid or expired session."}, 401,
                                      {"WWW-Authenticate": "Bearer"})(scope, receive, send)
        await _manager.handle_request(scope, receive, send)
