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

from pysaka import BrowserAuth, Group
from pysaka.credentials import TokenManager, get_auth_dir


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

    # Persist the browser profile under auth_data/<group> so it populates the
    # directory the integration-test skip gate checks (get_auth_dir() non-empty),
    # and so the headless refresh path can later reuse this session (PY-AUX-01).
    user_data_dir = str(get_auth_dir() / group.value)

    creds = await BrowserAuth.login(group, headless=args.headless, user_data_dir=user_data_dir)

    if creds:
        print()
        print("Login successful!")
        print(f"  Access token: {creds['access_token'][:20]}...")
        print(f"  Refresh token: {creds['refresh_token'][:20] if creds.get('refresh_token') else 'N/A'}...")

        # Store credentials using TokenManager. The real API is save_session
        # (there is no store_tokens); it keys by the string group value and
        # persists cookies, which the web-session refresh path requires (PY-AUX-01).
        token_manager = TokenManager()
        token_manager.save_session(
            group.value,
            creds["access_token"],
            creds.get("refresh_token"),
            creds.get("cookies"),
        )
        print()
        print("Credentials stored in system keyring.")
        print("You can now run integration tests:")
        print("  uv run pytest tests/test_integration.py -m integration -v")
    else:
        print("Login failed or was cancelled.")


if __name__ == "__main__":
    asyncio.run(main())
