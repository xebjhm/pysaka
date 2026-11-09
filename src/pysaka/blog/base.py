"""Base classes for blog scraping across all groups."""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

_JP_DATE_RE = re.compile(r"^\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*$")
_SLASH_DATE_RE = re.compile(r"^\s*(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*$")

#: Max concurrent profile-page requests when collecting birthdays.
BIRTHDAY_CONCURRENCY = 4
#: Pause (seconds) after each profile-page request, to stay polite.
BIRTHDAY_REQUEST_DELAY = 0.2


def parse_jp_birthday(text: str) -> str | None:
    """Parse ``YYYY年M月D日`` or ``YYYY/M/D`` into ``YYYY-MM-DD``.

    Args:
        text: Raw date text from an official site.

    Returns:
        ISO date string, or ``None`` if the text is not a valid date.
    """
    match = _JP_DATE_RE.match(text or "") or _SLASH_DATE_RE.match(text or "")
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


class BlogGoneError(Exception):
    """Raised when a blog post has been permanently removed (HTTP 404/410)."""

    pass


@dataclass
class MemberInfo:
    """Member information with thumbnail URL.

    Used by scrapers that support fetching member lists with profile images.

    Attributes:
        id: Member ID (ct parameter for blogs).
        name: Member name in Japanese.
        thumbnail_url: URL to member's profile image on CDN.
    """

    id: str
    name: str
    thumbnail_url: str


@dataclass
class BlogEntry:
    """Represents a single blog post from any group's official site.

    Attributes:
        id: Unique identifier for the blog post.
        title: Title of the blog post.
        content: HTML content of the blog post.
        published_at: Publication timestamp, or ``None`` when the source date
            could not be parsed (PY-MGR-04: scrapers never fabricate ``now()``
            for archival data — an unparseable date surfaces as ``None`` so
            consumers can decide rather than silently storing a wrong date).
        url: Full URL to the original blog post.
        images: List of image URLs found in the content.
        member_id: ID of the member who wrote the post.
        member_name: Name of the member who wrote the post.
    """

    id: str
    title: str
    content: str
    published_at: datetime | None
    url: str
    images: list[str] = field(default_factory=list)
    member_id: str = ""
    member_name: str = ""


class BaseBlogScraper(ABC):
    """Abstract base class for group-specific blog scrapers.

    Each group (Hinatazaka, Nogizaka, Sakurazaka) has a different website
    structure and API. This ABC defines the common interface for all scrapers.

    Attributes:
        session: aiohttp ClientSession for making HTTP requests.
        base_url: Base URL for the group's official website.
    """

    base_url: str = ""

    def __init__(self, session: aiohttp.ClientSession):
        """Initialize the scraper with an aiohttp session.

        Args:
            session: An active aiohttp ClientSession for making requests.
        """
        self.session = session

    def normalize_url(self, url: str) -> str:
        """Normalize a URL to be absolute.

        Handles relative URLs, protocol-relative URLs, and absolute URLs.

        Args:
            url: The URL to normalize.

        Returns:
            Absolute URL string.
        """
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        if not url.startswith("http"):
            return f"{self.base_url}/{url}"
        return url

    def normalize_html_urls(self, html: str) -> str:
        """Normalize all URLs within HTML content to be absolute.

        Handles src and href attributes that contain relative URLs.
        This ensures images and links work when rendered outside the
        original site context.

        Args:
            html: HTML content string.

        Returns:
            HTML with all URLs normalized to absolute URLs.
        """
        import re

        def replace_url(match: re.Match) -> str:
            attr = match.group(1)  # 'src' or 'href'
            quote = match.group(2)  # '"' or "'"
            url = match.group(3)
            normalized = self.normalize_url(url)
            return f"{attr}={quote}{normalized}{quote}"

        # Match src="..." or href="..." with either double or single quotes
        pattern = r'(src|href)=(["\'])([^"\']+)\2'
        return re.sub(pattern, replace_url, html)

    @abstractmethod
    async def get_members(self) -> dict[str, str]:
        """Fetch available blog members from the official site.

        Returns:
            Dictionary mapping member_id to member_name.
        """
        pass

    @abstractmethod
    def get_blogs_metadata(
        self,
        member_id: str,
        since_date: datetime | None = None,
        max_pages: int = 3,
        member_name: str | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog metadata (lightweight) for a specific member.

        FAST: Parses list pages only, NO detail fetches.
        Use for sync_blog_metadata() to quickly index blogs.

        Args:
            member_id: The ID of the member whose blogs to fetch.
            since_date: If provided, only yield blogs published after this date.
            max_pages: Maximum pages to fetch per member.
            member_name: If provided, filter to only blogs by this member.
                Some sites include "featured" blogs from other members.

        Yields:
            BlogEntry objects with metadata only (content is empty).
        """
        pass

    @abstractmethod
    def get_blogs(
        self,
        member_id: str,
        since_date: datetime | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog entries with FULL content for a specific member.

        NOTE: This is SLOW - fetches full detail for each blog.
        For metadata sync, use get_blogs_metadata() instead.

        Args:
            member_id: The ID of the member whose blogs to fetch.
            since_date: If provided, only yield blogs published after this date.

        Yields:
            BlogEntry objects for each blog post found.
        """
        pass

    @abstractmethod
    async def get_blog_detail(self, blog_id: str, member_id: str | None = None) -> BlogEntry:
        """Fetch the full content of a specific blog post.

        Some APIs return truncated content in list views. This method
        fetches the complete blog post content.

        Args:
            blog_id: The unique identifier of the blog post.
            member_id: Optional member ID to narrow the search. Some scrapers
                (like Nogizaka) search through all blogs which is slow.
                Providing member_id significantly speeds up the lookup.

        Returns:
            A BlogEntry with full content.

        Raises:
            ValueError: If the blog post is not found.
        """
        pass

    async def get_blog_detail_metadata(self, blog_id: str) -> tuple[str | None, datetime | None, str | None]:
        """Fetch authoritative metadata from a blog detail page.

        Some blog list pages have incomplete data (e.g. Sakurazaka: no time,
        no thumbnail, possibly truncated titles). This method fetches the
        detail page to get the single source of truth for all fields.
        Default implementation fetches full detail. Subclasses can override
        for efficiency.

        Args:
            blog_id: The unique identifier of the blog post.

        Returns:
            Tuple of (thumbnail_url, published_at, title). Any may be None.
        """
        try:
            entry = await self.get_blog_detail(blog_id)
            thumbnail = entry.images[0] if entry.images else None
            return thumbnail, entry.published_at, entry.title
        except Exception:
            return None, None, None

    async def get_member_birthdays(self) -> dict[str, str]:
        """Fetch member birthdays from the group's official site.

        Returns:
            Mapping of member display name (exactly as the site writes it) to
            ``"YYYY-MM-DD"``. Members without a parsable birthday are omitted.
            The base implementation returns an empty mapping.
        """
        return {}

    async def _collect_profile_birthdays(
        self,
        members: list[MemberInfo],
        profile_url: Callable[[str], str],
        parse: Callable[[str], str | None],
    ) -> dict[str, str]:
        """Fetch each member's profile page and parse its birthday.

        Requests are capped by ``BIRTHDAY_CONCURRENCY`` and each is followed by
        a short pause. Failures are logged per member id and skipped.

        Args:
            members: Members (id + display name) to look up.
            profile_url: Builds the profile page URL from a member id.
            parse: Extracts ``YYYY-MM-DD`` from profile HTML.

        Returns:
            Mapping of member name to ``"YYYY-MM-DD"``.
        """
        semaphore = asyncio.Semaphore(BIRTHDAY_CONCURRENCY)

        async def fetch(member: MemberInfo) -> tuple[str, str | None]:
            async with semaphore:
                try:
                    async with self.session.get(profile_url(member.id), params={"ima": "0000"}) as resp:
                        if resp.status != 200:
                            logger.warning(
                                "birthday_fetch_failed",
                                member_id=member.id,
                                error_type=f"http_{resp.status}",
                            )
                            return member.name, None
                        html = await resp.text()
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(
                        "birthday_fetch_failed",
                        member_id=member.id,
                        error_type=type(e).__name__,
                    )
                    return member.name, None
                finally:
                    await asyncio.sleep(BIRTHDAY_REQUEST_DELAY)
            birthday = parse(html)
            if birthday is None:
                logger.info("birthday_not_found", member_id=member.id)
            return member.name, birthday

        results = await asyncio.gather(*(fetch(m) for m in members))
        birthdays = {name: b for name, b in results if b}
        logger.info("birthdays_fetched", count=len(birthdays))
        return birthdays
