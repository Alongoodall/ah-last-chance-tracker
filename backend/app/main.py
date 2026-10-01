"""FastAPI application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy import text

from app.ah.token_store import TokenStore
from app.collector import CollectorService
from app.config import Settings, settings as default_settings
from app.db.models import Base
from app.db.session import async_session, engine
from app.logging import setup_logging

logger = structlog.stdlib.get_logger()


def _make_collector(settings: Settings) -> CollectorService:
    """Build a CollectorService from the current settings."""
    return CollectorService(
        store_ids=settings.store_ids,
        token_store=TokenStore(),
        session_factory=async_session,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle hook."""
    logger.info("application_startup", app_name=app.title)

    # 1. Ensure DB tables exist (idempotent alongside Alembic)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_ready")

    # 2. Start scheduler (only when stores are configured)
    settings: Settings = app.state.settings
    scheduler: AsyncIOScheduler | None = None

    if settings.store_ids:
        collector = _make_collector(settings)
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            collector.collect_all,
            trigger="interval",
            minutes=settings.collection_interval_minutes,
            id="collect_bargains",
            name="Collect AH bargains",
            max_instances=1,           # Never overlap runs
            misfire_grace_time=60,     # Allow up to 60 s late start
        )
        scheduler.start()
        logger.info(
            "scheduler_started",
            store_ids=settings.store_ids,
            interval_minutes=settings.collection_interval_minutes,
        )
        app.state.scheduler = scheduler
        app.state.collector = collector
    else:
        logger.warning(
            "scheduler_not_started",
            reason="AH_STORE_IDS not configured",
        )
        app.state.scheduler = None
        app.state.collector = None

    yield

    # 3. Graceful shutdown
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")

    await engine.dispose()
    logger.info("application_shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    if settings is None:
        settings = default_settings

    setup_logging(debug=settings.debug)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.settings = settings

    # --- Health endpoint ---------------------------------------------------

    @app.get("/api/health", tags=["system"])
    async def health() -> dict:
        """Liveness + DB + scheduler readiness check."""
        db_status = "ok"
        db_error: str | None = None

        try:
            async with async_session() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            db_status = "error"
            db_error = str(exc)
            logger.error("health_db_check_failed", error=db_error)

        scheduler: AsyncIOScheduler | None = getattr(app.state, "scheduler", None)
        scheduler_status = "running" if (scheduler and scheduler.running) else "stopped"

        configured_stores: list[int] = settings.store_ids

        payload: dict = {
            "status": "ok" if db_status == "ok" else "degraded",
            "database": db_status,
            "scheduler": scheduler_status,
            "configured_stores": configured_stores,
        }
        if db_error:
            payload["database_error"] = db_error

        return payload

    return app


# Module-level app object so `uvicorn app.main:app --app-dir backend` works.
app = create_app()
