# pysaka

[![PyPI version](https://badge.fury.io/py/pysaka.svg)](https://badge.fury.io/py/pysaka)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[![Build Status](https://github.com/xebjhm/pysaka/actions/workflows/test.yml/badge.svg)](https://github.com/xebjhm/pysaka/actions)

## Disclaimer & Warnings

> [!CAUTION]
> **Use at your own risk.** accessing the API via unauthorized means may violate the Terms of Service of the respective platforms. This library is for educational purposes only.

### 規約 / Terms of Service Ref.

Users must agree to the official Terms of Service of the respective platforms. The following are excerpts from the official terms (as of writing):

- [Nogizaka46 Message App Terms](https://contact.nogizaka46.com/s/n46app/page/app_terms)
- [Sakurazaka46 Message App Terms](https://sakurazaka46.com/s/s46app/page/app_terms)
- [Hinatazaka46 Message App Terms](https://www.hinatazaka46.com/s/h46app/page/app_terms)

**第3条（知的財産権）/ Article 3 (Intellectual Property)**
> 3. 当社が別に定める場合を除き、お客様が本コンテンツを複製、翻案、頒布、公衆送信等することは禁止します。

**第8条（禁止事項）/ Article 8 (Prohibited Acts)**
> (11) 当社または第三者の情報、データおよびソフトウェアを修正、改変、改ざん、リバースエンジニアリング、逆コンパイル、逆アッセンブルまたは消去等する行為

> (16) 当社が指定するアクセス方法以外の手段で本サービスにアクセスし、またはアクセスを試みる行為

> (17) 自動化された手段（クローラおよび類似の技術を含む）を用いて本サービスにアクセスし、またはアクセスを試みる行為


**Async Python client for Sakamichi Groups (Nogizaka46, Sakurazaka46, Hinatazaka46, Yodel) Message API.**

pysaka provides a robust, type-hinted, and async interface to interact with the official Message apps for all supported groups. It handles authentication (via browser), token management, and data retrieval.

## Features
- 🔐 **Browser Authentication**: Seamless interactive login via Playwright (compatible with MFA/SSO).
- 🍪 **Auto-Refresh**: Automatically refreshes access tokens using captured cookies.
- 🚀 **Async/Await**: Built on `aiohttp` for high-performance concurrent requests.
- 📦 **Multi-Group**: Supports Nogizaka46, Sakurazaka46, Hinatazaka46, and Yodel out of the box.
- 📝 **Blog Scraper**: Backup official blogs (HTML + images) for all three groups.
- 🛠️ **Type Hinted**: 100% type coverage for better IDE support.

## Configuration

pysaka uses `structlog` for observability. You can control the logging output via environment variables:

- `HAKO_ENV=development` (default): Pretty-printed, colored console logs.
- `HAKO_ENV=production`: Structured JSON logs with automatic secret redaction.

## Installation

Recommended install via `uv`:
```bash
uv add pysaka
```

For development:
```bash
git clone https://github.com/xebjhm/pysaka.git
cd pysaka
uv sync
```

## Quick Start

### 1. Authentication
Use `BrowserAuth` to log in interactively. This launches a browser window for you to enter credentials.

```python
import asyncio
from pysaka import BrowserAuth, Group

async def login():
    creds = await BrowserAuth.login(Group.NOGIZAKA46)
    print(creds['access_token'])

asyncio.run(login())
```

### 2. Fetching Data
Initialize the `Client` with your credentials.

```python
import asyncio
import aiohttp
from pysaka import Client, Group

async def main():
    # ... assume creds obtained via BrowserAuth ...
    token = "YOUR_ACCESS_TOKEN" 
    
    async with aiohttp.ClientSession() as session:
        client = Client(Group.NOGIZAKA46, access_token=token)
        
        # Get Profile
        profile = await client.get_profile(session)
        print(f"Hello, {profile['nickname']}!")
        
        # Get Groups (Members)
        groups = await client.get_groups(session)
        for g in groups:
            print(f"{g['name']} (ID: {g['id']})")

asyncio.run(main())
```

### 3. Blog Scraping (No Auth Required)
Scrape official blogs for any group without authentication.

```python
import asyncio
import aiohttp
from pysaka.blog import NogizakaBlogScraper, HinatazakaBlogScraper, SakurazakaBlogScraper

async def scrape_blogs():
    async with aiohttp.ClientSession() as session:
        scraper = NogizakaBlogScraper(session)

        # Get all active members
        members = await scraper.get_members()
        print(f"Found {len(members)} members")

        # Get blogs for a specific member
        async for blog in scraper.get_blogs(member_id="some_member_code"):
            print(f"{blog.title} - {blog.published_at}")

asyncio.run(scrape_blogs())
```

## API Reference

### `Client`
The main entry point.

- `__init__(group, access_token, ...)`: Initialize client.
- `get_profile(session)`: Get current user profile.
- `get_groups(session)`: List subscribed members/groups.
- `get_messages(session, group_id, ...)`: Fetch messages timeline.
- `get_news(session)`: Fetch official announcements.
- `get_tags(session)`: Fetch tags.
- `get_fc_contents(session)`: Fetch Fan Club content.
- `get_organizations(session)`: Fetch organizations.
- `get_products(session, product_type)`: Fetch products (subscriptions).
- `post_json(session, endpoint, data)`: Perform JSON POST requests.
- `delete_json(session, endpoint)`: Perform DELETE requests.
- `download_file(session, url, filepath)`: Download a file to local filesystem.
- `get_member(session, member_id)`: Fetch individual member details.
- `get_account(session)`: Fetch user account information.
- `get_letters(session, group_id)`: Fetch user's sent letters/cards.
- `get_past_messages(session, group_id)`: Fetch historical messages.
- `get_subscription_streak(session, group_id)`: Fetch consecutive subscription days.
- `add_favorite(session, message_id)`: Add a message to favorites.
- `remove_favorite(session, message_id)`: Remove a message from favorites.
- `refresh_if_needed(session)`: Lazy token refresh if expiring soon.

### Credential Management

- `get_token_manager()`: Get the singleton `TokenManager` instance for secure credential storage.
- `TokenManager.save_session(group, access_token, ...)`: Save session credentials to system keyring.
- `TokenManager.load_session(group)`: Load stored session credentials.
- `TokenManager.delete_session(group)`: Remove stored credentials.

### `BrowserAuth`
Helper for OAuth2 flow.

- `login(group, headless=False, ...)`: Perform login and capture tokens.

### Blog Scrapers
Public blog scrapers (no authentication required).

- `NogizakaBlogScraper(session)`: Nogizaka46 official blog.
- `SakurazakaBlogScraper(session)`: Sakurazaka46 official blog.
- `HinatazakaBlogScraper(session)`: Hinatazaka46 official blog.

Each scraper provides:
- `get_members()`: Get dict of member_id -> member_name.
- `get_blogs(member_id, since_date=None)`: AsyncIterator of BlogEntry objects.
- `get_blog_detail(blog_id, member_id=None)`: Fetch single blog by ID.

### Exceptions

- `SakaError`: Base exception for all pysaka errors.
- `AuthError`: Authentication related errors.
- `ApiError`: API request errors (includes `status_code` attribute).
- `SessionExpiredError`: Session invalidated server-side (e.g., logged in from another device).
- `RefreshFailedError`: All token refresh attempts failed unexpectedly.
- `BlogGoneError`: Blog post has been permanently removed (HTTP 404/410).

## Contributing
Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## License
MIT
