"""Repository layer for persisting AH bargain snapshots.

This module is the only place that writes bargain data to the database.
It translates the AH API response objects (``app.ah.models``) into ORM
rows (``app.db.models``) and handles all upsert logic.

Public API::

    repo = BargainRepository(session)
    snapshot = await repo.save_snapshot(store_id, ah_store, bargain_items)

Price convention:
    All prices are stored as **integer euro-cents** (e.g. €3.89 → 389).
    The helper ``price_to_cents()`` converts the API string representation.
    ``None`` is stored as-is when the API does not supply a price.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ah.models import AHBargainItem, AHStore
from app.db.models import BargainItem, Product, Snapshot, Store

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------


def price_to_cents(price_str: str) -> int | None:
    """Convert an API price string to integer euro-cents.

    Examples:
        >>> price_to_cents("3.89")
        389
        >>> price_to_cents("1.00")
        100
        >>> price_to_cents("0.45")
        45
        >>> price_to_cents("")
        None
    """
    if not price_str:
        return None
    # Remove any currency symbols or whitespace
    cleaned = re.sub(r"[€\s]", "", price_str)
    try:
        return round(float(cleaned) * 100)
    except ValueError:
        logger.warning("price_parse_failed", raw=price_str)
        return None


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class BargainRepository:
    """Handles all database writes for bargain collection runs.

    One instance per request / collection run — takes a session in the
    constructor so callers control the transaction lifetime.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Public API ----------------------------------------------------------

    async def save_snapshot(
        self,
        store_id: int,
        ah_store: AHStore | None,
        bargain_items: list[AHBargainItem],
    ) -> Snapshot:
        """Persist a full collection run and return the new ``Snapshot``.

        Steps:
        1. Upsert the store.
        2. Upsert all products (create/update metadata).
        3. Create a new Snapshot row.
        4. Bulk-insert BargainItem rows.

        Args:
            store_id: The AH store ID.
            ah_store: Store metadata from the API (can be None if the store
                lookup failed — store will still be created with minimal info).
            bargain_items: The list of bargain items returned by the API.

        Returns:
            The newly created ``Snapshot`` ORM object (id is populated).
        """
        logger.info("repo_save_snapshot_start", store_id=store_id, item_count=len(bargain_items))

        # 1. Store
        store = await self._upsert_store(store_id, ah_store)

        # 2. Products (upsert all at once)
        await self._upsert_products(bargain_items)

        # 3. Snapshot
        now = datetime.now(timezone.utc)
        snapshot = Snapshot(
            store_id=store.id,
            fetched_at=now,
            item_count=len(bargain_items),
        )
        self._session.add(snapshot)
        await self._session.flush()  # Populate snapshot.id

        # 4. BargainItems
        items = [
            self._make_bargain_item(snapshot.id, ah_item)
            for ah_item in bargain_items
        ]
        self._session.add_all(items)
        await self._session.flush()

        logger.info(
            "repo_save_snapshot_done",
            store_id=store_id,
            snapshot_id=snapshot.id,
            item_count=len(items),
        )
        return snapshot

    async def get_latest_snapshot(self, store_id: int) -> Snapshot | None:
        """Return the most recent snapshot for a store, or None."""
        result = await self._session.execute(
            select(Snapshot)
            .where(Snapshot.store_id == store_id)
            .order_by(Snapshot.fetched_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_snapshots_for_store(
        self,
        store_id: int,
        *,
        limit: int = 50,
    ) -> list[Snapshot]:
        """Return the most recent N snapshots for a store, newest first."""
        result = await self._session.execute(
            select(Snapshot)
            .where(Snapshot.store_id == store_id)
            .order_by(Snapshot.fetched_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_bargain_items_for_snapshot(
        self,
        snapshot_id: int,
    ) -> list[BargainItem]:
        """Return all bargain items for a given snapshot."""
        result = await self._session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == snapshot_id)
        )
        return list(result.scalars().all())

    # -- Private helpers -----------------------------------------------------

    async def _upsert_store(self, store_id: int, ah_store: AHStore | None) -> Store:
        """Create the Store row if it doesn't exist, update metadata if it does."""
        result = await self._session.execute(
            select(Store).where(Store.id == store_id)
        )
        store = result.scalar_one_or_none()

        if store is None:
            store = Store(id=store_id)
            self._session.add(store)
            logger.info("repo_store_created", store_id=store_id)

        if ah_store is not None:
            store.name = ah_store.name
            store.store_type = ah_store.store_type
            store.city = ah_store.address.city
            store.street = (
                f"{ah_store.address.street} {ah_store.address.house_number}"
            ).strip()
            store.postal_code = ah_store.address.postal_code

        await self._session.flush()
        return store

    async def _upsert_products(self, bargain_items: list[AHBargainItem]) -> None:
        """Upsert all products from the bargain item list."""
        if not bargain_items:
            return

        product_ids = [item.product.id for item in bargain_items]

        # Fetch existing products in one query
        result = await self._session.execute(
            select(Product).where(Product.id.in_(product_ids))
        )
        existing: dict[int, Product] = {p.id: p for p in result.scalars().all()}
        now = datetime.now(timezone.utc)

        for ah_item in bargain_items:
            p = ah_item.product
            if p.id in existing:
                # Update mutable metadata
                db_product = existing[p.id]
                db_product.title = p.title
                db_product.brand = p.brand
                db_product.category = ah_item.category_title
                db_product.sales_unit_size = p.sales_unit_size
                db_product.last_seen_at = now
            else:
                new_product = Product(
                    id=p.id,
                    title=p.title,
                    brand=p.brand,
                    category=ah_item.category_title,
                    sales_unit_size=p.sales_unit_size,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                self._session.add(new_product)
                existing[p.id] = new_product

        await self._session.flush()

    def _make_bargain_item(
        self,
        snapshot_id: int,
        ah_item: AHBargainItem,
    ) -> BargainItem:
        """Convert an API bargain item into a DB BargainItem row."""
        return BargainItem(
            snapshot_id=snapshot_id,
            product_id=ah_item.product.id,
            price_now_cents=price_to_cents(ah_item.bargain_price.price_now),
            price_was_cents=price_to_cents(ah_item.bargain_price.price_was),
            markdown_percentage=ah_item.markdown.markdown_percentage or None,
            markdown_type=ah_item.markdown.markdown_type,
            markdown_expiration_date=ah_item.markdown.markdown_expiration_date,
            stock=ah_item.stock,
        )
