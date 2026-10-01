"""Collector service — fetches bargains from AH and persists them.

This module contains a single class, ``CollectorService``, which:

1. Opens an ``AHClient`` with the saved user tokens.
2. For each configured store, fetches the current ``bargainItems``.
3. Persists the results via ``BargainRepository.save_snapshot()``.
4. Handles errors per-store (one failing store doesn't abort the others).

It is intentionally stateless: the scheduler can call ``collect_all()``
every N minutes and each call is independent.

Usage from the scheduler::

    service = CollectorService(
        store_ids=[2203, 1812],
        token_store=TokenStore(),
        session_factory=async_session,
    )
    results = await service.collect_all()

Usage from a one-shot script::

    result = await service.collect_once(store_id=2203)
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ah.client import AHClient
from app.ah import AHAuthenticationError, AHError
from app.ah.token_store import TokenStore
from app.db.models import Snapshot
from app.db.repository import BargainRepository

logger = structlog.get_logger(__name__)


class CollectionResult:
    """Outcome of one store collection run."""

    def __init__(
        self,
        store_id: int,
        snapshot: Snapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.store_id = store_id
        self.snapshot = snapshot
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:
        if self.ok and self.snapshot:
            return (
                f"<CollectionResult store={self.store_id} "
                f"snapshot={self.snapshot.id} items={self.snapshot.item_count}>"
            )
        return f"<CollectionResult store={self.store_id} error={self.error!r}>"


class CollectorService:
    """Fetches AH bargains for a set of stores and stores them in the DB."""

    def __init__(
        self,
        store_ids: list[int],
        token_store: TokenStore,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        if not store_ids:
            logger.warning("collector_no_stores_configured")
        self._store_ids = store_ids
        self._token_store = token_store
        self._session_factory = session_factory

    @property
    def store_ids(self) -> list[int]:
        return list(self._store_ids)

    async def collect_all(self) -> list[CollectionResult]:
        """Run one collection cycle for all configured stores.

        Returns a result per store — errors are captured in ``CollectionResult``
        rather than propagated, so all stores are attempted even if one fails.
        """
        if not self._store_ids:
            logger.warning("collector_skipped_no_stores")
            return []

        logger.info("collector_cycle_start", store_count=len(self._store_ids))
        results: list[CollectionResult] = []

        async with AHClient(token_store=self._token_store) as client:
            for store_id in self._store_ids:
                result = await self._collect_one(client, store_id)
                results.append(result)

        ok = sum(1 for r in results if r.ok)
        logger.info(
            "collector_cycle_done",
            total=len(results),
            succeeded=ok,
            failed=len(results) - ok,
        )
        return results

    async def collect_once(self, store_id: int) -> CollectionResult:
        """Run a single collection for one store (useful for manual / CLI runs)."""
        async with AHClient(token_store=self._token_store) as client:
            return await self._collect_one(client, store_id)

    # -- Private helpers -----------------------------------------------------

    async def _collect_one(
        self, client: AHClient, store_id: int
    ) -> CollectionResult:
        """Fetch bargains for one store and persist them.

        Never raises — errors are wrapped in ``CollectionResult``.
        """
        log = logger.bind(store_id=store_id)
        log.info("collector_store_start")

        try:
            bargains = await client.get_bargains(store_id)
            log.info("collector_fetched", item_count=len(bargains))

            async with self._session_factory() as session:
                repo = BargainRepository(session)
                snapshot = await repo.save_snapshot(
                    store_id=store_id,
                    ah_store=None,   # Store metadata not needed per-run
                    bargain_items=bargains,
                )
                await session.commit()

            log.info(
                "collector_store_done",
                snapshot_id=snapshot.id,
                item_count=snapshot.item_count,
            )
            return CollectionResult(store_id=store_id, snapshot=snapshot)

        except AHAuthenticationError as exc:
            log.error("collector_auth_error", error=str(exc))
            return CollectionResult(store_id=store_id, error=exc)

        except AHError as exc:
            log.error("collector_api_error", error=str(exc))
            return CollectionResult(store_id=store_id, error=exc)

        except Exception as exc:
            log.exception("collector_unexpected_error", error=str(exc))
            return CollectionResult(store_id=store_id, error=exc)
