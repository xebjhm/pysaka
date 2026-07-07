import asyncio
import os
from pathlib import Path
from typing import Any, Optional, TypedDict, Union

import structlog
from playwright.async_api import async_playwright

from .client import GROUP_CONFIG, Group

logger = structlog.get_logger()

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class LoginCredentials(TypedDict):
    access_token: str
    refresh_token: Optional[str]
    cookies: dict[str, str]
    app_id: str
    user_agent: str


class BrowserAuth:
    """Handles browser-based authentication for Sakamichi Groups Message."""

    @staticmethod
    def _scrape_token_headers(headers: Any) -> Optional[dict[str, Any]]:
        """Pull access_token + app-id + user-agent from an authenticated API request.

        Shared by the interactive-login and headless-refresh response handlers so the
        Bearer-capture contract lives in one place. Returns None if no usable Bearer.
        """
        auth = headers.get("authorization") or headers.get("Authorization")
        if not auth or "Bearer " not in auth:
            return None
        token = auth.split("Bearer ", 1)[1].strip()
        if not token:
            return None
        return {
            "access_token": token,
            "x-talk-app-id": headers.get("x-talk-app-id") or headers.get("X-Talk-App-ID"),
            "user-agent": headers.get("user-agent") or headers.get("User-Agent"),
        }

    @staticmethod
    async def login(
        group: Union[Group, str],
        headless: bool = False,
        user_data_dir: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Optional[LoginCredentials]:
        """
        Launches browser for login and captures tokens.

        Args:
            group: The target group (e.g. Group.NOGIZAKA46).
            headless: Whether to run browser in headless mode.
            user_data_dir: Path to directory for persistent browser session.
            channel: Browser channel (e.g. 'chrome', 'msedge').

        Returns:
            Dictionary containing the access token, refresh_token, and cookies,
            or None if login failed.

        Raises:
            ValueError: If invalid group provided.
        """
        if isinstance(group, str):
            try:
                group = Group(group.lower())
            except ValueError as err:
                raise ValueError(f"Invalid group: {group}. Must be one of {[g.value for g in Group]}") from err

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
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-infobars",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--disable-software-rasterizer",
                    ],
                    viewport={"width": 1280, "height": 800},
                    user_agent=_DEFAULT_USER_AGENT,
                )
                page = context.pages[0] if context.pages else await context.new_page()
            else:
                browser = await p.chromium.launch(
                    headless=headless,
                    channel=channel,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-infobars",
                    ],
                )
                context = await browser.new_context(
                    user_agent=_DEFAULT_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()

            # Stealth script
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            # Token capture container
            captured_data: dict[str, Any] = {}
            token_future: asyncio.Future[None] = asyncio.Future()

            async def handle_response(response):
                request = response.request
                if api_host not in request.url or response.status != 200:
                    return

                # Capture the long-lived refresh_token from the signin/token
                # response body. The web flow receives it too, but we previously
                # only scraped the access_token from request headers.
                if "refresh_token" not in captured_data and (
                    "/signin" in request.url or "/update_token" in request.url
                ):
                    try:
                        data = await response.json()
                        if isinstance(data, dict) and data.get("refresh_token"):
                            captured_data["refresh_token"] = data["refresh_token"]
                            logger.debug("Captured refresh_token from signin response")
                    except Exception as e:
                        logger.debug("Could not parse refresh_token from response", error=str(e))

                if token_future.done():
                    return

                creds = BrowserAuth._scrape_token_headers(request.headers)
                if creds:
                    captured_data.update(creds)
                    if not token_future.done():
                        token_future.set_result(True)

            page.on("response", handle_response)

            def handle_close(*_args):
                # User closed the browser/page before a token was captured.
                # Fail the wait so login() returns promptly and releases the
                # caller's lock, instead of hanging until the interactive timeout.
                if not token_future.done():
                    token_future.set_exception(RuntimeError("Browser closed before authentication completed."))

            page.on("close", handle_close)
            context.on("close", handle_close)

            try:
                # DESIGN DECISION: Trust the persistent browser context for OAuth session management.
                #
                # Previous implementation tried to selectively clear cookies, but this caused issues:
                # - Google cookies may be set on regional domains (e.g., .google.com.tw)
                # - Selective clearing can miss edge cases and break OAuth session persistence
                # - OAuth providers (Google/Apple/LINE) manage their own session state
                #
                # Industry best practice: Don't interfere with OAuth provider cookies.
                # The persistent context (user_data_dir) preserves all browser state including:
                # - OAuth session cookies (Google SID, HSID, Apple auth, LINE session)
                # - Account chooser state (allows "select account" instead of re-login)
                #
                # We only clear the SERVICE domain's localStorage/sessionStorage to ensure
                # the web app starts fresh without stale application state.

                try:
                    await page.goto(target_url, wait_until="commit", timeout=5000)
                    await page.evaluate("window.localStorage.clear(); window.sessionStorage.clear();")
                    logger.debug("Service domain localStorage/sessionStorage cleared")
                except Exception as clear_err:
                    logger.debug(f"Storage clear attempt (non-fatal): {clear_err}")

                await page.goto(target_url, timeout=60000)
                logger.debug(f"Navigated to auth URL: {target_url}")
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
                    if c["name"] == "session":
                        logger.debug("Found session cookie", domain=c.get("domain"), path=c.get("path"))

                    if target_domain in c.get("domain", ""):
                        relevant_cookies[c["name"]] = c["value"]

                captured_data["cookies"] = relevant_cookies
                logger.debug(f"Captured {len(relevant_cookies)} session cookies.")

                # Build the result and return. Cleanup happens once, in the
                # `finally` below — not here — so we never double-close (closing
                # a persistent context twice raised "Target page, context or
                # browser has been closed" on Windows and discarded the login).
                logger.info("Closing browser...")
                return {
                    "access_token": captured_data["access_token"],
                    "refresh_token": captured_data.get("refresh_token"),
                    "cookies": captured_data["cookies"],
                    "app_id": captured_data.get("x-talk-app-id", ""),
                    "user_agent": captured_data.get("user-agent", ""),
                }

            except asyncio.TimeoutError:
                logger.error("Login timed out.")
            except Exception as e:
                logger.error(f"Login error: {e}")
            finally:
                # Single cleanup site for every path (success, timeout, error).
                # Best-effort: a teardown error must never propagate out of a
                # successful login or mask a real error.
                try:
                    if user_data_dir:
                        await context.close()
                    else:
                        await browser.close()
                except Exception as close_err:
                    logger.debug(f"Browser close (non-fatal): {close_err}")

            return None

    @staticmethod
    async def refresh_token_headless(
        group: Group,
        auth_dir: Union[str, Path],
        auto_install: bool = True,
        channel: Optional[str] = None,
    ) -> Optional[LoginCredentials]:
        """
        Refreshes access token via headless browser using persistent context.

        Args:
            group: Target group for authentication.
            auth_dir: Path to persistent browser context directory.
            auto_install: If True, download Playwright's bundled Chromium when it
                is missing. Ignored when a system ``channel`` is in effect.
            channel: System browser channel to drive (e.g. ``"chrome"``,
                ``"msedge"``). May also be supplied via the
                ``PYSAKA_BROWSER_CHANNEL`` environment variable. When set, the
                refresh reuses the user's already-installed browser (the same one
                interactive login requires) and NEVER downloads Chromium — that
                runtime download runs a Node subprocess that pops a console
                window in a packaged GUI app. It tries the requested channel,
                then Edge (present on every modern Windows), then fails closed so
                the caller can prompt a normal re-login. Defaults to None
                (bundled Chromium, with ``auto_install`` as the fallback) so the
                library's headless-server behaviour is unchanged.
        """
        auth_dir = Path(auth_dir)
        if not auth_dir.exists():
            logger.error(f"Auth directory {auth_dir} does not exist.")
            return None

        # A caller (e.g. the desktop app) can pin the refresh to the user's
        # installed browser explicitly or, more conveniently, via env — so a
        # single startup setting covers every Client refresh path at once.
        channel = channel if channel is not None else (os.environ.get("PYSAKA_BROWSER_CHANNEL") or None)
        # Only the bundled-Chromium path (no system channel) may auto-download.
        allow_download = auto_install and channel is None

        # Extract config
        config = GROUP_CONFIG[group]
        api_host = config["api_base"]
        auth_url = config["auth_url"]

        captured_data: dict[str, Any] = {}
        token_future: asyncio.Future[None] = asyncio.Future()

        async with async_playwright() as p:
            context = await BrowserAuth._launch_refresh_context(p, auth_dir, channel, allow_download)
            if context is None:
                return None

            try:
                page = context.pages[0] if context.pages else await context.new_page()

                # NOTE: Do NOT clear cookies or localStorage here!
                # The headless refresh relies on the existing browser session state
                # (service cookies + localStorage token) to load the web app.
                # The web app will then make API calls with the token, which we capture.
                # Clearing state would break the session and show login page instead.

                async def handle_response(response):
                    if token_future.done():
                        return

                    # Match API host (robust check)
                    if (
                        api_host.replace("https://", "").split("/")[0] in response.request.url
                        and response.status == 200
                    ):
                        creds = BrowserAuth._scrape_token_headers(response.request.headers)
                        if creds:
                            captured_data.update(creds)
                            logger.debug(
                                "Headless refresh captured token",
                                capture_url=str(response.request.url),
                            )
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
                    if target_domain in c.get("domain", ""):
                        relevant_cookies[c["name"]] = c["value"]

                return {
                    "access_token": captured_data["access_token"],
                    "refresh_token": None,
                    "cookies": relevant_cookies,
                    "app_id": captured_data.get("x-talk-app-id", ""),
                    "user_agent": captured_data.get("user-agent", ""),
                }

            except Exception as e:
                logger.error(f"Headless refresh failed: {e}")
                return None
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    @staticmethod
    async def _launch_refresh_context(p: Any, auth_dir: Path, channel: Optional[str], allow_download: bool) -> Any:
        """Open the persistent context for a silent refresh.

        With a system ``channel`` we try it, then Edge (guaranteed on modern
        Windows), and never download. Only the bundled-Chromium path
        (``channel is None``) may auto-install. Returns the launched context, or
        None if every attempt failed (the caller then fails the refresh cleanly).
        """
        launch_args = ["--disable-blink-features=AutomationControlled"]

        if channel:
            channels_to_try = [channel]
            if channel != "msedge":
                channels_to_try.append("msedge")  # always present on modern Windows
        else:
            channels_to_try = [None]

        last_error: Optional[Exception] = None
        for ch in channels_to_try:
            try:
                return await p.chromium.launch_persistent_context(
                    user_data_dir=str(auth_dir),
                    headless=True,
                    channel=ch,
                    args=launch_args,
                )
            except Exception as e:
                last_error = e
                if ch is None and "Executable doesn't exist" in str(e) and allow_download:
                    if await BrowserAuth._install_bundled_chromium():
                        try:
                            return await p.chromium.launch_persistent_context(
                                user_data_dir=str(auth_dir),
                                headless=True,
                                channel=None,
                                args=launch_args,
                            )
                        except Exception as retry_error:
                            last_error = retry_error
                logger.warning(f"Headless launch failed (channel={ch!r}): {e}")

        logger.error(f"Failed to launch headless browser: {last_error}")
        return None

    @staticmethod
    async def _install_bundled_chromium() -> bool:
        """Download Playwright's bundled Chromium (one-time setup).

        Used only by the library's bundled-Chromium fallback — never in a
        system-channel run — because it runs a Node download subprocess that
        pops a console window in a windowed app. Returns True if the install
        command ran to completion.
        """
        logger.info("Downloading headless browser for auto-refresh (one-time setup)...")
        # Store browsers inside the Playwright package dir (frozen-bundle friendly).
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        try:
            import sys

            from playwright.__main__ import main

            # In a frozen build, re-invoking sys.executable with '-m' fails (the
            # exe parses '-m' as its own argument), so call the CLI entry directly.
            old_argv = sys.argv
            try:
                sys.argv = ["playwright", "install", "chromium"]
                main()
            except SystemExit:
                pass  # Playwright CLI calls sys.exit() on success
            finally:
                sys.argv = old_argv
            logger.info("Playwright chromium installed successfully.")
            return True
        except Exception as install_error:
            logger.error(f"Failed to auto-install Playwright browser: {install_error}")
            return False
