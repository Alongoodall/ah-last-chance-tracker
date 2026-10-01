"""Bargains router — /api/stores/{store_id}/bargains endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.schemas import BargainItemResponse
from app.db.models import BargainItem, Product, Snapshot, Store
from app.db.session import get_db

router = APIRouter(prefix="/api/stores", tags=["bargains"])


@router.get("/{store_id}/bargains", response_model=list[BargainItemResponse])
async def get_bargains(
    store_id: int,
    snapshot_id: int | None = Query(
        default=None,
        description="Specific snapshot ID. Defaults to the latest snapshot.",
    ),
    min_discount: float | None = Query(
        default=None,
        ge=0,
        le=100,
        description="Only return items with at least this discount percentage.",
    ),
    category: str | None = Query(
        default=None,
        description="Filter by product category (case-insensitive substring match).",
    ),
    in_stock: bool = Query(
        default=True,
        description="Only return items with stock > 0.",
    ),
    db: AsyncSession = Depends(get_db),
) -> list[BargainItemResponse]:
    """Return bargain items for a store.

    By default returns the **latest** collection snapshot.
    Pass ``?snapshot_id=X`` to retrieve a historical snapshot.

    Filtering:
    - ``min_discount``: e.g. ``?min_discount=40`` for 40%+ deals only
    - ``category``: e.g. ``?category=vlees`` (case-insensitive)
    - ``in_stock``: ``?in_stock=false`` to include out-of-stock items too
    """
    # Validate store exists
    store_result = await db.execute(select(Store).where(Store.id == store_id))
    if store_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"Store {store_id} not found")

    # Resolve snapshot
    if snapshot_id is not None:
        snap_result = await db.execute(
            select(Snapshot).where(
                Snapshot.id == snapshot_id,
                Snapshot.store_id == store_id,
            )
        )
        snapshot = snap_result.scalar_one_or_none()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot {snapshot_id} not found for store {store_id}",
            )
    else:
        # Latest snapshot for this store
        snap_result = await db.execute(
            select(Snapshot)
            .where(Snapshot.store_id == store_id)
            .order_by(Snapshot.fetched_at.desc())
            .limit(1)
        )
        snapshot = snap_result.scalar_one_or_none()
        if snapshot is None:
            # Store exists but has never been collected
            return []

    # Fetch bargain items with product joined
    query = (
        select(BargainItem)
        .where(BargainItem.snapshot_id == snapshot.id)
        .options(selectinload(BargainItem.product))
        .order_by(BargainItem.markdown_percentage.desc().nulls_last())
    )

    if in_stock:
        query = query.where(BargainItem.stock > 0)

    if min_discount is not None:
        query = query.where(BargainItem.markdown_percentage >= min_discount)

    result = await db.execute(query)
    items = result.scalars().all()

    # Category filter (done in Python — category is stored on Product, not BargainItem)
    if category:
        cat_lower = category.lower()
        items = [i for i in items if cat_lower in (i.product.category or "").lower()]

    return [
        BargainItemResponse.from_orm_item(item, product=item.product)
        for item in items
    ]
