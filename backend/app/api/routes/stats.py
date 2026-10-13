"""Collection and storage statistics — /api/stats."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import BargainRepository
from app.db.session import get_db

router = APIRouter(prefix="/api/stats", tags=["system"])


class StatsResponse(BaseModel):
    snapshot_count: int
    oldest_snapshot: datetime | None
    newest_snapshot: datetime | None
    product_count: int
    db_size_bytes: int = Field(
        description=(
            "SQLite disk usage in bytes, including the main database, WAL and "
            "shared-memory files; 0 for in-memory or non-SQLite databases."
        )
    )

    @field_validator("oldest_snapshot", "newest_snapshot")
    @classmethod
    def _timestamps_in_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite returns naive datetimes even for DateTime(timezone=True).
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


@router.get("", response_model=StatsResponse)
async def get_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> StatsResponse:
    """Return snapshot range, retained product count and database disk usage."""
    return StatsResponse.model_validate(await BargainRepository(db).get_stats())
