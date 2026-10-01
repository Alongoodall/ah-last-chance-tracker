"""Products router — /api/products endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    BargainItemResponse,
    ProductHistoryEntry,
    ProductHistoryResponse,
    ProductResponse,
)
from app.db.models import BargainItem, Product, Snapshot
from app.db.session import get_db

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
) -> ProductResponse:
    """Return product metadata by AH product ID."""
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    return ProductResponse.model_validate(product)


@router.get("/{product_id}/history", response_model=ProductHistoryResponse)
async def get_product_history(
    product_id: int,
    store_id: int = Query(
        ...,
        description="The store to fetch history for. Required.",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=500,
        description="Maximum number of history entries to return.",
    ),
    db: AsyncSession = Depends(get_db),
) -> ProductHistoryResponse:
    """Return the price/discount history for a product at a specific store.

    History is ordered oldest-to-newest so it can be plotted directly as a
    time series.

    This endpoint answers: "how did this product's discount evolve today?"
    """
    # Validate product exists
    prod_result = await db.execute(select(Product).where(Product.id == product_id))
    product = prod_result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")

    # Fetch history: join BargainItem → Snapshot, filter by store
    result = await db.execute(
        select(BargainItem, Snapshot.fetched_at)
        .join(Snapshot, BargainItem.snapshot_id == Snapshot.id)
        .where(
            BargainItem.product_id == product_id,
            Snapshot.store_id == store_id,
        )
        .order_by(Snapshot.fetched_at.asc())
        .limit(limit)
    )
    rows = result.all()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No history found for product {product_id} at store {store_id}. "
                "Run a collection first."
            ),
        )

    from app.api.schemas import _cents_to_euros

    entries = [
        ProductHistoryEntry(
            snapshot_id=item.snapshot_id,
            fetched_at=fetched_at,
            price_now=_cents_to_euros(item.price_now_cents),
            price_was=_cents_to_euros(item.price_was_cents),
            markdown_percentage=item.markdown_percentage,
            stock=item.stock,
        )
        for item, fetched_at in rows
    ]

    return ProductHistoryResponse(
        product=ProductResponse.model_validate(product),
        store_id=store_id,
        entries=entries,
    )
