"""Tests for the database ORM models (Step 3a).

These tests create an in-memory SQLite database, build the schema, and verify:
- All tables are created correctly.
- Relationships (FK / back-populates) work.
- Unique constraints are enforced.
- Repr strings are sensible.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BargainItem,
    Product,
    Snapshot,
    Store,
)


# ---------------------------------------------------------------------------
# Fixtures — in-memory DB, independent of .env / disk
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="module")
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
        await s.rollback()  # keep tests isolated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(**kw) -> Store:
    defaults = dict(id=1, name="Test Store", store_type="REGULAR", city="Amsterdam",
                    street="Teststraat 1", postal_code="1091AB")
    return Store(**{**defaults, **kw})


def _product(**kw) -> Product:
    defaults = dict(id=123456, title="AH Roodbaarsfilet", brand="AH",
                    category="Vis", sales_unit_size="ca. 200 g")
    return Product(**{**defaults, **kw})


# ---------------------------------------------------------------------------
# Schema smoke tests
# ---------------------------------------------------------------------------


class TestSchemaCreation:
    async def test_all_tables_exist(self, engine):
        async with engine.connect() as conn:
            table_names = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_table_names()
            )
        assert "stores" in table_names
        assert "products" in table_names
        assert "snapshots" in table_names
        assert "bargain_items" in table_names

    async def test_bargain_items_indexes_exist(self, engine):
        async with engine.connect() as conn:
            indexes = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_indexes("bargain_items")
            )
        index_names = {i["name"] for i in indexes}
        assert "ix_bargain_items_snapshot_id" in index_names
        assert "ix_bargain_items_product_id" in index_names


# ---------------------------------------------------------------------------
# Model construction & relationships
# ---------------------------------------------------------------------------


class TestModelConstruction:
    async def test_store_insert_and_read(self, session: AsyncSession):
        store = _store(id=10)
        session.add(store)
        await session.flush()

        result = await session.execute(select(Store).where(Store.id == 10))
        found = result.scalar_one()
        assert found.name == "Test Store"
        assert found.city == "Amsterdam"

    async def test_product_insert_and_read(self, session: AsyncSession):
        product = _product(id=999)
        session.add(product)
        await session.flush()

        result = await session.execute(select(Product).where(Product.id == 999))
        found = result.scalar_one()
        assert found.title == "AH Roodbaarsfilet"
        assert found.brand == "AH"

    async def test_snapshot_requires_store_fk(self, session: AsyncSession):
        store = _store(id=20)
        session.add(store)
        await session.flush()

        snapshot = Snapshot(store_id=20, item_count=5)
        session.add(snapshot)
        await session.flush()

        assert snapshot.id is not None
        assert snapshot.item_count == 5

    async def test_bargain_item_full_row(self, session: AsyncSession):
        store = _store(id=30)
        product = _product(id=30001)
        session.add_all([store, product])
        await session.flush()

        snapshot = Snapshot(store_id=30, item_count=1)
        session.add(snapshot)
        await session.flush()

        item = BargainItem(
            snapshot_id=snapshot.id,
            product_id=30001,
            price_now_cents=189,
            price_was_cents=299,
            markdown_percentage=37.0,
            markdown_type="EXPIRATION",
            markdown_expiration_date="2026-09-30",
            stock=4,
        )
        session.add(item)
        await session.flush()

        result = await session.execute(
            select(BargainItem).where(BargainItem.snapshot_id == snapshot.id)
        )
        found = result.scalar_one()
        assert found.price_now_cents == 189
        assert found.price_was_cents == 299
        assert found.markdown_percentage == pytest.approx(37.0)
        assert found.stock == 4

    async def test_nullable_prices(self, session: AsyncSession):
        """price_was can be NULL when the API doesn't provide an original price."""
        store = _store(id=40)
        product = _product(id=40001)
        session.add_all([store, product])
        await session.flush()

        snapshot = Snapshot(store_id=40, item_count=1)
        session.add(snapshot)
        await session.flush()

        item = BargainItem(
            snapshot_id=snapshot.id,
            product_id=40001,
            price_now_cents=99,
            price_was_cents=None,
            stock=1,
        )
        session.add(item)
        await session.flush()

        result = await session.execute(
            select(BargainItem).where(BargainItem.product_id == 40001)
        )
        found = result.scalar_one()
        assert found.price_was_cents is None
        assert found.price_now_cents == 99


# ---------------------------------------------------------------------------
# Unique constraint
# ---------------------------------------------------------------------------


class TestUniqueConstraint:
    async def test_duplicate_snapshot_product_raises(self, session: AsyncSession):
        """Inserting the same (snapshot_id, product_id) pair twice should fail."""
        from sqlalchemy.exc import IntegrityError

        store = _store(id=50)
        product = _product(id=50001)
        session.add_all([store, product])
        await session.flush()

        snapshot = Snapshot(store_id=50, item_count=2)
        session.add(snapshot)
        await session.flush()

        item1 = BargainItem(snapshot_id=snapshot.id, product_id=50001, stock=2)
        session.add(item1)
        await session.flush()

        # Adding a second item with the same (snapshot, product) should fail
        item2 = BargainItem(snapshot_id=snapshot.id, product_id=50001, stock=5)
        session.add(item2)
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# Repr strings
# ---------------------------------------------------------------------------


class TestReprs:
    def test_store_repr(self):
        s = Store(id=1, name="Amsterdam Oost")
        assert "Amsterdam Oost" in repr(s)

    def test_product_repr(self):
        p = Product(id=42, title="AH Kaas")
        assert "AH Kaas" in repr(p)

    def test_snapshot_repr(self):
        dt = datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc)
        sn = Snapshot(id=7, store_id=1, fetched_at=dt, item_count=12)
        assert "store_id=1" in repr(sn)
        assert "items=12" in repr(sn)

    def test_bargain_item_repr(self):
        bi = BargainItem(snapshot_id=1, product_id=99, price_now_cents=250, markdown_percentage=25.0)
        assert "250" in repr(bi)
        assert "25.0" in repr(bi)
