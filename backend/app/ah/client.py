"""Albert Heijn mobile API client.

All endpoint details are derived from the appie-go open-source project
(https://github.com/gwillem/appie-go).  The AH mobile API is undocumented
and may change without notice.

Authentication flow
-------------------
1. POST /mobile-auth/v1/auth/token/anonymous  →  anonymous access_token
2. Use ``Authorization: Bearer <token>`` on all subsequent requests.

No user credentials are required for browsing products or bargains.

GraphQL
-------
Both store search and bargain retrieval use a single ``POST /graphql``
endpoint with different query documents.
"""

from __future__ import annotations

import time
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

    Usage::

        async with AHClient() as client:
            stores = await client.search_stores("1091")
            bargains = await client.get_bargains(stores[0].id)
    """

    def __init__(
        self,
        *,
        base_url: str = _BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._access_token: str | None = None
        self._http: httpx.AsyncClient | None = None

    # -- Context manager --------------------------------------------------

    async def __aenter__(self) -> "AHClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
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

    async def _ensure_token(self) -> None:
        """Obtain an anonymous token if we don't have one yet."""
        if self._access_token is not None:
            return
        await self.authenticate()

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
        self, query: str, variables: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the AH API."""
        await self._ensure_token()

        body = {"query": query, "variables": variables}
        data = await self._post_json("/graphql", body)

        # Check for GraphQL-level errors
        errors = data.get("errors", [])
        if errors:
            msg = errors[0].get("message", "Unknown GraphQL error")
            raise AHGraphQLError(msg)

        return data.get("data", {})

    # -- Public API -------------------------------------------------------

    async def authenticate(self) -> None:
        """Obtain an anonymous access token.

        No user credentials are required.  The token allows browsing
        products, stores, and bargains.
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

    async def search_stores(self, postal_code: str) -> list[AHStore]:
        """Search for AH stores near a postal code.

        Returns up to 5 stores matching the postal code.

        Raises:
            AHStoreNotFoundError: No stores found for the given postal code.
        """
        variables: dict[str, Any] = {
            "filter": {"postalCode": postal_code},
        }

        raw = await self._graphql(_STORES_SEARCH_QUERY, variables)

        logger.info(
            "ah_stores_search",
            postal_code=postal_code,
            raw_keys=list(raw.keys()),
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

        Args:
            store_id: The numeric AH store identifier.

        Returns:
            List of bargain items.  May be empty if the store currently
            has no discounted items (e.g. outside opening hours or all
            items sold out).
        """
        variables: dict[str, Any] = {"storeId": str(store_id)}

        raw = await self._graphql(_BARGAIN_ITEMS_QUERY, variables)

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

        Same request as :meth:`get_bargains` but returns the unprocessed
        GraphQL ``data`` dict.
        """
        variables: dict[str, Any] = {"storeId": str(store_id)}
        return await self._graphql(_BARGAIN_ITEMS_QUERY, variables)


async def _async_sleep(seconds: float) -> None:
    """Async sleep wrapper (allows easier testing)."""
    import asyncio
    await asyncio.sleep(seconds)
