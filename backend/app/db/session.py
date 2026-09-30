"""Async SQLAlchemy engine and session factory.

Usage in application code::

    from app.db.session import async_session

    async with async_session() as session:
        result = await session.execute(select(Store))

Usage in FastAPI via the ``get_db`` dependency::

    from app.db.session import get_db

    @router.get("/stores")
    async def list_stores(db: AsyncSession = Depends(get_db)):
        ...
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

engine = create_async_engine(
    settings.database_url,
    # Echo SQL statements in DEBUG mode for easier troubleshooting
    echo=settings.debug,
    # SQLite specific: enable WAL mode for better concurrent reads
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session and close it when the request is done."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
