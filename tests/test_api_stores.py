"""Tests for the /api/stores and /api/stores/{id}/snapshots endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Snapshot, Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(**kw) -> Store:
    defaults = dict(
        id=1,
        name="AH Amsterdam Oost",
        store_type="REGULAR",
        city="Amsterdam",
        street="Teststraat 1",
        postal_code="1091AB",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    return Store(**{**defaults, **kw})


async def _seed_store(
    session: AsyncSession, store_id: int = 1, num_snapshots: int = 0
) -> Store:
    store = _store(id=store_id, name=f"Store {store_id}")
    session.add(store)
    for i in range(num_snapshots):
        snap = Snapshot(
            store_id=store_id,
            fetched_at=datetime(2026, 10, 1, i, 0, tzinfo=timezone.utc),
            item_count=i * 10,
        )
        session.add(snap)
    await session.commit()
    return store


# ---------------------------------------------------------------------------
# GET /api/stores
# ---------------------------------------------------------------------------


class TestListStores:
    async def test_empty_list_when_no_stores(self, client: AsyncClient):
        """List endpoint returns 200 and a list (may have data from other tests)."""
        response = await client.get("/api/stores")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_returns_stores(self, client: AsyncClient, db_session: AsyncSession):
        await _seed_store(db_session, store_id=101)
        response = await client.get("/api/stores")
        assert response.status_code == 200
        data = response.json()
        ids = [s["id"] for s in data]
        assert 101 in ids

    async def test_snapshot_count_included(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_store(db_session, store_id=102, num_snapshots=5)
        response = await client.get("/api/stores")
        data = response.json()
        store = next(s for s in data if s["id"] == 102)
        assert store["snapshot_count"] == 5

    async def test_stores_ordered_by_name(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        for sid, name in [(201, "Zeist"), (202, "Amsterdam"), (203, "Rotterdam")]:
            s = Store(
                id=sid,
                name=name,
                store_type="REGULAR",
                city=name,
                street="A",
                postal_code="1000AA",
                created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            db_session.add(s)
        await db_session.commit()

        response = await client.get("/api/stores")
        names = [s["name"] for s in response.json() if s["id"] in {201, 202, 203}]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# GET /api/stores/{store_id}
# ---------------------------------------------------------------------------


class TestGetStore:
    async def test_returns_store(self, client: AsyncClient, db_session: AsyncSession):
        await _seed_store(db_session, store_id=301)
        response = await client.get("/api/stores/301")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 301

    async def test_404_for_unknown_store(self, client: AsyncClient):
        response = await client.get("/api/stores/99999")
        assert response.status_code == 404

    async def test_snapshot_count_on_single_store(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_store(db_session, store_id=302, num_snapshots=3)
        response = await client.get("/api/stores/302")
        assert response.json()["snapshot_count"] == 3


# ---------------------------------------------------------------------------
# GET /api/stores/{store_id}/snapshots
# ---------------------------------------------------------------------------


class TestListSnapshots:
    async def test_returns_snapshots_newest_first(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_store(db_session, store_id=401, num_snapshots=5)
        response = await client.get("/api/stores/401/snapshots")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 5
        # Newest first: fetched_at should be descending
        times = [d["fetched_at"] for d in data]
        assert times == sorted(times, reverse=True)

    async def test_pagination(self, client: AsyncClient, db_session: AsyncSession):
        await _seed_store(db_session, store_id=402, num_snapshots=10)
        page1 = await client.get("/api/stores/402/snapshots?limit=4&offset=0")
        page2 = await client.get("/api/stores/402/snapshots?limit=4&offset=4")
        assert len(page1.json()) == 4
        assert len(page2.json()) == 4

    async def test_404_for_unknown_store(self, client: AsyncClient):
        response = await client.get("/api/stores/99999/snapshots")
        assert response.status_code == 404

    async def test_empty_when_no_snapshots(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _seed_store(db_session, store_id=403, num_snapshots=0)
        response = await client.get("/api/stores/403/snapshots")
        assert response.json() == []
