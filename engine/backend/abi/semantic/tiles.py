"""Live tiles: an SSE stream per project that says when a live ClickHouse table changed. PRD RP-7, SE-5, SC-2.

One poller per project reads every live table's change stamp (`live.stamp`, D-2 allow, scrubbed errors) every TICK
seconds, only while a stream of that project is open, and stops when the last one closes. Each stream gets
`changed {tables}`; the report page re-fetches the visuals whose `live` tables (from `visual_data`) intersect.
Auth is the Bearer header (no token in the URL, SC-4), so the UI reads the stream with fetch, as the chat does.
"""

import asyncio
import logging
from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent

from abi import db
from abi.actions import ActionFailed, audit, current_user
from abi.security import User, role_in
from abi.semantic import Project, live

TICK = 5.0  # ponytail: a fixed 5 s poll is the freshness ceiling; ClickHouse has no change feed to push instead
MAX_STREAMS, MAX_PER_USER = 200, 10  # D-17: open streams are bounded, so a client can't pile up pollers or queues
router = APIRouter(prefix="/api")
_subs: dict[str, dict[int, tuple[int, set, asyncio.Event]]] = {}  # project -> {id: (user id, pending tables, wake)}
_pollers: dict[str, asyncio.Task] = {}


def _stamps(project: str, refused: set) -> dict[str, str | None]:
    """Stamps of the project's live tables; a refused (D-2) or failed (logged, scrubbed) read gives no stamp, so no
    event that tick. A refusal is audited (SC-6) once per run of refused ticks (`refused`), not every TICK."""
    out = {}
    for t in live._sources(project):
        try:
            out[t] = live.stamp(project, t)
            refused.discard(t)
        except ActionFailed as e:
            if e.status == 403 and t not in refused:
                refused.add(t)
                with db.connect() as conn:
                    audit(conn, None, "worker", "live_stamp", project, {"table": t}, 403, str(e))
    return out


async def _poll(project: str) -> None:
    last: dict = {}
    refused: set = set()
    while True:
        try:
            now = await asyncio.to_thread(_stamps, project, refused)
        except Exception:  # never let a bad source file end the poller; the next tick retries
            logging.getLogger(__name__).exception("live stamps of %s failed", project)
            now = {}
        if changed := {t for t, s in now.items() if t in last and (s is None or s != last[t])}:
            for _, pending, wake in _subs.get(project, {}).values():
                pending |= changed
                wake.set()
        last |= now
        await asyncio.sleep(TICK)


def _has_role(user: User, project: str) -> bool:
    with db.connect() as conn:
        return role_in(conn, user, project) is not None


def _streamer(project: Project, user: Annotated[User, Depends(current_user)]) -> str:
    """A dependency, so a refusal is an HTTP status before the stream starts."""
    if not _has_role(user, project):  # SC-2: any role may view
        raise HTTPException(403, f"You have no role in project '{project}'.")
    subs = [s for p in _subs.values() for s in p.values()]
    if len(subs) >= MAX_STREAMS or sum(s[0] == user.id for s in subs) >= MAX_PER_USER:
        raise HTTPException(503, "Too many open live streams; close another report tab or try again later.")
    return project


@router.get("/projects/{project}/live", response_class=EventSourceResponse)
async def live_stream(project: Annotated[str, Depends(_streamer)],
                      user: Annotated[User, Depends(current_user)]) -> AsyncIterable[ServerSentEvent]:
    """RP-7: `changed {tables}` whenever a live table of the project changes."""
    pending, wake = set(), asyncio.Event()
    subs = _subs.setdefault(project, {})
    subs[id(wake)] = (user.id, pending, wake)
    if (p := _pollers.get(project)) is None or p.done():
        _pollers[project] = asyncio.create_task(_poll(project))
    try:
        while True:
            await wake.wait()
            wake.clear()
            tables = sorted(pending)
            pending.clear()
            if not await asyncio.to_thread(_has_role, user, project):  # role removed while open (SC-2)
                return
            yield ServerSentEvent(event="changed", data={"tables": tables})
    finally:
        subs.pop(id(wake), None)
        if not subs:
            _subs.pop(project, None)
            if p := _pollers.pop(project, None):
                p.cancel()
