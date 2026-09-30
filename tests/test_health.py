"""Smoke tests — verify the app starts, the DB is reachable, and the health
endpoint responds correctly."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    """GET /api/health should return 200 with status ok."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_includes_database_status(client: AsyncClient) -> None:
    """Health response should report database connectivity."""
    response = await client.get("/api/health")
    data = response.json()
    assert "database" in data
    assert data["database"] == "ok"


@pytest.mark.asyncio
async def test_openapi_docs_available(client: AsyncClient) -> None:
    """The auto-generated OpenAPI schema should be reachable."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "AH Last Chance Tracker"
