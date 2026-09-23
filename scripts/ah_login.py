#!/usr/bin/env python3
"""Interactive login to Albert Heijn.

Usage:
    python scripts/ah_login.py
    python scripts/ah_login.py --manual   # paste redirect URL manually
"""

import argparse
import asyncio
import sys

sys.path.insert(0, "backend")

from app.ah.client import AHClient  # noqa: E402
from app.ah.login import extract_code_from_url, interactive_login  # noqa: E402
from app.ah.token_store import TokenStore  # noqa: E402


async def exchange(code: str) -> None:
    """Exchange the auth code for tokens and save them."""
    token_store = TokenStore()

    async with AHClient(token_store=token_store) as client:
        tokens = await client.exchange_code(code)

    print(f"✓ Login successful!")
    print(f"  Tokens saved to {token_store.path}")
    if tokens.expires_at:
        print(f"  Access token expires in {tokens.expires_in_human}")


def manual_flow() -> str:
    """Manual fallback: user pastes the redirect URL."""
    print("\nManual login flow")
    print("=" * 40)
    print()
    print("1. Open this URL in your browser:")
    print()
    print("   https://login.ah.nl/login?client_id=appie-ios&response_type=code&redirect_uri=appie://login-exit")
    print()
    print("2. Log in with your Albert Heijn account.")
    print()
    print("3. After login, your browser will try to open an 'appie://...' URL.")
    print("   This will fail (that's expected).")
    print()
    print("4. Copy the FULL URL from your browser's address bar and paste it below.")
    print()

    url = input("Paste the redirect URL or authorization code: ").strip()
    if not url:
        print("No input provided.", file=sys.stderr)
        sys.exit(1)

    return extract_code_from_url(url)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Log in to Albert Heijn for Last Chance Tracker",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Use manual login flow (paste redirect URL instead of auto-capture)",
    )
    args = parser.parse_args()

    print()
    print("AH Last Chance Tracker — Login")
    print("=" * 40)

    # Check existing tokens
    token_store = TokenStore()
    existing = token_store.load()
    if existing and not existing.is_expired:
        print()
        print(f"You are already authenticated (token expires in {existing.expires_in_human}).")
        answer = input("Log in again? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing tokens.")
            return

    if args.manual:
        code = manual_flow()
    else:
        try:
            code = interactive_login()
        except TimeoutError as exc:
            print(f"\n✗ {exc}", file=sys.stderr)
            print("Try again with: python scripts/ah_login.py --manual")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n\nLogin cancelled.")
            sys.exit(0)
        except Exception as exc:
            print(f"\n✗ Automatic login failed: {exc}", file=sys.stderr)
            print("\nFalling back to manual login flow...\n")
            code = manual_flow()

    asyncio.run(exchange(code))


if __name__ == "__main__":
    main()
