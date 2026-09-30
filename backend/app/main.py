"""FastAPI application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from sqlalchemy import text

from app.config import Settings, settings as default_settings
from app.db.session import async_session, engine
from app.db.models import Base
from app.logging import setup_logging

logger = structlog.stdlib.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle hook.

    On startup: ensure all tables exist (idempotent — safe to run even if
    Alembic migrations have already been applied).
    On shutdown: dispose the connection pool gracefully.
    """
    logger.info("application_startup", app_name=app.title)

    # Ensure tables exist (Alembic handles versioning; this is a safety net
    # for development and for first-run without running `alembic upgrade head`).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_ready")

    yield

    await engine.dispose()
    logger.info("application_shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the configured FastAPI application.

    Using a factory function (rather than a module-level ``app`` object) makes
    testing easier: each test can get a fresh app with overridden settings.
    """
    if settings is None:
        settings = default_settings

    setup_logging(debug=settings.debug)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store settings on app state so routers can access them.
    app.state.settings = settings

    # --- Health endpoint -------------------------------------------------------

    @app.get("/api/health", tags=["system"])
    async def health() -> dict:
        """Liveness + database readiness check."""
        db_status = "ok"
        db_error: str | None = None

        try:
            async with async_session() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            db_status = "error"
            db_error = str(exc)
            logger.error("health_db_check_failed", error=db_error)

        payload: dict = {
            "status": "ok" if db_status == "ok" else "degraded",
            "database": db_status,
        }
        if db_error:
            payload["database_error"] = db_error

        return payload

    return app


# Module-level app object so `uvicorn app.main:app --app-dir backend` works.
# Tests use create_app() directly with custom settings.
app = create_app()
