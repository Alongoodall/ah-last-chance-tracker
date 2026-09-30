"""Tests for the BargainRepository (Step 3c).

All tests use an in-memory SQLite database — no real API calls,
no disk I/O beyond what SQLite does in-process.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ah.models import (
    AHBargainItem,
    AHBargainMarkdown,
    AHBargainPrice,
    AHBargainProduct,
    AHStore,
    AHStoreAddress,
)
from app.db.models import Base, BargainItem, Product, Snapshot, Store
from app.db.repository import BargainRepository, price_to_cents


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="module")
async def engine():
    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
        await s.rollback()


@pytest.fixture
def repo(session: AsyncSession) -> BargainRepository:
    return BargainRepository(session)


# ---------------------------------------------------------------------------
# Builder helpers — easy to build realistic AH API objects in tests
# ---------------------------------------------------------------------------


def _ah_store(store_id: int = 2203) -> AHStore:
    return AHStore(
        id=store_id,
        name="Amsterdam Eerste Oosterparkstraat",
        storeType="REGULAR",
        address=AHStoreAddress(
            street="Eerste Oosterparkstraat",
            houseNumber="100",
            houseNumberExtra=None,
            postalCode="1091HE",
            city="Amsterdam",
        ),
    )


def _ah_item(
    product_id: int = 12345,
    title: str = "AH Roodbaarsfilet",
    brand: str = "AH",
    category: str = "Vis",
    unit_size: str = "ca. 200 g",
    price_now: str = "3.89",
    price_was: str = "5.99",
    discount: float = 35.0,
    stock: int = 4,
    markdown_type: str = "EXPIRATION",
    expiry: str = "2026-09-30",
) -> AHBargainItem:
    return AHBargainItem(
        product=AHBargainProduct(
            id=product_id,
            title=title,
            brand=brand,
            salesUnitSize=unit_size,
        ),
        categoryTitle=category,
        markdown=AHBargainMarkdown(
            markdownType=markdown_type,
            markdownExpirationDate=expiry,
            markdownPercentage=discount,
        ),
        stock=stock,
        bargainPrice=AHBargainPrice(
            priceWas=price_was,
            priceNow=price_now,
        ),
    )


# ---------------------------------------------------------------------------
# price_to_cents
# ---------------------------------------------------------------------------


class TestPriceToCents:
    def test_normal_price(self):
        assert price_to_cents("3.89") == 389

    def test_round_price(self):
        assert price_to_cents("1.00") == 100

    def test_cheap_price(self):
        assert price_to_cents("0.45") == 45

    def test_euro_symbol(self):
        assert price_to_cents("€3.89") == 389

    def test_empty_string(self):
        assert price_to_cents("") is None

    def test_invalid_string(self):
        assert price_to_cents("n/a") is None


# ---------------------------------------------------------------------------
# save_snapshot
# ---------------------------------------------------------------------------


class TestSaveSnapshot:
    async def test_creates_store_on_first_run(
        self, session: AsyncSession, repo: BargainRepository
    ):
        await repo.save_snapshot(2001, _ah_store(2001), [_ah_item(10001)])

        result = await session.execute(select(Store).where(Store.id == 2001))
        store = result.scalar_one()
        assert store.name == "Amsterdam Eerste Oosterparkstraat"
        assert store.city == "Amsterdam"
        assert store.postal_code == "1091HE"

    async def test_creates_product_on_first_run(
        self, session: AsyncSession, repo: BargainRepository
    ):
        await repo.save_snapshot(2002, _ah_store(2002), [_ah_item(20001)])

        result = await session.execute(select(Product).where(Product.id == 20001))
        product = result.scalar_one()
        assert product.title == "AH Roodbaarsfilet"
        assert product.brand == "AH"
        assert product.category == "Vis"

    async def test_creates_snapshot_row(
        self, session: AsyncSession, repo: BargainRepository
    ):
        snapshot = await repo.save_snapshot(2003, _ah_store(2003), [_ah_item(30001)])

        assert snapshot.id is not None
        assert snapshot.store_id == 2003
        assert snapshot.item_count == 1
        assert snapshot.fetched_at is not None

    async def test_creates_bargain_item_with_correct_prices(
        self, session: AsyncSession, repo: BargainRepository
    ):
        snapshot = await repo.save_snapshot(
            2004,
            _ah_store(2004),
            [_ah_item(40001, price_now="3.89", price_was="5.99", discount=35.0)],
        )

        result = await session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == snapshot.id)
        )
        item = result.scalar_one()
        assert item.price_now_cents == 389
        assert item.price_was_cents == 599
        assert item.markdown_percentage == pytest.approx(35.0)
        assert item.stock == 4

    async def test_handles_empty_bargain_list(
        self, session: AsyncSession, repo: BargainRepository
    ):
        """An empty bargain list is valid — store opens with no deals."""
        snapshot = await repo.save_snapshot(2005, _ah_store(2005), [])

        assert snapshot.item_count == 0
        result = await session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == snapshot.id)
        )
        assert result.scalars().all() == []

    async def test_handles_null_price_was(
        self, session: AsyncSession, repo: BargainRepository
    ):
        snapshot = await repo.save_snapshot(
            2006, _ah_store(2006), [_ah_item(60001, price_was="")]
        )
        result = await session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == snapshot.id)
        )
        item = result.scalar_one()
        assert item.price_was_cents is None

    async def test_multiple_items_in_one_snapshot(
        self, session: AsyncSession, repo: BargainRepository
    ):
        items = [_ah_item(70001 + i, title=f"Product {i}") for i in range(5)]
        snapshot = await repo.save_snapshot(2007, _ah_store(2007), items)

        assert snapshot.item_count == 5
        result = await session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == snapshot.id)
        )
        db_items = result.scalars().all()
        assert len(db_items) == 5

    async def test_store_metadata_updated_on_second_run(
        self, session: AsyncSession, repo: BargainRepository
    ):
        """Second snapshot for same store updates the store's metadata."""
        await repo.save_snapshot(2008, _ah_store(2008), [_ah_item(80001)])

        # Simulate store name change
        updated_store = AHStore(
            id=2008,
            name="New Name",
            storeType="REGULAR",
            address=AHStoreAddress(
                street="Nieuwe Straat",
                houseNumber="1",
                houseNumberExtra=None,
                postalCode="1000AA",
                city="Amsterdam",
            ),
        )
        repo2 = BargainRepository(session)
        await repo2.save_snapshot(2008, updated_store, [_ah_item(80001)])

        result = await session.execute(select(Store).where(Store.id == 2008))
        store = result.scalar_one()
        assert store.name == "New Name"
        assert store.postal_code == "1000AA"

    async def test_product_metadata_updated_on_second_run(
        self, session: AsyncSession, repo: BargainRepository
    ):
        """Second snapshot with same product updates product title/brand."""
        await repo.save_snapshot(
            2009, _ah_store(2009), [_ah_item(90001, title="Old Title")]
        )

        repo2 = BargainRepository(session)
        await repo2.save_snapshot(
            2009, _ah_store(2009), [_ah_item(90001, title="New Title")]
        )

        result = await session.execute(select(Product).where(Product.id == 90001))
        product = result.scalar_one()
        assert product.title == "New Title"

    async def test_two_snapshots_for_same_store(
        self, session: AsyncSession, repo: BargainRepository
    ):
        """Two collection runs create two separate snapshot rows."""
        s1 = await repo.save_snapshot(
            2010, _ah_store(2010), [_ah_item(100001, stock=5)]
        )
        repo2 = BargainRepository(session)
        s2 = await repo2.save_snapshot(
            2010, _ah_store(2010), [_ah_item(100001, stock=3)]
        )

        assert s1.id != s2.id

        # Both snapshots have their own bargain items
        r1 = await session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == s1.id)
        )
        r2 = await session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == s2.id)
        )
        assert r1.scalar_one().stock == 5
        assert r2.scalar_one().stock == 3

    async def test_save_snapshot_without_ah_store_metadata(
        self, session: AsyncSession, repo: BargainRepository
    ):
        """ah_store=None is allowed — store row gets minimal defaults."""
        snapshot = await repo.save_snapshot(2011, None, [_ah_item(110001)])
        result = await session.execute(select(Store).where(Store.id == 2011))
        store = result.scalar_one()
        assert store.id == 2011
        assert store.name == ""


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


class TestReadHelpers:
    async def test_get_latest_snapshot(
        self, session: AsyncSession, repo: BargainRepository
    ):
        store = Store(id=3001, name="Test", store_type="REGULAR", city="A",
                      street="B", postal_code="1000AA")
        session.add(store)
        await session.flush()

        s1 = Snapshot(store_id=3001, fetched_at=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc), item_count=5)
        s2 = Snapshot(store_id=3001, fetched_at=datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc), item_count=3)
        session.add_all([s1, s2])
        await session.flush()

        latest = await repo.get_latest_snapshot(3001)
        assert latest is not None
        assert latest.item_count == 3  # Most recent

    async def test_get_latest_snapshot_none_when_no_snapshots(
        self, session: AsyncSession, repo: BargainRepository
    ):
        result = await repo.get_latest_snapshot(99999)
        assert result is None

    async def test_get_snapshots_for_store(
        self, session: AsyncSession, repo: BargainRepository
    ):
        store = Store(id=3002, name="Test", store_type="REGULAR", city="A",
                      street="B", postal_code="1000AA")
        session.add(store)
        await session.flush()

        for i in range(5):
            s = Snapshot(
                store_id=3002,
                fetched_at=datetime(2026, 9, 30, i, 0, tzinfo=timezone.utc),
                item_count=i,
            )
            session.add(s)
        await session.flush()

        snapshots = await repo.get_snapshots_for_store(3002, limit=3)
        assert len(snapshots) == 3
        # Should be newest first (SQLite may return tz-naive datetimes)
        t0 = snapshots[0].fetched_at.replace(tzinfo=None)
        t1 = snapshots[1].fetched_at.replace(tzinfo=None)
        assert t0 > t1
