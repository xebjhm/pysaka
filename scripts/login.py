#!/usr/bin/env python3
"""Interactive login script to store credentials for integration tests.

Usage:
    uv run python scripts/login.py
    uv run python scripts/login.py --group hinatazaka46
    uv run python scripts/login.py --group sakurazaka46
    uv run python scripts/login.py --group nogizaka46
"""

import argparse
import asyncio

from pyhako import BrowserAuth, Group
from pyhako.credentials import TokenManager


async def main() -> None:
    parser = argparse.ArgumentParser(description="Login and store credentials for integration tests")
    parser.add_argument(
        "--group",
        "-g",
        type=str,
        default="hinatazaka46",
        choices=["hinatazaka46", "sakurazaka46", "nogizaka46"],
        help="Group to login to (default: hinatazaka46)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (not recommended for login)",
    )
    args = parser.parse_args()

    group_map = {
        "hinatazaka46": Group.HINATAZAKA46,
        "sakurazaka46": Group.SAKURAZAKA46,
        "nogizaka46": Group.NOGIZAKA46,
    }
    group = group_map[args.group]

    print(f"Logging in to {args.group}...")
    print("A browser window will open. Please complete the login process.")
    print()

    creds = await BrowserAuth.login(group, headless=args.headless)

    if creds:
        print()
        print("Login successful!")
        print(f"  Access token: {creds['access_token'][:20]}...")
        print(f"  Refresh token: {creds['refresh_token'][:20] if creds.get('refresh_token') else 'N/A'}...")

        # Store credentials using TokenManager
        token_manager = TokenManager()
        token_manager.store_tokens(
            group=group,
            access_token=creds["access_token"],
            refresh_token=creds.get("refresh_token"),
        )
        print()
        print("Credentials stored in system keyring.")
        print("You can now run integration tests:")
        print("  uv run pytest tests/test_integration.py -m integration -v")
    else:
        print("Login failed or was cancelled.")


if __name__ == "__main__":
    asyncio.run(main())
