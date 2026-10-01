"""Smoke tests — verify the app starts, the DB is reachable, and the health
endpoint responds correctly. In test mode no stores are configured so the
scheduler doesn't start."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    """GET /api/health should return 200 with overall status ok."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_includes_database_status(client: AsyncClient) -> None:
    """Health response should report database connectivity."""
    response = await client.get("/api/health")
    data = response.json()
    assert data["database"] == "ok"


@pytest.mark.asyncio
async def test_health_includes_scheduler_status(client: AsyncClient) -> None:
    """Health response should include scheduler key.

    In test mode (no stores configured) the scheduler is 'stopped'.
    """
    response = await client.get("/api/health")
    data = response.json()
    assert "scheduler" in data
    # Tests use Settings with ah_store_ids="" so scheduler doesn't start
    assert data["scheduler"] == "stopped"


@pytest.mark.asyncio
async def test_health_reports_configured_stores(client: AsyncClient) -> None:
    """configured_stores should be an empty list in test mode."""
    response = await client.get("/api/health")
    data = response.json()
    assert data["configured_stores"] == []


@pytest.mark.asyncio
async def test_openapi_docs_available(client: AsyncClient) -> None:
    """The auto-generated OpenAPI schema should be reachable."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "AH Last Chance Tracker"
