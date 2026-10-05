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
from app.event_bus import EventBus
from app.notifier import (
    AlertDetector,
    NotifierService,
    fetch_snapshot_items,
)

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
        *,
        postal_codes: dict[int, str] | None = None,
        name_overrides: dict[int, str] | None = None,
        notifier: NotifierService | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        if not store_ids:
            logger.warning("collector_no_stores_configured")
        self._store_ids = store_ids
        self._token_store = token_store
        self._session_factory = session_factory
        self._postal_codes: dict[int, str] = postal_codes or {}
        self._name_overrides: dict[int, str] = name_overrides or {}
        self._notifier = notifier
        self._event_bus = event_bus
        self._detector = AlertDetector()

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
            # Fetch bargains (requires authenticated token)
            bargains = await client.get_bargains(store_id)
            log.info("collector_fetched", item_count=len(bargains))

            # --- Store metadata ---
            # Only fetch from API when the store row has no name yet.
            # We use search_stores (postal-code based) if a postal code is configured.
            async with self._session_factory() as session:
                from sqlalchemy import select
                from app.db.models import Store as StoreModel
                result = await session.execute(
                    select(StoreModel).where(StoreModel.id == store_id)
                )
                existing_store = result.scalar_one_or_none()
                needs_metadata = existing_store is None or not existing_store.name

            ah_store = None
            if needs_metadata and store_id in self._postal_codes:
                postal_code = self._postal_codes[store_id]
                try:
                    stores = await client.search_stores(postal_code)
                    # Pick the matching store ID from results
                    ah_store = next((s for s in stores if s.id == store_id), None)
                    if ah_store:
                        log.info("collector_store_metadata_fetched", name=ah_store.name)
                    else:
                        log.warning(
                            "collector_store_not_in_search_results",
                            postal_code=postal_code,
                        )
                except Exception as exc:
                    log.warning("collector_store_search_failed", error=str(exc))

            # Apply name override (from AH_STORE_NAMES) regardless of API result.
            # If we got an AHStore from the API we patch its name in-place;
            # if not, we create a minimal store object just to carry the override name.
            if store_id in self._name_overrides:
                override_name = self._name_overrides[store_id]
                if ah_store is not None:
                    # Patch name on existing AHStore
                    object.__setattr__(ah_store, 'name', override_name) if hasattr(ah_store, '__setattr__') else None
                    try:
                        ah_store.name = override_name
                    except Exception:
                        pass
                elif needs_metadata:
                    # Build a minimal AHStore just to carry the display name
                    from app.ah.models import AHStore, AHStoreAddress
                    ah_store = AHStore(
                        id=store_id,
                        name=override_name,
                        storeType="AH",
                        address=AHStoreAddress(
                            street="",
                            houseNumber="",
                            postalCode="",
                            city="",
                        ),
                    )
                log.info("collector_store_name_override_applied", name=override_name)

            async with self._session_factory() as session:
                repo = BargainRepository(session)
                snapshot = await repo.save_snapshot(
                    store_id=store_id,
                    ah_store=ah_store,
                    bargain_items=bargains,
                )
                await session.commit()

            log.info(
                "collector_store_done",
                snapshot_id=snapshot.id,
                item_count=snapshot.item_count,
            )

            # Publish SSE event so connected browsers refresh immediately
            if self._event_bus is not None:
                await self._event_bus.publish(
                    "snapshot_ready",
                    {
                        "store_id":    store_id,
                        "snapshot_id": snapshot.id,
                        "item_count":  snapshot.item_count,
                        "fetched_at":  snapshot.fetched_at.isoformat(),
                    },
                )

            # Fire alerts (best-effort — errors are swallowed with a warning)
            await self._notify_if_needed(store_id, snapshot)

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

    async def _notify_if_needed(
        self, store_id: int, snapshot: Snapshot
    ) -> None:
        """Detect alert events and dispatch them.

        Fetches the *previous* snapshot for this store (if any) from the DB,
        runs the detector, and dispatches via the configured notifier.
        Errors are logged as warnings so they never interrupt collection.
        """
        if self._notifier is None or not self._notifier.has_channels:
            return

        try:
            from sqlalchemy import select
            from app.db.models import Snapshot as SnapModel

            async with self._session_factory() as session:
                # Find the snapshot immediately before this one for the same store
                prev_result = await session.execute(
                    select(SnapModel)
                    .where(
                        SnapModel.store_id == store_id,
                        SnapModel.id < snapshot.id,
                    )
                    .order_by(SnapModel.id.desc())
                    .limit(1)
                )
                prev_snap = prev_result.scalar_one_or_none()

                curr_items = await fetch_snapshot_items(session, snapshot.id)
                prev_items = (
                    await fetch_snapshot_items(session, prev_snap.id)
                    if prev_snap
                    else []
                )

            events = self._detector.compare(prev_items, curr_items, store_id)
            if events:
                logger.info(
                    "collector_alerts_detected",
                    store_id=store_id,
                    count=len(events),
                )
            await self._notifier.dispatch_all(events)

        except Exception as exc:
            logger.warning(
                "collector_notify_failed",
                store_id=store_id,
                error=str(exc),
            )
