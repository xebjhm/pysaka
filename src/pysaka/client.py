import asyncio
import contextlib
import math
import os
import re
import time
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Union, overload
from urllib.parse import urlsplit, urlunsplit

import aiofiles
import aiohttp
import structlog

from .credentials import get_token_manager
from .exceptions import ApiError, RateLimitedError, RefreshFailedError, SessionExpiredError
from .utils import get_jwt_remaining_seconds, get_media_extension, media_file_is_present

logger = structlog.get_logger()

# Endpoint path of a group's timeline; the only endpoint ``timeline_min_interval`` paces.
_TIMELINE_ENDPOINT = re.compile(r"^/groups/\d+/timeline$")


def _redact_url(url: str) -> str:
    """Return ``scheme://host[:port]/path`` of ``url`` for logging.

    Signed media URLs carry their credentials in the query string, so the query
    and fragment are dropped, and so is any ``user:password@`` in the authority
    (the host is lowercased; an invalid port is dropped). A URL that cannot be
    parsed is replaced by ``"<unparseable url>"`` so that logging never raises.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if host and port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _int_or_none(value: Any) -> Optional[int]:
    """``value`` if it is a true integer (not a bool), else None — for logging
    server-supplied ids without echoing arbitrary content."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rate_limited(endpoint: str, resp: Any) -> RateLimitedError:
    """Build the error for a 429 response.

    ``retry_after`` is the ``Retry-After`` header as seconds when it is a finite,
    non-negative number, else None (absent, an HTTP date, ``nan``, ``inf``, negative).
    """
    headers = getattr(resp, "headers", None)
    raw = headers.get("Retry-After") if isinstance(headers, Mapping) else None
    ignored: dict[str, str] = {}  # why a present Retry-After header was not used
    try:
        retry_after: Optional[float] = float(raw) if raw is not None else None
    except (TypeError, ValueError) as e:
        retry_after = None
        ignored = {"retry_after_ignored": "unparsable", "error_type": type(e).__name__}
    if retry_after is not None and not (math.isfinite(retry_after) and retry_after >= 0):
        retry_after = None
        ignored = {"retry_after_ignored": "not_finite_or_negative"}
    logger.warning("API rate limited", endpoint=endpoint, retry_after=retry_after, **ignored)
    return RateLimitedError(f"Rate limited at {endpoint}", retry_after)


class Group(Enum):
    HINATAZAKA46 = "hinatazaka46"
    NOGIZAKA46 = "nogizaka46"
    SAKURAZAKA46 = "sakurazaka46"
    YODEL = "yodel"


GROUP_CONFIG = {
    Group.HINATAZAKA46: {
        "api_base": "https://api.message.hinatazaka46.com/v2",
        "app_id": "jp.co.sonymusic.communication.keyakizaka 2.5",
        "auth_url": "https://message.hinatazaka46.com/",
        "display_name": "日向坂46",
        "organization_id": 1,
        "mobile_api_base": "https://api.kh.glastonr.net/v2",
    },
    Group.NOGIZAKA46: {
        "api_base": "https://api.message.nogizaka46.com/v2",
        "app_id": "jp.co.sonymusic.communication.nogizaka 2.5",
        "auth_url": "https://message.nogizaka46.com/",
        "display_name": "乃木坂46",
        "organization_id": 1,
        "mobile_api_base": "https://api.n46.glastonr.net/v2",
    },
    Group.SAKURAZAKA46: {
        "api_base": "https://api.message.sakurazaka46.com/v2",
        "app_id": "jp.co.sonymusic.communication.sakurazaka 2.5",
        "auth_url": "https://message.sakurazaka46.com/",
        "display_name": "櫻坂46",
        "organization_id": 1,
        "mobile_api_base": "https://api.s46.glastonr.net/v2",
    },
    Group.YODEL: {
        "api_base": "https://api.service.yodel-app.com/v2",
        "app_id": "jp.co.sonymusic.communication.yodel 2.5",
        "auth_url": "https://service.yodel-app.com/",
        "display_name": "Yodel",
        "organization_id": None,
        "mobile_api_base": None,
    },
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
        use_token_storage: bool = False,
        platform: str = "web",
        timeline_min_interval: float = 0.0,
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
            platform: Request profile — "web" (default, browser-like) or "android"
                (Flutter app: Dart UA, mobile host, x-talk-app-platform=android).
            timeline_min_interval: Minimum seconds between the starts of two
                ``/groups/{id}/timeline`` requests made through this client
                (default 0.0 = no pacing). See the ``timeline_min_interval`` property.

        Raises:
            ValueError: If an invalid group string is provided, or
                ``timeline_min_interval`` is negative, NaN or infinite.
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
                self.token_manager = get_token_manager()
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
        self.auth_dir = Path(auth_dir) if auth_dir else None

        # Single-flight guard for token refresh (PY-I5): concurrent 401s must not
        # each POST /update_token with the same (soon-to-be-rotated) cookie.
        # PY-CORE-05: created lazily on first use (see _get_refresh_lock) rather
        # than here. On Python 3.9 (still supported) asyncio.Lock() binds the
        # event loop at construction; a `Client(...)` built at import/setup time
        # then awaited under asyncio.run() would raise "bound to a different event
        # loop". Deferring creation to the running loop avoids that.
        self._refresh_lock: Optional[asyncio.Lock] = None
        self._refresh_lock_loop: Optional[asyncio.AbstractEventLoop] = None

        # Timeline pacing (see the ``timeline_min_interval`` property). The lock is
        # created lazily for the same reason as ``_refresh_lock``.
        self._timeline_min_interval = 0.0
        self._timeline_lock: Optional[asyncio.Lock] = None
        self._timeline_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._timeline_last_start: Optional[float] = None
        self._timeline_paced_requests = 0
        self._timeline_waited = 0.0
        self.timeline_min_interval = timeline_min_interval

        # Platform profile: "web" (default) mimics the browser client;
        # "android" mimics the Flutter/Dart app (different UA, host, headers).
        if platform not in ("web", "android"):
            logger.warning(f"Unknown platform '{platform}', defaulting to 'web'.")
            platform = "web"
        self.platform = platform

        self.app_id = app_id or self.config["app_id"]

        # Host: android uses the per-group mobile host when known, else the web host.
        self.api_base = self.config["api_base"]
        if self.platform == "android":
            mobile_base = self.config.get("mobile_api_base")
            if mobile_base:
                self.api_base = mobile_base
            else:
                logger.info(f"No mobile_api_base for {self.group.value}; using web host in android mode.")

        # User-Agent: explicit override wins; else platform default.
        if user_agent:
            self.user_agent = user_agent
        elif self.platform == "android":
            self.user_agent = "Dart/3.7 (dart:io)"
        else:
            self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        self.headers = self._build_headers()
        if self.access_token:
            self.headers["Authorization"] = f"Bearer {self.access_token}"

    @property
    def timeline_min_interval(self) -> float:
        """Minimum seconds between the starts of two timeline requests (0.0 = off).

        While above 0, every HTTP request to ``/groups/{id}/timeline`` made through
        this client (``get_messages`` pages, the ``order=asc`` probe, the 401 retry
        inside ``fetch_json``, ``mark_group_read``) waits so that consecutive request
        starts are at least this many seconds apart, also under concurrent callers.
        Other endpoints and CDN downloads are never paced. May be changed at any time;
        setting a negative, NaN or infinite value raises ``ValueError``. Pacing is per client,
        i.e. per service. Callers that enable it can pass ``page_delay=0`` to
        ``get_messages`` so the pause is not doubled. The spacing is between request
        starts at this client, not on the wire: a slow response or a retry does not
        shift it.
        """
        return self._timeline_min_interval

    @timeline_min_interval.setter
    def timeline_min_interval(self, value: float) -> None:
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"timeline_min_interval must be a finite number >= 0, got {value}")
        old = self._timeline_min_interval
        if value == old:
            return
        if value == 0.0 and self._timeline_paced_requests:
            logger.info(
                "Timeline pacing summary",
                requests=self._timeline_paced_requests,
                waited_seconds=round(self._timeline_waited, 3),
            )
        if old == 0.0:
            self._timeline_paced_requests = 0
            self._timeline_waited = 0.0
        self._timeline_min_interval = value
        logger.debug("Timeline pacing set", interval=value)

    async def _pace_timeline(self, endpoint: str) -> None:
        """Wait until a request to ``endpoint`` may start (timeline endpoints only).

        Serialised with one lock per client; the start time is recorded after any
        wait, so concurrent callers queue up ``timeline_min_interval`` apart.
        """
        if self._timeline_min_interval <= 0 or not _TIMELINE_ENDPOINT.match(endpoint):
            return
        loop = asyncio.get_running_loop()
        if self._timeline_lock is None or self._timeline_lock_loop is not loop:
            # First use, or the client moved to another event loop (e.g. a second
            # ``asyncio.run``): a lock with waiters is bound to its loop. The last
            # start time is kept, so the spacing carries over.
            self._timeline_lock = asyncio.Lock()
            self._timeline_lock_loop = loop
        async with self._timeline_lock:
            interval = self._timeline_min_interval  # may have changed while queued
            if interval > 0:
                if self._timeline_last_start is not None:
                    wait = self._timeline_last_start + interval - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                        self._timeline_waited += wait
                self._timeline_paced_requests += 1
            self._timeline_last_start = time.monotonic()

    def _build_headers(self) -> dict[str, str]:
        """Build request headers for the active platform profile.

        web:     mimics the official browser client (origin/referer, Mozilla UA).
        android: mimics the Flutter app (Dart UA, x-talk-app-platform=android,
                 no origin/referer, JP-first accept-language).
        """
        headers = {
            "x-talk-app-id": self.app_id,
            "user-agent": self.user_agent,
            "content-type": "application/json",
            "accept": "application/json",
        }
        if self.platform == "android":
            headers["x-talk-app-platform"] = "android"
            headers["accept-language"] = "ja-JP;q=1.0,en-US;q=0.9"
        else:
            headers["x-talk-app-platform"] = "web"
            headers["origin"] = self.config["auth_url"].rstrip("/")
            headers["referer"] = self.config["auth_url"]
            headers["accept-language"] = "ja,en-US;q=0.9,en;q=0.8"
        return headers

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
                self.token_manager.save_session(self.group.value, self.access_token, self.refresh_token, self.cookies)
            except Exception as e:
                logger.warning(f"Failed to auto-save refreshed token: {e}")

    def save_session(self) -> None:
        """Manually save current session to storage if configured."""
        if self.token_manager and self.access_token:
            self.token_manager.save_session(self.group.value, self.access_token, self.refresh_token, self.cookies)

    async def fetch_json(
        self, session: aiohttp.ClientSession, endpoint: str, params: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """
        Helper method to perform JSON GET requests.

        Args:
            session: Active aiohttp ClientSession.
            endpoint: API endpoint path (e.g. "/groups"). Requests to
                ``/groups/{id}/timeline``, including the retry after a 401, wait
                first when ``timeline_min_interval`` is above 0.
            params: Query parameters.

        Returns:
            JSON response as dict, or None for any other failure: a client error
            (4xx other than 429), a 401 that the token refresh could not fix, or an
            unexpected error while handling the response.

        Raises:
            ApiError: On a server error (5xx) or a network error / timeout.
            RateLimitedError: On HTTP 429 (also after a token refresh).
            SessionExpiredError: If the session is invalidated during the request.
            RefreshFailedError: If a 401 triggers a token refresh and all refresh
                plans fail (surfaced so callers can prompt re-login).
        """
        url = f"{self.api_base}{endpoint}"
        logger.debug("API GET request", endpoint=endpoint, params=params)
        try:
            await self._pace_timeline(endpoint)
            async with session.get(url, headers=self.headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug("API GET success", endpoint=endpoint, status=200)
                    return data
                elif resp.status == 401:
                    logger.warning("API returned 401 Unauthorized", endpoint=endpoint, will_retry=True)
                    if await self.refresh_access_token(session):
                        # Retry the request with new token
                        await self._pace_timeline(endpoint)
                        async with session.get(url, headers=self.headers, params=params) as resp_retry:
                            if resp_retry.status == 200:
                                return await resp_retry.json()
                            elif resp_retry.status == 401:
                                logger.warning(f"Unauthorized at {endpoint} even after refresh.")
                                return None
                            elif resp_retry.status == 429:
                                raise _rate_limited(endpoint, resp_retry)
                            elif resp_retry.status >= 500:
                                raise ApiError(f"Server error {resp_retry.status}", resp_retry.status)
                    return None
                elif resp.status == 429:
                    raise _rate_limited(endpoint, resp)
                elif resp.status >= 500:
                    logger.error(f"Server error {resp.status} at {endpoint}")
                    raise ApiError(f"Server error {resp.status}", resp.status)
                else:
                    logger.warning(f"Unexpected status {resp.status} at {endpoint}")
                    return None
        except ApiError:
            raise
        except (SessionExpiredError, RefreshFailedError) as e:
            # Propagate auth failures so callers can prompt re-login. Without
            # RefreshFailedError here it would fall into the broad handler below
            # and be swallowed into None — the app would render empty results
            # instead of surfacing that the session died (parity with delete_json).
            logger.debug(f"Auth error during request to {endpoint}: {e}")
            raise
        except aiohttp.ClientError as e:
            # Only the type: aiohttp's text can embed the full (possibly signed) URL.
            logger.error("Network error fetching", url=_redact_url(url), error_type=type(e).__name__)
            raise ApiError(f"Network error: {type(e).__name__}") from e
        except asyncio.TimeoutError as e:
            # PY-CORE-02: aiohttp's TOTAL request timeout raises asyncio.TimeoutError,
            # which is NOT an aiohttp.ClientError subclass. Without this branch a hung
            # request would fall into the broad handler below and be swallowed into
            # None — indistinguishable from "no data" — letting callers treat a
            # transient timeout as an empty result. Surface it as an ApiError so the
            # docstring contract (network problems raise) actually holds.
            logger.error("Timeout fetching", url=_redact_url(url), error_type=type(e).__name__)
            raise ApiError(f"Timeout: {type(e).__name__}") from e
        except Exception as e:
            # Only the type: the response body being handled may be quoted in the text.
            logger.error("Unexpected error fetching", endpoint=endpoint, error_type=type(e).__name__)
            return None

    def _get_refresh_lock(self) -> asyncio.Lock:
        """Return the single-flight refresh lock, creating it lazily.

        PY-CORE-05: the lock is created on first use (inside a running coroutine)
        rather than in ``__init__`` so that on Python 3.9 it binds to the event
        loop that actually awaits it, not whichever loop happened to exist at
        construction time. It is rebuilt when the running loop is not the one it
        was created on (a client reused across ``asyncio.run`` calls), because a
        lock that has waiters is bound to its loop and raises ``RuntimeError`` on
        another.
        """
        loop = asyncio.get_running_loop()
        if self._refresh_lock is None or self._refresh_lock_loop is not loop:
            self._refresh_lock = asyncio.Lock()
            self._refresh_lock_loop = loop
        return self._refresh_lock

    async def refresh_access_token(self, session: aiohttp.ClientSession) -> bool:
        """
        Attempt to refresh the access token (refresh_token → cookies → headless).

        Guarded by an ``asyncio.Lock`` so concurrent 401s do not race and rotate
        the session cookie under each other (single-flight, PY-I5). After
        acquiring the lock, re-checks whether another caller already refreshed
        the token and returns early if so.

        Returns:
            True if a refresh was performed (or another caller already refreshed
            the token while this call waited on the lock); False otherwise.
        """
        # Snapshot the token before we contend for the lock. If it changes while
        # we wait, another concurrent caller already refreshed → skip our attempt.
        token_before_wait = self.access_token
        async with self._get_refresh_lock():
            # PY-CORE-01: The ONLY safe early-exit is "another in-flight caller
            # already refreshed the token while we waited on the lock". Detect
            # that by the token having actually CHANGED, not by the token merely
            # looking valid: a server-revoked-but-unexpired JWT still has plenty
            # of `exp` remaining, and short-circuiting on `remaining > 300` there
            # would re-send the rejected token forever (and silently no-op a
            # consumer's proactive refresh, e.g. SakaDesk's 10-minute refresh).
            # So a genuine refresh request always reaches the server unless a
            # concurrent refresh just succeeded.
            if self.access_token != token_before_wait and self.access_token:
                remaining = self.get_token_expiry_seconds()
                # Only trust the concurrent refresh if it produced a token that
                # isn't itself already inside the danger window (otherwise fall
                # through and refresh again).
                if remaining is None or remaining > 300:
                    logger.debug(
                        "Token already refreshed by another caller; skipping redundant refresh.",
                        remaining_seconds=remaining,
                    )
                    return True
            return await self._perform_refresh(session)

    async def _perform_refresh(self, session: aiohttp.ClientSession) -> bool:
        """Run the actual token refresh flow. Must be called with the refresh lock held.

        See :meth:`refresh_access_token` for the return/raise contract.
        """
        has_refresh_token = bool(self.refresh_token)
        has_cookies = bool(self.cookies)
        has_auth_dir = bool(self.auth_dir and self.auth_dir.exists())
        cookie_keys = list(self.cookies.keys()) if self.cookies else []

        # Cookies are a WEB-only refresh credential. In android mode we never send web
        # session cookies (absolute fingerprint purity), so treat them as unusable here.
        cookies_usable = bool(self.cookies) and self.platform == "web"

        logger.info(
            "Token refresh attempt starting",
            has_refresh_token=has_refresh_token,
            has_cookies=has_cookies,
            has_auth_dir=has_auth_dir,
            cookie_keys=cookie_keys,
            current_token_expiry_seconds=self.get_token_expiry_seconds(),
        )

        # Early exit only if ALL refresh methods are unavailable.
        # PY-CORE-06: a token-only Client whose token expired has no way to
        # refresh — surface that as an auth error so callers can prompt re-login,
        # instead of returning False → fetch_json None → silent empty results
        # forever. Mirrors the "all plans failed" RefreshFailedError below.
        if not self.refresh_token and not cookies_usable and not (self.auth_dir and self.auth_dir.exists()):
            logger.warning("No credentials (refresh_token/cookies/auth_dir) available for refresh.")
            raise RefreshFailedError(
                "The access token expired and no refresh credentials "
                "(refresh_token/cookies/auth_dir) are available. Please log in again."
            )

        url = f"{self.api_base}/update_token"

        # Headers for refresh: exclude Authorization, but keep Platform/Origin/Referer
        refresh_headers = self.headers.copy()
        refresh_headers.pop("Authorization", None)

        # 1. Plan A — refresh_token grant. Used by mobile/android mode, and now also by
        # web sessions when the /signin response carried a refresh_token (captured at login).
        if self.refresh_token:
            logger.debug("Attempting refresh using refresh_token...")
            try:
                async with session.post(
                    url, headers=refresh_headers, json={"refresh_token": self.refresh_token}
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        new_at = data.get("access_token")
                        if new_at:
                            await self.update_token(new_at, data.get("refresh_token"))
                            logger.info("Token refreshed successfully via refresh_token.")
                            return True
                    elif resp.status in (400, 401):
                        logger.warning(f"refresh_token failed (Status {resp.status}). Falling back...")
            except Exception as e:
                logger.warning(f"Error during refresh_token attempt: {e}")

        # 2. Try cookies (Web Session) if available — WEB platform only (purity)
        if cookies_usable:
            logger.debug(
                "Attempting cookie-based refresh (Plan B)",
                cookie_count=len(self.cookies),
                session_cookie_present="session" in self.cookies,
            )
            try:
                # For web sessions, pass cookies with refresh_token:null
                async with session.post(
                    url, headers=refresh_headers, json={"refresh_token": None}, cookies=self.cookies
                ) as resp:
                    # Safe cookie key extraction (handles both real responses and mocks)
                    try:
                        response_cookie_keys = list(resp.cookies.keys()) if resp.cookies else []
                    except (TypeError, AttributeError):
                        response_cookie_keys = []

                    logger.info(
                        "Cookie refresh response received", status=resp.status, response_cookies=response_cookie_keys
                    )

                    if resp.status == 200:
                        data = await resp.json()
                        new_token = data.get("access_token")
                        if new_token:
                            old_expiry = self.get_token_expiry_seconds()

                            # CRITICAL (PY-C2): Capture the rotated session cookies
                            # from the response BEFORE persisting. The server
                            # rotates the session cookie on each update_token call;
                            # persisting before capture would store the
                            # already-consumed cookie, forcing a re-login on the
                            # next restart.
                            cookies_updated = []
                            if resp.cookies:
                                for key, cookie in resp.cookies.items():
                                    self.cookies[key] = cookie.value
                                    cookies_updated.append(key)
                                logger.info(
                                    "Session cookies updated from response",
                                    updated_cookies=cookies_updated,
                                    new_cookie_count=len(self.cookies),
                                )

                            # Persist token + freshly-rotated cookies together.
                            await self.update_token(new_token)
                            new_expiry = self.get_token_expiry_seconds()

                            logger.info(
                                "Token refreshed successfully via session cookies",
                                old_expiry_seconds=old_expiry,
                                new_expiry_seconds=new_expiry,
                            )
                            return True
                    elif resp.status == 400:
                        # Session invalidated (e.g., user logged in from another browser)
                        body = await resp.json()
                        logger.warning(
                            "Cookie refresh returned 400",
                            response_code=body.get("code"),
                            response_message=body.get("message"),
                        )
                        if body.get("code") == "invalid_parameter":
                            logger.error(
                                "Session invalidated - user may have logged in elsewhere or session expired on server."
                            )
                            raise SessionExpiredError(
                                "Your session has been invalidated. "
                                "This usually happens when you log in from another browser."
                            )
                        logger.warning(f"Cookie refresh failed: {body}")
                    elif resp.status == 401:
                        logger.warning("Cookie refresh returned 401 - session cookies may be expired")
                    else:
                        # PY-SEC-03: do NOT log the raw /update_token body — this
                        # endpoint echoes fresh access_token/refresh_token, and a
                        # non-standard status (e.g. a 403/5xx that still carries a
                        # token) would write credential material to debug.log. Log
                        # only the status; the 400 branch above already surfaces
                        # the safe allowlisted fields (code/message).
                        logger.warning(
                            "Cookie refresh failed with unexpected status",
                            status=resp.status,
                        )
            except SessionExpiredError:
                raise
            except Exception as e:
                logger.error(f"Cookie refresh attempt failed with exception: {e}", exc_info=True)

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
                    await self.update_token(creds["access_token"])
                    self.cookies = creds.get("cookies", {})

                    # Persist immediately
                    if self.token_manager:
                        self.token_manager.save_session(
                            self.group.value, self.access_token, self.refresh_token, self.cookies
                        )

                    logger.info("Headless refresh successful!")
                    return True
            except Exception as e:
                logger.warning(f"Headless refresh failed: {e}")

        # All refresh plans exhausted - this is unexpected and should be reported
        logger.error("All token refresh plans failed - raising RefreshFailedError")
        raise RefreshFailedError(
            "All token refresh attempts failed unexpectedly. Please log in again to continue using the service."
        )

    def get_token_expiry_seconds(self) -> Optional[int]:
        """
        Extract expiry time from the current access token (JWT).

        Parses the JWT payload to extract the 'exp' claim and calculates
        seconds remaining until expiry. Does not verify the token signature.

        Returns:
            Seconds remaining until token expiry (can be negative if expired),
            or None if no token is set or token cannot be parsed.

        Example:
            >>> remaining = client.get_token_expiry_seconds()
            >>> if remaining is not None and remaining < 300:
            ...     # Token expires in less than 5 minutes
            ...     await client.refresh_access_token(session)
        """
        if not self.access_token:
            return None
        return get_jwt_remaining_seconds(self.access_token)

    async def refresh_if_needed(self, session: aiohttp.ClientSession, min_seconds_remaining: int = 300) -> bool:
        """
        Refresh token only if it expires within min_seconds_remaining.

        This implements "lazy refresh" - avoiding unnecessary API calls when
        the token is still valid for a reasonable time.

        Args:
            session: Active aiohttp ClientSession.
            min_seconds_remaining: Threshold in seconds. Refresh if token
                expires within this time. Default: 300 (5 minutes).

        Returns:
            True if refresh happened, False if skipped (token still valid).

        Raises:
            SessionExpiredError: If refresh fails due to invalidated session.
            RefreshFailedError: If the token is expired and no refresh path is
                available (no refresh_token/cookies/auth_dir), or all refresh
                attempts fail. Callers doing proactive refresh should catch this
                (in addition to SessionExpiredError) and prompt re-login.
        """
        remaining = self.get_token_expiry_seconds()

        logger.info(
            "Checking if token refresh needed",
            remaining_seconds=remaining,
            threshold_seconds=min_seconds_remaining,
            token_present=bool(self.access_token),
            cookies_present=bool(self.cookies),
            cookie_count=len(self.cookies) if self.cookies else 0,
        )

        if remaining is None:
            # Can't parse expiry, refresh to be safe
            logger.warning("Cannot parse token expiry (JWT decode failed), refreshing conservatively")
            return await self.refresh_access_token(session)

        if remaining <= 0:
            logger.warning("Token already expired!", expired_by_seconds=abs(remaining))
            return await self.refresh_access_token(session)

        if remaining <= min_seconds_remaining:
            logger.info(
                "Token expires soon, triggering refresh",
                remaining_seconds=remaining,
                threshold_seconds=min_seconds_remaining,
            )
            return await self.refresh_access_token(session)

        logger.debug(
            "Token still valid, skipping refresh",
            remaining_seconds=remaining,
            remaining_minutes=round(remaining / 60, 1),
            threshold_seconds=min_seconds_remaining,
        )
        return False

    async def get_groups(self, session: aiohttp.ClientSession, include_inactive: bool = False) -> list[dict[str, Any]]:
        """
        Fetch all subscribed groups (artists).

        Args:
            session: Active aiohttp ClientSession.
            include_inactive: If True, includes expired/suspended subscriptions
                              for open groups.

        Returns:
            List of group objects. Includes closed groups (state='closed')
            for graduation detection — callers should skip syncing these
            as their timeline/members endpoints return 404.

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        org_id = self.config.get("organization_id")
        params = {"organization_id": org_id} if org_id is not None else None
        groups = await self.fetch_json(session, "/groups", params)
        if not groups:
            return []

        filtered = []
        for g in groups:
            # Always include closed groups for graduation detection.
            # Callers must skip syncing these (timeline/members return 404).
            if g.get("state") == "closed":
                filtered.append(g)
                continue

            # Open groups: check subscription state
            sub = g.get("subscription")
            sub_state = sub.get("state") if sub else None

            if sub_state == "active":
                filtered.append(g)
            elif include_inactive and sub_state is not None:
                # Include expired/suspended open groups the user previously subscribed to.
                # Skip groups with no subscription (never subscribed — avoids pulling
                # all unsubscribed orgs when organization_id is omitted for Yodel).
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

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        result = await self.fetch_json(session, f"/groups/{group_id}/members")
        return result or []

    async def _timeline_first_id(self, session: aiohttp.ClientSession, group_id: int) -> Optional[int]:
        """Id of the room's very first message (``order=asc``, one item), or None
        if the probe failed. Used to tell the true start of a timeline apart from
        a dropped continuation on a full page."""
        data = await self.fetch_json(
            session,
            f"/groups/{group_id}/timeline",
            {"count": 1, "order": "asc", "clear_unread": "false"},
        )
        messages = (data or {}).get("messages") or []
        return messages[0].get("id") if messages else None

    async def get_messages(
        self,
        session: aiohttp.ClientSession,
        group_id: int,
        since_id: Optional[int] = None,
        since_ts: Optional[str] = None,
        max_id: Optional[int] = None,
        progress_callback=None,
        clear_unread: bool = False,
        page_delay: float = 0.5,
    ) -> list[dict[str, Any]]:
        """
        Fetch all new messages from a group's timeline.

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/artist.
            since_id: (Deprecated) Message ID cursor. Ignored when since_ts is set.
            since_ts: ISO timestamp cursor. Fetch messages published after this time.
            max_id: Ignored by API, kept for compatibility.
            progress_callback: Optional async function(date_str, count).
            clear_unread: If True, the server clears the account's unread badge for
                this group (the official app sends this on chat-open). Defaults to
                False so a background sync does NOT zero the user's unread count on
                the official mobile app.
            page_delay: Seconds to wait between page requests (default 0.5). To stay
                under the API rate limit during long full-history fetches, set
                ``timeline_min_interval`` on the client instead (and pass
                ``page_delay=0`` so the pause is not doubled).

        Returns:
            List of message dicts, sorted by ID ascending. When the timeline ends
            on a partial page (no continuation, or one echoing its continuation)
            before reaching the cursor (``since_ts``, or one found via
            ``since_id``), that is the start of the timeline and every fetched
            message is returned. A full last page without a continuation is also
            accepted as the start of the timeline when the room's first message
            (probed with ``order=asc``) has the smallest id among the collected
            messages; merely being collected is not enough.

        Raises:
            ApiError: If pagination stops in a way that could hide unfetched
                messages — a failed page fetch or an empty page carrying a
                continuation after earlier pages were collected, or, while the
                cursor is still unreached: a full page with a repeated
                continuation (always), a full page with a missing continuation
                (unless the ``order=asc`` probe confirms the timeline start), or
                a page that replays already-collected messages.
            RateLimitedError: On HTTP 429 for any page (a subclass of
                ``ApiError``), instead of returning ``[]`` or a partial list.
        """
        all_messages: dict[int, dict[str, Any]] = {}
        page = 0
        current_continuation = None

        # Resolve the effective timestamp cursor.
        # When since_ts is given, use it directly.
        # When only since_id is given (legacy), we must discover the timestamp
        # by finding the since_id message in the stream (old behaviour).
        effective_ts: Optional[str] = since_ts
        need_discover_ts = since_id is not None and since_ts is None
        first_page_retried = False

        while True:
            params: dict[str, Any] = {
                "count": 200,
                "order": "desc",
                # Defaults to false so a background sync does NOT advance the
                # server-side read pointer (which would zero the user's unread
                # badge on the official mobile app); only the caller opting in
                # (e.g. mark_group_read) advances it.
                "clear_unread": "true" if clear_unread else "false",
            }
            if current_continuation:
                params["continuation"] = current_continuation

            if max_id and page == 0 and not current_continuation:
                params["max_id"] = max_id

            data = await self.fetch_json(session, f"/groups/{group_id}/timeline", params)
            if not data:
                if page == 0 and not first_page_retried and await self.refresh_access_token(session):
                    first_page_retried = True
                    continue
                # fetch_json returns None only on a genuine error (auth/unexpected
                # status). If we have already collected earlier (newer) pages and
                # have NOT reached the cursor/end, returning that partial newest-only
                # set would let the caller advance its timestamp cursor PAST the
                # un-fetched older-but-still-new messages — silent data loss. Fail
                # closed so the sync is retried and the cursor is left untouched.
                if all_messages:
                    raise ApiError(
                        f"Timeline pagination for group {group_id} aborted before "
                        f"reaching the cursor after {len(all_messages)} message(s); "
                        "refusing to return a partial page that would skip a gap."
                    )
                break

            messages = data.get("messages", [])
            if not messages:
                # An empty page mid-pagination (earlier/newer pages already
                # collected) that STILL carries a continuation means the server
                # claims more data exists but returned nothing — breaking here and
                # returning the partial newest set would advance the cursor past the
                # un-fetched gap. Fail closed, mirroring the None-page guard above.
                # An empty page with no continuation is a legitimate end-of-timeline.
                if all_messages and data.get("continuation"):
                    raise ApiError(
                        f"Timeline pagination for group {group_id} returned an empty "
                        f"page with a continuation after {len(all_messages)} "
                        "message(s); refusing to skip a possible gap."
                    )
                break

            known_before_page = len(all_messages)
            for m in messages:
                all_messages[m["id"]] = m

                # Legacy path: discover timestamp of since_id message
                if need_discover_ts and m["id"] == since_id:
                    effective_ts = m.get("published_at")
                    need_discover_ts = False

            if progress_callback and messages:
                oldest_in_batch = messages[-1].get("published_at")
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(oldest_in_batch, len(all_messages))
                else:
                    progress_callback(oldest_in_batch, len(all_messages))

            # Stop condition: oldest message in this batch is older than cursor
            if effective_ts and messages:
                oldest_timestamp = messages[-1].get("published_at", "")
                if oldest_timestamp and oldest_timestamp <= effective_ts:
                    break

            current_continuation = data.get("continuation")
            oldest_timestamp = messages[-1].get("published_at", "")
            before_cursor = bool(effective_ts and oldest_timestamp and oldest_timestamp > effective_ts)
            # Cursor not reached yet: a timestamp cursor still older than this page,
            # or a legacy since_id whose message has not been seen yet.
            cursor_unreached = before_cursor or need_discover_ts
            repeated = bool(current_continuation) and current_continuation == params.get("continuation")
            if not current_continuation or repeated:
                # The server offers no further page. That is the genuine START of the
                # timeline only on a partial page (fewer than ``count``) — whether the
                # server omits the token or echoes it — and only if the page is not a
                # replay of messages already collected. A FULL page means more data
                # should follow (the token was dropped), and a replay means the server
                # ignored the token; breaking on either would let the caller advance
                # its cursor past everything older, so fail closed while the cursor is
                # unreached and retry next sync.
                replayed = repeated and len(all_messages) == known_before_page
                timeline_start = len(messages) < params["count"] and not replayed
                probe_detail = ""
                if cursor_unreached and not timeline_start and not replayed and not repeated:
                    # A FULL last page with no continuation is ambiguous: dropped
                    # token, or a history that is an exact multiple of the page
                    # size. Ask the server for the room's first message; if we
                    # already hold it as our OLDEST message, nothing older exists
                    # (KNOWN_BUGS #1). Ids grow over time, so the room's first
                    # message must be the smallest id collected; merely holding it
                    # would also accept a server that ignores ``order=asc`` and
                    # answers with the newest message.
                    first_id = await self._timeline_first_id(session, group_id)
                    oldest_id = min(all_messages) if all_messages else None
                    timeline_start = first_id is not None and oldest_id is not None and first_id == oldest_id
                    if not timeline_start:
                        probe_first_id, oldest_collected_id = _int_or_none(first_id), _int_or_none(oldest_id)
                        logger.warning(
                            "Timeline start probe rejected",
                            group_id=group_id,
                            probe_first_id=probe_first_id,
                            oldest_collected_id=oldest_collected_id,
                        )
                        probe_detail = (
                            f" (order=asc probe: probe_first_id={probe_first_id}, "
                            f"oldest_collected_id={oldest_collected_id})"
                        )
                if cursor_unreached and not timeline_start:
                    raise ApiError(
                        f"Timeline pagination for group {group_id} ended (missing or "
                        f"repeated continuation) on a full or replayed page while the "
                        f"cursor (since_ts={effective_ts}, since_id={since_id}) is "
                        f"still unreached{probe_detail}; refusing to skip a possible gap."
                    )
                if before_cursor:
                    # Routine, not a gap: e.g. a caller's overlap window subtracted
                    # from a cursor pinned at the room's oldest message. Nothing older
                    # exists, so raising would fail every sync and freeze the room.
                    logger.warning(
                        "Timeline starts after cursor",
                        group_id=group_id,
                        oldest=oldest_timestamp,
                        cursor=effective_ts,
                    )
                elif repeated and not timeline_start:
                    logger.warning(
                        "Timeline pagination stuck without a cursor; returning collected pages",
                        group_id=group_id,
                        collected=len(all_messages),
                    )
                break

            page += 1
            await asyncio.sleep(page_delay)

        # Filter: keep only messages newer than the cursor
        if effective_ts:
            all_messages = {k: v for k, v in all_messages.items() if (v.get("published_at") or "") >= effective_ts}
        elif since_id:
            # Pure legacy fallback (no timestamp available at all)
            all_messages = {k: v for k, v in all_messages.items() if k > since_id}

        return sorted(all_messages.values(), key=lambda x: x["id"])

    async def download_file(
        self,
        session: aiohttp.ClientSession,
        url: str,
        filepath: Path,
        timestamp: Optional[str] = None,
        *,
        retries: int = 3,
    ) -> bool:
        """
        Download a file to disk safely: write to a ``*.part`` temp then atomically
        rename. Validates the body is non-empty and (when the server sends
        ``Content-Length`` and no ``Content-Encoding``) matches the declared size.
        The length check is skipped when the response carries a
        ``Content-Encoding`` header (e.g. gzip/deflate/br), since aiohttp
        transparently decompresses the body while ``Content-Length`` still
        reflects the compressed wire size. Retries transient failures
        with a short backoff. On ultimate failure leaves NO file behind — a prior
        truncated/0-byte stub is treated as missing and re-downloaded.

        Args:
            session: Active aiohttp ClientSession.
            url: The download URL.
            filepath: Destination Path object.
            timestamp: Optional ISO timestamp (reserved; unused).
            retries: Max attempts before giving up.

        Returns:
            True if a non-empty file exists at ``filepath`` afterward, else False.
        """
        if not url:
            return True
        # A zero-byte stub from a past truncated write is NOT complete.
        if media_file_is_present(filepath):
            return True

        filepath.parent.mkdir(parents=True, exist_ok=True)
        tmp = filepath.with_name(filepath.name + ".part")
        last_err: Optional[str] = None
        safe_url = _redact_url(url)

        for attempt in range(retries):
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        last_err = f"status {resp.status}"
                        logger.warning("Download failed", status=resp.status, url=safe_url)
                    else:
                        data = await resp.read()
                        declared = resp.headers.get("Content-Length")
                        encoding = resp.headers.get("Content-Encoding")
                        if not data:
                            last_err = "empty body"
                        elif (
                            encoding is None
                            and declared is not None
                            and declared.isdigit()
                            and int(declared) != len(data)
                        ):
                            last_err = f"size mismatch got={len(data)} declared={declared}"
                        else:
                            async with aiofiles.open(tmp, "wb") as f:
                                await f.write(data)
                            os.replace(tmp, filepath)
                            return True
            except Exception as e:  # noqa: BLE001 - transient network errors are retried
                # str(e) can embed the signed URL, so only the exception type is kept.
                last_err = type(e).__name__
                logger.warning("Download error (will retry)", url=safe_url, error_type=last_err, attempt=attempt + 1)

            if attempt < retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))

        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        logger.error("Download ultimately failed", url=safe_url, error_type=last_err)
        return False

    async def download_message_media(
        self, session: aiohttp.ClientSession, message: dict[str, Any], output_dir: Path
    ) -> Optional[Path]:
        """
        Download media associated with a message to the specified directory.

        Args:
            session: Active aiohttp session.
            message: Message dictionary from API.
            output_dir: Root directory for the member.

        Returns:
            Path to the downloaded file, or None if no media/download failed.
        """
        raw_type = message.get("type")
        msg_type = "text"
        if raw_type in ["image", "picture"]:
            msg_type = "picture"
        elif raw_type in ["video", "movie"]:
            msg_type = "video"
        elif raw_type == "voice":
            msg_type = "voice"

        media_url = message.get("file") or message.get("thumbnail")
        if not media_url or msg_type == "text":
            return None

        try:
            # PY-SEC-01: message["id"] is server-controlled and flows straight
            # into an on-disk filename. Coerce it to an int so a tampered/
            # malicious id (e.g. "../../evil") cannot escape target_dir via path
            # traversal; a non-integer id means we can't safely name the file.
            try:
                message_id = int(message["id"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping media with non-integer message id", message_id=message.get("id"))
                return None

            ext = get_media_extension(media_url, raw_type)
            target_dir = output_dir / msg_type
            target_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{message_id}.{ext}"
            filepath = target_dir / filename

            if await self.download_file(session, media_url, filepath):
                return filepath
        except Exception as e:
            logger.error("Message media download error", error_type=type(e).__name__)

        return None

    async def get_profile(self, session: aiohttp.ClientSession) -> Optional[dict[str, Any]]:
        """
        Fetch the current user's profile.

        Args:
            session: Active aiohttp ClientSession.

        Returns:
            Dict containing profile info (nickname, etc.), or None when the
            request failed without raising (e.g. a 4xx other than 429; see
            ``fetch_json``).

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning None.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
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

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        params = {"platform": self.platform, "count": count}
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

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
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

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
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

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        data = await self.fetch_json(session, "/organizations")
        if data and "organizations" in data:
            return data["organizations"]
        return []

    async def get_products(
        self, session: aiohttp.ClientSession, product_type: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """
        Fetch products (subscriptions etc).

        Args:
            session: Active aiohttp ClientSession.
            product_type: Optional filter (e.g. 'subscription', 'fc_subscription').

        Returns:
            List of products.

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        params = {}
        if product_type:
            params["type"] = product_type
        data = await self.fetch_json(session, "/products", params)
        if data and "products" in data:
            return data["products"]
        return []

    # -------------------------------------------------------------------------
    # Additional API methods (official-app feature parity)
    # -------------------------------------------------------------------------

    async def post_json(
        self, session: aiohttp.ClientSession, endpoint: str, data: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """
        Helper method to perform JSON POST requests.

        Args:
            session: Active aiohttp ClientSession.
            endpoint: API endpoint path (e.g. "/messages/123/favorite").
            data: Request body as dict (can be None for empty body).

        Returns:
            JSON response as dict or None if failed.
        """
        url = f"{self.api_base}{endpoint}"
        logger.debug("API POST request", endpoint=endpoint, data_keys=list(data.keys()) if data else None)
        try:
            async with session.post(url, headers=self.headers, json=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    logger.debug("API POST success", endpoint=endpoint, status=200)
                    return result
                elif resp.status == 401:
                    logger.warning("API POST returned 401 Unauthorized", endpoint=endpoint, will_retry=True)
                    if await self.refresh_access_token(session):
                        async with session.post(url, headers=self.headers, json=data) as resp_retry:
                            if resp_retry.status == 200:
                                return await resp_retry.json()
                    return None
                elif resp.status >= 500:
                    raise ApiError(f"Server error {resp.status}", resp.status)
                else:
                    logger.warning(f"Unexpected status {resp.status} at POST {endpoint}")
                    return None
        except ApiError:
            raise
        except aiohttp.ClientError as e:
            logger.error("Network error posting", url=_redact_url(url), error_type=type(e).__name__)
            raise ApiError(f"Network error: {type(e).__name__}") from e

    async def delete_json(self, session: aiohttp.ClientSession, endpoint: str) -> bool:
        """
        Helper method to perform DELETE requests.

        Args:
            session: Active aiohttp ClientSession.
            endpoint: API endpoint path.

        Returns:
            True if successful (2xx), False otherwise.
        """
        url = f"{self.api_base}{endpoint}"
        try:
            async with session.delete(url, headers=self.headers) as resp:
                if resp.status in (200, 204):
                    return True
                elif resp.status == 401:
                    if await self.refresh_access_token(session):
                        async with session.delete(url, headers=self.headers) as resp_retry:
                            return resp_retry.status in (200, 204)
                    return False
                else:
                    logger.warning(f"DELETE {endpoint} returned {resp.status}")
                    return False
        except (SessionExpiredError, RefreshFailedError):
            raise
        except Exception as e:
            logger.error("Error deleting", url=_redact_url(url), error_type=type(e).__name__)
            return False

    async def get_letters(
        self, session: aiohttp.ClientSession, group_id: int, updated_from: Optional[str] = None, count: int = 200
    ) -> list[dict[str, Any]]:
        """
        Fetch user's sent letters/cards to a member.

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/member.
            updated_from: ISO timestamp to fetch letters updated after.
            count: Number of letters to fetch (default 200).

        Returns:
            List of letter objects.

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]``.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        params: dict[str, Any] = {"count": count}
        if updated_from:
            params["updated_from"] = updated_from

        data = await self.fetch_json(session, f"/groups/{group_id}/letters", params)
        if data and "letters" in data:
            return data["letters"]
        return []

    @overload
    async def get_past_messages(
        self, session: aiohttp.ClientSession, group_id: int, *, strict: Literal[False] = ...
    ) -> list[dict[str, Any]]: ...

    @overload
    async def get_past_messages(
        self, session: aiohttp.ClientSession, group_id: int, *, strict: Literal[True]
    ) -> Optional[list[dict[str, Any]]]: ...

    @overload
    async def get_past_messages(
        self, session: aiohttp.ClientSession, group_id: int, *, strict: bool
    ) -> Optional[list[dict[str, Any]]]: ...

    async def get_past_messages(
        self, session: aiohttp.ClientSession, group_id: int, *, strict: bool = False
    ) -> Optional[list[dict[str, Any]]]:
        """
        Fetch historical messages (before user's subscription start date).

        Returns the roughly 1–1.5 days of messages immediately before the
        account's first subscription to the room. The server keeps returning
        them after the subscription lapses or is renewed. Does NOT mark messages
        as read.

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/member.
            strict: If True, return None when the request failed or the response
                has no ``messages`` list (missing or not a list), so callers can
                tell a failure apart from a room with no earlier messages.

        Returns:
            List of historical message objects (possibly empty); None only when
            ``strict`` is True and the request failed or returned no
            ``messages`` list.

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning ``[]`` (or None with ``strict``).
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        data = await self.fetch_json(session, f"/groups/{group_id}/past_messages")
        if strict:
            messages = data.get("messages") if isinstance(data, dict) else None
            return messages if isinstance(messages, list) else None
        if data and "messages" in data:
            return data["messages"]
        return []

    async def mark_group_read(self, session: aiohttp.ClientSession, group_id: int) -> bool:
        """Clear the account's unread count for a group on the server.

        Sends a minimal timeline fetch with ``clear_unread=true`` — the same
        signal the official app sends when you open a room. Call this only when
        the user opens a conversation and has opted in to syncing read status to
        their phone. A background sync must NOT call this (use ``get_messages``,
        which defaults to ``clear_unread=false``).

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/member room to mark read.

        Returns:
            True if the request succeeded; False when it failed without raising
            (e.g. a 4xx other than 429; see ``fetch_json``).

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning False.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        params: dict[str, Any] = {"count": 1, "order": "desc", "clear_unread": "true"}
        data = await self.fetch_json(session, f"/groups/{group_id}/timeline", params)
        return data is not None

    async def get_subscription_streak(self, session: aiohttp.ClientSession, group_id: int) -> Optional[dict[str, Any]]:
        """
        Fetch consecutive subscription days for a member.

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/member.

        Returns:
            Dict with streak information, or None when the request failed
            without raising (e.g. a 4xx other than 429; see ``fetch_json``).

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning None.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        return await self.fetch_json(session, f"/groups/{group_id}/consecutive-subscription-day")

    async def get_member(self, session: aiohttp.ClientSession, member_id: int) -> Optional[dict[str, Any]]:
        """
        Fetch one member's public profile (``GET /members/{id}``).

        Works for members who are no longer in any room's member list (e.g. a
        graduated member who once posted in an official room).

        Args:
            session: Active aiohttp ClientSession.
            member_id: The member's id.

        Returns:
            The member dict (``id``, ``name``, ``thumbnail``, ``phone_image``, ...),
            or None when the request failed without raising (e.g. a 4xx other
            than 429; see ``fetch_json``) or the body is not a JSON object.

        Raises:
            ApiError: On a server error (5xx) or a network error.
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``).
            SessionExpiredError: If the session is invalidated during the request.
            RefreshFailedError: If a 401 triggers a token refresh and it fails.
        """
        data = await self.fetch_json(session, f"/members/{member_id}")
        if data is not None and not isinstance(data, dict):
            logger.warning(
                "Member lookup returned a non-object body", member_id=member_id, found_type=type(data).__name__
            )
        return data if isinstance(data, dict) else None

    async def get_account(self, session: aiohttp.ClientSession) -> Optional[dict[str, Any]]:
        """
        Fetch user account information.

        Args:
            session: Active aiohttp ClientSession.

        Returns:
            Account info dict, or None when the request failed without raising
            (e.g. a 4xx other than 429; see ``fetch_json``).

        Raises:
            RateLimitedError: On HTTP 429 (a subclass of ``ApiError``), instead of
                returning None.
            ApiError: On a server error (5xx) or a network error.
            SessionExpiredError, RefreshFailedError: auth failures (from ``fetch_json``).
        """
        return await self.fetch_json(session, "/account")

    async def add_favorite(self, session: aiohttp.ClientSession, message_id: int) -> bool:
        """
        Add a message to favorites (server-side).

        Args:
            session: Active aiohttp ClientSession.
            message_id: The ID of the message to favorite.

        Returns:
            True if successful, False otherwise.
        """
        result = await self.post_json(session, f"/messages/{message_id}/favorite", data=None)
        # API returns {} on success
        return result is not None

    async def remove_favorite(self, session: aiohttp.ClientSession, message_id: int) -> bool:
        """
        Remove a message from favorites (server-side).

        Args:
            session: Active aiohttp ClientSession.
            message_id: The ID of the message to unfavorite.

        Returns:
            True if successful, False otherwise.
        """
        return await self.delete_json(session, f"/messages/{message_id}/favorite")
