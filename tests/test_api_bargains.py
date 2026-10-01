"""Tests for /api/stores/{store_id}/bargains and /api/products endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BargainItem, Product, Snapshot, Store


# ---------------------------------------------------------------------------
# Shared seeding helpers
# ---------------------------------------------------------------------------


async def _seed_full(
    session: AsyncSession,
    store_id: int = 5001,
    product_ids: list[int] | None = None,
    discounts: list[float] | None = None,
    stock: list[int] | None = None,
    categories: list[str] | None = None,
) -> Snapshot:
    """Seed store + products + one snapshot + bargain items."""
    if product_ids is None:
        product_ids = [50001, 50002, 50003]
    n = len(product_ids)
    discounts = discounts or [25.0] * n
    stock = stock or [3] * n
    categories = categories or ["Vlees"] * n

    store = Store(
        id=store_id,
        name=f"Store {store_id}",
        store_type="REGULAR",
        city="Amsterdam",
        street="Teststraat 1",
        postal_code="1091AB",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    session.add(store)

    for i, pid in enumerate(product_ids):
        session.add(
            Product(
                id=pid,
                title=f"Product {pid}",
                brand="AH",
                category=categories[i],
                sales_unit_size="200 g",
            )
        )

    await session.flush()

    snapshot = Snapshot(
        store_id=store_id,
        fetched_at=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
        item_count=n,
    )
    session.add(snapshot)
    await session.flush()

    for i, pid in enumerate(product_ids):
        session.add(
            BargainItem(
                snapshot_id=snapshot.id,
                product_id=pid,
                price_now_cents=199,
                price_was_cents=299,
                markdown_percentage=discounts[i],
                markdown_type="EXPIRATION",
                markdown_expiration_date="2026-10-02",
                stock=stock[i],
            )
        )

    await session.commit()
    return snapshot


# ---------------------------------------------------------------------------
# GET /api/stores/{store_id}/bargains
# ---------------------------------------------------------------------------


class TestGetBargains:
    async def test_returns_latest_snapshot_items(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(db_session, store_id=6001, product_ids=[60001, 60002])
        response = await client.get("/api/stores/6001/bargains")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    async def test_prices_in_euros(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(db_session, store_id=6002, product_ids=[60003])
        data = (await client.get("/api/stores/6002/bargains")).json()
        item = data[0]
        assert item["price_now"] == pytest.approx(1.99)
        assert item["price_was"] == pytest.approx(2.99)

    async def test_product_info_embedded(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(db_session, store_id=6003, product_ids=[60010])
        data = (await client.get("/api/stores/6003/bargains")).json()
        assert data[0]["product"]["title"] == "Product 60010"
        assert data[0]["product"]["brand"] == "AH"

    async def test_ordered_by_discount_descending(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(
            db_session,
            store_id=6004,
            product_ids=[60020, 60021, 60022],
            discounts=[25.0, 70.0, 40.0],
        )
        data = (await client.get("/api/stores/6004/bargains")).json()
        percentages = [d["markdown_percentage"] for d in data]
        assert percentages == sorted(percentages, reverse=True)

    async def test_filter_min_discount(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(
            db_session,
            store_id=6005,
            product_ids=[60030, 60031, 60032],
            discounts=[25.0, 40.0, 70.0],
        )
        response = await client.get("/api/stores/6005/bargains?min_discount=40")
        data = response.json()
        assert all(d["markdown_percentage"] >= 40 for d in data)
        assert len(data) == 2

    async def test_filter_out_of_stock_excluded_by_default(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(
            db_session,
            store_id=6006,
            product_ids=[60040, 60041],
            stock=[0, 5],
        )
        data = (await client.get("/api/stores/6006/bargains")).json()
        assert all(d["stock"] > 0 for d in data)
        assert len(data) == 1

    async def test_filter_in_stock_false_includes_zero_stock(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(
            db_session,
            store_id=6007,
            product_ids=[60050, 60051],
            stock=[0, 2],
        )
        data = (await client.get("/api/stores/6007/bargains?in_stock=false")).json()
        assert len(data) == 2

    async def test_filter_by_category(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(
            db_session,
            store_id=6008,
            product_ids=[60060, 60061, 60062],
            categories=["Vlees", "Vis", "Vleeswaren"],
        )
        data = (await client.get("/api/stores/6008/bargains?category=vlees")).json()
        # Matches "Vlees" and "Vleeswaren" but not "Vis"
        assert len(data) == 2

    async def test_specific_snapshot_id(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Request bargains from a specific (non-latest) snapshot."""
        store = Store(
            id=6009, name="Store 6009", store_type="REGULAR", city="A",
            street="B", postal_code="1000AA",
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        product = Product(id=60070, title="Old Product", brand="AH",
                          category="Test", sales_unit_size="1 st")
        db_session.add_all([store, product])
        await db_session.flush()

        old_snap = Snapshot(store_id=6009,
                            fetched_at=datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc),
                            item_count=1)
        new_snap = Snapshot(store_id=6009,
                            fetched_at=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
                            item_count=0)
        db_session.add_all([old_snap, new_snap])
        await db_session.flush()

        db_session.add(BargainItem(
            snapshot_id=old_snap.id, product_id=60070,
            price_now_cents=100, stock=2,
            markdown_percentage=25.0, markdown_type="EXP",
            markdown_expiration_date="2026-10-02",
        ))
        await db_session.commit()

        # Latest has 0 items, old snapshot has 1
        latest = (await client.get("/api/stores/6009/bargains")).json()
        assert latest == []

        historical = (
            await client.get(f"/api/stores/6009/bargains?snapshot_id={old_snap.id}")
        ).json()
        assert len(historical) == 1

    async def test_empty_list_when_no_snapshots(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        store = Store(id=6010, name="New Store", store_type="REGULAR", city="A",
                      street="B", postal_code="1000AA",
                      created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        db_session.add(store)
        await db_session.commit()

        response = await client.get("/api/stores/6010/bargains")
        assert response.status_code == 200
        assert response.json() == []

    async def test_404_for_unknown_store(self, client: AsyncClient):
        response = await client.get("/api/stores/99999/bargains")
        assert response.status_code == 404

    async def test_404_for_unknown_snapshot_id(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_full(db_session, store_id=6011, product_ids=[60080])
        response = await client.get("/api/stores/6011/bargains?snapshot_id=99999")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/products/{product_id}
# ---------------------------------------------------------------------------


class TestGetProduct:
    async def test_returns_product(self, client: AsyncClient, db_session: AsyncSession):
        db_session.add(Product(
            id=70001, title="AH Kaas", brand="AH",
            category="Zuivel", sales_unit_size="400 g",
        ))
        await db_session.commit()

        response = await client.get("/api/products/70001")
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "AH Kaas"
        assert data["category"] == "Zuivel"

    async def test_404_for_unknown_product(self, client: AsyncClient):
        response = await client.get("/api/products/99999")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/products/{product_id}/history
# ---------------------------------------------------------------------------


class TestProductHistory:
    async def _seed_history(
        self,
        session: AsyncSession,
        store_id: int,
        product_id: int,
        observations: list[tuple[int, float]],  # (price_now_cents, discount%)
    ) -> None:
        store = Store(
            id=store_id, name=f"Store {store_id}", store_type="REGULAR",
            city="A", street="B", postal_code="1000AA",
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        product = Product(
            id=product_id, title=f"Product {product_id}", brand="AH",
            category="Test", sales_unit_size="200 g",
        )
        session.add_all([store, product])
        await session.flush()

        for hour, (cents, disc) in enumerate(observations):
            snap = Snapshot(
                store_id=store_id,
                fetched_at=datetime(2026, 10, 1, hour, 0, tzinfo=timezone.utc),
                item_count=1,
            )
            session.add(snap)
            await session.flush()
            session.add(BargainItem(
                snapshot_id=snap.id, product_id=product_id,
                price_now_cents=cents, price_was_cents=399,
                markdown_percentage=disc, markdown_type="EXPIRATION",
                markdown_expiration_date="2026-10-02", stock=5,
            ))
        await session.commit()

    async def test_returns_history_oldest_first(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await self._seed_history(
            db_session, store_id=8001, product_id=80001,
            observations=[(299, 25.0), (239, 40.0), (119, 70.0)],
        )
        response = await client.get("/api/products/80001/history?store_id=8001")
        assert response.status_code == 200
        data = response.json()

        assert data["product"]["id"] == 80001
        assert data["store_id"] == 8001
        assert len(data["entries"]) == 3

        # Prices in euros, oldest first
        prices = [e["price_now"] for e in data["entries"]]
        assert prices == [pytest.approx(2.99), pytest.approx(2.39), pytest.approx(1.19)]
        discounts = [e["markdown_percentage"] for e in data["entries"]]
        assert discounts == [25.0, 40.0, 70.0]

    async def test_404_unknown_product(self, client: AsyncClient):
        response = await client.get("/api/products/99999/history?store_id=1")
        assert response.status_code == 404

    async def test_404_no_history_for_store(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Product exists but has no observations at the requested store."""
        db_session.add(Product(
            id=80010, title="Lonely Product", brand="AH",
            category="Test", sales_unit_size="1 st",
        ))
        await db_session.commit()
        response = await client.get("/api/products/80010/history?store_id=99999")
        assert response.status_code == 404

    async def test_limit_parameter(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await self._seed_history(
            db_session, store_id=8002, product_id=80020,
            observations=[(199, 25.0)] * 10,
        )
        response = await client.get(
            "/api/products/80020/history?store_id=8002&limit=3"
        )
        assert len(response.json()["entries"]) == 3
