import asyncio

import pysaka
from pysaka import BrowserAuth, Group

# Configure logging
pysaka.configure_logging()


async def main():
    # Login to Hinatazaka46
    print("Logging in...")
    creds = await BrowserAuth.login(
        group=Group.HINATAZAKA46,
        # Interactive login must stay visible (headless=False): the user signs in
        # by hand. headless=True only works for refreshing an existing session via
        # BrowserAuth.refresh_token_headless with a persistent user_data_dir.
        headless=False,
    )

    if creds:
        print("Login Successful!")
        print(f"Access Token: {creds['access_token'][:10]}...")
        print(f"App ID: {creds['app_id']}")
    else:
        print("Login Failed.")


if __name__ == "__main__":
    asyncio.run(main())
