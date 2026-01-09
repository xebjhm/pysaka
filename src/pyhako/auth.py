import asyncio
from pathlib import Path
from typing import Any, Optional, TypedDict, Union

import structlog
from playwright.async_api import async_playwright

from .client import GROUP_CONFIG, Group

logger = structlog.get_logger()

class LoginCredentials(TypedDict):
    access_token: str
    refresh_token: Optional[str]
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
            except ValueError as err:
                raise ValueError(
                    f"Invalid group: {group}. Must be one of {[g.value for g in Group]}"
                ) from err

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
                # CRITICAL: Force clear stale state before fresh login
                # This prevents capturing mixed state (Valid Token + Old/Invalid Session Cookie)
                # if the app tries to resume an expired session.
                await context.clear_cookies()
                try:
                    await page.goto(target_url, wait_until="commit", timeout=5000) # Short wait to access origin
                    await page.evaluate("window.localStorage.clear(); window.sessionStorage.clear();")
                except:
                    pass # Ignore errors clearing storage (e.g. if page load fails)

                await page.goto(target_url, timeout=60000)
            except Exception as e:
                logger.warning(f"Navigation error (ignoring): {e}")

            try:
                # Wait for token capture (timeout 5 mins for interactive, 30s for headless/cached)
                timeout = 300 if not headless else 45
                await asyncio.wait_for(token_future, timeout=timeout)
                # Capture domain-specific cookies (Web Session) for token refresh
                cookies_list = await context.cookies()
                target_domain = group.value  # e.g. 'hinatazaka46', 'nogizaka46'
                # Filter to keep only cookies relevant to the service (ignore Google/Analytics)
                relevant_cookies = {}

                logger.debug("--- Capturing Cookies ---")
                for c in cookies_list:
                    if c['name'] == 'session':
                        logger.debug(f"Found session cookie: {c['value']} | Domain: {c.get('domain')} | Path: {c.get('path')}")

                    if target_domain in c.get('domain', ''):
                        relevant_cookies[c['name']] = c['value']

                captured_data['cookies'] = relevant_cookies
                logger.debug(f"Captured {len(relevant_cookies)} session cookies.")

                logger.info("Closing browser...")
                await page.close()
                await context.close()
                if not user_data_dir:
                    await browser.close()

                return {
                    "access_token": captured_data['access_token'],
                    "refresh_token": None,
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

    @staticmethod
    async def refresh_token_headless(
        group: Group,
        auth_dir: Union[str, Path],
        auto_install: bool = True
    ) -> Optional[LoginCredentials]:
        """
        Refreshes access token via headless browser using persistent context.
        
        Args:
            group: Target group for authentication.
            auth_dir: Path to persistent browser context directory.
            auto_install: If True, automatically install Playwright chromium if missing.
        """
        auth_dir = Path(auth_dir)
        if not auth_dir.exists():
            logger.error(f"Auth directory {auth_dir} does not exist.")
            return None

        # Extract config
        config = GROUP_CONFIG[group]
        api_host = config["api_base"]
        auth_url = config["auth_url"]

        captured_data = {}
        token_future = asyncio.Future()

        async with async_playwright() as p:
            try:
                # Launch persistent context
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(auth_dir),
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
            except Exception as e:
                if "Executable doesn't exist" in str(e) and auto_install:
                    # UX: Explain why we are downloading
                    logger.info("Downloading headless browser for auto-refresh (One-time setup)...")
                    # Force Playwright to look in global cache, not frozen bundle
                    import os
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

                    try:
                        import sys

                        from playwright.__main__ import main

                        # In frozen environment, calling subprocess with sys.executable fails
                        # caused by the executable trying to parse '-m' as an argument.
                        # We must call the internal CLI entry point directly.
                        old_argv = sys.argv
                        try:
                            sys.argv = ["playwright", "install", "chromium"]
                            main()
                        except SystemExit:
                            # Playwright CLI calls sys.exit(), which is expected
                            pass
                        except Exception as e:
                            logger.error(f"Failed to install Playwright browser: {e}")
                            return None
                        finally:
                            sys.argv = old_argv
                        logger.info("Playwright chromium installed successfully. Retrying...")

                        # Retry launch after installation
                        context = await p.chromium.launch_persistent_context(
                            user_data_dir=str(auth_dir),
                            headless=True,
                            args=["--disable-blink-features=AutomationControlled"]
                        )
                    except Exception as install_error:
                        logger.error(f"Failed to auto-install Playwright browser: {install_error}")
                        return None
                else:
                    logger.error(f"Failed to launch headless browser: {e}")
                    return None

            try:
                page = context.pages[0] if context.pages else await context.new_page()

                async def handle_response(response):
                    if token_future.done(): return

                    # Match API host (robust check)
                    if api_host.replace("https://", "").split("/")[0] in response.request.url and response.status == 200:
                        headers = response.request.headers
                        auth = headers.get('authorization') or headers.get('Authorization')
                        if auth and 'Bearer' in auth:
                            token = auth.split('Bearer ')[1]
                            captured_data['access_token'] = token
                            captured_data['x-talk-app-id'] = headers.get('x-talk-app-id') or headers.get('X-Talk-App-ID')
                            captured_data['user-agent'] = headers.get('user-agent') or headers.get('User-Agent')
                            if not token_future.done():
                                token_future.set_result(True)

                page.on("response", handle_response)

                logger.info(f"Navigating to {auth_url} for silent refresh...")
                await page.goto(auth_url, timeout=45000, wait_until="networkidle")

                try:
                    await asyncio.wait_for(token_future, timeout=45)
                except asyncio.TimeoutError:
                    logger.warning("Headless refresh timed out.")
                    return None

                # Capture updated cookies
                cookies_list = await context.cookies()
                target_domain = group.value
                relevant_cookies = {}
                for c in cookies_list:
                   if target_domain in c.get('domain', ''):
                        relevant_cookies[c['name']] = c['value']

                return {
                    "access_token": captured_data['access_token'],
                    "refresh_token": None,
                    "cookies": relevant_cookies,
                    "app_id": captured_data.get('x-talk-app-id', ''),
                    "user_agent": captured_data.get('user-agent', '')
                }

            except Exception as e:
                logger.error(f"Headless refresh failed: {e}")
                return None
            finally:
                try:
                    await context.close()
                except:
                    pass
