"""Isolated endpoint tests for collection totals and SQLite disk usage."""

from datetime import UTC, datetime, timedelta

import pytest
from app.config import Settings
from app.db.models import Base, Product, Snapshot, Store
from app.db.repository import BargainRepository
from app.db.session import get_db
from app.main import create_app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def stats_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _request_stats(session):
    app = create_app(
        Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", ah_store_ids="")
    )

    async def database_override():
        yield session

    app.dependency_overrides[get_db] = database_override
    # Exercise the router with the isolated DB without starting the live scheduler.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/stats")


async def test_stats_empty_database(stats_session):
    response = await _request_stats(stats_session)
    assert response.status_code == 200
    assert response.json() == {
        "snapshot_count": 0,
        "oldest_snapshot": None,
        "newest_snapshot": None,
        "product_count": 0,
        "db_size_bytes": 0,
    }


async def test_stats_counts_all_stores_and_serializes_utc_range(stats_session):
    oldest = datetime(2026, 9, 1, 10, tzinfo=UTC)
    newest = datetime(2026, 10, 12, 20, tzinfo=UTC)
    stats_session.add_all([Store(id=1), Store(id=2), Product(id=10), Product(id=20)])
    await stats_session.flush()
    stats_session.add_all(
        [
            Snapshot(store_id=1, fetched_at=newest, item_count=0),
            Snapshot(store_id=2, fetched_at=oldest, item_count=0),
            Snapshot(store_id=1, fetched_at=oldest + timedelta(days=2), item_count=0),
        ]
    )
    await stats_session.commit()

    response = await _request_stats(stats_session)
    assert response.status_code == 200
    data = response.json()
    assert data["snapshot_count"] == 3
    assert data["product_count"] == 2
    assert data["oldest_snapshot"] == "2026-09-01T10:00:00Z"
    assert data["newest_snapshot"] == "2026-10-12T20:00:00Z"
    assert data["db_size_bytes"] == 0


async def test_stats_reflect_pruning_without_losing_product_count(stats_session):
    now = datetime.now(UTC)
    stats_session.add_all([Store(id=1), Product(id=10)])
    await stats_session.flush()
    stats_session.add_all(
        [
            Snapshot(store_id=1, fetched_at=now - timedelta(days=60), item_count=0),
            Snapshot(store_id=1, fetched_at=now, item_count=0),
        ]
    )
    await stats_session.commit()
    assert await BargainRepository(stats_session).prune_old_snapshots(30) == 1
    await stats_session.commit()

    data = (await _request_stats(stats_session)).json()
    assert data["snapshot_count"] == 1
    assert data["product_count"] == 1
    assert data["oldest_snapshot"] == data["newest_snapshot"]


async def test_stats_measure_connected_database_including_wal(tmp_path):
    database_path = tmp_path / "tracker with spaces.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Product(id=1))
            await session.commit()
            response = await _request_stats(session)
            expected_size = sum(
                path.stat().st_size
                for path in [
                    database_path,
                    database_path.with_name(database_path.name + "-wal"),
                    database_path.with_name(database_path.name + "-shm"),
                ]
                if path.exists()
            )
            assert response.status_code == 200
            assert expected_size > database_path.stat().st_size
            assert response.json()["db_size_bytes"] == expected_size
    finally:
        await engine.dispose()
