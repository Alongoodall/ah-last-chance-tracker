"""FastAPI application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.config import Settings
from app.logging import setup_logging

logger = structlog.stdlib.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle hook."""
    logger.info("application_startup", app_name=app.title)
    yield
    logger.info("application_shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the configured FastAPI application.

    Using a factory function (rather than a module-level ``app`` object) makes
    testing easier: each test can get a fresh app with overridden settings.
    """
    if settings is None:
        settings = Settings()

    setup_logging(debug=settings.debug)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store settings on app state so routers can access them.
    app.state.settings = settings

    # --- Health / root endpoint ---
    @app.get("/api/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Simple liveness check."""
        return {"status": "ok"}

    return app


# Module-level app object so `uvicorn app.main:app --app-dir backend` works.
# Tests use create_app() directly with custom settings.
app = create_app()
