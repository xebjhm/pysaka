"""Nogizaka46 blog scraper implementation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import structlog
from bs4 import BeautifulSoup

from .base import BaseBlogScraper, BlogEntry
from .config import (
    FULL_CONTENT_PAGE_DELAY,
    MAX_PAGES_SAFETY_CAP,
    PAGE_DELAY,
    parse_jst_datetime,
)
from .hinatazaka import MemberInfo

logger = structlog.get_logger(__name__)


def parse_jsonp(text: str, callback: str = "res") -> dict[str, Any]:
    """Parse a JSONP response into a dictionary.

    Args:
        text: Raw JSONP response text.
        callback: The callback function name (default: "res").

    Returns:
        Parsed JSON dictionary.

    Raises:
        ValueError: If the JSONP format is invalid.
    """
    prefix = f"{callback}("
    if not text.startswith(prefix):
        raise ValueError(f"Invalid JSONP format: expected '{prefix}' prefix")

    # Find the matching closing parenthesis
    # The format is: res({...}) or res({...});
    # Strip trailing semicolon and whitespace, then remove the closing paren
    text = text.strip().rstrip(";")
    if not text.endswith(")"):
        raise ValueError("Invalid JSONP format: missing closing parenthesis")
    json_str = text[len(prefix) : -1]
    result: dict[str, Any] = json.loads(json_str)
    return result


class NogizakaBlogScraper(BaseBlogScraper):
    """Scraper for Nogizaka46 official blog.

    Uses JSON API from www.nogizaka46.com with JSONP callbacks.
    The API returns full blog content, so detail fetching is optional.

    Attributes:
        base_url: Base URL for Nogizaka46 official site.
        message_api_url: Base URL for message API (used for member metadata).
    """

    base_url = "https://www.nogizaka46.com"
    message_api_url = "https://api.message.nogizaka46.com"

    async def get_members(self) -> dict[str, str]:
        """Fetch available blog members from the JSON API.

        Returns:
            Dictionary mapping member_id (code) to member_name.
        """
        url = f"{self.base_url}/s/n46/api/list/member"
        params = {"callback": "res"}

        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(
                    "failed_to_fetch_members",
                    status=resp.status,
                    url=url,
                )
                return {}

            text = await resp.text()
            try:
                data = parse_jsonp(text)
                return {
                    member["code"]: member["name"]
                    for member in data.get("data", [])
                    if member.get("code")
                    and member.get("name")
                    and member.get("graduation") == "NO"  # Active members only
                    and member.get("code") not in ("46", "10001")  # Exclude group accounts
                }
            except (ValueError, json.JSONDecodeError) as e:
                logger.warning(
                    "failed_to_parse_members",
                    error=str(e),
                )
                return {}

    async def get_members_with_thumbnails(self) -> list[MemberInfo]:
        """Fetch blog members with their profile thumbnail URLs.

        Uses the same JSON API as get_members but also extracts
        thumbnail URLs from the member data.

        Returns:
            List of MemberInfo objects with id, name, and thumbnail_url.
        """
        url = f"{self.base_url}/s/n46/api/list/member"
        params = {"callback": "res"}

        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(
                    "failed_to_fetch_members_with_thumbnails",
                    status=resp.status,
                    url=url,
                )
                return []

            text = await resp.text()
            try:
                data = parse_jsonp(text)
                members: list[MemberInfo] = []
                for member in data.get("data", []):
                    if not member.get("code") or not member.get("name"):
                        continue
                    if member.get("graduation") != "NO":
                        continue  # Skip graduated members
                    if member.get("code") in ("46", "10001"):
                        continue  # Skip group accounts

                    # Build thumbnail URL from member image field
                    thumbnail_url = ""
                    if member.get("img"):
                        thumbnail_url = self.normalize_url(member["img"])

                    members.append(MemberInfo(
                        id=member["code"],
                        name=member["name"],
                        thumbnail_url=thumbnail_url,
                    ))
                return members
            except (ValueError, json.JSONDecodeError) as e:
                logger.warning(
                    "failed_to_parse_members_with_thumbnails",
                    error=str(e),
                )
                return []

    async def get_blogs_metadata(
        self,
        member_id: str,
        since_date: datetime | None = None,
        max_pages: int = 3,
        member_name: str | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog metadata (lightweight) for a specific member.

        FAST: Uses JSON API, only extracts basic metadata.
        Skips heavy HTML parsing of content for images.

        Args:
            member_id: The member's code (ct parameter).
            since_date: If provided, stop when reaching blogs before this date.
            max_pages: Maximum pages to fetch per member.

        Yields:
            BlogEntry objects with metadata only.
        """
        offset = 0
        page_size = 32
        seen_ids: set[str] = set()
        page_count = 0

        while page_count < max_pages:
            url = f"{self.base_url}/s/n46/api/list/blog"
            params = {
                "ct": member_id,
                "rw": page_size,
                "st": offset,
                "callback": "res",
            }

            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    break

                text = await resp.text()
                try:
                    data = parse_jsonp(text)
                except (ValueError, json.JSONDecodeError):
                    break

                blogs = data.get("data", [])
                if not blogs:
                    break

                found_new = False
                for blog in blogs:
                    blog_id = blog.get("code", "")
                    if not blog_id or blog_id in seen_ids:
                        continue

                    seen_ids.add(blog_id)
                    found_new = True

                    # Parse date
                    date_str = blog.get("date", "")
                    published_at = parse_jst_datetime(date_str)

                    if since_date and published_at < since_date:
                        return

                    # Use main image as thumbnail - skip content image parsing
                    images: list[str] = []
                    main_img = blog.get("img", "")
                    if main_img:
                        images.append(self.normalize_url(main_img))

                    yield BlogEntry(
                        id=blog_id,
                        title=blog.get("title", ""),
                        content="",  # Empty for metadata-only
                        published_at=published_at,
                        url=blog.get("link", ""),
                        images=images,
                        member_id=blog.get("arti_code", ""),
                        member_name=blog.get("name", ""),
                    )

                if not found_new or len(blogs) < page_size:
                    break

                offset += page_size
                page_count += 1
                await asyncio.sleep(PAGE_DELAY)

    async def get_blogs(
        self,
        member_id: str,
        since_date: datetime | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog entries with FULL content for a specific member.

        Uses the JSON API which returns full blog content.
        Pagination is handled automatically.

        Args:
            member_id: The member's code (ct parameter).
            since_date: If provided, stop when reaching blogs before this date.

        Yields:
            BlogEntry objects for each blog post found.
        """
        offset = 0
        page_size = 32
        seen_ids: set[str] = set()
        page_count = 0

        while page_count < MAX_PAGES_SAFETY_CAP:
            url = f"{self.base_url}/s/n46/api/list/blog"
            params = {
                "ct": member_id,
                "rw": page_size,
                "st": offset,
                "callback": "res",
            }

            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "blog_list_fetch_failed",
                        status=resp.status,
                        member_id=member_id,
                        offset=offset,
                    )
                    break

                text = await resp.text()
                try:
                    data = parse_jsonp(text)
                except (ValueError, json.JSONDecodeError) as e:
                    logger.warning(
                        "failed_to_parse_blog_list",
                        error=str(e),
                        member_id=member_id,
                    )
                    break

                blogs = data.get("data", [])
                if not blogs:
                    break

                found_new = False
                for blog in blogs:
                    blog_id = blog.get("code", "")
                    if not blog_id or blog_id in seen_ids:
                        continue

                    seen_ids.add(blog_id)
                    found_new = True

                    try:
                        entry = self._parse_blog_from_api(blog)

                        # Check date filter
                        if since_date and entry.published_at < since_date:
                            return

                        yield entry
                    except Exception as e:
                        logger.warning(
                            "blog_parse_failed",
                            blog_id=blog_id,
                            error=str(e),
                        )

                if not found_new or len(blogs) < page_size:
                    break

                offset += page_size
                page_count += 1
                await asyncio.sleep(FULL_CONTENT_PAGE_DELAY)

    async def get_blog_detail(self, blog_id: str, member_id: str | None = None) -> BlogEntry:
        """Fetch the full content of a specific blog post.

        For Nogizaka, the list API already returns full content.
        This method searches through the API to find the specific blog by ID.

        Args:
            blog_id: The unique identifier of the blog post (code).
            member_id: Optional member code (ct parameter) to narrow the search.
                Providing this significantly speeds up lookups for old blogs.

        Returns:
            A BlogEntry with full content.

        Raises:
            ValueError: If the blog post cannot be found.
        """
        # The Nogizaka API returns full content in list responses.
        # We search through pages to find the blog by ID.
        # If member_id is provided, filter by ct parameter for faster lookup.
        offset = 0
        page_size = 32
        max_pages = MAX_PAGES_SAFETY_CAP

        for _ in range(max_pages):
            url = f"{self.base_url}/s/n46/api/list/blog"
            params: dict[str, str | int] = {
                "rw": page_size,
                "st": offset,
                "callback": "res",
            }
            # Filter by member if provided - significantly speeds up old blog lookups
            if member_id:
                params["ct"] = member_id

            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise ValueError(f"Failed to fetch blog list: HTTP {resp.status}")

                text = await resp.text()
                try:
                    data = parse_jsonp(text)
                except (ValueError, json.JSONDecodeError) as e:
                    raise ValueError(f"Failed to parse blog API response: {e}")

                blogs = data.get("data", [])
                if not blogs:
                    break

                # Search for the blog by ID (code field)
                for blog in blogs:
                    if blog.get("code") == blog_id:
                        return self._parse_blog_from_api(blog)

                offset += page_size
                await asyncio.sleep(PAGE_DELAY)

        raise ValueError(f"Blog {blog_id} not found in API")

    def _parse_blog_from_api(self, blog: dict) -> BlogEntry:
        """Parse a blog entry from the JSON API response.

        Args:
            blog: Raw blog data from the API.

        Returns:
            Parsed BlogEntry.
        """
        blog_id = blog.get("code", "")
        title = blog.get("title", "")
        content = blog.get("text", "")
        url = blog.get("link", "")
        member_name = blog.get("name", "")
        artist_code = blog.get("arti_code", "")

        # Parse date: "2026/01/08 20:17:04"
        date_str = blog.get("date", "")
        published_at = parse_jst_datetime(date_str)

        # Extract images from content
        images: list[str] = []
        soup = BeautifulSoup(content, "html.parser")
        for img in soup.select("img"):
            src = img.get("src", "")
            if src:
                images.append(self.normalize_url(src))

        # Also add the main image if present
        main_img = blog.get("img", "")
        if main_img and main_img not in images:
            images.insert(0, main_img)

        return BlogEntry(
            id=blog_id,
            title=title,
            content=content,
            published_at=published_at,
            url=url,
            images=images,
            member_id=artist_code,
            member_name=member_name,
        )

