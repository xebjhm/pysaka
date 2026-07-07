import asyncio

import aiohttp

import pysaka
from pysaka import BrowserAuth, Client, Group

pysaka.configure_logging()


async def main():
    # 1. Login (or provide tokens manually).
    # Interactive OAuth login needs a visible window for the user to sign in, so
    # keep headless=False. A fresh cookie-less headless context has nobody to
    # enter credentials and times out (PY-AUX-03). Headless refresh of an already
    # authenticated session is a separate path: BrowserAuth.refresh_token_headless
    # with a persistent user_data_dir.
    creds = await BrowserAuth.login(Group.HINATAZAKA46, headless=False)
    if not creds:
        print("Login failed")
        return

    # 2. Initialize Client
    async with aiohttp.ClientSession() as session:
        client = Client(
            group=Group.HINATAZAKA46,
            access_token=creds["access_token"],
            cookies=creds["cookies"],
            app_id=creds["app_id"],
            user_agent=creds["user_agent"],
        )

        # 3. Fetch Profile
        profile = await client.get_profile(session)
        print(f"Profile: {profile}")

        # 4. Fetch News
        news = await client.get_news(session, count=5)
        print(f"Latest News: {[n['title'] for n in news]}")


if __name__ == "__main__":
    asyncio.run(main())
