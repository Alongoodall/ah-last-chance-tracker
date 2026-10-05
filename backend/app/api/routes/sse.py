"""Server-Sent Events endpoint.

Clients connect to ``GET /api/events`` and receive a stream of SSE messages.
The connection stays open indefinitely; a ``ping`` message is sent every
30 seconds so proxies / load balancers don't kill idle connections.

Message types
-------------
``snapshot_ready``
    Emitted by the collector after each successful snapshot.
    Payload: ``{store_id, snapshot_id, item_count, fetched_at}``

``ping``
    Keepalive; clients should ignore the payload.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.event_bus import PING_MESSAGE, EventBus, make_sse_message

router = APIRouter(tags=["events"])

_PING_INTERVAL_SECONDS = 30


@router.get(
    "/api/events",
    summary="Server-Sent Events stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def sse_stream(request: Request) -> StreamingResponse:
    """Open an SSE connection and receive live updates.

    The stream stays open until the client disconnects. Events:

    - ``snapshot_ready`` — new bargain data is available for a store
    - ``ping`` — keepalive (every 30 s); ignore this

    Example JavaScript::

        const es = new EventSource('/api/events');
        es.addEventListener('snapshot_ready', e => {
            const data = JSON.parse(e.data);
            console.log('New data for store', data.store_id);
        });
    """
    bus: EventBus = request.app.state.event_bus
    queue = bus.subscribe()

    async def generate() -> AsyncIterator[str]:
        try:
            while True:
                # Check if client disconnected before waiting for next event
                if await request.is_disconnected():
                    break

                try:
                    envelope = await asyncio.wait_for(
                        queue.get(), timeout=_PING_INTERVAL_SECONDS
                    )
                    yield make_sse_message(envelope["type"], envelope["data"])
                except asyncio.TimeoutError:
                    # Send keepalive ping so the connection stays alive
                    yield PING_MESSAGE
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
            "Connection": "keep-alive",
        },
    )
