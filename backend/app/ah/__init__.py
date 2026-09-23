"""Custom exceptions and public types for AH API interactions."""

from app.ah.token_store import AuthTokens, TokenStore


class AHError(Exception):
    """Base exception for all AH API errors."""


class AHAuthenticationError(AHError):
    """Failed to obtain or refresh an authentication token."""


class AHAPIError(AHError):
    """The AH API returned an error response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class AHGraphQLError(AHAPIError):
    """The AH GraphQL endpoint returned an error in the response body."""


class AHStoreNotFoundError(AHError):
    """No stores matched the search criteria."""


class AHNoBargainsError(AHError):
    """The store returned no bargain items (may be expected outside store hours)."""


__all__ = [
    "AHError",
    "AHAuthenticationError",
    "AHAPIError",
    "AHGraphQLError",
    "AHStoreNotFoundError",
    "AHNoBargainsError",
    "AuthTokens",
    "TokenStore",
]
