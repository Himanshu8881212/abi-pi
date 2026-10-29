"""MCP server at /api/mcp (SE-9, §7.2 channel MCP, DECISIONS D-33).

An outside LLM gets exactly the Main Agent's powers: the tools are `agent.tools()` (no SecretStr action, SC-4), each
call runs in-process through `agent._call` as the bearer token's user with `via="mcp"`, so every agent rule applies
(`actions.by_agent()`: edits become proposals, WB-9; approvals and token rotation stay a person's click in the app).
A confirm token never leaves: a call that needs a confirmation returns an error result saying "do it in the app".
Transport: the official `mcp` SDK's Streamable HTTP session manager, stateless with JSON responses (both protocol
eras: 2026-07-28 per-request metadata and the initialize handshake); auth is the app's session token (no OAuth, D-33).
"""

import contextlib
import json
from urllib.parse import urlsplit

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse

from abi import agent, db
from abi.security import user_for_token


async def _list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[types.Tool(name=f["name"], description=f["description"], input_schema=f["parameters"])
                                        for f in (t["function"] for t in agent.tools())])


async def _call_tool(ctx, params) -> types.CallToolResult:
    user = ctx.request.scope["abi_user"]
    args = json.dumps(params.arguments or {})
    result, confirm = await anyio.to_thread.run_sync(lambda: agent._call(params.name, args, user, "mcp"))
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


def _user(header: str):
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    with db.connect() as conn:
        return user_for_token(conn, token)


class Endpoint:
    """ASGI endpoint: session auth (401) and the spec's Origin check (403: a browser page from another origin)."""

    async def __call__(self, scope, receive, send):
        h = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        if "origin" in h and urlsplit(h["origin"]).netloc != h.get("host"):
            return await JSONResponse({"detail": "Cross-origin MCP requests are refused."}, 403)(scope, receive, send)
        scope["abi_user"] = await anyio.to_thread.run_sync(_user, h.get("authorization", ""))
        if scope["abi_user"] is None:
            return await JSONResponse({"detail": "Invalid or expired session."}, 401,
                                      {"WWW-Authenticate": "Bearer"})(scope, receive, send)
        await _manager.handle_request(scope, receive, send)
