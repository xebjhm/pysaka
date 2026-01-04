import asyncio
from pathlib import Path
from typing import Any, Optional, TypedDict, Union

import structlog
from playwright.async_api import async_playwright

from .client import GROUP_CONFIG, Group

logger = structlog.get_logger()

class LoginCredentials(TypedDict):
    access_token: str
    cookies: dict[str, str]
    app_id: str
    user_agent: str

class BrowserAuth:
    """Handles browser-based authentication for Sakamichi Groups Message."""

    @staticmethod
    async def login(
        group: Union[Group, str],
        headless: bool = False,
        user_data_dir: Optional[str] = None,
        channel: Optional[str] = None
    ) -> Optional[LoginCredentials]:
        """
        Launches browser for login and captures tokens.

        Args:
            group: The target group (e.g. Group.NOGIZAKA46).
            headless: Whether to run browser in headless mode.
            user_data_dir: Path to directory for persistent browser session.
            channel: Browser channel (e.g. 'chrome', 'msedge').

        Returns:
            Dictionary containing access token and cookies, or None if failed.

        Raises:
            ValueError: If invalid group provided.
        """
        if isinstance(group, str):
            try:
                group = Group(group.lower())
            except ValueError:
                raise ValueError(f"Invalid group: {group}. Must be one of {[g.value for g in Group]}")

        config = GROUP_CONFIG[group]
        target_url = config["auth_url"]
        api_host = config["api_base"].replace("https://", "").split("/")[0]

        async with async_playwright() as p:
            logger.info(f"Launching browser for {group.value} login...")

            if user_data_dir:
                user_data_path = Path(user_data_dir).absolute()
                user_data_path.mkdir(parents=True, exist_ok=True)

                context = await p.chromium.launch_persistent_context(
                    user_data_dir=user_data_path,
                    headless=headless,
                    channel=channel,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-infobars',
                        '--disable-gpu',
                        '--disable-dev-shm-usage',
                        '--disable-software-rasterizer',
                    ],
                    viewport={'width': 1280, 'height': 800},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                page = context.pages[0] if context.pages else await context.new_page()
            else:
                browser = await p.chromium.launch(
                    headless=headless,
                    channel=channel,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-infobars',
                    ]
                )
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    viewport={'width': 1280, 'height': 800}
                )
                page = await context.new_page()

            # Stealth script
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            # Token capture container
            captured_data: dict[str, Any] = {}
            token_future = asyncio.Future()

            async def handle_response(response):
                if token_future.done(): return

                request = response.request
                if api_host in request.url and response.status == 200:
                    headers = request.headers
                    auth = headers.get('authorization') or headers.get('Authorization')

                    if auth and 'Bearer' in auth:
                        token = auth.split('Bearer ')[1]
                        if token:
                            captured_data['access_token'] = token
                            captured_data['x-talk-app-id'] = headers.get('x-talk-app-id') or headers.get('X-Talk-App-ID')
                            captured_data['user-agent'] = headers.get('user-agent') or headers.get('User-Agent')
                            if not token_future.done():
                                token_future.set_result(True)

            page.on("response", handle_response)

            try:
                await page.goto(target_url, timeout=60000)
            except Exception as e:
                logger.warning(f"Navigation error (ignoring): {e}")

            try:
                # Wait for token capture (timeout 5 mins for interactive, 30s for headless/cached)
                timeout = 300 if not headless else 45
                await asyncio.wait_for(token_future, timeout=timeout)
                logger.info("Token captured successfully!")

                # Get cookies
                cookies_list = await context.cookies()
                captured_data['cookies'] = {c['name']: c['value'] for c in cookies_list}

                logger.info("Closing browser...")
                await page.close()
                await context.close()
                if not user_data_dir:
                    await browser.close()

                return {
                    "access_token": captured_data['access_token'],
                    "cookies": captured_data['cookies'],
                    "app_id": captured_data.get('x-talk-app-id', ''),
                    "user_agent": captured_data.get('user-agent', '')
                }

            except asyncio.TimeoutError:
                logger.error("Login timed out.")
            except Exception as e:
                logger.error(f"Login error: {e}")
            finally:
                try:
                    if user_data_dir:
                        await context.close()
                    else:
                        await browser.close()
                except:
                    pass

            return None
