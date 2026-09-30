"""Alembic environment — async SQLite edition.

This env.py wires Alembic to our SQLAlchemy models and app configuration:

- Reads the database URL from ``app.config.settings``.
- Imports ``app.db.models.Base`` so autogenerate can detect model changes.
- Uses ``run_async_migrations()`` because our engine is async (aiosqlite).
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# ---------------------------------------------------------------------------
# Load Alembic config (gives access to values in alembic.ini)
# ---------------------------------------------------------------------------

config = context.config

# Interpret the config file for logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Import our models so Alembic's autogenerate can see them
# ---------------------------------------------------------------------------

# These imports must happen AFTER sys.path is set up by alembic.ini
from app.config import settings  # noqa: E402
from app.db.models import Base  # noqa: E402

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def get_url() -> str:
    """Return the database URL from our app settings (not alembic.ini)."""
    return settings.database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection).

    Useful for generating raw SQL scripts.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # Required for SQLite ALTER TABLE support
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against a live async database connection."""
    connectable = create_async_engine(get_url(), echo=False)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # Required for SQLite ALTER TABLE support
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Entry point for online migration mode."""
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
