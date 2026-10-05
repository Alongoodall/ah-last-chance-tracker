"""Lightweight in-process pub/sub for Server-Sent Events.

Design
------
- One ``EventBus`` instance lives on ``app.state.event_bus`` for the lifetime
  of the FastAPI application.
- The collector calls ``await bus.publish(event)`` after each snapshot.
- The SSE endpoint subscribes a per-connection ``asyncio.Queue``, reads from
  it in a loop, and unsubscribes when the client disconnects.

Thread-safety
-------------
All methods are called from the asyncio event loop (FastAPI/uvicorn is
single-threaded async). No locking is needed.

Back-pressure
-------------
Each queue has a fixed maxsize. If a slow client's queue is full the event
is silently dropped (better than blocking the collector).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_QUEUE_MAXSIZE = 50  # events per subscriber before drop


class EventBus:
    """Fanout pub/sub backed by per-subscriber asyncio Queues."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []

    # ------------------------------------------------------------------
    # Publisher API
    # ------------------------------------------------------------------

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast an event to all connected subscribers.

        Args:
            event_type: SSE event name (e.g. ``"snapshot_ready"``).
            data:       JSON-serialisable payload dict.
        """
        if not self._queues:
            return  # fast-path: nothing to do

        envelope = {"type": event_type, "data": data}
        dead: list[asyncio.Queue] = []

        for q in self._queues:
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                logger.warning(
                    "event_bus_queue_full",
                    event_type=event_type,
                    hint="Slow SSE client — event dropped",
                )
                dead.append(q)

        # Prune queues that are consistently full (dead clients)
        for q in dead:
            self._try_remove(q)

        logger.debug(
            "event_bus_published",
            event_type=event_type,
            subscriber_count=len(self._queues),
        )

    # ------------------------------------------------------------------
    # Subscriber API
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber and return its queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._queues.append(q)
        logger.debug("event_bus_subscribed", total=len(self._queues))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber's queue (called when the client disconnects)."""
        self._try_remove(q)
        logger.debug("event_bus_unsubscribed", total=len(self._queues))

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _try_remove(self, q: asyncio.Queue) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass  # already removed


def make_sse_message(event_type: str, data: dict[str, Any]) -> str:
    """Format a single SSE message string.

    The SSE wire format is::

        event: <type>\\n
        data: <json>\\n
        \\n
    """
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


PING_MESSAGE = "event: ping\ndata: {}\n\n"
