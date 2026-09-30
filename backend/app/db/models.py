"""SQLAlchemy ORM models for the AH Last Chance Tracker.

Schema overview:

    Store  ──< Snapshot ──< BargainItem >── Product

- ``Store``: an AH store location (discovered at collection time).
- ``Product``: a stable AH product (id comes from the AH API, is stable
  across stores and time).
- ``Snapshot``: one collection run for one store at one point in time.
- ``BargainItem``: a single product-in-snapshot row, capturing the exact
  price, discount, and stock level at the moment of that snapshot.

Design decisions:
- We keep ``Product`` as a denormalised cache of the last-seen metadata
  (title, brand, category, unit size).  This avoids expensive joins for
  display purposes while still giving us a stable FK target.
- Prices are stored as integers in euro-cents to avoid floating-point
  rounding.  A NULL means the API did not supply a value.
- ``BargainItem.snapshot_id`` is indexed because every query that reads
  items will filter by snapshot.
- ``BargainItem`` has a UNIQUE constraint on (snapshot_id, product_id)
  so we can use INSERT OR IGNORE / ON CONFLICT DO NOTHING safely.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store(Base):
    """An Albert Heijn store location."""

    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """The AH store ID (from the API, e.g. 2203)."""

    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    store_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    street: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    postal_code: Mapped[str] = mapped_column(String(10), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    snapshots: Mapped[list[Snapshot]] = relationship(
        "Snapshot", back_populates="store", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Store id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------


class Product(Base):
    """A unique AH product.

    The ``id`` is the AH product ID (stable across stores and time).
    All other fields are denormalised from the most recent API response
    and updated on every collection run.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    """The AH product ID (from the API)."""

    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    brand: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    sales_unit_size: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )

    bargain_items: Mapped[list[BargainItem]] = relationship(
        "BargainItem", back_populates="product"
    )

    def __repr__(self) -> str:
        return f"<Product id={self.id} title={self.title!r}>"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class Snapshot(Base):
    """One collection run for one store.

    A snapshot groups all ``BargainItem`` rows fetched in a single API call.
    """

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    store: Mapped[Store] = relationship("Store", back_populates="snapshots")
    bargain_items: Mapped[list[BargainItem]] = relationship(
        "BargainItem", back_populates="snapshot", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Snapshot id={self.id} store_id={self.store_id} "
            f"fetched_at={self.fetched_at.isoformat()} items={self.item_count}>"
        )


# ---------------------------------------------------------------------------
# BargainItem
# ---------------------------------------------------------------------------


class BargainItem(Base):
    """A single product observed in a snapshot.

    Price columns use euro-cents (integer) to avoid float rounding.
    A value of ``None`` means the API did not provide that price.
    """

    __tablename__ = "bargain_items"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "product_id", name="uq_snapshot_product"),
        Index("ix_bargain_items_snapshot_id", "snapshot_id"),
        Index("ix_bargain_items_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id"), nullable=False
    )

    # Pricing (euro-cents; None = not provided by API)
    price_now_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_was_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Discount
    markdown_percentage: Mapped[float | None] = mapped_column(nullable=True)
    markdown_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    markdown_expiration_date: Mapped[str] = mapped_column(
        String(20), nullable=False, default=""
    )

    # Stock
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    snapshot: Mapped[Snapshot] = relationship("Snapshot", back_populates="bargain_items")
    product: Mapped[Product] = relationship("Product", back_populates="bargain_items")

    def __repr__(self) -> str:
        return (
            f"<BargainItem snapshot={self.snapshot_id} product={self.product_id} "
            f"now={self.price_now_cents}¢ disc={self.markdown_percentage}%>"
        )
