"""Application-wide settings loaded from environment / .env file."""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration.

    Values are read from environment variables (or a .env file).
    Every setting has a sensible default so the app starts out of the box.

    Key variables for the collector:
        AH_STORE_IDS=2203,1812   (comma-separated list of store IDs to collect)
        COLLECTION_INTERVAL_MINUTES=10
    """

    # --- App ---
    app_name: str = "AH Last Chance Tracker"
    debug: bool = False

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///data/ah_tracker.db"

    # --- AH Collector ---
    # Comma-separated store IDs, e.g. "2203,1812,1315"
    # Set via AH_STORE_IDS env var or in .env
    ah_store_ids: str = ""

    collection_interval_minutes: int = 10

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("collection_interval_minutes")
    @classmethod
    def _validate_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("collection_interval_minutes must be at least 1")
        return v

    @property
    def store_ids(self) -> list[int]:
        """Return store IDs as a parsed list of ints.

        Returns an empty list if AH_STORE_IDS is not set.

        Example:
            AH_STORE_IDS=2203,1812  →  [2203, 1812]
        """
        if not self.ah_store_ids.strip():
            return []
        return [int(s.strip()) for s in self.ah_store_ids.split(",") if s.strip()]


# Module-level singleton — import this everywhere instead of instantiating
# Settings() repeatedly.
settings = Settings()
