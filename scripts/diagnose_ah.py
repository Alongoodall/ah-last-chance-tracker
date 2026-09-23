#!/usr/bin/env python3
"""Diagnostic script: replicate appie-go's exact bargainItems request.

This sends the EXACT same HTTP request as appie-go's client and logs
every detail so we can compare our Python implementation.
"""

import asyncio
import json
import sys

import httpx

BASE_URL = "https://api.ah.nl"
CLIENT_ID = "appie-ios"
CLIENT_VERSION = "9.28"
APPLICATION = "AHWEBSHOP"
USER_AGENT = "Appie/9.28 (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"

# Exact GraphQL query from appie-go/koopjes.go (identical whitespace)
BARGAIN_ITEMS_QUERY = """query BargainItems($storeId: String!) {
\tbargainItems(storeId: $storeId) {
\t\tproduct {
\t\t\tid
\t\t\ttitle
\t\t\tbrand
\t\t\tsalesUnitSize
\t\t}
\t\tcategoryTitle
\t\tmarkdown {
\t\t\tmarkdownType
\t\t\tmarkdownExpirationDate
\t\t\tmarkdownPercentage
\t\t}
\t\tstock
\t\tbargainPrice {
\t\t\tpriceWas
\t\t\tpriceNow
\t\t}
\t}
}"""

STORES_SEARCH_QUERY = """query StoresSearch($filter: StoresFilterInput) {
\tstoresSearch(filter: $filter, limit: 5) {
\t\tresult {
\t\t\tid
\t\t\tname
\t\t\tstoreType
\t\t\taddress { street houseNumber houseNumberExtra postalCode city }
\t\t}
\t}
}"""


async def get_anonymous_token(client: httpx.AsyncClient) -> str:
    """Get an anonymous token — exactly as appie-go does it."""
    headers = {
        "User-Agent": USER_AGENT,
        "x-client-name": CLIENT_ID,
        "x-client-version": CLIENT_VERSION,
        "x-application": APPLICATION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = {"clientId": CLIENT_ID}

    print("=== Step 1: Get anonymous token ===")
    print(f"POST {BASE_URL}/mobile-auth/v1/auth/token/anonymous")
    print(f"Headers: {json.dumps(headers, indent=2)}")
    print(f"Body: {json.dumps(body)}")

    resp = await client.post(
        f"{BASE_URL}/mobile-auth/v1/auth/token/anonymous",
        json=body,
        headers=headers,
    )

    print(f"Status: {resp.status_code}")
    data = resp.json()

    # Print token metadata but not the actual token
    token = data.get("access_token", "")
    print(f"Got token: {len(token)} chars, starts with '{token[:20]}...'")
    print(f"Refresh token present: {bool(data.get('refresh_token'))}")
    print(f"Full response keys: {list(data.keys())}")
    print()

    return token


async def search_stores(client: httpx.AsyncClient, token: str, postal_code: str) -> int:
    """Search stores and return the first store ID."""
    headers = {
        "User-Agent": USER_AGENT,
        "x-client-name": CLIENT_ID,
        "x-client-version": CLIENT_VERSION,
        "x-application": APPLICATION,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "query": STORES_SEARCH_QUERY,
        "variables": {"filter": {"postalCode": postal_code}},
    }

    print(f"=== Step 2: Search stores near '{postal_code}' ===")
    resp = await client.post(f"{BASE_URL}/graphql", json=body, headers=headers)

    print(f"Status: {resp.status_code}")
    data = resp.json()

    stores = data.get("data", {}).get("storesSearch", {}).get("result", [])
    errors = data.get("errors", [])

    if errors:
        print(f"GraphQL errors: {json.dumps(errors, indent=2)}")
    if stores:
        store = stores[0]
        print(f"First store: ID={store['id']}, Name={store['name']}")
        print()
        return store["id"]
    else:
        print("No stores found!")
        print()
        return 0


async def get_bargains(client: httpx.AsyncClient, token: str, store_id: int) -> None:
    """Fetch bargains — exact replica of appie-go's request."""
    headers = {
        "User-Agent": USER_AGENT,
        "x-client-name": CLIENT_ID,
        "x-client-version": CLIENT_VERSION,
        "x-application": APPLICATION,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "query": BARGAIN_ITEMS_QUERY,
        "variables": {"storeId": str(store_id)},
    }

    print(f"=== Step 3: Get bargains for store {store_id} ===")
    print(f"POST {BASE_URL}/graphql")
    print(f"Request body: {json.dumps(body, indent=2)}")

    resp = await client.post(f"{BASE_URL}/graphql", json=body, headers=headers)

    print(f"Status: {resp.status_code}")
    print(f"Response headers (relevant):")
    for key in ["content-type", "x-request-id", "x-correlation-id"]:
        if key in resp.headers:
            print(f"  {key}: {resp.headers[key]}")

    data = resp.json()
    print(f"Full response:\n{json.dumps(data, indent=2, ensure_ascii=False)}")
    print()

    return data


async def main() -> None:
    postal_code = sys.argv[1] if len(sys.argv) > 1 else "1091"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Step 1: Get anonymous token
        token = await get_anonymous_token(client)

        # Step 2: Search stores
        store_id = await search_stores(client, token, postal_code)
        if not store_id:
            print("Cannot continue without a store ID.")
            return

        # Step 3: Try bargains with anonymous token
        print("--- Attempt with ANONYMOUS token ---")
        data = await get_bargains(client, token, store_id)

        errors = data.get("errors", [])
        bargains = (data.get("data") or {}).get("bargainItems", [])

        if bargains:
            print(f"SUCCESS: Got {len(bargains)} bargains with anonymous token!")
            for b in bargains[:3]:
                p = b["product"]
                bp = b["bargainPrice"]
                print(f"  - {p['title']}: €{bp['priceWas']} → €{bp['priceNow']}")
        elif errors:
            print(f"FAILED with anonymous token: {errors[0]['message']}")
            if "extensions" in data:
                print(f"Extensions: {json.dumps(data['extensions'], indent=2)}")
            print()
            print("=== CONCLUSION ===")
            print("The bargainItems endpoint returns an error with anonymous tokens.")
            print("The appie-go CLI requires login (orderSetup checks IsAuthenticated).")
            print("This confirms: bargainItems requires AUTHENTICATED access.")


if __name__ == "__main__":
    asyncio.run(main())
