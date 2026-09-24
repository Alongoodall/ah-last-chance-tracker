"""Secure local token storage for AH authentication.

Stores access and refresh tokens in a JSON file outside the repository,
using restrictive file permissions.  The default location follows OS
conventions via ``platformdirs`` (falling back to ``~/.config/``).

Design notes
------------
- Tokens are NEVER logged.
- The token file is chmod 0600 (owner read/write only).
- The containing directory is chmod 0700.
- The file is stored outside the repository so it cannot be committed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

logger = structlog.stdlib.get_logger()

# ---------------------------------------------------------------------------
# Default token file location
# ---------------------------------------------------------------------------
_APP_NAME = "ah-last-chance-tracker"


def _default_config_dir() -> Path:
    """Return the config directory for token storage.

    Uses ``~/.config/<app>`` following the XDG convention (matching
    ``appie-go``'s behaviour).  Respects ``$XDG_CONFIG_HOME`` if set.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME
    return Path.home() / ".config" / _APP_NAME


def default_token_path() -> Path:
    """Return the default path for the token file."""
    return _default_config_dir() / "tokens.json"


# ---------------------------------------------------------------------------
# Token model
# ---------------------------------------------------------------------------


class AuthTokens(BaseModel):
    """Authentication tokens returned by the AH API.

    ``expires_at`` is computed from the ``expires_in`` value at the time
    the tokens are issued, so we can check expiry without needing to
    remember when we saved them.
    """

    access_token: str
    refresh_token: str
    member_id: str = ""
    expires_at: datetime | None = None

    @classmethod
    def from_api_response(cls, data: dict) -> "AuthTokens":
        """Create an ``AuthTokens`` from a raw API token response.

        The API returns ``expires_in`` (seconds).  We convert that to an
        absolute ``expires_at`` timestamp.
        """
        expires_at = None
        expires_in = data.get("expires_in")
        if expires_in and isinstance(expires_in, (int, float)) and expires_in > 0:
            from datetime import timedelta

            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            member_id=data.get("member_id", ""),
            expires_at=expires_at,
        )

    @property
    def is_expired(self) -> bool:
        """Return ``True`` if the access token has expired.

        If ``expires_at`` was never set, we assume the token is still
        valid (let the API reject it if not).
        """
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def expires_in_human(self) -> str:
        """Return a human-readable string for time until expiry."""
        if self.expires_at is None:
            return "unknown"
        delta = self.expires_at - datetime.now(timezone.utc)
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return "expired"
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------


class TokenStore:
    """Manages reading, writing, and clearing AH auth tokens.

    Tokens are stored as a JSON file with restrictive permissions.
    The file lives outside the repository to prevent accidental commits.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_token_path()

    @property
    def path(self) -> Path:
        """Return the path to the token file."""
        return self._path

    def load(self) -> AuthTokens | None:
        """Load tokens from disk.

        Returns ``None`` if the token file does not exist or is
        unreadable.  Never raises on missing/corrupt files — the caller
        should handle the ``None`` case by prompting for login.
        """
        if not self._path.exists():
            logger.debug("token_store_no_file", path=str(self._path))
            return None

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            tokens = AuthTokens.model_validate(data)
            logger.info("token_store_loaded", path=str(self._path))
            return tokens
        except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
            logger.warning(
                "token_store_load_failed",
                path=str(self._path),
                error=str(exc),
            )
            return None

    def save(self, tokens: AuthTokens) -> None:
        """Save tokens to disk with restrictive permissions.

        Creates parent directories if needed.
        """
        # Ensure directory exists with restrictive permissions
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass  # Best-effort on platforms that don't support chmod

        # Serialize
        data = tokens.model_dump(mode="json")
        # Convert datetime to ISO string for JSON
        if data.get("expires_at") is not None:
            data["expires_at"] = tokens.expires_at.isoformat()  # type: ignore[union-attr]

        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

        # Write atomically (write to temp, then rename)
        tmp_path = self._path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.chmod(tmp_path, 0o600)
            tmp_path.rename(self._path)
        except OSError:
            # Fallback: direct write
            self._path.write_text(content, encoding="utf-8")
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

        logger.info("token_store_saved", path=str(self._path))

    def clear(self) -> None:
        """Remove the token file if it exists."""
        try:
            self._path.unlink(missing_ok=True)
            logger.info("token_store_cleared", path=str(self._path))
        except OSError as exc:
            logger.warning(
                "token_store_clear_failed",
                path=str(self._path),
                error=str(exc),
            )
