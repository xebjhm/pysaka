import asyncio
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

import aiofiles
import aiohttp
import structlog

from .credentials import TokenManager
from .exceptions import ApiError, SessionExpiredError
from .utils import get_media_extension

logger = structlog.get_logger()

class Group(Enum):
    HINATAZAKA46 = "hinatazaka46"
    NOGIZAKA46 = "nogizaka46"
    SAKURAZAKA46 = "sakurazaka46"

GROUP_CONFIG = {
    Group.HINATAZAKA46: {
        "api_base": "https://api.message.hinatazaka46.com/v2",
        "app_id": "jp.co.sonymusic.communication.keyakizaka 2.5",
        "auth_url": "https://message.hinatazaka46.com/",
        "display_name": "日向坂46"
    },
    Group.NOGIZAKA46: {
        "api_base": "https://api.message.nogizaka46.com/v2",
        "app_id": "jp.co.sonymusic.communication.nogizaka 2.5",
        "auth_url": "https://message.nogizaka46.com/",
        "display_name": "乃木坂46"
    },
    Group.SAKURAZAKA46: {
        "api_base": "https://api.message.sakurazaka46.com/v2",
        "app_id": "jp.co.sonymusic.communication.sakurazaka 2.5",
        "auth_url": "https://message.sakurazaka46.com/",
        "display_name": "櫻坂46"
    }
}

class Client:
    """
    Async client for Sakamichi Groups Message API.

    Provides methods to authenticate, explore groups/members, and fetch messages.
    Handles token refresh automatically via cookies or refresh_token.

    Attributes:
        group (Group): The target Sakamichi group.
        access_token (str): Current OAuth2 access token.
        refresh_token (str): OAuth2 refresh token.
        cookies (dict): Session cookies used for token refreshing.
        token_manager (TokenManager): Optional manager for persistent underlying storage.
    """

    def __init__(
        self,
        group: Union[Group, str] = Group.HINATAZAKA46,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        cookies: Optional[dict[str, str]] = None,
        app_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        auth_dir: Optional[Union[str, Path]] = None,
        use_token_storage: bool = False
    ):
        """
        Initialize the client.

        Args:
            group: The target sakamichi group (enum or string).
            access_token: The Bearer token for API authentication.
            refresh_token: The Refresh token (if available).
            cookies: Dictionary of browser cookies ("key": "value"), required for refreshing access tokens.
            app_id: The X-Talk-App-ID header value (optional, defaults to group config).
            user_agent: The User-Agent header value (optional, defaults to hardcoded).
            use_token_storage: If True, attempts to load/save credentials using system keyring (or file fallback).

        Raises:
            ValueError: If an invalid group string is provided.
        """
        if isinstance(group, str):
            try:
                group = Group(group.lower())
            except ValueError:
                raise ValueError(f"Invalid group: {group}. Must be one of {[g.value for g in Group]}") from None

        self.group = group
        self.config = GROUP_CONFIG[group]

        self.token_manager = None
        if use_token_storage:
            try:
                self.token_manager = TokenManager()
                # Attempt to auto-load if explicit tokens not provided
                if not access_token:
                    saved = self.token_manager.load_session(self.group.value)
                    if saved:
                        access_token = saved.get("access_token")
                        refresh_token = saved.get("refresh_token") or refresh_token
                        cookies = saved.get("cookies") or cookies
                        logger.info(f"Loaded credentials for {self.group.value} from storage.")
            except Exception as e:
                logger.warning(f"Failed to initialize token storage: {e}")

        self.access_token = access_token
        self.refresh_token = refresh_token
        self.cookies = cookies
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.app_id = app_id or self.config["app_id"]
        self.api_base = self.config["api_base"]
        self.auth_dir = Path(auth_dir) if auth_dir else None

        self.headers = {
            "x-talk-app-id": self.app_id,
            "user-agent": self.user_agent,
            "content-type": "application/json",
            "x-talk-app-platform": "web",
            "origin": self.config["auth_url"].rstrip('/'),
            "referer": self.config["auth_url"],
            "accept": "application/json",
            "accept-language": "ja,en-US;q=0.9,en;q=0.8"
        }
        if self.access_token:
            self.headers["Authorization"] = f"Bearer {self.access_token}"

    async def update_token(self, new_token: str, new_refresh_token: Optional[str] = None) -> None:
        """
        Update the instance's access token and headers.

        Args:
            new_token: The new Bearer token string.
            new_refresh_token: Optional new refresh token.
        """
        self.access_token = new_token
        if new_refresh_token:
            self.refresh_token = new_refresh_token

        self.headers["Authorization"] = f"Bearer {new_token}"
        logger.debug("Access token updated.")

        if self.token_manager:
            try:
                self.token_manager.save_session(
                    self.group.value,
                    self.access_token,
                    self.refresh_token,
                    self.cookies
                )
            except Exception as e:
                logger.warning(f"Failed to auto-save refreshed token: {e}")

    def save_session(self) -> None:
        """Manually save current session to storage if configured."""
        if self.token_manager and self.access_token:
            self.token_manager.save_session(
                self.group.value,
                self.access_token,
                self.refresh_token,
                self.cookies
            )

    async def fetch_json(self, session: aiohttp.ClientSession, endpoint: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """
        Helper method to perform JSON GET requests.

        Args:
            session: Active aiohttp ClientSession.
            endpoint: API endpoint path (e.g. "/groups").
            params: Query parameters.

        Returns:
            JSON response as dict or None if failed.

        Raises:
            ApiError: If the API returns a server error (5xx) or other unhandled status.
        """
        url = f"{self.api_base}{endpoint}"
        try:
            async with session.get(url, headers=self.headers, params=params, ssl=False) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    logger.info(f"Unauthorized (401) at {endpoint}. Attempting automatic token refresh...")
                    if await self.refresh_access_token(session):
                        # Retry the request with new token
                        async with session.get(url, headers=self.headers, params=params, ssl=False) as resp_retry:
                            if resp_retry.status == 200:
                                return await resp_retry.json()
                            elif resp_retry.status == 401:
                                logger.warning(f"Unauthorized at {endpoint} even after refresh.")
                                return None
                            elif resp_retry.status >= 500:
                                raise ApiError(f"Server error {resp_retry.status}", resp_retry.status)
                    return None
                elif resp.status >= 500:
                    logger.error(f"Server error {resp.status} at {endpoint}")
                    raise ApiError(f"Server error {resp.status}", resp.status)
                else:
                    logger.warning(f"Unexpected status {resp.status} at {endpoint}")
                    return None
        except ApiError:
            raise
        except SessionExpiredError as e:
            logger.debug(f"Session expired during request to {endpoint}: {e}")
            raise
        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching {url}: {e}")
            raise ApiError(f"Network error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            return None

    async def refresh_access_token(self, session: aiohttp.ClientSession) -> bool:
        """
        Attempt to refresh the access token using stored cookies.

        Returns:
            True if refresh was successful, False otherwise.
        """
        # Early exit only if ALL refresh methods are unavailable
        if not self.refresh_token and not self.cookies and not (self.auth_dir and self.auth_dir.exists()):
            logger.debug("No credentials (refresh_token/cookies/auth_dir) available for refresh.")
            return False

        url = f"{self.api_base}/update_token"

        # Headers for refresh: exclude Authorization, but keep Platform/Origin/Referer
        refresh_headers = self.headers.copy()
        refresh_headers.pop("Authorization", None)


        # 1. Try refresh_token if available (Plan A - Unused in Web Flow, kept for future mobile support)
        # Note: Web flow sets refresh_token=None, so this block is skipped.
        if self.refresh_token:
            logger.debug("Attempting refresh using refresh_token...")
            try:
                async with session.post(url, headers=refresh_headers, json={"refresh_token": self.refresh_token}, ssl=False) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        new_at = data.get('access_token')
                        if new_at:
                            await self.update_token(new_at, data.get('refresh_token'))
                            logger.info("Token refreshed successfully via refresh_token.")
                            return True
                    elif resp.status in (400, 401):
                        logger.warning(f"refresh_token failed (Status {resp.status}). Falling back...")
            except Exception as e:
                 logger.warning(f"Error during refresh_token attempt: {e}")

        # 2. Try cookies (Web Session) if available
        if self.cookies:
            try:
                # For web sessions, pass cookies with refresh_token:null (as browser does per HAR)
                async with session.post(url, headers=refresh_headers, json={"refresh_token": None}, cookies=self.cookies, ssl=False) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        new_token = data.get('access_token')
                        if new_token:
                            await self.update_token(new_token)

                            # CRITICAL: Capture new session cookies from response
                            # The server rotates the session cookie on each update_token call
                            if resp.cookies:
                                for key, cookie in resp.cookies.items():
                                    self.cookies[key] = cookie.value
                                logger.debug(f"Updated session cookies from response: {list(resp.cookies.keys())}")

                            logger.info("Token refreshed successfully via session cookies.")
                            return True
                    elif resp.status == 400:
                        # Session invalidated (e.g., user logged in from another browser)
                        body = await resp.json()
                        if body.get('code') == 'invalid_parameter':
                            logger.debug("Session invalidated - user may have logged in elsewhere.")
                            raise SessionExpiredError(
                                "Your session has been invalidated. "
                                "This usually happens when you log in from another browser."
                            )
                        logger.warning(f"Cookie refresh failed: {body}")
                    else:
                        logger.warning(f"Cookie refresh failed: Status {resp.status}")
            except SessionExpiredError:
                raise
            except Exception as e:
                logger.warning(f"Cookie refresh attempt failed: {e}")

        # 3. Try Headless Browser (Plan C)
        if self.auth_dir and self.auth_dir.exists():
            try:
                # Lazy import to avoid circular dependency
                from .auth import BrowserAuth
                logger.info("Attempting headless browser refresh (Plan C)...")

                # Check if playwright is installed by trying import, though BrowserAuth import essentially checked it
                creds = await BrowserAuth.refresh_token_headless(self.group, self.auth_dir)
                if creds:
                    # Update local state
                    await self.update_token(creds['access_token'])
                    self.cookies = creds.get('cookies', {})

                    # Persist immediately
                    if self.token_manager:
                        self.token_manager.save_session(self.group.value, self.access_token, self.refresh_token, self.cookies)

                    logger.info("Headless refresh successful!")
                    return True
            except Exception as e:
                logger.warning(f"Headless refresh failed: {e}")

        return False

    async def get_groups(self, session: aiohttp.ClientSession, include_inactive: bool = False) -> list[dict[str, Any]]:
        """
        Fetch all subscribed groups (artists).

        Args:
            session: Active aiohttp ClientSession.
            include_inactive: If True, includes expired/suspended subscriptions.

        Returns:
            List of group objects.
        """
        groups = await self.fetch_json(session, "/groups", {"organization_id": 1})
        if not groups:
            return []

        filtered = []
        for g in groups:
            sub = g.get('subscription')
            if sub:
                state = sub.get('state')
                if state == 'active' or (include_inactive and state in ['expired', 'suspended', 'canceled']):
                    filtered.append(g)
        return filtered

    async def get_members(self, session: aiohttp.ClientSession, group_id: int) -> list[dict[str, Any]]:
        """
        Fetch all members (timelines) within a group.

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/artist.

        Returns:
            List of member objects.
        """
        result = await self.fetch_json(session, f"/groups/{group_id}/members")
        return result or []

    async def get_messages(
        self,
        session: aiohttp.ClientSession,
        group_id: int,
        since_id: Optional[int] = None,
        max_id: Optional[int] = None,
        progress_callback=None
    ) -> list[dict[str, Any]]:
        """
        Fetch all new messages from a group's timeline.
        Automatically handles pagination to retrieve all messages newer than `since_id`.

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/artist.
            since_id: The message ID to start fetching from (exclusive).
            max_id: Ignored by API, kept for compatibility/filtering.
            progress_callback: Optional async function(date_str, count) to call on each page.

        Returns:
            List of message objects sorted by ID ascending.
        """
        all_messages: dict[int, dict[str, Any]] = {}
        page = 0
        current_continuation = None

        while True:
            params: dict[str, Any] = {
                "count": 200,
                "order": "desc"
            }
            if current_continuation:
                params["continuation"] = current_continuation

            if max_id and page == 0 and not current_continuation:
                params["max_id"] = max_id

            data = await self.fetch_json(session, f"/groups/{group_id}/timeline", params)
            if not data:
                if page == 0 and await self.refresh_access_token(session):
                    # Retry once if token was refreshed on first page
                    continue
                break

            messages = data.get('messages', [])
            if not messages:
                break

            # Add to collection
            reached_since_id = False
            for m in messages:
                msg_id = m['id']
                if since_id and msg_id <= since_id:
                    reached_since_id = True
                    break

                all_messages[msg_id] = m

            if progress_callback and messages:
                oldest_in_batch = messages[-1].get('published_at')
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(oldest_in_batch, len(all_messages))
                else:
                    progress_callback(oldest_in_batch, len(all_messages))

            if reached_since_id:
                break

            current_continuation = data.get('continuation')
            if not current_continuation or current_continuation == params.get("continuation"):
                break

            page += 1
            await asyncio.sleep(0.5)

        return sorted(all_messages.values(), key=lambda x: x['id'])

    async def download_file(self, session: aiohttp.ClientSession, url: str, filepath: Path, timestamp: Optional[str] = None) -> bool:
        """
        Download a file from a URL to the local filesystem.

        Args:
            session: Active aiohttp ClientSession.
            url: The download URL.
            filepath: Destination Path object.
            timestamp: Optional ISO timestamp (unused currently, reserved for future use).

        Returns:
            True if successful or already exists, False on failure.
        """
        if not url or filepath.exists():
            return True

        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            async with session.get(url, ssl=False) as resp:
                if resp.status == 200:
                    async with aiofiles.open(filepath, 'wb') as f:
                        await f.write(await resp.read())
                    return True
                else:
                    logger.warning(f"Download failed {resp.status} for {url}")
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
        return False

    async def download_message_media(self, session: aiohttp.ClientSession, message: dict[str, Any], output_dir: Path) -> Optional[Path]:
        """
        Download media associated with a message to the specified directory.

        Args:
            session: Active aiohttp session.
            message: Message dictionary from API.
            output_dir: Root directory for the member.

        Returns:
            Path to the downloaded file, or None if no media/download failed.
        """
        raw_type = message.get('type')
        msg_type = 'text'
        if raw_type in ['image', 'picture']:
            msg_type = 'picture'
        elif raw_type in ['video', 'movie']:
            msg_type = 'video'
        elif raw_type == 'voice':
            msg_type = 'voice'

        media_url = message.get('file') or message.get('thumbnail')
        if not media_url or msg_type == 'text':
            return None

        try:
            ext = get_media_extension(media_url, raw_type)
            target_dir = output_dir / msg_type
            target_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{message['id']}.{ext}"
            filepath = target_dir / filename

            if await self.download_file(session, media_url, filepath):
                return filepath
        except Exception as e:
            logger.error(f"Message media download error: {e}")
            pass

        return None

    async def get_profile(self, session: aiohttp.ClientSession) -> Optional[dict[str, Any]]:
        """
        Fetch the current user's profile.

        Args:
            session: Active aiohttp ClientSession.

        Returns:
            Dict containing profile info (nickname, etc.) or None if failed.
        """
        return await self.fetch_json(session, "/profile")

    async def get_news(self, session: aiohttp.ClientSession, count: int = 20) -> list[dict[str, Any]]:
        """
        Fetch official news (announcements).

        Args:
            session: Active aiohttp ClientSession.
            count: Number of items to fetch (default 20).

        Returns:
            List of news items.
        """
        params = {"platform": "web", "count": count}
        data = await self.fetch_json(session, "/announcements", params)
        if data and "announcements" in data:
            return data["announcements"]
        return []

    async def get_tags(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        """
        Fetch available tags.

        Args:
            session: Active aiohttp ClientSession.

        Returns:
            List of tags.
        """
        data = await self.fetch_json(session, "/tags")
        if data and "tags" in data:
            return data["tags"]
        return []

    async def get_fc_contents(self, session: aiohttp.ClientSession, organization_id: int = 1) -> list[dict[str, Any]]:
        """
        Fetch Fan Club contents.

        Args:
            session: Active aiohttp ClientSession.
            organization_id: Organization ID (default 1).

        Returns:
            List of FC content items.
        """
        data = await self.fetch_json(session, "/fc-contents", {"organization_id": organization_id})
        if data and "contents" in data:
            return data["contents"]
        return []

    async def get_organizations(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        """
        Fetch available organizations.

        Args:
            session: Active aiohttp ClientSession.

        Returns:
            List of organizations.
        """
        data = await self.fetch_json(session, "/organizations")
        if data and "organizations" in data:
            return data["organizations"]
        return []

    async def get_products(self, session: aiohttp.ClientSession, product_type: Optional[str] = None) -> list[dict[str, Any]]:
        """
        Fetch products (subscriptions etc).

        Args:
            session: Active aiohttp ClientSession.
            product_type: Optional filter (e.g. 'subscription', 'fc_subscription').

        Returns:
            List of products.
        """
        params = {}
        if product_type:
            params["type"] = product_type
        data = await self.fetch_json(session, "/products", params)
        if data and "products" in data:
            return data["products"]
        return []
