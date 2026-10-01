"""Stores router — /api/stores endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import SnapshotResponse, StoreResponse
from app.db.models import Snapshot, Store
from app.db.session import get_db

router = APIRouter(prefix="/api/stores", tags=["stores"])


@router.get("", response_model=list[StoreResponse])
async def list_stores(db: AsyncSession = Depends(get_db)) -> list[StoreResponse]:
    """Return all tracked stores with their snapshot counts."""
    # Fetch stores and snapshot counts in one query
    result = await db.execute(
        select(Store, func.count(Snapshot.id).label("snapshot_count"))
        .outerjoin(Snapshot, Snapshot.store_id == Store.id)
        .group_by(Store.id)
        .order_by(Store.name)
    )
    rows = result.all()

    return [
        StoreResponse(
            id=store.id,
            name=store.name,
            store_type=store.store_type,
            city=store.city,
            street=store.street,
            postal_code=store.postal_code,
            created_at=store.created_at,
            snapshot_count=count,
        )
        for store, count in rows
    ]


@router.get("/{store_id}", response_model=StoreResponse)
async def get_store(
    store_id: int,
    db: AsyncSession = Depends(get_db),
) -> StoreResponse:
    """Return one store by ID."""
    result = await db.execute(
        select(Store, func.count(Snapshot.id).label("snapshot_count"))
        .outerjoin(Snapshot, Snapshot.store_id == Store.id)
        .where(Store.id == store_id)
        .group_by(Store.id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Store {store_id} not found")

    store, count = row
    return StoreResponse(
        id=store.id,
        name=store.name,
        store_type=store.store_type,
        city=store.city,
        street=store.street,
        postal_code=store.postal_code,
        created_at=store.created_at,
        snapshot_count=count,
    )


@router.get("/{store_id}/snapshots", response_model=list[SnapshotResponse])
async def list_snapshots(
    store_id: int,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[SnapshotResponse]:
    """Return the collection run history for a store, newest first.

    Args:
        store_id: The AH store ID.
        limit: Maximum number of snapshots to return (default 50, max 200).
        offset: Skip this many snapshots (for pagination).
    """
    # Validate store exists
    store_result = await db.execute(select(Store).where(Store.id == store_id))
    if store_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"Store {store_id} not found")

    limit = min(limit, 200)

    result = await db.execute(
        select(Snapshot)
        .where(Snapshot.store_id == store_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(limit)
        .offset(offset)
    )
    snapshots = result.scalars().all()
    return [SnapshotResponse.model_validate(s) for s in snapshots]
