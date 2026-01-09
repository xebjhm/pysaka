import asyncio

import aiohttp

import pyhako
from pyhako import BrowserAuth, Client, Group

pyhako.configure_logging()

async def main():
    # 1. Login (or provide tokens manually)
    creds = await BrowserAuth.login(Group.HINATAZAKA46, headless=True)
    if not creds:
        print("Login failed")
        return

    # 2. Initialize Client
    async with aiohttp.ClientSession() as session:
        client = Client(
            group=Group.HINATAZAKA46,
            access_token=creds['access_token'],
            cookies=creds['cookies'],
            app_id=creds['app_id'],
            user_agent=creds['user_agent']
        )

        # 3. Fetch Profile
        profile = await client.get_profile(session)
        print(f"Profile: {profile}")

        # 4. Fetch News
        news = await client.get_news(session, count=5)
        print(f"Latest News: {[n['title'] for n in news]}")

if __name__ == "__main__":
    asyncio.run(main())
