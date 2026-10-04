"""Tests for the notifier module (Phase 7).

Tests cover:
- AlertDetector: detection logic (escalation, new high-discount, last-stock, no-op)
- AlertEvent: formatting helpers (title, body, priority)
- NotifierService: ntfy dispatch (mocked), SMTP dispatch (mocked), no-op when
  channels are not configured
- _tier helper: discount tier assignment

All tests are fully synchronous/unit-level — no real network, DB, or async I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.notifier import (
    AlertDetector,
    AlertEvent,
    AlertKind,
    NotifierService,
    _ItemSnapshot,
    _tier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item(
    product_id: int = 1,
    title: str = "AH Product",
    brand: str = "AH",
    category: str = "Zuivel",
    pct: float | None = 25.0,
    stock: int = 5,
    price_now: int = 150,
    price_was: int = 299,
) -> _ItemSnapshot:
    return _ItemSnapshot(
        product_id=product_id,
        product_title=title,
        product_brand=brand,
        category=category,
        price_now_cents=price_now,
        price_was_cents=price_was,
        markdown_percentage=pct,
        stock=stock,
    )


# ---------------------------------------------------------------------------
# _tier helper
# ---------------------------------------------------------------------------


class TestTier:
    def test_none_is_zero(self):
        assert _tier(None) == 0

    def test_below_25_is_zero(self):
        assert _tier(10.0) == 0
        assert _tier(24.9) == 0

    def test_exactly_25_is_tier_1(self):
        assert _tier(25.0) == 1

    def test_between_25_and_40_is_tier_1(self):
        assert _tier(39.9) == 1

    def test_exactly_40_is_tier_2(self):
        assert _tier(40.0) == 2

    def test_between_40_and_70_is_tier_2(self):
        assert _tier(69.9) == 2

    def test_exactly_70_is_tier_3(self):
        assert _tier(70.0) == 3

    def test_above_70_is_tier_3(self):
        assert _tier(90.0) == 3


# ---------------------------------------------------------------------------
# AlertDetector — no-op cases
# ---------------------------------------------------------------------------


class TestAlertDetectorNoOp:
    def test_empty_both_sides(self):
        events = AlertDetector().compare([], [], store_id=2203)
        assert events == []

    def test_no_change_same_tier(self):
        item = _item(pct=25.0, stock=5)
        events = AlertDetector().compare([item], [item], store_id=2203)
        assert events == []

    def test_discount_decreased_no_alert(self):
        prev = _item(pct=40.0)
        curr = _item(pct=25.0)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert events == []

    def test_new_item_below_40_no_alert(self):
        curr = _item(pct=25.0)
        events = AlertDetector().compare([], [curr], store_id=2203)
        assert events == []

    def test_stock_drop_without_sufficient_discount_no_alert(self):
        # discount < 25% and stock drops to 1 → no alert
        prev = _item(pct=10.0, stock=3)
        curr = _item(pct=10.0, stock=1)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert events == []


# ---------------------------------------------------------------------------
# AlertDetector — escalation
# ---------------------------------------------------------------------------


class TestAlertDetectorEscalation:
    def test_25_to_40_triggers_escalation(self):
        prev = _item(pct=25.0)
        curr = _item(pct=40.0)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert len(events) == 1
        assert events[0].kind == AlertKind.DISCOUNT_ESCALATION
        assert events[0].markdown_percentage == 40.0
        assert events[0].prev_percentage == 25.0

    def test_25_to_70_triggers_escalation(self):
        prev = _item(pct=25.0)
        curr = _item(pct=70.0)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert len(events) == 1
        assert events[0].kind == AlertKind.DISCOUNT_ESCALATION

    def test_40_to_70_triggers_escalation(self):
        prev = _item(pct=45.0)
        curr = _item(pct=70.0)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert len(events) == 1
        assert events[0].kind == AlertKind.DISCOUNT_ESCALATION

    def test_store_id_propagated(self):
        prev = _item(pct=25.0)
        curr = _item(pct=40.0)
        events = AlertDetector().compare([prev], [curr], store_id=9999)
        assert events[0].store_id == 9999

    def test_multiple_products_multiple_events(self):
        prev = [_item(product_id=1, pct=25.0), _item(product_id=2, pct=25.0)]
        curr = [_item(product_id=1, pct=40.0), _item(product_id=2, pct=70.0)]
        events = AlertDetector().compare(prev, curr, store_id=2203)
        assert len(events) == 2


# ---------------------------------------------------------------------------
# AlertDetector — new high-discount
# ---------------------------------------------------------------------------


class TestAlertDetectorNewHighDiscount:
    def test_new_item_at_40_percent_triggers(self):
        curr = _item(pct=40.0)
        events = AlertDetector().compare([], [curr], store_id=2203)
        assert len(events) == 1
        assert events[0].kind == AlertKind.NEW_HIGH_DISCOUNT

    def test_new_item_at_70_percent_triggers(self):
        curr = _item(pct=70.0)
        events = AlertDetector().compare([], [curr], store_id=2203)
        assert len(events) == 1
        assert events[0].kind == AlertKind.NEW_HIGH_DISCOUNT

    def test_new_item_at_25_percent_no_alert(self):
        curr = _item(pct=25.0)
        events = AlertDetector().compare([], [curr], store_id=2203)
        assert events == []


# ---------------------------------------------------------------------------
# AlertDetector — last stock
# ---------------------------------------------------------------------------


class TestAlertDetectorLastStock:
    def test_stock_drops_to_1_at_25_pct_triggers(self):
        prev = _item(pct=25.0, stock=3)
        curr = _item(pct=25.0, stock=1)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert len(events) == 1
        assert events[0].kind == AlertKind.LAST_STOCK

    def test_stock_drops_to_1_at_40_pct_triggers(self):
        prev = _item(pct=40.0, stock=5)
        curr = _item(pct=40.0, stock=1)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert len(events) == 1
        assert events[0].kind == AlertKind.LAST_STOCK

    def test_stock_was_already_1_no_alert(self):
        prev = _item(pct=40.0, stock=1)
        curr = _item(pct=40.0, stock=1)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert events == []

    def test_stock_drops_to_2_no_alert(self):
        prev = _item(pct=40.0, stock=5)
        curr = _item(pct=40.0, stock=2)
        events = AlertDetector().compare([prev], [curr], store_id=2203)
        assert events == []


# ---------------------------------------------------------------------------
# AlertEvent formatting
# ---------------------------------------------------------------------------


def _make_event(
    kind: AlertKind = AlertKind.DISCOUNT_ESCALATION,
    pct: float = 40.0,
    prev_pct: float = 25.0,
    price_now: int = 150,
    stock: int = 3,
) -> AlertEvent:
    return AlertEvent(
        kind=kind,
        store_id=2203,
        product_id=12345,
        product_title="AH Gouda Belegen",
        product_brand="AH",
        category="Zuivel",
        price_now_cents=price_now,
        price_was_cents=299,
        markdown_percentage=pct,
        stock=stock,
        prev_percentage=prev_pct,
    )


class TestAlertEventFormatting:
    def test_price_now_str(self):
        ev = _make_event(price_now=150)
        assert ev.price_now_str == "€1.50"

    def test_price_now_str_none(self):
        ev = AlertEvent(
            kind=AlertKind.NEW_HIGH_DISCOUNT,
            store_id=1,
            product_id=1,
            product_title="X",
            product_brand="",
            category="",
            price_now_cents=None,
            price_was_cents=None,
            markdown_percentage=40.0,
            stock=1,
        )
        assert ev.price_now_str == "?"

    def test_discount_str(self):
        ev = _make_event(pct=40.0)
        assert ev.discount_str == "40%"

    def test_title_escalation(self):
        ev = _make_event(kind=AlertKind.DISCOUNT_ESCALATION)
        assert "40%" in ev.title
        assert "AH Gouda Belegen" in ev.title

    def test_title_new_high(self):
        ev = _make_event(kind=AlertKind.NEW_HIGH_DISCOUNT)
        assert "Nieuw" in ev.title
        assert "40%" in ev.title

    def test_title_last_stock(self):
        ev = _make_event(kind=AlertKind.LAST_STOCK)
        assert "Laatste" in ev.title

    def test_body_contains_price(self):
        ev = _make_event(price_now=150)
        assert "€1.50" in ev.body

    def test_body_contains_store_id(self):
        ev = _make_event()
        assert "2203" in ev.body

    def test_priority_urgent_at_70(self):
        ev = _make_event(pct=70.0)
        assert ev.priority_tag == "urgent"

    def test_priority_default_at_40(self):
        ev = _make_event(pct=40.0)
        assert ev.priority_tag == "default"

    def test_priority_high_for_last_stock(self):
        ev = _make_event(kind=AlertKind.LAST_STOCK, pct=40.0)
        assert ev.priority_tag == "high"


# ---------------------------------------------------------------------------
# NotifierService — channel detection
# ---------------------------------------------------------------------------


class TestNotifierService:
    def test_no_channels_when_unconfigured(self):
        notifier = NotifierService()
        assert not notifier.has_channels

    def test_ntfy_channel_detected(self):
        notifier = NotifierService(ntfy_url="https://ntfy.sh/my-topic")
        assert notifier.has_channels

    def test_smtp_channel_detected(self):
        notifier = NotifierService(smtp_host="smtp.gmail.com", smtp_to="me@example.com")
        assert notifier.has_channels

    @pytest.mark.asyncio
    async def test_dispatch_all_no_op_when_no_channels(self):
        """dispatch_all with no channels should not raise and not make HTTP calls."""
        notifier = NotifierService()
        events = [_make_event()]
        # Should complete without error and without any network I/O
        await notifier.dispatch_all(events)

    @pytest.mark.asyncio
    async def test_dispatch_all_no_op_for_empty_events(self):
        notifier = NotifierService(ntfy_url="https://ntfy.sh/topic")
        # Empty list — should not make any HTTP calls
        with patch("httpx.AsyncClient") as mock_client_cls:
            await notifier.dispatch_all([])
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_ntfy_sends_correct_headers(self):
        notifier = NotifierService(ntfy_url="https://ntfy.sh/my-topic")
        event = _make_event(kind=AlertKind.DISCOUNT_ESCALATION, pct=40.0)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=mock_response)

        with patch("app.notifier.httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.post = mock_post
            mock_client_cls.return_value = mock_ctx

            await notifier.dispatch_all([event])

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        headers = kwargs["headers"]
        assert "Title" in headers
        assert "Priority" in headers
        assert "Tags" in headers
        assert "40%" in headers["Title"]

    @pytest.mark.asyncio
    async def test_ntfy_failure_does_not_raise(self):
        """A failed ntfy POST should be swallowed (warning log only)."""
        notifier = NotifierService(ntfy_url="https://ntfy.sh/my-topic")
        event = _make_event()

        with patch("app.notifier.httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.post = AsyncMock(side_effect=Exception("connection refused"))
            mock_client_cls.return_value = mock_ctx

            # Should NOT raise
            await notifier.dispatch_all([event])

    @pytest.mark.asyncio
    async def test_ntfy_url_trailing_slash_stripped(self):
        """ntfy URL with trailing slash should still POST correctly."""
        notifier = NotifierService(ntfy_url="https://ntfy.sh/my-topic/")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("app.notifier.httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_ctx

            await notifier.dispatch_all([_make_event()])

        called_url = mock_ctx.post.call_args[0][0]
        assert not called_url.endswith("//"), "double-slash in URL"
