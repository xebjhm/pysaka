"""Nogizaka46 blog scraper implementation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from bs4 import BeautifulSoup

from .base import BaseBlogScraper, BlogEntry

logger = structlog.get_logger(__name__)

JST = ZoneInfo("Asia/Tokyo")


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
                }
            except (ValueError, json.JSONDecodeError) as e:
                logger.warning(
                    "failed_to_parse_members",
                    error=str(e),
                )
                return {}

    async def get_blogs(
        self,
        member_id: str,
        since_date: datetime | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog entries for a specific member.

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

        while True:
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
                await asyncio.sleep(1)  # Polite delay between pages

    async def get_blog_detail(self, blog_id: str) -> BlogEntry:
        """Fetch the full content of a specific blog post.

        For Nogizaka, the list API already returns full content,
        but this method provides a way to fetch a single blog by ID.

        Args:
            blog_id: The unique identifier of the blog post.

        Returns:
            A BlogEntry with full content.

        Raises:
            ValueError: If the blog post cannot be found.
        """
        # The API doesn't support direct ID lookup, so we fetch the detail page
        detail_url = f"{self.base_url}/s/n46/diary/detail/{blog_id}"
        params = {"ima": "0000", "cd": "MEMBER"}

        async with self.session.get(detail_url, params=params) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to fetch blog {blog_id}: HTTP {resp.status}")

            html = await resp.text()
            return self._parse_blog_detail(html, blog_id, str(resp.url))

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
        published_at = datetime.now(JST)
        if date_str:
            try:
                published_at = datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S")
                published_at = published_at.replace(tzinfo=JST)
            except ValueError:
                logger.warning(
                    "failed_to_parse_date",
                    date_str=date_str,
                    blog_id=blog_id,
                )

        # Extract images from content
        images: list[str] = []
        soup = BeautifulSoup(content, "html.parser")
        for img in soup.select("img"):
            src = img.get("src", "")
            if src:
                # Make absolute URL
                if src.startswith("/"):
                    src = f"{self.base_url}{src}"
                elif not src.startswith("http"):
                    src = f"{self.base_url}/{src}"
                images.append(src)

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

    def _parse_blog_detail(self, html: str, blog_id: str, url: str) -> BlogEntry:
        """Parse a blog detail page HTML into a BlogEntry.

        This is a fallback method for when we need to fetch by ID.
        Uses og:meta tags which contain the blog info.

        Args:
            html: Raw HTML content.
            blog_id: The blog ID.
            url: The full URL of the blog.

        Returns:
            Parsed BlogEntry.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Extract from og:meta tags (most reliable)
        title_meta = soup.select_one('meta[property="og:title"]')
        title = title_meta.get("content", "") if title_meta else ""

        description_meta = soup.select_one('meta[property="og:description"]')
        content = description_meta.get("content", "") if description_meta else ""

        image_meta = soup.select_one('meta[property="og:image"]')
        images = []
        if image_meta:
            img_url = image_meta.get("content", "")
            if img_url:
                images.append(img_url)

        # Date from URL is not reliable, use current time as fallback
        published_at = datetime.now(JST)

        return BlogEntry(
            id=blog_id,
            title=title,
            content=content,
            published_at=published_at,
            url=url,
            images=images,
            member_id="",
            member_name="",
        )
