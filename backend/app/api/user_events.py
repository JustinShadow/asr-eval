"""跨页面资源变更的用户级 SSE 接口。"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ApiError
from app.core.rate_limit import require_limit, sse_connection_limiter
from app.core.sse import format_sse
from app.domain.auth import consume_stream_ticket
from app.domain.user_events import user_event_bus

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.get("/events")
async def user_events(
    ticket: str = Query(),
    db: Session = Depends(get_db),
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    """输出跨页面变更信号，支持一次性 ticket、游标续传与 resync。"""
    username = consume_stream_ticket(db, ticket)
    if username is None:
        raise ApiError("E_TICKET_INVALID", "stream ticket 无效或已使用", status_code=401)
    if not sse_connection_limiter.acquire(username, 5):
        require_limit(1, message="同时打开的事件流数量已达上限")
    last_event_id = last_event_id_header or last_event_id_query

    async def generate() -> AsyncIterator[str]:
        deadline = asyncio.get_running_loop().time() + 30 * 60
        cursor = 0
        replay = user_event_bus.buffer.replay("user", last_event_id)
        if last_event_id is None:
            cursor = replay.latest.event_id if replay.latest else 0
        elif replay.resync:
            message = user_event_bus.publish_resync("history_unavailable")
            cursor = message.event_id
            yield format_sse(message)
        else:
            for message in replay.events:
                cursor = message.event_id
                yield format_sse(message)
            if replay.latest is not None:
                cursor = max(cursor, replay.latest.event_id)

        last_heartbeat = time.monotonic()
        while asyncio.get_running_loop().time() <= deadline:
            latest = user_event_bus.buffer.replay("user", None).latest
            if latest is not None and latest.event_id > cursor:
                replay = user_event_bus.buffer.replay("user", str(cursor))
                if replay.resync:
                    message = user_event_bus.publish_resync("history_unavailable")
                    cursor = message.event_id
                    yield format_sse(message)
                else:
                    for message in replay.events:
                        cursor = message.event_id
                        yield format_sse(message)
            if time.monotonic() - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(0.25)
        yield 'event: end\ndata: {"reason":"expired"}\n\n'

    async def managed_stream() -> AsyncIterator[str]:
        try:
            async for chunk in generate():
                yield chunk
        finally:
            sse_connection_limiter.release(username)

    return StreamingResponse(
        managed_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


user_events.__dict__["requires_authentication"] = True
