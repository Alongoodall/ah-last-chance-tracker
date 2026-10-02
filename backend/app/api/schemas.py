"""Pydantic response schemas for the REST API.

These are deliberately separate from the ORM models (``app.db.models``).
The ORM models represent the database structure; these schemas represent
what the API returns to clients.

Design choices:
- Prices are exposed as floats (euros) not raw cents — clients shouldn't
  need to know about the internal cent representation.
- All models use ``model_config = ConfigDict(from_attributes=True)`` so
  they can be built directly from SQLAlchemy ORM instances.
- Optional fields use ``None`` so clients can detect missing data.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field, model_validator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cents_to_euros(cents: int | None) -> float | None:
    """Convert integer cents to float euros, or None if not provided."""
    if cents is None:
        return None
    return round(cents / 100, 2)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class StoreResponse(BaseModel):
    """A tracked Albert Heijn store."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    store_type: str
    city: str
    street: str
    postal_code: str
    created_at: datetime
    snapshot_count: int = 0  # Injected by the route handler


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class SnapshotResponse(BaseModel):
    """One collection run for a store."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    fetched_at: datetime
    item_count: int


# ---------------------------------------------------------------------------
# Product (embedded in bargain responses)
# ---------------------------------------------------------------------------


class ProductResponse(BaseModel):
    """Basic product metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    brand: str
    category: str
    sales_unit_size: str
    image_url: str | None = None


# ---------------------------------------------------------------------------
# BargainItem
# ---------------------------------------------------------------------------


class BargainItemResponse(BaseModel):
    """A single bargain deal at a specific point in time.

    Prices are in euros (float). The ORM stores them as integer cents.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    snapshot_id: int
    product_id: int

    # Prices as euros (computed from the raw cent columns)
    price_now: float | None = None
    price_was: float | None = None

    markdown_percentage: float | None
    markdown_type: str
    markdown_expiration_date: str
    stock: int

    # Product metadata is joined in by the route handler
    product: ProductResponse | None = None

    @model_validator(mode="before")
    @classmethod
    def _convert_prices(cls, data: object) -> object:
        """Convert price_now_cents / price_was_cents → price_now / price_was.

        Works both when ``data`` is a dict (e.g. from tests) and when it is an
        ORM object (``from_attributes=True``).
        """
        if isinstance(data, dict):
            if "price_now" not in data:
                data["price_now"] = _cents_to_euros(data.get("price_now_cents"))
            if "price_was" not in data:
                data["price_was"] = _cents_to_euros(data.get("price_was_cents"))
        else:
            # ORM object — read attributes directly
            object.__setattr__(
                data,
                "__price_now__",
                _cents_to_euros(getattr(data, "price_now_cents", None)),
            ) if False else None  # (we handle this differently below)
        return data

    @classmethod
    def from_orm_item(
        cls,
        item: object,
        product: object | None = None,
    ) -> "BargainItemResponse":
        """Build from an ORM BargainItem, converting prices and joining product."""
        from app.db.models import BargainItem, Product  # avoid circular import

        assert isinstance(item, BargainItem)
        return cls(
            id=item.id,
            snapshot_id=item.snapshot_id,
            product_id=item.product_id,
            price_now=_cents_to_euros(item.price_now_cents),
            price_was=_cents_to_euros(item.price_was_cents),
            markdown_percentage=item.markdown_percentage,
            markdown_type=item.markdown_type,
            markdown_expiration_date=item.markdown_expiration_date,
            stock=item.stock,
            product=ProductResponse.model_validate(product) if product else None,
        )


# ---------------------------------------------------------------------------
# Product history
# ---------------------------------------------------------------------------


class ProductHistoryEntry(BaseModel):
    """One price observation for a product (used in history endpoint)."""

    snapshot_id: int
    fetched_at: datetime
    price_now: float | None
    price_was: float | None
    markdown_percentage: float | None
    stock: int


class ProductHistoryResponse(BaseModel):
    """Full price/discount history for one product at one store."""

    product: ProductResponse
    store_id: int
    entries: list[ProductHistoryEntry]


# ---------------------------------------------------------------------------
# Pagination / envelope
# ---------------------------------------------------------------------------


class PaginatedResponse(BaseModel):
    """Generic paginated list wrapper."""

    total: int
    limit: int
    offset: int
    items: list  # Typed by the specific route
