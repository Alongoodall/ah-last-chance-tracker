"""Tests for CollectorService (Step 4a).

All AH API calls are mocked — these tests never touch the real AH API
or the disk-based token store. They use an in-memory SQLite DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ah.models import (
    AHBargainItem,
    AHBargainMarkdown,
    AHBargainPrice,
    AHBargainProduct,
)
from app.ah import AHAuthenticationError, AHError
from app.collector import CollectionResult, CollectorService
from app.db.models import Base, Snapshot
from sqlalchemy import select


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
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def token_store():
    """Minimal fake TokenStore — the collector only passes it to AHClient."""
    ts = MagicMock()
    ts.load.return_value = MagicMock(
        access_token="fake-token",
        refresh_token="fake-refresh",
        is_expired=lambda: False,
    )
    return ts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bargain_item(product_id: int = 10001, stock: int = 3) -> AHBargainItem:
    return AHBargainItem(
        product=AHBargainProduct(
            id=product_id,
            title="AH Testproduct",
            brand="AH",
            salesUnitSize="200 g",
        ),
        categoryTitle="Test",
        markdown=AHBargainMarkdown(
            markdownType="EXPIRATION",
            markdownExpirationDate="2026-10-01",
            markdownPercentage=25.0,
        ),
        stock=stock,
        bargainPrice=AHBargainPrice(priceWas="3.99", priceNow="2.99"),
    )


def _make_service(
    store_ids: list[int],
    token_store,
    session_factory,
    bargain_items: list[AHBargainItem] | None = None,
    raise_error: Exception | None = None,
) -> CollectorService:
    """Return a CollectorService whose AHClient.get_bargains() is mocked."""
    return CollectorService(
        store_ids=store_ids,
        token_store=token_store,
        session_factory=session_factory,
    )


# ---------------------------------------------------------------------------
# CollectionResult
# ---------------------------------------------------------------------------


class TestCollectionResult:
    def test_ok_when_snapshot_present(self):
        snap = MagicMock(spec=Snapshot)
        r = CollectionResult(store_id=1, snapshot=snap)
        assert r.ok is True

    def test_not_ok_when_error(self):
        r = CollectionResult(store_id=1, error=ValueError("boom"))
        assert r.ok is False

    def test_repr_ok(self):
        snap = MagicMock(spec=Snapshot, id=7, item_count=42)
        r = CollectionResult(store_id=1, snapshot=snap)
        assert "42" in repr(r)

    def test_repr_error(self):
        r = CollectionResult(store_id=1, error=ValueError("boom"))
        assert "boom" in repr(r)


# ---------------------------------------------------------------------------
# collect_all
# ---------------------------------------------------------------------------


class TestCollectAll:
    async def test_returns_empty_when_no_stores(self, token_store, session_factory):
        service = CollectorService(
            store_ids=[], token_store=token_store, session_factory=session_factory
        )
        with patch("app.collector.AHClient") as MockClient:
            results = await service.collect_all()

        assert results == []
        MockClient.assert_not_called()

    async def test_collects_one_store(self, token_store, session_factory):
        service = CollectorService(
            store_ids=[2203], token_store=token_store, session_factory=session_factory
        )
        items = [_make_bargain_item(10001), _make_bargain_item(10002)]

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(return_value=items)
            mock_client.get_store_by_id = AsyncMock(return_value=None)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await service.collect_all()

        assert len(results) == 1
        assert results[0].ok
        assert results[0].store_id == 2203
        assert results[0].snapshot is not None
        assert results[0].snapshot.item_count == 2

    async def test_collects_multiple_stores(self, token_store, session_factory):
        service = CollectorService(
            store_ids=[2203, 1812, 1315],
            token_store=token_store,
            session_factory=session_factory,
        )
        items = [_make_bargain_item(20001 + i) for i in range(3)]

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(return_value=items)
            mock_client.get_store_by_id = AsyncMock(return_value=None)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await service.collect_all()

        assert len(results) == 3
        assert all(r.ok for r in results)

    async def test_auth_error_captured_not_raised(self, token_store, session_factory):
        service = CollectorService(
            store_ids=[2203], token_store=token_store, session_factory=session_factory
        )

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(
                side_effect=AHAuthenticationError("Not authenticated")
            )
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await service.collect_all()

        assert len(results) == 1
        assert results[0].ok is False
        assert isinstance(results[0].error, AHAuthenticationError)

    async def test_api_error_captured_not_raised(self, token_store, session_factory):
        service = CollectorService(
            store_ids=[2203], token_store=token_store, session_factory=session_factory
        )

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(
                side_effect=AHError("API blew up")
            )
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await service.collect_all()

        assert not results[0].ok
        assert isinstance(results[0].error, AHError)

    async def test_one_failing_store_does_not_stop_others(
        self, token_store, session_factory
    ):
        """If store A fails, store B should still be attempted."""
        service = CollectorService(
            store_ids=[2203, 1812],
            token_store=token_store,
            session_factory=session_factory,
        )

        call_count = 0

        async def flaky_get_bargains(store_id: int):
            nonlocal call_count
            call_count += 1
            if store_id == 2203:
                raise AHError("store 2203 unavailable")
            return [_make_bargain_item(30001)]

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = flaky_get_bargains
            mock_client.get_store_by_id = AsyncMock(return_value=None)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await service.collect_all()

        assert call_count == 2  # Both stores were attempted
        assert results[0].ok is False   # 2203 failed
        assert results[1].ok is True    # 1812 succeeded

    async def test_empty_bargain_list_is_valid(self, token_store, session_factory):
        """A store returning 0 bargains is a valid state (not an error)."""
        service = CollectorService(
            store_ids=[2203], token_store=token_store, session_factory=session_factory
        )

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(return_value=[])
            mock_client.get_store_by_id = AsyncMock(return_value=None)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await service.collect_all()

        assert results[0].ok
        assert results[0].snapshot.item_count == 0

    async def test_snapshot_persisted_to_db(self, token_store, session_factory, engine):
        """Verify the snapshot actually lands in the database."""
        service = CollectorService(
            store_ids=[9901],
            token_store=token_store,
            session_factory=session_factory,
        )
        items = [_make_bargain_item(99001, stock=7)]

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(return_value=items)
            mock_client.get_store_by_id = AsyncMock(return_value=None)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await service.collect_all()

        snapshot_id = results[0].snapshot.id
        async with session_factory() as session:
            result = await session.execute(
                select(Snapshot).where(Snapshot.id == snapshot_id)
            )
            db_snapshot = result.scalar_one()
            assert db_snapshot.store_id == 9901
            assert db_snapshot.item_count == 1


# ---------------------------------------------------------------------------
# collect_once
# ---------------------------------------------------------------------------


class TestCollectOnce:
    async def test_collect_once_success(self, token_store, session_factory):
        service = CollectorService(
            store_ids=[2203], token_store=token_store, session_factory=session_factory
        )
        items = [_make_bargain_item(50001)]

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(return_value=items)
            mock_client.get_store_by_id = AsyncMock(return_value=None)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await service.collect_once(store_id=2203)

        assert result.ok
        assert result.snapshot.item_count == 1

    async def test_collect_once_error(self, token_store, session_factory):
        service = CollectorService(
            store_ids=[], token_store=token_store, session_factory=session_factory
        )

        with patch("app.collector.AHClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get_bargains = AsyncMock(
                side_effect=AHAuthenticationError("expired")
            )
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await service.collect_once(store_id=2203)

        assert not result.ok
        assert isinstance(result.error, AHAuthenticationError)


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestConfig:
    def test_store_ids_parsed_correctly(self):
        from app.config import Settings
        s = Settings(ah_store_ids="2203,1812,1315")
        assert s.store_ids == [2203, 1812, 1315]

    def test_store_ids_empty_when_not_set(self):
        from app.config import Settings
        s = Settings(ah_store_ids="")
        assert s.store_ids == []

    def test_store_ids_handles_whitespace(self):
        from app.config import Settings
        s = Settings(ah_store_ids=" 2203 , 1812 ")
        assert s.store_ids == [2203, 1812]

    def test_interval_validation(self):
        from app.config import Settings
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            Settings(collection_interval_minutes=0)
