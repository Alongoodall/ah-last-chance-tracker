#!/usr/bin/env python3
"""Show Albert Heijn authentication status.

Usage:
    python scripts/ah_auth_status.py
"""

import _cli_common  # noqa: F401 — suppress logging

from app.ah.token_store import TokenStore


def main() -> None:
    token_store = TokenStore()

    print()
    print("AH Authentication Status")
    print("=" * 40)
    print(f"  Token file:     {token_store.path}")

    tokens = token_store.load()

    if tokens is None:
        print(f"  Authenticated:  no")
        print()
        print("Run: python scripts/ah_login.py")
        return

    print(f"  Authenticated:  yes")

    if tokens.is_expired:
        print(f"  Access token:   expired")
    elif tokens.expires_at:
        print(f"  Access token:   valid (expires in {tokens.expires_in_human})")
    else:
        print(f"  Access token:   present (expiry unknown)")

    print(f"  Refresh token:  {'present' if tokens.refresh_token else 'missing'}")
    print(f"  Member ID:      {'present' if tokens.member_id else 'not set'}")
    print()


if __name__ == "__main__":
    main()
