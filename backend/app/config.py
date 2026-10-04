"""Application-wide settings loaded from environment / .env file."""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration.

    Values are read from environment variables (or a .env file).
    Every setting has a sensible default so the app starts out of the box.

    Key variables:
        AH_STORE_IDS=2203,1812
            Comma-separated store IDs to collect.

        AH_STORE_POSTAL_CODES=2203:1091GH,1812:1234AB
            Optional: postal codes per store so the collector can look up
            the store name/address from the AH API automatically.

        AH_STORE_NAMES=2203:AH Eerste Oosterparkstraat,1812:AH Centrum
            Optional: manual display-name overrides per store.
            Takes precedence over whatever the API returns.
    """

    # --- App ---
    app_name: str = "AH Last Chance Tracker"
    debug: bool = False

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///data/ah_tracker.db"

    # --- AH Collector ---
    ah_store_ids: str = ""
    ah_store_postal_codes: str = ""   # "storeId:postalCode,..."
    ah_store_names: str = ""          # "storeId:Display Name,..."

    collection_interval_minutes: int = 10

    # --- Notifications ---
    # ntfy.sh (or self-hosted ntfy) push notifications.
    # Set to your topic URL, e.g. https://ntfy.sh/my-ah-tracker-topic
    ah_notify_url: str = ""

    # Optional email via SMTP (all must be set to enable email)
    ah_smtp_host:     str = ""
    ah_smtp_port:     int = 587
    ah_smtp_user:     str = ""
    ah_smtp_password: str = ""
    ah_smtp_from:     str = ""
    ah_smtp_to:       str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("collection_interval_minutes")
    @classmethod
    def _validate_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("collection_interval_minutes must be at least 1")
        return v

    @property
    def store_ids(self) -> list[int]:
        """Return store IDs as a parsed list of ints."""
        if not self.ah_store_ids.strip():
            return []
        return [int(s.strip()) for s in self.ah_store_ids.split(",") if s.strip()]

    @property
    def store_postal_codes(self) -> dict[int, str]:
        """Return {store_id: postal_code} mapping.

        Example:
            AH_STORE_POSTAL_CODES=2203:1091GH,1812:1234AB
            → {2203: '1091GH', 1812: '1234AB'}
        """
        result: dict[int, str] = {}
        for part in self.ah_store_postal_codes.split(","):
            part = part.strip()
            if ":" in part:
                sid, pc = part.split(":", 1)
                try:
                    result[int(sid.strip())] = pc.strip()
                except ValueError:
                    pass
        return result

    @property
    def store_names(self) -> dict[int, str]:
        """Return {store_id: display_name} mapping (manual overrides).

        Example:
            AH_STORE_NAMES=2203:AH Eerste Oosterparkstraat,1812:AH Centrum
            → {2203: 'AH Eerste Oosterparkstraat', 1812: 'AH Centrum'}
        """
        result: dict[int, str] = {}
        for part in self.ah_store_names.split(","):
            part = part.strip()
            if ":" in part:
                sid, name = part.split(":", 1)
                try:
                    result[int(sid.strip())] = name.strip()
                except ValueError:
                    pass
        return result


# Module-level singleton — import this everywhere instead of instantiating
# Settings() repeatedly.
settings = Settings()
