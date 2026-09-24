"""Tests for the TokenStore and AuthTokens classes."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ah.token_store import AuthTokens, TokenStore


# ---------------------------------------------------------------------------
# AuthTokens
# ---------------------------------------------------------------------------


class TestAuthTokens:
    """Tests for the AuthTokens model."""

    def test_from_api_response_basic(self) -> None:
        data = {
            "access_token": "at_abc123",
            "refresh_token": "rt_xyz789",
            "member_id": "m_42",
            "expires_in": 3600,
        }
        tokens = AuthTokens.from_api_response(data)

        assert tokens.access_token == "at_abc123"
        assert tokens.refresh_token == "rt_xyz789"
        assert tokens.member_id == "m_42"
        assert tokens.expires_at is not None
        # Should be roughly 1 hour from now
        delta = tokens.expires_at - datetime.now(timezone.utc)
        assert 3500 < delta.total_seconds() < 3601

    def test_from_api_response_missing_optional_fields(self) -> None:
        data = {
            "access_token": "at_abc123",
        }
        tokens = AuthTokens.from_api_response(data)

        assert tokens.access_token == "at_abc123"
        assert tokens.refresh_token == ""
        assert tokens.member_id == ""
        assert tokens.expires_at is None

    def test_is_expired_when_in_future(self) -> None:
        tokens = AuthTokens(
            access_token="at",
            refresh_token="rt",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert tokens.is_expired is False

    def test_is_expired_when_in_past(self) -> None:
        tokens = AuthTokens(
            access_token="at",
            refresh_token="rt",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert tokens.is_expired is True

    def test_is_expired_when_no_expiry(self) -> None:
        tokens = AuthTokens(
            access_token="at",
            refresh_token="rt",
            expires_at=None,
        )
        assert tokens.is_expired is False

    def test_expires_in_human_future(self) -> None:
        tokens = AuthTokens(
            access_token="at",
            refresh_token="rt",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1, minutes=23),
        )
        human = tokens.expires_in_human
        assert "1h" in human
        # Minutes may be 22 or 23 depending on execution time
        assert "22m" in human or "23m" in human

    def test_expires_in_human_expired(self) -> None:
        tokens = AuthTokens(
            access_token="at",
            refresh_token="rt",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert tokens.expires_in_human == "expired"

    def test_expires_in_human_unknown(self) -> None:
        tokens = AuthTokens(
            access_token="at",
            refresh_token="rt",
            expires_at=None,
        )
        assert tokens.expires_in_human == "unknown"


# ---------------------------------------------------------------------------
# TokenStore
# ---------------------------------------------------------------------------


class TestTokenStore:
    """Tests for the TokenStore file-based persistence."""

    def test_load_returns_none_when_no_file(self, tmp_path: Path) -> None:
        store = TokenStore(path=tmp_path / "nonexistent" / "tokens.json")
        assert store.load() is None

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path=path)

        tokens = AuthTokens(
            access_token="at_test",
            refresh_token="rt_test",
            member_id="m_42",
            expires_at=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        )

        store.save(tokens)
        loaded = store.load()

        assert loaded is not None
        assert loaded.access_token == "at_test"
        assert loaded.refresh_token == "rt_test"
        assert loaded.member_id == "m_42"
        assert loaded.expires_at is not None
        # Compare as ISO strings to avoid microsecond differences
        assert loaded.expires_at.year == 2026
        assert loaded.expires_at.month == 12

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "tokens.json"
        store = TokenStore(path=path)

        tokens = AuthTokens(access_token="at", refresh_token="rt")
        store.save(tokens)

        assert path.exists()

    def test_save_sets_restrictive_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path=path)

        tokens = AuthTokens(access_token="at", refresh_token="rt")
        store.save(tokens)

        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        # Should be 0600 (owner read/write only)
        assert file_mode == 0o600

    def test_save_sets_restrictive_dir_permissions(self, tmp_path: Path) -> None:
        subdir = tmp_path / "secure_dir"
        path = subdir / "tokens.json"
        store = TokenStore(path=path)

        tokens = AuthTokens(access_token="at", refresh_token="rt")
        store.save(tokens)

        dir_mode = stat.S_IMODE(os.stat(subdir).st_mode)
        # Should be 0700 (owner only)
        assert dir_mode == 0o700

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path=path)

        tokens = AuthTokens(access_token="at", refresh_token="rt")
        store.save(tokens)
        assert path.exists()

        store.clear()
        assert not path.exists()

    def test_clear_no_error_when_no_file(self, tmp_path: Path) -> None:
        store = TokenStore(path=tmp_path / "nonexistent.json")
        store.clear()  # Should not raise

    def test_load_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        path.write_text("not valid json {{{", encoding="utf-8")

        store = TokenStore(path=path)
        assert store.load() is None

    def test_load_returns_none_on_missing_required_field(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        # Missing access_token
        path.write_text('{"refresh_token": "rt"}', encoding="utf-8")

        store = TokenStore(path=path)
        assert store.load() is None

    def test_saved_file_is_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path=path)

        tokens = AuthTokens(
            access_token="at_test",
            refresh_token="rt_test",
            expires_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        )
        store.save(tokens)

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["access_token"] == "at_test"
        assert raw["refresh_token"] == "rt_test"
        assert "2026" in raw["expires_at"]

    def test_path_property(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path=path)
        assert store.path == path
