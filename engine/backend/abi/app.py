"""app container entrypoint: FastAPI + the built web UI. PRD §4."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from abi import (
    actions,
    agent,
    db,
    docs,  # noqa: F401  registers the docs actions
    flows,  # noqa: F401  registers the flow actions
    ingest,
    ml,  # noqa: F401  registers the ML actions
    ops,
    reports,  # noqa: F401  registers the report actions
    semantic,
    tools,
)
from abi.agent import chatgpt, mcp
from abi.agents import (  # noqa: F401  registers agent and chat actions
    chats,
    runs,
    studio,
)
from abi.flows import inbound
from abi.project import (  # noqa: F401  registers the project actions
    git,
    imports,
    remote,
    sharing,
)
from abi.reports import embed, share
from abi.security import (
    accounts,
    mail,
    oidc,
    tenants,
    totp,
    usage,
)
from abi.semantic import (  # noqa: F401  registers their actions
    explain,
    lineage,
    model,
    rls,
    sql,
    subscriptions,
    tiles,
)
from abi.writeback import (  # noqa: F401  registers sync_edits (WB-10), edit_total (WB-11)
    sync,
    total,
)


@asynccontextmanager
async def lifespan(app):
    db.init()
    await sql.start()  # SE-10, when ABI_SQL_PORT is set
    try:
        async with mcp.running():
            yield
    finally:
        await sql.stop()


BODY_MAX = 10 * 1024**2  # D-111: FastAPI reads a JSON body whole before auth runs, so every body is capped first


class BodyLimit(RequestBodyLimitMiddleware):
    """413 past BODY_MAX, counted as bytes stream in. IN-7 chunks are exempt: `upload_chunk` checks the user and
    session before reading and caps each body itself (CHUNK_MAX, streamed to disk)."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "PATCH" and scope["path"].startswith("/api/uploads/"):
            return await self.app(scope, receive, send)
        await super().__call__(scope, receive, send)


HEADERS = {  # H-01.2-fix-3: browser hardening on every response; a route's own value wins (embed page CSP, tenant logo)
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "frame-ancestors 'none'; script-src 'self'; object-src 'none'; base-uri 'none'",
}


class Headers:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def headed(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                for k, v in HEADERS.items():
                    h.setdefault(k, v)
            await send(message)

        await self.app(scope, receive, headed if scope["type"] == "http" else send)


app = FastAPI(title="Agentic Open BI Platform", lifespan=lifespan)
app.add_middleware(BodyLimit, max_body_size=BODY_MAX)  # FastAPI ignores Starlette's max_body_size argument
app.add_middleware(Headers)  # outermost: a 413 from BodyLimit gets the headers too
app.add_middleware(ops.Timed)  # H-04: times every response, 413s included


@app.exception_handler(RequestValidationError)
async def invalid(request, e: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 without `input`: it echoes typed passwords and link tokens (SC-4, H-01.1-fix-6), as actions do."""
    return JSONResponse(jsonable_encoder({"detail": [{k: v for k, v in x.items() if k != "input"} for x in e.errors()]}),
                        status_code=422)

app.include_router(accounts.router)
app.include_router(actions.router)
app.include_router(agent.router)
# ABI-PI: no ChatGPT link flow (models come from Settings): chatgpt.router is not mounted
# app.include_router(chatgpt.router)  # AG-6: also /callback, OpenAI's loopback redirect (D-53)
app.include_router(inbound.router)
app.include_router(mail.router)
app.include_router(totp.router)
app.include_router(oidc.router)
app.include_router(ingest.router)
app.include_router(ops.router)  # H-04 /api/ready, /metrics
app.include_router(ingest.oauth.router)  # API-SRC.3 connect flow
app.include_router(semantic.router)
app.include_router(embed.router)
app.include_router(share.router)
app.include_router(tenants.router)
app.include_router(tiles.router)
app.include_router(tools.builtin.router)
app.include_router(usage.router)
app.router.add_route("/api/mcp", mcp.Endpoint())  # SE-9 MCP (D-33)
app.router.add_route("/api/mcp/outside", mcp.Endpoint(outside=True))  # M4.5: the app's /mcp, personal tokens


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# D-98 AGPL §13: every network user can get the running version's source. The default is the public repo;
# a modified deployment sets ABI_SOURCE_URL to where its own changed source is published.
@app.get("/api/version")
def version() -> dict:
    source = os.environ.get("ABI_SOURCE_URL") or "https://github.com/Himanshu8881212/abi-pi"  # compose passes it empty when unset
    return {"version": db.VERSION, "source": source}


# The Docker image copies the Vite build here; in dev, Vite serves the UI itself.
_ui = Path(__file__).parent.parent / "static"


@app.get("/embed/{kid}", include_in_schema=False)
def embed_page(kid: str) -> FileResponse:
    """RP-9: the UI page an embed iframe loads, framable only by its key's origins (the token stays in the fragment)."""
    return FileResponse(_ui / "index.html", headers={"Content-Security-Policy": embed.frame_ancestors(kid)})


if _ui.is_dir():
    app.mount("/", StaticFiles(directory=_ui, html=True), name="ui")
