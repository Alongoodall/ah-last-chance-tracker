"""Tests for AHClient authentication and bargain parsing."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.ah import AHAuthenticationError
from app.ah.client import AHClient
from app.ah.models import AHBargainItem, BargainItemsResult
from app.ah.token_store import AuthTokens, TokenStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_TOKEN_RESPONSE = {
    "access_token": "test_access_token_abc",
    "refresh_token": "test_refresh_token_xyz",
    "member_id": "member_123",
    "expires_in": 7200,
}

MOCK_BARGAIN_RESPONSE = {
    "data": {
        "bargainItems": [
            {
                "product": {
                    "id": 12345,
                    "title": "AH Roodbaarsfilet",
                    "brand": "AH",
                    "salesUnitSize": "ca. 200 g",
                },
                "categoryTitle": "Vis",
                "markdown": {
                    "markdownType": "PERCENTAGE",
                    "markdownExpirationDate": "2026-09-24",
                    "markdownPercentage": 35.0,
                },
                "stock": 3,
                "bargainPrice": {
                    "priceWas": "5.99",
                    "priceNow": "3.89",
                },
            },
            {
                "product": {
                    "id": 67890,
                    "title": "AH Biologisch Vollemelk",
                    "brand": "AH Biologisch",
                    "salesUnitSize": "1 l",
                },
                "categoryTitle": "Zuivel",
                "markdown": {
                    "markdownType": "PERCENTAGE",
                    "markdownExpirationDate": "2026-09-24",
                    "markdownPercentage": 25.0,
                },
                "stock": 5,
                "bargainPrice": {
                    "priceWas": "1.45",
                    "priceNow": "1.09",
                },
            },
        ]
    }
}


# ---------------------------------------------------------------------------
# Client behaviour — no tokens
# ---------------------------------------------------------------------------


class TestAHClientNoAuth:
    """Test client behaviour when no tokens are available."""

    async def test_get_bargains_raises_without_auth(self) -> None:
        """Bargains require authentication — should raise early."""
        async with AHClient() as client:
            with pytest.raises(AHAuthenticationError, match="Not authenticated"):
                await client.get_bargains(1234)


# ---------------------------------------------------------------------------
# Client behaviour — with tokens
# ---------------------------------------------------------------------------


class TestAHClientWithAuth:
    """Test client with mocked HTTP responses."""

    def _make_store_with_tokens(self, tmp_path: Path) -> TokenStore:
        """Create a token store with valid tokens."""
        store = TokenStore(path=tmp_path / "tokens.json")
        tokens = AuthTokens(
            access_token="valid_access_token",
            refresh_token="valid_refresh_token",
            member_id="m_42",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        store.save(tokens)
        return store

    async def test_loads_tokens_on_enter(self, tmp_path: Path) -> None:
        store = self._make_store_with_tokens(tmp_path)

        async with AHClient(token_store=store) as client:
            assert client.is_authenticated
            assert client._access_token == "valid_access_token"

    async def test_exchange_code_saves_tokens(self, tmp_path: Path) -> None:
        store = TokenStore(path=tmp_path / "tokens.json")

        async with AHClient(token_store=store) as client:
            # Mock the HTTP call
            mock_response = httpx.Response(200, json=MOCK_TOKEN_RESPONSE)
            with patch.object(client._client, "post", return_value=mock_response):
                tokens = await client.exchange_code("test_auth_code")

        assert tokens.access_token == "test_access_token_abc"
        assert tokens.refresh_token == "test_refresh_token_xyz"

        # Verify tokens were saved
        loaded = store.load()
        assert loaded is not None
        assert loaded.access_token == "test_access_token_abc"

    async def test_refresh_saves_new_tokens(self, tmp_path: Path) -> None:
        """Test that _refresh_access_token saves new tokens to store."""
        store = TokenStore(path=tmp_path / "tokens.json")

        # Start with valid (non-expired) tokens so __aenter__ doesn't auto-refresh
        valid_tokens = AuthTokens(
            access_token="old_token",
            refresh_token="valid_refresh_token",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        store.save(valid_tokens)

        refresh_response = {
            "access_token": "fresh_new_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 7200,
        }
        mock_response = httpx.Response(200, json=refresh_response)

        async with AHClient(token_store=store) as client:
            # Now mock the HTTP call and trigger refresh manually
            with patch.object(client._client, "post", return_value=mock_response):
                await client._refresh_access_token()

            assert client._access_token == "fresh_new_token"

        # Verify new tokens were saved to the store
        loaded = store.load()
        assert loaded is not None
        assert loaded.access_token == "fresh_new_token"
        assert loaded.refresh_token == "new_refresh_token"


# ---------------------------------------------------------------------------
# Bargain parsing
# ---------------------------------------------------------------------------


class TestBargainParsing:
    """Test parsing of bargainItems GraphQL responses."""

    def test_parse_bargain_response(self) -> None:
        """Parse a realistic bargainItems response."""
        raw_data = MOCK_BARGAIN_RESPONSE["data"]
        result = BargainItemsResult.model_validate(raw_data)

        assert len(result.bargain_items) == 2

        b0 = result.bargain_items[0]
        assert b0.product.id == 12345
        assert b0.product.title == "AH Roodbaarsfilet"
        assert b0.product.brand == "AH"
        assert b0.product.sales_unit_size == "ca. 200 g"
        assert b0.category_title == "Vis"
        assert b0.markdown.markdown_type == "PERCENTAGE"
        assert b0.markdown.markdown_percentage == 35.0
        assert b0.markdown.markdown_expiration_date == "2026-09-24"
        assert b0.stock == 3
        assert b0.bargain_price.price_was == "5.99"
        assert b0.bargain_price.price_now == "3.89"

        b1 = result.bargain_items[1]
        assert b1.product.title == "AH Biologisch Vollemelk"
        assert b1.category_title == "Zuivel"
        assert b1.markdown.markdown_percentage == 25.0
        assert b1.stock == 5

    def test_parse_empty_bargain_response(self) -> None:
        """An empty bargainItems list is valid."""
        raw = {"bargainItems": []}
        result = BargainItemsResult.model_validate(raw)
        assert result.bargain_items == []

    def test_parse_bargain_with_nulls(self) -> None:
        """Handle missing/null optional fields gracefully."""
        raw = {
            "bargainItems": [
                {
                    "product": {
                        "id": 99,
                        "title": "Unknown Product",
                        "brand": None,
                        "salesUnitSize": None,
                    },
                    "categoryTitle": None,
                    "markdown": {
                        "markdownType": None,
                        "markdownExpirationDate": None,
                        "markdownPercentage": 0,
                    },
                    "stock": 0,
                    "bargainPrice": {
                        "priceWas": "1.00",
                        "priceNow": "0.50",
                    },
                }
            ]
        }
        result = BargainItemsResult.model_validate(raw)
        assert len(result.bargain_items) == 1
        b = result.bargain_items[0]
        assert b.product.title == "Unknown Product"
        assert b.bargain_price.price_now == "0.50"

    def test_parse_fixture_if_exists(self) -> None:
        """If a real fixture file exists, verify it parses correctly."""
        fixture_path = Path("tests/fixtures/ah_bargains_raw.json")
        if not fixture_path.exists():
            pytest.skip("No real fixture available yet — run test_ah.py --save-fixture")

        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
        result = BargainItemsResult.model_validate(raw)

        # Just verify it parses without error and has items
        assert isinstance(result.bargain_items, list)
        for item in result.bargain_items:
            assert item.product.id > 0
            assert item.product.title
            assert item.bargain_price.price_now


# ---------------------------------------------------------------------------
# Login code extraction
# ---------------------------------------------------------------------------


class TestCodeExtraction:
    """Test extracting auth codes from redirect URLs."""

    def test_extract_from_appie_url(self) -> None:
        from app.ah.login import extract_code_from_url

        url = "appie://login-exit?code=abc123xyz"
        assert extract_code_from_url(url) == "abc123xyz"

    def test_extract_from_callback_url(self) -> None:
        from app.ah.login import extract_code_from_url

        url = "http://127.0.0.1:54321/callback?code=the_code_here&other=param"
        assert extract_code_from_url(url) == "the_code_here"

    def test_extract_bare_code(self) -> None:
        from app.ah.login import extract_code_from_url

        code = "a_very_long_authorization_code_string_1234567890"
        assert extract_code_from_url(code) == code

    def test_extract_raises_on_empty_url(self) -> None:
        from app.ah.login import extract_code_from_url

        with pytest.raises(ValueError, match="Could not extract"):
            extract_code_from_url("appie://login-exit?nope=1")
