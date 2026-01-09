import asyncio

import pyhako
from pyhako import BrowserAuth, Group

# Configure logging
pyhako.configure_logging()

async def main():
    # Login to Hinatazaka46
    print("Logging in...")
    creds = await BrowserAuth.login(
        group=Group.HINATAZAKA46,
        headless=False # Set to True for headless mode
    )

    if creds:
        print("Login Successful!")
        print(f"Access Token: {creds['access_token'][:10]}...")
        print(f"App ID: {creds['app_id']}")
    else:
        print("Login Failed.")

if __name__ == "__main__":
    asyncio.run(main())
