"""Shared test fixtures.

Key fixtures:
    settings    — test settings (in-memory DB, no store IDs → no scheduler)
    app         — FastAPI app with get_db dependency overridden to use the
                  same in-memory DB as db_session
    client      — Async HTTP client that runs the full ASGI lifespan
    db_session  — Direct DB session for seeding test data (same DB as the app)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import Base
from app.db.session import get_db
from app.main import create_app

# ---------------------------------------------------------------------------
# Shared in-memory database (one per test session)
# ---------------------------------------------------------------------------

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
async def _test_engine():
    """One in-memory SQLite engine shared across the whole test session."""
    engine = create_async_engine(_TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def _session_factory(_test_engine):
    return async_sessionmaker(_test_engine, expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------------------
# Settings and app
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    """Test settings: in-memory DB, no store IDs → scheduler stays off."""
    return Settings(
        debug=False,
        database_url=_TEST_DB_URL,
        ah_store_ids="",
    )


@pytest.fixture()
def app(settings: Settings, _session_factory):
    """FastAPI app whose get_db dependency is overridden to use the shared DB.

    This is the critical piece: without this override, the app's session
    factory points to a *different* in-memory database than db_session does,
    so seeded data would be invisible to the API.
    """
    _app = create_app(settings)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with _session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    _app.dependency_overrides[get_db] = _override_get_db
    return _app


@pytest.fixture()
async def client(app) -> AsyncClient:
    """Async HTTP client that runs the full FastAPI lifespan."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            yield ac


# ---------------------------------------------------------------------------
# DB session for test data seeding
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_session(_session_factory) -> AsyncSession:
    """Async session for seeding test data.

    Rolls back after each test to keep tests isolated.
    """
    async with _session_factory() as session:
        yield session
        await session.rollback()
