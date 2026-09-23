"""Albert Heijn mobile API client.

All endpoint details are derived from the appie-go open-source project
(https://github.com/gwillem/appie-go).  The AH mobile API is undocumented
and may change without notice.

Authentication
--------------
The client supports two authentication modes:

1. **Anonymous** — ``POST /mobile-auth/v1/auth/token/anonymous``.
   Sufficient for store search but NOT for bargain retrieval.

2. **Authenticated** — Browser-based OAuth login via ``login.ah.nl``.
   Required for ``bargainItems`` and most other personalised endpoints.
   The client loads tokens from a :class:`TokenStore`, and automatically
   refreshes expired access tokens using the refresh token.

GraphQL
-------
Both store search and bargain retrieval use a single ``POST /graphql``
endpoint with different query documents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from app.ah import (
    AHAPIError,
    AHAuthenticationError,
    AHGraphQLError,
    AHStoreNotFoundError,
)
from app.ah.models import (
    AHBargainItem,
    AHStore,
    BargainItemsResult,
    StoresSearchResult,
)
from app.ah.token_store import AuthTokens, TokenStore

logger = structlog.stdlib.get_logger()

# ---------------------------------------------------------------------------
# Constants — sourced from appie-go/client.go
# ---------------------------------------------------------------------------
_BASE_URL = "https://api.ah.nl"
_CLIENT_ID = "appie-ios"
_CLIENT_VERSION = "9.28"
_APPLICATION = "AHWEBSHOP"
_USER_AGENT = "Appie/9.28 (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"

# GraphQL query documents — sourced from appie-go/koopjes.go
_STORES_SEARCH_QUERY = """\
query StoresSearch($filter: StoresFilterInput) {
  storesSearch(filter: $filter, limit: 5) {
    result {
      id
      name
      storeType
      address { street houseNumber houseNumberExtra postalCode city }
    }
  }
}"""

_BARGAIN_ITEMS_QUERY = """\
query BargainItems($storeId: String!) {
  bargainItems(storeId: $storeId) {
    product {
      id
      title
      brand
      salesUnitSize
    }
    categoryTitle
    markdown {
      markdownType
      markdownExpirationDate
      markdownPercentage
    }
    stock
    bargainPrice {
      priceWas
      priceNow
    }
  }
}"""

# Default timeout for HTTP requests (seconds)
_DEFAULT_TIMEOUT = 15.0

# Maximum retries for transient errors
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0  # seconds, doubled on each retry


class AHClient:
    """Async client for the Albert Heijn mobile API.

    Usage with authenticated access::

        token_store = TokenStore()
        async with AHClient(token_store=token_store) as client:
            stores = await client.search_stores("1091")
            bargains = await client.get_bargains(stores[0].id)

    Usage with anonymous access (stores only)::

        async with AHClient() as client:
            stores = await client.search_stores("1091")
    """

    def __init__(
        self,
        *,
        base_url: str = _BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        token_store: TokenStore | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._token_store = token_store
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._http: httpx.AsyncClient | None = None

    # -- Context manager --------------------------------------------------

    async def __aenter__(self) -> "AHClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        # Load tokens from store if available
        if self._token_store:
            tokens = self._token_store.load()
            if tokens:
                self._access_token = tokens.access_token
                self._refresh_token = tokens.refresh_token
                if tokens.is_expired:
                    logger.info("ah_token_expired_on_load")
                    await self._refresh_access_token()
                else:
                    logger.info("ah_token_loaded_from_store")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # -- Internal helpers -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Build the common request headers."""
        headers = {
            "User-Agent": _USER_AGENT,
            "x-client-name": _CLIENT_ID,
            "x-client-version": _CLIENT_VERSION,
            "x-application": _APPLICATION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "AHClient must be used as an async context manager: "
                "`async with AHClient() as client:`"
            )
        return self._http

    @property
    def is_authenticated(self) -> bool:
        """Return ``True`` if we have an access token (anonymous or login-based)."""
        return self._access_token is not None

    async def _ensure_authenticated(self) -> None:
        """Ensure we have a valid authenticated token.

        Raises ``AHAuthenticationError`` if no tokens are available.
        This is for endpoints that require login-based auth (e.g. bargainItems).
        """
        if self._access_token is not None:
            return

        raise AHAuthenticationError(
            "Not authenticated. Run: python scripts/ah_login.py"
        )

    async def _ensure_any_token(self) -> None:
        """Ensure we have at least an anonymous token.

        Used for endpoints that work with anonymous access (e.g. storesSearch).
        """
        if self._access_token is not None:
            return
        await self._get_anonymous_token()

    async def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        include_auth: bool = True,
    ) -> dict[str, Any]:
        """POST JSON to the AH API with retry/backoff for transient errors."""
        url = f"{self._base_url}{path}"
        headers = self._headers()
        if not include_auth:
            headers.pop("Authorization", None)

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = await self._client.post(
                    url, json=body, headers=headers,
                )

                if response.status_code >= 500:
                    # Transient server error — retry
                    last_error = AHAPIError(
                        f"Server error {response.status_code}: {response.text[:200]}",
                        status_code=response.status_code,
                    )
                    wait = _RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "ah_api_transient_error",
                        status_code=response.status_code,
                        attempt=attempt + 1,
                        retry_in=wait,
                    )
                    await _async_sleep(wait)
                    continue

                if response.status_code == 401:
                    raise AHAuthenticationError(
                        f"Authentication failed ({response.status_code}): "
                        f"{response.text[:200]}"
                    )

                if response.status_code >= 400:
                    raise AHAPIError(
                        f"API error {response.status_code}: {response.text[:500]}",
                        status_code=response.status_code,
                    )

                data: dict[str, Any] = response.json()
                return data

            except httpx.TimeoutException as exc:
                last_error = exc
                wait = _RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "ah_api_timeout", attempt=attempt + 1, retry_in=wait,
                )
                await _async_sleep(wait)
            except httpx.HTTPError as exc:
                last_error = exc
                wait = _RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "ah_api_network_error",
                    error=str(exc),
                    attempt=attempt + 1,
                    retry_in=wait,
                )
                await _async_sleep(wait)

        raise AHAPIError(
            f"Request to {path} failed after {_MAX_RETRIES} attempts: {last_error}"
        )

    async def _graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        require_auth: bool = False,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the AH API."""
        if require_auth:
            await self._ensure_authenticated()
        else:
            await self._ensure_any_token()

        body = {"query": query, "variables": variables}
        data = await self._post_json("/graphql", body)

        # Check for GraphQL-level errors
        errors = data.get("errors", [])
        if errors:
            msg = errors[0].get("message", "Unknown GraphQL error")
            raise AHGraphQLError(msg)

        return data.get("data", {})

    # -- Authentication ---------------------------------------------------

    async def _get_anonymous_token(self) -> None:
        """Obtain an anonymous access token.

        No user credentials are required.  The token allows browsing
        stores but NOT bargain retrieval.
        """
        body = {"clientId": _CLIENT_ID}

        logger.info("ah_auth_anonymous_start")
        data = await self._post_json(
            "/mobile-auth/v1/auth/token/anonymous",
            body,
            include_auth=False,
        )

        token = data.get("access_token")
        if not token:
            raise AHAuthenticationError(
                "Anonymous token response did not contain access_token"
            )

        self._access_token = token
        logger.info("ah_auth_anonymous_success")

    async def exchange_code(self, code: str) -> AuthTokens:
        """Exchange an authorization code for access and refresh tokens.

        This is step 2 of the OAuth flow: after the user logs in and we
        capture the code, we exchange it here.

        Args:
            code: The authorization code from the login callback.

        Returns:
            The :class:`AuthTokens` containing access and refresh tokens.
        """
        body = {
            "clientId": _CLIENT_ID,
            "code": code,
        }

        logger.info("ah_auth_exchange_start")
        data = await self._post_json(
            "/mobile-auth/v1/auth/token",
            body,
            include_auth=False,
        )

        tokens = AuthTokens.from_api_response(data)
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token

        # Save to store if available
        if self._token_store:
            self._token_store.save(tokens)

        logger.info("ah_auth_exchange_success")
        return tokens

    async def _refresh_access_token(self) -> None:
        """Refresh the access token using the refresh token.

        Called automatically when the access token has expired.
        """
        if not self._refresh_token:
            raise AHAuthenticationError(
                "No refresh token available. Run: python scripts/ah_login.py"
            )

        body = {
            "clientId": _CLIENT_ID,
            "refreshToken": self._refresh_token,
        }

        logger.info("ah_auth_refresh_start")
        try:
            data = await self._post_json(
                "/mobile-auth/v1/auth/token/refresh",
                body,
                include_auth=False,
            )
        except AHAuthenticationError:
            # Refresh token is also expired/invalid
            logger.warning("ah_auth_refresh_failed")
            self._access_token = None
            self._refresh_token = None
            if self._token_store:
                self._token_store.clear()
            raise AHAuthenticationError(
                "Refresh token expired. Run: python scripts/ah_login.py"
            )

        tokens = AuthTokens.from_api_response(data)
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token

        # Save refreshed tokens
        if self._token_store:
            self._token_store.save(tokens)

        logger.info("ah_auth_refresh_success")

    # -- Public API -------------------------------------------------------

    async def search_stores(self, postal_code: str) -> list[AHStore]:
        """Search for AH stores near a postal code.

        Returns up to 5 stores matching the postal code.
        Works with both anonymous and authenticated tokens.

        Raises:
            AHStoreNotFoundError: No stores found for the given postal code.
        """
        variables: dict[str, Any] = {
            "filter": {"postalCode": postal_code},
        }

        raw = await self._graphql(
            _STORES_SEARCH_QUERY, variables, require_auth=False,
        )

        result = StoresSearchResult.model_validate(raw)
        stores = result.stores_search.result

        if not stores:
            raise AHStoreNotFoundError(
                f"No Albert Heijn stores found near postal code '{postal_code}'"
            )

        logger.info(
            "ah_stores_found",
            postal_code=postal_code,
            count=len(stores),
        )

        return stores

    async def get_bargains(self, store_id: int) -> list[AHBargainItem]:
        """Retrieve Laatste Kans Koopjes for a specific store.

        **Requires authenticated access** (login-based token).

        Args:
            store_id: The numeric AH store identifier.

        Returns:
            List of bargain items.  May be empty if the store currently
            has no discounted items (e.g. outside opening hours or all
            items sold out).
        """
        variables: dict[str, Any] = {"storeId": str(store_id)}

        raw = await self._graphql(
            _BARGAIN_ITEMS_QUERY, variables, require_auth=True,
        )

        result = BargainItemsResult.model_validate(raw)
        bargains = result.bargain_items

        logger.info(
            "ah_bargains_retrieved",
            store_id=store_id,
            count=len(bargains),
        )

        return bargains

    async def get_bargains_raw(self, store_id: int) -> dict[str, Any]:
        """Retrieve raw JSON for Laatste Kans Koopjes (for debugging).

        **Requires authenticated access** (login-based token).

        Same request as :meth:`get_bargains` but returns the unprocessed
        GraphQL ``data`` dict.
        """
        variables: dict[str, Any] = {"storeId": str(store_id)}
        return await self._graphql(
            _BARGAIN_ITEMS_QUERY, variables, require_auth=True,
        )


async def _async_sleep(seconds: float) -> None:
    """Async sleep wrapper (allows easier testing)."""
    import asyncio
    await asyncio.sleep(seconds)
