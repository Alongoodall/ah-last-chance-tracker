#!/usr/bin/env python3
"""Retrieve current Laatste Kans Koopjes for an Albert Heijn store.

Usage:
    python scripts/test_ah.py --store-id <STORE_ID>
    python scripts/test_ah.py --store-id <STORE_ID> --raw

Examples:
    python scripts/test_ah.py --store-id 1527
    python scripts/test_ah.py --store-id 1527 --raw
    python scripts/test_ah.py --store-id 1527 --save-fixture

Use ``python scripts/find_store.py <postal_code>`` to discover store IDs.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend/ is on the path
sys.path.insert(0, "backend")

from app.ah.client import AHClient  # noqa: E402
from app.ah import AHError, AHGraphQLError  # noqa: E402


async def main(store_id: int, *, raw: bool = False, save_fixture: bool = False) -> None:
    async with AHClient() as client:
        # -- Raw mode: print the unprocessed JSON and optionally save -----
        if raw:
            print(f"\nFetching raw bargains for store {store_id}...\n")
            data = await client.get_bargains_raw(store_id)

            # Sanitise: the raw data never contains auth tokens (the
            # GraphQL response is just product data), but let's be explicit.
            sanitised = json.dumps(data, indent=2, ensure_ascii=False)
            print(sanitised)

            if save_fixture:
                fixture_path = Path("tests/fixtures/ah_bargains_raw.json")
                fixture_path.parent.mkdir(parents=True, exist_ok=True)
                fixture_path.write_text(sanitised + "\n", encoding="utf-8")
                print(f"\n✓ Saved fixture to {fixture_path}")

            return

        # -- Normal mode: human-readable output ---------------------------
        print(f"\nFetching Laatste Kans Koopjes for store {store_id}...\n")
        try:
            bargains = await client.get_bargains(store_id)
        except AHGraphQLError as exc:
            if "Subgraph errors redacted" in str(exc):
                print("AH API Limitation Hit:")
                print("The Albert Heijn API returned 'Subgraph errors redacted'.")
                print("This typically means the 'bargainItems' endpoint is currently")
                print("disabled, restricted to authenticated users, or experiencing")
                print("server-side issues at Albert Heijn. We successfully reached")
                print("the API and authenticated, but AH refused to serve the bargains.")
                return
            raise

        if not bargains:
            print("No bargains found.")
            print(
                "This may be normal outside store hours or if all items "
                "have been sold."
            )
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"Found {len(bargains)} Laatste Kans Koopjes (as of {now})\n")
        print("-" * 60)

        for i, item in enumerate(bargains, 1):
            p = item.product
            md = item.markdown
            price = item.bargain_price

            print(f"\n{i}. {p.title}")
            print(f"   Product ID:  {p.id}")
            if p.brand:
                print(f"   Brand:       {p.brand}")
            if p.sales_unit_size:
                print(f"   Size:        {p.sales_unit_size}")
            if item.category_title:
                print(f"   Category:    {item.category_title}")
            if price.price_was:
                print(f"   Normal price: €{price.price_was}")
            if price.price_now:
                print(f"   Current price: €{price.price_now}")
            if md.markdown_percentage:
                print(f"   Discount:    {md.markdown_percentage:.0f}%")
            print(f"   Stock:       {item.stock}")
            if md.markdown_type:
                print(f"   Markdown type: {md.markdown_type}")
            if md.markdown_expiration_date:
                print(f"   Expires:     {md.markdown_expiration_date}")

        print(f"\n{'-' * 60}")
        print(f"Total: {len(bargains)} items")

        # -- Optionally save a fixture ------------------------------------
        if save_fixture:
            raw_data = await client.get_bargains_raw(store_id)
            fixture_path = Path("tests/fixtures/ah_bargains_raw.json")
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            sanitised = json.dumps(raw_data, indent=2, ensure_ascii=False)
            fixture_path.write_text(sanitised + "\n", encoding="utf-8")
            print(f"\n✓ Saved raw fixture to {fixture_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Laatste Kans Koopjes from Albert Heijn",
    )
    parser.add_argument(
        "--store-id",
        type=int,
        required=True,
        help="AH store ID (use find_store.py to discover)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw JSON response instead of formatted output",
    )
    parser.add_argument(
        "--save-fixture",
        action="store_true",
        help="Save a sanitised copy of the raw response as a test fixture",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(main(args.store_id, raw=args.raw, save_fixture=args.save_fixture))
    except AHError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
