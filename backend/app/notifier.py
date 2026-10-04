"""Discount alert detection and notification dispatch.

Architecture
------------

AlertDetector
    Pure logic — compares two snapshots and emits AlertEvent objects.
    No I/O, fully unit-testable.

NotifierService
    Dispatches AlertEvents via configured channels:
      • ntfy.sh (or self-hosted ntfy) — simple HTTP POST, no account needed
      • Email via SMTP (optional, configured in .env)

Usage
-----
    detector = AlertDetector()
    events = detector.compare(prev_items, curr_items, store_id=2203)
    await notifier.dispatch_all(events)

Alert triggers
--------------
1. **Discount escalation** — product's markdown_percentage crossed a tier
   boundary since the previous snapshot (e.g. 25 → 40, 40 → 70).
   Tiers: 25%, 40%, 70%.

2. **Last stock** — product's stock dropped to 1 (or appeared with stock=1
   and discount ≥ 40%) suggesting it is nearly gone.

3. **New high-discount item** — product appeared for the first time with
   markdown_percentage ≥ 40%.

Design choices
--------------
- All monetary values stay as euro-cents internally; formatted for display.
- The detector is stateless — the caller (CollectorService) provides the
  previous and current item lists.
- Channels fail silently with a warning log so a bad SMTP config can't
  break collection.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import Enum, auto
from typing import TYPE_CHECKING

import httpx
import structlog

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tier helpers
# ---------------------------------------------------------------------------

_TIERS = (25.0, 40.0, 70.0)


def _tier(pct: float | None) -> int:
    """Return which discount tier a percentage belongs to (0 = below 25%)."""
    if pct is None:
        return 0
    for i, t in enumerate(reversed(_TIERS), start=1):
        if pct >= t:
            return len(_TIERS) - i + 1
    return 0


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------


class AlertKind(Enum):
    DISCOUNT_ESCALATION = auto()   # crossed a tier boundary upward
    NEW_HIGH_DISCOUNT   = auto()   # appeared fresh with ≥ 40% off
    LAST_STOCK          = auto()   # stock dropped to 1


@dataclass(frozen=True)
class AlertEvent:
    """A single alert to dispatch."""

    kind:                AlertKind
    store_id:            int
    product_id:          int
    product_title:       str
    product_brand:       str
    category:            str
    price_now_cents:     int | None
    price_was_cents:     int | None
    markdown_percentage: float | None
    stock:               int
    prev_percentage:     float | None = None  # for ESCALATION only

    # ------------------------------------------------------------------
    # Formatting helpers (used by delivery channels)
    # ------------------------------------------------------------------

    @property
    def price_now_str(self) -> str:
        if self.price_now_cents is None:
            return "?"
        return f"€{self.price_now_cents / 100:.2f}"

    @property
    def discount_str(self) -> str:
        if self.markdown_percentage is None:
            return "?"
        return f"{self.markdown_percentage:.0f}%"

    @property
    def prev_discount_str(self) -> str:
        if self.prev_percentage is None:
            return "?"
        return f"{self.prev_percentage:.0f}%"

    @property
    def title(self) -> str:
        """Short notification title."""
        if self.kind == AlertKind.DISCOUNT_ESCALATION:
            return (
                f"🔥 {self.discount_str} korting! "
                f"{self.product_title}"
            )
        if self.kind == AlertKind.NEW_HIGH_DISCOUNT:
            return f"⚡ Nieuw: {self.discount_str} korting! {self.product_title}"
        if self.kind == AlertKind.LAST_STOCK:
            return f"⏰ Laatste stuk! {self.product_title}"
        return self.product_title  # fallback

    @property
    def body(self) -> str:
        """Multi-line notification body."""
        lines = []
        if self.product_brand:
            lines.append(self.product_brand)
        if self.category:
            lines.append(self.category)
        lines.append(f"Prijs: {self.price_now_str}")
        if self.kind == AlertKind.DISCOUNT_ESCALATION:
            lines.append(
                f"Korting gestegen: {self.prev_discount_str} → {self.discount_str}"
            )
        else:
            lines.append(f"Korting: {self.discount_str}")
        lines.append(f"Op voorraad: {self.stock}")
        lines.append(f"Winkel #{self.store_id}")
        return "\n".join(lines)

    @property
    def priority_tag(self) -> str:
        """ntfy priority string."""
        if self.markdown_percentage and self.markdown_percentage >= 70:
            return "urgent"
        if self.kind == AlertKind.LAST_STOCK:
            return "high"
        return "default"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass
class _ItemSnapshot:
    """Minimal view of a DB BargainItem used for comparison."""
    product_id:          int
    product_title:       str
    product_brand:       str
    category:            str
    price_now_cents:     int | None
    price_was_cents:     int | None
    markdown_percentage: float | None
    stock:               int


class AlertDetector:
    """Pure comparison logic — no I/O.

    Call :meth:`compare` with the previous and current item lists
    (as :class:`_ItemSnapshot` objects) to get a list of :class:`AlertEvent`.
    """

    def compare(
        self,
        prev_items: list[_ItemSnapshot],
        curr_items: list[_ItemSnapshot],
        store_id: int,
    ) -> list[AlertEvent]:
        """Detect alert-worthy changes between two snapshots.

        Args:
            prev_items: Items from the previous snapshot (may be empty
                        if this is the first collection for this store).
            curr_items: Items from the most recent snapshot.
            store_id:   The AH store ID (for display/routing).

        Returns:
            A list of :class:`AlertEvent`.  May be empty.
        """
        prev_by_id = {i.product_id: i for i in prev_items}
        events: list[AlertEvent] = []

        for curr in curr_items:
            prev = prev_by_id.get(curr.product_id)
            ev = self._check(curr, prev, store_id)
            if ev:
                events.append(ev)

        return events

    # -- private -----------------------------------------------------------

    def _check(
        self,
        curr: _ItemSnapshot,
        prev: _ItemSnapshot | None,
        store_id: int,
    ) -> AlertEvent | None:
        """Return an AlertEvent for a single item, or None."""
        curr_tier = _tier(curr.markdown_percentage)

        if prev is None:
            # Brand-new product this snapshot
            if curr_tier >= 2:  # ≥ 40%
                return self._event(
                    AlertKind.NEW_HIGH_DISCOUNT, curr, prev, store_id
                )
            return None

        prev_tier = _tier(prev.markdown_percentage)

        # Discount escalated to a higher tier
        if curr_tier > prev_tier and curr_tier >= 1:
            return self._event(
                AlertKind.DISCOUNT_ESCALATION, curr, prev, store_id
            )

        # Stock just dropped to 1 (was > 1 before), discount ≥ 25%
        if (
            curr.stock == 1
            and prev.stock > 1
            and curr_tier >= 1
        ):
            return self._event(AlertKind.LAST_STOCK, curr, prev, store_id)

        return None

    @staticmethod
    def _event(
        kind: AlertKind,
        curr: _ItemSnapshot,
        prev: _ItemSnapshot | None,
        store_id: int,
    ) -> AlertEvent:
        return AlertEvent(
            kind=kind,
            store_id=store_id,
            product_id=curr.product_id,
            product_title=curr.product_title,
            product_brand=curr.product_brand,
            category=curr.category,
            price_now_cents=curr.price_now_cents,
            price_was_cents=curr.price_was_cents,
            markdown_percentage=curr.markdown_percentage,
            stock=curr.stock,
            prev_percentage=prev.markdown_percentage if prev else None,
        )


# ---------------------------------------------------------------------------
# NotifierService
# ---------------------------------------------------------------------------


class NotifierService:
    """Dispatches AlertEvents over configured channels.

    Channels are enabled by setting the corresponding env vars:
        AH_NOTIFY_URL   → ntfy.sh topic URL (e.g. https://ntfy.sh/my-topic)
        AH_SMTP_HOST    → enables email dispatch

    Both channels fail *silently* (warning log only) so a misconfigured
    notifier can't break the collection cycle.
    """

    def __init__(
        self,
        *,
        ntfy_url: str | None = None,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        smtp_from: str = "",
        smtp_to: str = "",
    ) -> None:
        self._ntfy_url = ntfy_url.rstrip("/") if ntfy_url else None
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_password = smtp_password
        self._smtp_from = smtp_from
        self._smtp_to = smtp_to

    @property
    def has_channels(self) -> bool:
        """Return True if at least one delivery channel is configured."""
        return bool(self._ntfy_url or self._smtp_host)

    async def dispatch_all(self, events: list[AlertEvent]) -> None:
        """Dispatch all events to all configured channels."""
        if not events:
            return
        if not self.has_channels:
            logger.debug(
                "notifier_no_channels",
                event_count=len(events),
                hint="Set AH_NOTIFY_URL in .env to enable push notifications",
            )
            return

        logger.info("notifier_dispatching", event_count=len(events))
        for event in events:
            await self._dispatch_one(event)

    async def _dispatch_one(self, event: AlertEvent) -> None:
        if self._ntfy_url:
            await self._send_ntfy(event)
        if self._smtp_host and self._smtp_to:
            await self._send_email(event)

    async def _send_ntfy(self, event: AlertEvent) -> None:
        """POST to an ntfy.sh topic."""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    self._ntfy_url,
                    content=event.body.encode(),
                    headers={
                        "Title":    event.title,
                        "Priority": event.priority_tag,
                        "Tags":     _ntfy_tags(event),
                    },
                )
                resp.raise_for_status()
                logger.info(
                    "notifier_ntfy_sent",
                    kind=event.kind.name,
                    product_id=event.product_id,
                )
        except Exception as exc:
            logger.warning(
                "notifier_ntfy_failed",
                kind=event.kind.name,
                product_id=event.product_id,
                error=str(exc),
            )

    async def _send_email(self, event: AlertEvent) -> None:
        """Send via SMTP (blocking — run in threadpool for async compat)."""
        import asyncio
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._send_email_sync,
                event,
            )
            logger.info(
                "notifier_email_sent",
                kind=event.kind.name,
                product_id=event.product_id,
            )
        except Exception as exc:
            logger.warning(
                "notifier_email_failed",
                kind=event.kind.name,
                product_id=event.product_id,
                error=str(exc),
            )

    def _send_email_sync(self, event: AlertEvent) -> None:
        msg = EmailMessage()
        msg["Subject"] = event.title
        msg["From"] = self._smtp_from or self._smtp_user
        msg["To"] = self._smtp_to
        msg.set_content(event.body)

        with smtplib.SMTP(self._smtp_host, self._smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            if self._smtp_user:
                smtp.login(self._smtp_user, self._smtp_password)
            smtp.send_message(msg)


# ---------------------------------------------------------------------------
# Factory helpers (used by the collector / main.py)
# ---------------------------------------------------------------------------


def make_notifier_from_settings(settings: object) -> NotifierService:
    """Build a NotifierService from the application Settings object."""
    return NotifierService(
        ntfy_url=getattr(settings, "ah_notify_url", None) or None,
        smtp_host=getattr(settings, "ah_smtp_host", ""),
        smtp_port=getattr(settings, "ah_smtp_port", 587),
        smtp_user=getattr(settings, "ah_smtp_user", ""),
        smtp_password=getattr(settings, "ah_smtp_password", ""),
        smtp_from=getattr(settings, "ah_smtp_from", ""),
        smtp_to=getattr(settings, "ah_smtp_to", ""),
    )


def _ntfy_tags(event: AlertEvent) -> str:
    """Return comma-separated ntfy tag emoji strings."""
    tags = []
    if event.kind == AlertKind.DISCOUNT_ESCALATION:
        tags.append("rotating_light")
    elif event.kind == AlertKind.NEW_HIGH_DISCOUNT:
        tags.append("zap")
    elif event.kind == AlertKind.LAST_STOCK:
        tags.append("timer_clock")
    if event.markdown_percentage and event.markdown_percentage >= 70:
        tags.append("fire")
    return ",".join(tags)


# ---------------------------------------------------------------------------
# Repository helper — fetch _ItemSnapshot rows from the DB
# ---------------------------------------------------------------------------


async def fetch_snapshot_items(
    session,
    snapshot_id: int,
) -> list[_ItemSnapshot]:
    """Load bargain items + product metadata for a given snapshot.

    Returns a list of :class:`_ItemSnapshot` objects ready for comparison.
    """
    from sqlalchemy import select
    from app.db.models import BargainItem, Product

    result = await session.execute(
        select(BargainItem, Product)
        .join(Product, BargainItem.product_id == Product.id)
        .where(BargainItem.snapshot_id == snapshot_id)
    )
    rows = result.all()

    return [
        _ItemSnapshot(
            product_id=item.product_id,
            product_title=product.title,
            product_brand=product.brand,
            category=product.category,
            price_now_cents=item.price_now_cents,
            price_was_cents=item.price_was_cents,
            markdown_percentage=item.markdown_percentage,
            stock=item.stock,
        )
        for item, product in rows
    ]
