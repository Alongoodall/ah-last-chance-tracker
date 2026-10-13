"""Product images survive persistence, collection cycles and schema upgrades."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.ah.client import AHClient
from app.ah.models import AHBargainItem
from app.ah.token_store import TokenStore
from app.api.schemas import ProductResponse
from app.collector import CollectorService
from app.db.models import Base, Product
from app.db.repository import BargainRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_IMAGE_URL = "https://static.ah.nl/dam/product/AHI_example?rendition=400x400_JPG_Q85"


def _item(webshop_id: int | None = 12345, image_url: str | None = None) -> AHBargainItem:
    return AHBargainItem.model_validate({
        "product": {
            "id": 1000, "title": "Test product", "webshopId": webshop_id,
            "images": [{"url": image_url}] if image_url else [],
        },
        "markdown": {"markdownPercentage": 35},
        "bargainPrice": {"priceNow": "1.29", "priceWas": "1.99"},
        "stock": 3,
    })


@pytest.fixture
async def image_sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_repository_persists_and_exposes_resolved_image(image_sessions):
    async with image_sessions() as session:
        repo = BargainRepository(session)
        snapshot = await repo.save_snapshot(2301, None, [_item(image_url=_IMAGE_URL)])
        await session.commit()
        product = await session.get(Product, 1000)
        response = ProductResponse.model_validate(product)
        assert response.webshop_id == 12345
        assert response.image_url == _IMAGE_URL
        items = await repo.get_bargain_items_for_snapshot(snapshot.id)
        assert items[0].price_now_cents == 129
        assert items[0].price_was_cents == 199


async def test_partial_update_preserves_cached_image_and_webshop_id(image_sessions):
    async with image_sessions() as session:
        repo = BargainRepository(session)
        await repo.save_snapshot(2301, None, [_item(image_url=_IMAGE_URL)])
        await repo.save_snapshot(2301, None, [_item(webshop_id=None)])
        product = await session.get(Product, 1000)
        assert product.webshop_id == 12345
        assert product.image_url == _IMAGE_URL


async def test_changed_webshop_id_invalidates_old_image(image_sessions):
    async with image_sessions() as session:
        repo = BargainRepository(session)
        await repo.save_snapshot(2301, None, [_item(image_url=_IMAGE_URL)])
        await repo.save_snapshot(2301, None, [_item(webshop_id=67890)])
        product = await session.get(Product, 1000)
        assert product.webshop_id == 67890
        assert product.image_url is None


async def test_collector_reuses_db_cache_and_looks_up_changed_webshop_id(
    image_sessions, tmp_path,
):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        webshop_id = int(request.url.params["ids"])
        return httpx.Response(200, json=[{
            "webshopId": webshop_id,
            "images": [{"url": f"{_IMAGE_URL}&id={webshop_id}"}],
        }])

    transport = httpx.MockTransport(respond)
    http_factory = httpx.AsyncClient
    service = CollectorService(
        store_ids=[2301], token_store=TokenStore(path=tmp_path / "tokens.json"),
        session_factory=image_sessions,
    )
    with (
        patch("app.ah.client.httpx.AsyncClient", side_effect=lambda **kwargs: http_factory(
            transport=transport, **kwargs,
        )),
        patch.object(AHClient, "get_bargains", new=AsyncMock(side_effect=[
            [_item()], [_item(webshop_id=None)], [_item(webshop_id=67890)],
        ])),
    ):
        for _ in range(3):
            result = await service.collect_once(2301)
            assert result.ok, result.error

    assert [request.url.params["ids"] for request in requests] == ["12345", "67890"]
    async with image_sessions() as session:
        product = (await session.execute(select(Product))).scalar_one()
        assert product.webshop_id == 67890
        assert product.image_url == f"{_IMAGE_URL}&id=67890"


async def test_collector_saves_snapshot_when_image_endpoint_is_down(image_sessions, tmp_path):
    http_factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    service = CollectorService(
        store_ids=[2301], token_store=TokenStore(path=tmp_path / "tokens.json"),
        session_factory=image_sessions,
    )
    with (
        patch("app.ah.client.httpx.AsyncClient", side_effect=lambda **kwargs: http_factory(
            transport=transport, **kwargs,
        )),
        patch.object(AHClient, "get_bargains", new=AsyncMock(return_value=[_item()])),
    ):
        result = await service.collect_once(2301)
    assert result.ok, result.error
    assert result.snapshot.item_count == 1
    async with image_sessions() as session:
        product = await session.get(Product, 1000)
        assert product.image_url is None
        items = await BargainRepository(session).get_bargain_items_for_snapshot(result.snapshot.id)
        assert items[0].price_now_cents == 129


def test_webshop_migration_round_trip_preserves_existing_products(tmp_path):
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=project, env=env,
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "4cd14aed6478")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO products (id,title,brand,category,sales_unit_size,"
            "first_seen_at,last_seen_at,image_url) VALUES (?,?,?,?,?,?,?,?)",
            (1000, "Existing product", "AH", "Test", "200 g",
             "2026-10-01", "2026-10-01", _IMAGE_URL),
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title, webshop_id, image_url FROM products WHERE id=1000",
        ).fetchone() == ("Existing product", None, _IMAGE_URL)
    migrate("downgrade", "4cd14aed6478")
    with sqlite3.connect(database) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(products)")]
        assert "webshop_id" not in columns
        assert connection.execute("SELECT image_url FROM products").fetchone() == (_IMAGE_URL,)
    migrate("upgrade", "head")
