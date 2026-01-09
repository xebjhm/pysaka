import asyncio
from pathlib import Path

import aiohttp

import pyhako
from pyhako import Client, Group

pyhako.configure_logging()

async def main():
    # Example token (replace with real one)
    token = "YOUR_ACCESS_TOKEN"

    async with aiohttp.ClientSession() as session:
        client = Client(Group.NOGIZAKA46, access_token=token)

        # Mock message object
        message = {
            "id": 12345,
            "type": "picture",
            "file": "https://example.com/image.jpg",
            "published_at": "2023-01-01T12:00:00Z"
        }

        output_dir = Path("./downloads")
        path = await client.download_message_media(session, message, output_dir)

        if path:
            print(f"Downloaded to: {path}")
        else:
            print("Download failed or no media.")

if __name__ == "__main__":
    asyncio.run(main())
