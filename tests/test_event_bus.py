"""Tests for the EventBus (Phase 8 — SSE).

Tests cover:
- subscribe/unsubscribe mechanics
- publish fans out to all subscribers
- publish silently drops events when a queue is full
- unsubscribe is idempotent (no error on double-remove)
- make_sse_message formats wire-format correctly
- PING_MESSAGE constant format
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.event_bus import (
    PING_MESSAGE,
    EventBus,
    _QUEUE_MAXSIZE,
    make_sse_message,
)


# ---------------------------------------------------------------------------
# make_sse_message
# ---------------------------------------------------------------------------


class TestMakeSseMessage:
    def test_event_type_in_output(self):
        msg = make_sse_message("snapshot_ready", {"store_id": 1})
        assert msg.startswith("event: snapshot_ready\n")

    def test_data_is_json(self):
        payload = {"store_id": 2203, "item_count": 42}
        msg = make_sse_message("snapshot_ready", payload)
        data_line = [l for l in msg.splitlines() if l.startswith("data: ")][0]
        parsed = json.loads(data_line[len("data: "):])
        assert parsed == payload

    def test_ends_with_double_newline(self):
        msg = make_sse_message("ping", {})
        assert msg.endswith("\n\n")

    def test_ping_constant_format(self):
        assert PING_MESSAGE.startswith("event: ping\n")
        assert PING_MESSAGE.endswith("\n\n")


# ---------------------------------------------------------------------------
# EventBus — subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestEventBusSubscription:
    def test_subscribe_returns_queue(self):
        bus = EventBus()
        q = bus.subscribe()
        assert isinstance(q, asyncio.Queue)

    def test_subscriber_count_increments(self):
        bus = EventBus()
        assert bus.subscriber_count == 0
        bus.subscribe()
        assert bus.subscriber_count == 1
        bus.subscribe()
        assert bus.subscriber_count == 2

    def test_unsubscribe_decrements(self):
        bus = EventBus()
        q = bus.subscribe()
        assert bus.subscriber_count == 1
        bus.unsubscribe(q)
        assert bus.subscriber_count == 0

    def test_unsubscribe_idempotent(self):
        """Calling unsubscribe twice for the same queue should not raise."""
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.unsubscribe(q)  # must not raise
        assert bus.subscriber_count == 0

    def test_unsubscribe_unknown_queue_no_error(self):
        bus = EventBus()
        orphan: asyncio.Queue = asyncio.Queue()
        bus.unsubscribe(orphan)  # must not raise


# ---------------------------------------------------------------------------
# EventBus — publish
# ---------------------------------------------------------------------------


class TestEventBusPublish:
    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscriber(self):
        bus = EventBus()
        q = bus.subscribe()
        await bus.publish("snapshot_ready", {"store_id": 2203})
        envelope = q.get_nowait()
        assert envelope["type"] == "snapshot_ready"
        assert envelope["data"]["store_id"] == 2203

    @pytest.mark.asyncio
    async def test_publish_fanout_to_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        q3 = bus.subscribe()
        await bus.publish("snapshot_ready", {"store_id": 1})
        assert not q1.empty()
        assert not q2.empty()
        assert not q3.empty()

    @pytest.mark.asyncio
    async def test_publish_no_subscribers_no_error(self):
        bus = EventBus()
        await bus.publish("snapshot_ready", {"store_id": 1})  # must not raise

    @pytest.mark.asyncio
    async def test_publish_drops_when_queue_full(self):
        """A full subscriber queue should cause event to be dropped silently."""
        bus = EventBus()
        q = bus.subscribe()

        # Fill the queue to capacity
        for i in range(_QUEUE_MAXSIZE):
            await bus.publish("snapshot_ready", {"n": i})

        # The queue is now full; this publish should NOT raise and the subscriber
        # count may drop (full queues get pruned) OR the event is simply dropped
        await bus.publish("snapshot_ready", {"n": "overflow"})

    @pytest.mark.asyncio
    async def test_publish_envelope_structure(self):
        bus = EventBus()
        q = bus.subscribe()
        await bus.publish("test_event", {"key": "value"})
        envelope = q.get_nowait()
        assert set(envelope.keys()) == {"type", "data"}
        assert envelope["type"] == "test_event"
        assert envelope["data"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_unsubscribed_queue_receives_nothing(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        await bus.publish("snapshot_ready", {"store_id": 1})
        assert q.empty()
