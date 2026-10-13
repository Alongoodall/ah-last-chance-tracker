"""Product images use real REST URLs without risking bargain collection."""

from asyncio import CancelledError
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.ah import AHAPIError, AHAuthenticationError, AHGraphQLError
from app.ah.client import (
    _BARGAIN_ITEMS_QUERY,
    _BARGAIN_ITEMS_QUERY_WITHOUT_WEBSHOP_ID,
    AHClient,
)
from app.ah.models import AHBargainItem, AHBargainProduct


def bargain(product_id: int, **product_fields) -> AHBargainItem:
    return AHBargainItem.model_validate({
        "product": {"id": product_id, "title": "AH Testproduct", **product_fields},
        "markdown": {"markdownPercentage": 35},
        "stock": 2,
        "bargainPrice": {"priceWas": "2.00", "priceNow": "1.30"},
    })


def card(product_id: int) -> dict:
    return {
        "webshopId": product_id,
        "images": [{"url": f"https://static.ah.nl/dam/product/asset-{product_id}"}],
    }


@asynccontextmanager
async def image_client(handler):
    """Inject an offline HTTP transport without loading account tokens."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AHClient(base_url="https://ah.test")
        client._http = http
        yield client


def test_webshop_id_optional_alias_and_no_guessed_image_url() -> None:
    product = AHBargainProduct(id=123, title="AH Product")
    assert product.webshop_id is None
    assert product.image_url is None
    aliased = AHBargainProduct.model_validate({
        "id": 123, "title": "AH Product", "webshopId": 456,
    })
    assert aliased.webshop_id == 456
    assert aliased.image_url is None
    assert AHBargainProduct(id=123, title="AH Product", webshop_id=456).webshop_id == 456


async def test_batch_request_maps_out_of_order_cards_by_webshop_id() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/mobile-services/product/search/v2/products"
        assert request.url.params.get_list("ids") == ["456", "789"]
        assert request.url.params["sortOn"] == "INPUT_PRODUCT_IDS"
        return httpx.Response(200, json=[card(789), card(456), card(999)])

    items = [bargain(123, webshopId=456), bargain(789)]
    async with image_client(handler) as client:
        await client.enrich_product_images(items)

    assert len(requests) == 1
    assert items[0].product.id == 123
    assert items[0].product.webshop_id == 456
    assert items[0].product.image_url == card(456)["images"][0]["url"]
    assert items[1].product.webshop_id == 789
    assert items[1].product.image_url == card(789)["images"][0]["url"]


async def test_supplied_images_skip_lookup_and_are_preserved() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    items = [bargain(123, images=[{"url": "https://static.ah.nl/existing.jpg"}])]
    async with image_client(handler) as client:
        await client.enrich_product_images(items)
        await client.enrich_product_images([])

    assert items[0].product.image_url == "https://static.ah.nl/existing.jpg"
    assert requests == []


@pytest.mark.parametrize(("dimensions", "expected_index"), [
    ([(48, 48), (200, 200), (400, 400), (800, 800)], 2),
    ([(48, 48), (300, 200), (200, 200)], 1),
    ([(None, None), (None, None)], 0),
])
async def test_selects_sharp_thumbnail_without_guessing_url(dimensions, expected_index) -> None:
    images = [
        {
            "url": f"https://static.ah.nl/dam/product/source-image-{index}",
            "width": width,
            "height": height,
        }
        for index, (width, height) in enumerate(dimensions)
    ]

    def handler(request):
        return httpx.Response(200, json=[{"webshopId": 123, "images": images}])

    items = [bargain(123)]
    async with image_client(handler) as client:
        await client.enrich_product_images(items)
        second_store = [bargain(123)]
        await client.enrich_product_images(second_store)

    expected_url = images[expected_index]["url"]
    assert items[0].product.image_url == expected_url
    assert second_store[0].product.image_url == expected_url
    assert {image.url for image in items[0].product.images} == {
        image["url"] for image in images
    }


async def test_requests_deduplicate_products_and_limit_batches_to_fifty() -> None:
    batches = []

    def handler(request):
        ids = [int(value) for value in request.url.params.get_list("ids")]
        batches.append(ids)
        return httpx.Response(200, json=[card(product_id) for product_id in ids])

    items = [bargain(product_id) for product_id in range(1, 102)]
    items.extend([bargain(1), bargain(60), bargain(101)])
    async with image_client(handler) as client:
        await client.enrich_product_images(items)

    assert [len(batch) for batch in batches] == [50, 50, 1]
    assert [product_id for batch in batches for product_id in batch] == list(range(1, 102))
    assert all(item.product.image_url for item in items)


async def test_cache_reuses_images_for_same_product_in_another_store() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[card(456)])

    first_store = [bargain(123, webshopId=456)]
    second_store = [bargain(321, webshopId=456)]
    async with image_client(handler) as client:
        await client.enrich_product_images(first_store)
        await client.enrich_product_images(second_store)

    assert len(requests) == 1
    assert first_store[0].product.image_url == second_store[0].product.image_url


@pytest.mark.parametrize("response", [
    [],
    [{"webshopId": 123}],
    [{"webshopId": 123, "images": None}],
    [{"webshopId": 123, "images": [{}, None, {"url": ""}]}],
])
async def test_absent_images_are_cached_without_inventing_urls(response) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    items = [bargain(123)]
    async with image_client(handler) as client:
        await client.enrich_product_images(items)
        await client.enrich_product_images([bargain(123)])

    assert len(requests) == 1
    assert items[0].product.image_url is None
    assert items[0].product.webshop_id == (123 if response else None)
    assert items[0].stock == 2
    assert items[0].bargain_price.price_now == "1.30"


@pytest.mark.parametrize("failure", [
    "unavailable", "unauthorized", "timeout", "malformed_json", "unexpected_envelope",
    "invalid_card", "invalid_image", "overflow_id",
])
async def test_image_endpoint_failure_does_not_break_bargains_or_repeat_lookup(failure) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("Image lookup timed out", request=request)
        if failure == "unavailable":
            return httpx.Response(503, json={"error": "service_unreachable"})
        if failure == "unauthorized":
            return httpx.Response(401)
        if failure == "malformed_json":
            return httpx.Response(200, content=b"not JSON")
        if failure == "unexpected_envelope":
            return httpx.Response(200, json={"products": [card(123)]})
        if failure == "invalid_card":
            return httpx.Response(200, json=[None])
        if failure == "overflow_id":
            return httpx.Response(200, content=b'[{"webshopId": Infinity, "images": []}]')
        return httpx.Response(200, json=[{"webshopId": 123, "images": [{"url": 42}]}])

    items = [bargain(123)]
    async with image_client(handler) as client:
        await client.enrich_product_images(items)
        await client.enrich_product_images([bargain(123)])

    assert len(requests) == 1
    assert items[0].product.image_url is None
    assert items[0].stock == 2
    assert items[0].bargain_price.price_now == "1.30"


async def test_failed_later_batch_preserves_images_resolved_in_earlier_batch() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(503)
        ids = [int(value) for value in request.url.params.get_list("ids")]
        return httpx.Response(200, json=[card(product_id) for product_id in ids])

    items = [bargain(product_id) for product_id in range(1, 102)]
    async with image_client(handler) as client:
        await client.enrich_product_images(items)
        await client.enrich_product_images([bargain(51)])

    assert len(requests) == 2
    assert all(item.product.image_url for item in items[:50])
    assert all(item.product.image_url is None for item in items[50:])


async def test_unauthorized_image_request_refreshes_once_and_retries() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401) if len(requests) == 1 else httpx.Response(200, json=[card(123)])

    items = [bargain(123)]
    async with image_client(handler) as client:
        client._refresh_token = "unused-test-placeholder"
        with patch.object(client, "_refresh_access_token", new_callable=AsyncMock) as refresh:
            await client.enrich_product_images(items)
            refresh.assert_awaited_once_with()

    assert len(requests) == 2
    assert requests[0].url == requests[1].url
    assert items[0].product.image_url == card(123)["images"][0]["url"]


async def test_image_refresh_failure_remains_optional() -> None:
    items = [bargain(123)]
    async with image_client(lambda request: httpx.Response(401)) as client:
        client._refresh_token = "unused-test-placeholder"
        with patch.object(client, "_refresh_access_token", new_callable=AsyncMock) as refresh:
            refresh.side_effect = AHAuthenticationError("Authentication unavailable")
            await client.enrich_product_images(items)

    assert items[0].product.image_url is None
    assert items[0].bargain_price.price_now == "1.30"


async def test_malformed_refresh_response_does_not_break_price_collection() -> None:
    items = [bargain(123)]
    async with image_client(lambda request: httpx.Response(401)) as client:
        client._refresh_token = "unused-test-placeholder"
        with patch.object(client, "_post_json", new_callable=AsyncMock) as post:
            post.return_value = []
            await client.enrich_product_images(items)
            post.assert_awaited_once()

    assert items[0].product.image_url is None
    assert items[0].bargain_price.price_now == "1.30"


async def test_image_lookup_cancellation_propagates() -> None:
    def handler(request):
        raise CancelledError

    async with image_client(handler) as client:
        with pytest.raises(CancelledError):
            await client.enrich_product_images([bargain(123)])


@pytest.mark.parametrize("error", [
    AHAPIError('Cannot query field "webshopId" on type "Product".', status_code=400),
    AHGraphQLError('FieldUndefined: Field "webshopId" is not defined'),
])
async def test_webshop_field_fallback_preserves_bargains_and_is_remembered(error) -> None:
    client = AHClient()
    with patch.object(client, "_graphql", new_callable=AsyncMock) as graphql:
        graphql.side_effect = [error, {"bargainItems": []}, {"bargainItems": []}]
        assert await client.get_bargains(2301) == []
        assert await client.get_bargains_raw(2302) == {"bargainItems": []}

    calls = graphql.await_args_list
    assert len(calls) == 3
    assert "webshopId" in calls[0].args[0]
    assert all("webshopId" not in call.args[0] for call in calls[1:])
    assert calls[0].args[1] == {"storeId": "2301"}
    assert calls[2].args[1] == {"storeId": "2302"}
    assert all(call.kwargs == {"require_auth": True} for call in calls)
    assert all("images" not in call.args[0] for call in calls)


@pytest.mark.parametrize("error", [
    AHAPIError('Cannot query field "webshopId" on type "Product".', status_code=503),
    AHAPIError("Permission denied for webshopId", status_code=400),
    AHGraphQLError('Cannot query field "unrelated" on type "Product".'),
    AHAuthenticationError("Not authenticated"),
])
async def test_unrelated_query_failures_are_rethrown_without_fallback(error) -> None:
    client = AHClient()
    with patch.object(client, "_graphql", new_callable=AsyncMock) as graphql:
        graphql.side_effect = error
        with pytest.raises(type(error)) as raised:
            await client.get_bargains(2301)
        assert raised.value is error
        graphql.assert_awaited_once()


def test_bargain_query_variants_never_request_unsupported_images_field() -> None:
    assert "images" not in _BARGAIN_ITEMS_QUERY
    assert "images" not in _BARGAIN_ITEMS_QUERY_WITHOUT_WEBSHOP_ID
