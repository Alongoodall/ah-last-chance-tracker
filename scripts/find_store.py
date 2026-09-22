#!/usr/bin/env python3
"""Find Albert Heijn stores near a postal code.

Usage:
    python scripts/find_store.py <postal_code>

Example:
    python scripts/find_store.py 1091
    python scripts/find_store.py 3521GZ
"""

import asyncio
import sys

# Ensure backend/ is on the path
sys.path.insert(0, "backend")

from app.ah.client import AHClient  # noqa: E402
from app.ah import AHStoreNotFoundError  # noqa: E402


async def main(postal_code: str) -> None:
    print(f"\nSearching for Albert Heijn stores near '{postal_code}'...\n")

    async with AHClient() as client:
        try:
            stores = await client.search_stores(postal_code)
        except AHStoreNotFoundError:
            print(f"No stores found near '{postal_code}'.")
            sys.exit(1)

        for store in stores:
            addr = store.address
            addr_line = f"{addr.street} {addr.house_number}"
            if addr.house_number_extra:
                addr_line += f" {addr.house_number_extra}"
            addr_line += f", {addr.postal_code} {addr.city}"

            print(f"  Store ID: {store.id}")
            print(f"  Name:     {store.name}")
            print(f"  Type:     {store.store_type}")
            print(f"  Address:  {addr_line}")
            print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/find_store.py <postal_code>")
        print("Example: python scripts/find_store.py 1091")
        sys.exit(1)

    asyncio.run(main(sys.argv[1]))
