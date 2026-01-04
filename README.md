# PyHako

[![PyPI version](https://badge.fury.io/py/pyhako.svg)](https://badge.fury.io/py/pyhako)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build Status](https://github.com/xebjhm/PyHako/actions/workflows/ci.yml/badge.svg)](https://github.com/xebjhm/PyHako/actions)

**Async Python client for Sakamichi Groups (Nogizaka46, Sakurazaka46, Hinatazaka46) Message API.**

PyHako provides a robust, type-hinted, and async interface to interact with the official Message apps for all three Sakamichi groups. It handles authentication (via browser), token management, and data retrieval.

## Features
- 🔐 **Browser Authentication**: Seamless interactive login via Playwright (compatible with MFA/SSO).
- 🍪 **Auto-Refresh**: Automatically refreshes access tokens using captured cookies.
- 🚀 **Async/Await**: Built on `aiohttp` for high-performance concurrent requests.
- 📦 **Multi-Group**: Supports Nogizaka46, Sakurazaka46, and Hinatazaka46 out of the box.
- 🛠️ **Type Hinted**: 100% type coverage for better IDE support.

## Configuration

PyHako uses `structlog` for observability. You can control the logging output via environment variables:

- `HAKO_ENV=development` (default): Pretty-printed, colored console logs.
- `HAKO_ENV=production`: Structured JSON logs with automatic secret redaction.

## Installation

Recommended install via `uv` (standard) or `pip`:
```bash
uv add pyhako
# or
pip install pyhako
```

For development:
```bash
git clone https://github.com/xebjhm/PyHako.git
cd PyHako
uv sync
```

## Quick Start

### 1. Authentication
Use `BrowserAuth` to log in interactively. This launches a browser window for you to enter credentials.

```python
import asyncio
from pyhako import BrowserAuth, Group

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
from pyhako import Client, Group

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

### `BrowserAuth`
Helper for OAuth2 flow.

- `login(group, headless=False, ...)`: Perform login and capture tokens.

## Contributing
Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## License
MIT
