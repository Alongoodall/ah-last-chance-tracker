"""Application-wide settings loaded from environment / .env file."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration.

    Values are read from environment variables (or a .env file).
    Every setting has a sensible default so the app starts out of the box.
    """

    # --- App ---
    app_name: str = "AH Last Chance Tracker"
    debug: bool = False

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///data/ah_tracker.db"

    # --- AH API (placeholder; filled in Phase 2) ---
    ah_store_id: str = ""

    # --- Collector ---
    collection_interval_minutes: int = 10

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
