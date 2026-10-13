"""Retention removes expired history atomically, including SQLite child rows."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.collector import CollectionResult, CollectorService
from app.config import Settings
from app.db.models import BargainItem, Base, Product, Snapshot, Store
from app.db.repository import BargainRepository
from app.event_bus import EventBus
from app.main import _make_collector
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def retention_sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_history(session, now):
    session.add_all([Store(id=1), Store(id=2), Product(id=10), Product(id=20)])
    await session.flush()
    snapshots = [
        Snapshot(store_id=1, fetched_at=now - timedelta(days=31), item_count=1),
        Snapshot(store_id=2, fetched_at=now - timedelta(days=60), item_count=1),
        Snapshot(store_id=1, fetched_at=now - timedelta(days=30), item_count=1),
        Snapshot(store_id=2, fetched_at=now - timedelta(days=2), item_count=1),
    ]
    session.add_all(snapshots)
    await session.flush()
    session.add_all(
        BargainItem(snapshot_id=snapshot.id, product_id=10 if i % 2 == 0 else 20)
        for i, snapshot in enumerate(snapshots)
    )
    await session.commit()
    return snapshots


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_pruning_removes_items_and_keeps_boundary_and_metadata(
    retention_sessions, foreign_keys
):
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    async with retention_sessions() as session:
        await session.execute(text(f"PRAGMA foreign_keys={foreign_keys}"))
        snapshots = await _seed_history(session, now)
        with patch("app.db.repository.datetime") as clock:
            clock.now.return_value = now
            deleted = await BargainRepository(session).prune_old_snapshots(30)
        await session.commit()

        assert deleted == 2
        kept_ids = set((await session.scalars(select(Snapshot.id))).all())
        assert kept_ids == {snapshots[2].id, snapshots[3].id}
        item_snapshot_ids = set((await session.scalars(select(BargainItem.snapshot_id))).all())
        assert item_snapshot_ids == kept_ids
        assert await session.scalar(select(func.count(Product.id))) == 2
        assert await session.scalar(select(func.count(Store.id))) == 2


async def test_zero_retention_does_not_query_or_delete():
    session = AsyncMock()
    assert await BargainRepository(session).prune_old_snapshots(0) == 0
    session.execute.assert_not_awaited()


async def test_negative_retention_is_rejected_before_database_access():
    session = AsyncMock()
    with pytest.raises(ValueError, match="non-negative"):
        await BargainRepository(session).prune_old_snapshots(-1)
    session.execute.assert_not_awaited()


async def test_empty_database_prunes_nothing(retention_sessions):
    async with retention_sessions() as session:
        assert await BargainRepository(session).prune_old_snapshots(30) == 0


async def test_pruning_transaction_can_be_rolled_back(retention_sessions):
    now = datetime.now(UTC)
    async with retention_sessions() as session:
        await _seed_history(session, now)
        await BargainRepository(session).prune_old_snapshots(30)
        await session.rollback()
        assert await session.scalar(select(func.count(Snapshot.id))) == 4
        assert await session.scalar(select(func.count(BargainItem.id))) == 4


def test_retention_setting_default_zero_and_validation(monkeypatch):
    monkeypatch.delenv("AH_RETENTION_DAYS", raising=False)
    assert Settings(_env_file=None).ah_retention_days == 30
    assert Settings(_env_file=None, ah_retention_days=0).ah_retention_days == 0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ah_retention_days=-1)
    monkeypatch.setenv("AH_RETENTION_DAYS", "7")
    assert Settings(_env_file=None).ah_retention_days == 7


def test_settings_retention_passed_to_collector():
    settings = Settings(_env_file=None, ah_store_ids="1", ah_retention_days=7)
    with patch("app.main.CollectorService") as collector:
        _make_collector(settings, EventBus())
    assert collector.call_args.kwargs["retention_days"] == 7


def _service(sessions, retention_days=30):
    return CollectorService(
        store_ids=[1, 2],
        token_store=MagicMock(),
        session_factory=sessions,
        retention_days=retention_days,
    )


async def test_collector_prunes_once_after_all_stores_and_commits(retention_sessions):
    now = datetime.now(UTC)
    async with retention_sessions() as session:
        snapshots = await _seed_history(session, now)

    service = _service(retention_sessions)
    observed_stores = []

    async def collect_one(client, store_id):
        observed_stores.append(store_id)
        async with retention_sessions() as session:
            # Old rows must still exist while each store is being collected.
            assert await session.scalar(select(func.count(Snapshot.id))) == 4
        return CollectionResult(store_id=store_id)

    service._collect_one = AsyncMock(side_effect=collect_one)
    original_prune = BargainRepository.prune_old_snapshots
    prune_calls = []

    async def prune(repo, retention_days):
        assert observed_stores == [1, 2]
        prune_calls.append(retention_days)
        return await original_prune(repo, retention_days)

    with patch("app.collector.AHClient") as client, patch.object(
        BargainRepository, "prune_old_snapshots", prune
    ):
        client.return_value.__aenter__ = AsyncMock()
        client.return_value.__aexit__ = AsyncMock(return_value=False)
        results = await service.collect_all()

    assert len(results) == 2
    assert prune_calls == [30]
    async with retention_sessions() as session:
        assert await session.scalar(select(func.count(Snapshot.id))) == 1
        assert await session.scalar(select(func.count(BargainItem.id))) == 1
        assert await session.scalar(select(Snapshot.id)) == snapshots[3].id


async def test_collector_prune_failure_rolls_back_and_preserves_results(retention_sessions):
    now = datetime.now(UTC)
    async with retention_sessions() as session:
        await _seed_history(session, now)

    service = _service(retention_sessions)
    service._collect_one = AsyncMock(
        side_effect=[CollectionResult(1), CollectionResult(2, error=RuntimeError("store failed"))]
    )

    async def fail_after_delete(repo, retention_days):
        await repo._session.execute(text("DELETE FROM bargain_items"))
        raise RuntimeError("database failure")

    with patch("app.collector.AHClient") as client, patch.object(
        BargainRepository, "prune_old_snapshots", fail_after_delete
    ):
        client.return_value.__aenter__ = AsyncMock()
        client.return_value.__aexit__ = AsyncMock(return_value=False)
        results = await service.collect_all()

    assert results[0].ok
    assert not results[1].ok
    async with retention_sessions() as session:
        assert await session.scalar(select(func.count(Snapshot.id))) == 4
        assert await session.scalar(select(func.count(BargainItem.id))) == 4


async def test_disabled_retention_skips_housekeeping(retention_sessions):
    service = _service(retention_sessions, retention_days=0)
    service._collect_one = AsyncMock(return_value=CollectionResult(1))
    with patch("app.collector.AHClient") as client, patch.object(
        BargainRepository, "prune_old_snapshots", new_callable=AsyncMock
    ) as prune:
        client.return_value.__aenter__ = AsyncMock()
        client.return_value.__aexit__ = AsyncMock(return_value=False)
        await service.collect_all()
    prune.assert_not_awaited()


async def test_collect_once_does_not_prune(retention_sessions):
    service = _service(retention_sessions)
    service._collect_one = AsyncMock(return_value=CollectionResult(1))
    with patch("app.collector.AHClient") as client, patch.object(
        BargainRepository, "prune_old_snapshots", new_callable=AsyncMock
    ) as prune:
        client.return_value.__aenter__ = AsyncMock()
        client.return_value.__aexit__ = AsyncMock(return_value=False)
        await service.collect_once(1)
    prune.assert_not_awaited()
