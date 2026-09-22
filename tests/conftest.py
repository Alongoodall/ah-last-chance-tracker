"""Shared test fixtures."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings() -> Settings:
    """Return test-specific settings (in-memory DB, debug on)."""
    return Settings(
        debug=True,
        database_url="sqlite+aiosqlite:///:memory:",
    )


@pytest.fixture()
def app(settings: Settings):
    """Return a fresh FastAPI app wired to test settings."""
    return create_app(settings)


@pytest.fixture()
async def client(app) -> AsyncClient:
    """Async HTTP client that talks directly to the ASGI app (no server needed)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
