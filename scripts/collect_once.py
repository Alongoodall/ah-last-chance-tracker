"""One-shot collection script.

Fetches current Laatste Kans Koopjes for one or more stores and persists them
to the database. Useful for:
- Manually triggering a collection run
- Verifying end-to-end flow (API → DB)
- Initial data seeding

Usage:
    # Collect for a single store
    python scripts/collect_once.py --store-id 2203

    # Collect for multiple stores
    python scripts/collect_once.py --store-id 2203 --store-id 1812

    # Collect for all stores configured in AH_STORE_IDS
    python scripts/collect_once.py

    # Verbose: print each item fetched
    python scripts/collect_once.py --store-id 2203 --verbose
"""

import argparse
import asyncio
import sys
from pathlib import Path

# ── allow `python scripts/collect_once.py` from the repo root ───────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import _cli_common  # noqa: F401 — suppress structlog noise

from app.ah.token_store import TokenStore
from app.collector import CollectorService
from app.config import settings
from app.db.models import Base
from app.db.session import async_session, engine


async def main(store_ids: list[int], verbose: bool) -> int:
    """Run one collection cycle and print results. Returns exit code."""

    # Initialise DB schema if needed (idempotent)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if not store_ids:
        store_ids = settings.store_ids

    if not store_ids:
        print(
            "✗ No stores configured.\n"
            "  Pass --store-id <ID> or set AH_STORE_IDS in .env",
            file=sys.stderr,
        )
        return 1

    token_store = TokenStore()
    if token_store.load() is None:
        print(
            "✗ Not authenticated. Run:\n"
            "  python scripts/ah_login.py",
            file=sys.stderr,
        )
        return 1

    service = CollectorService(
        store_ids=store_ids,
        token_store=token_store,
        session_factory=async_session,
        postal_codes=settings.store_postal_codes,
        name_overrides=settings.store_names,
    )

    print(f"\nCollecting bargains for {len(store_ids)} store(s): {store_ids}")
    print("=" * 60)

    results = await service.collect_all()

    exit_code = 0
    for result in results:
        if result.ok and result.snapshot:
            snap = result.snapshot
            print(
                f"\n✓ Store {result.store_id}  "
                f"snapshot #{snap.id}  "
                f"→ {snap.item_count} item(s) saved"
            )
            if verbose:
                # Re-read items from DB and print them
                from app.db.repository import BargainRepository
                async with async_session() as session:
                    repo = BargainRepository(session)
                    items = await repo.get_bargain_items_for_snapshot(snap.id)
                for item in items:
                    discount = (
                        f"{item.markdown_percentage:.0f}%" 
                        if item.markdown_percentage else "?"
                    )
                    now = (
                        f"€{item.price_now_cents / 100:.2f}" 
                        if item.price_now_cents is not None else "?"
                    )
                    print(
                        f"   product={item.product_id}  "
                        f"now={now}  disc={discount}  stock={item.stock}"
                    )
        else:
            print(f"\n✗ Store {result.store_id}  error: {result.error}")
            exit_code = 1

    print("\n" + "=" * 60)
    ok = sum(1 for r in results if r.ok)
    print(f"Done: {ok}/{len(results)} store(s) succeeded.")
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Collect AH Laatste Kans Koopjes and save to DB"
    )
    parser.add_argument(
        "--store-id",
        dest="store_ids",
        type=int,
        action="append",
        metavar="ID",
        help="Store ID to collect (repeat for multiple). Defaults to AH_STORE_IDS.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each fetched bargain item",
    )
    args = parser.parse_args()

    sys.exit(asyncio.run(main(args.store_ids or [], args.verbose)))
