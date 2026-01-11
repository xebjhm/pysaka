"""Sakurazaka46 blog scraper implementation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from bs4 import BeautifulSoup

from .base import BaseBlogScraper, BlogEntry

logger = structlog.get_logger(__name__)

JST = ZoneInfo("Asia/Tokyo")


class SakurazakaBlogScraper(BaseBlogScraper):
    """Scraper for Sakurazaka46 official blog.

    Uses HTML scraping from sakurazaka46.com.

    Attributes:
        base_url: Base URL for Sakurazaka46 official site.
    """

    base_url = "https://sakurazaka46.com"

    async def get_members(self) -> dict[str, str]:
        """Fetch available blog members from the public website.

        Scrapes the artist page to get member IDs and names.
        No authentication required.

        Returns:
            Dictionary mapping member_id (artist ID) to member_name.
        """
        url = f"{self.base_url}/s/s46/search/artist"
        params = {"ima": "0000"}

        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(
                    "failed_to_fetch_members",
                    status=resp.status,
                    url=url,
                )
                return {}

            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")

            members: dict[str, str] = {}
            # Look for member links in format /s/s46/artist/{ID}
            for link in soup.select('a[href*="/s/s46/artist/"]'):
                href = str(link.get("href", ""))
                match = re.search(r"/s/s46/artist/(\d+)", href)
                if match:
                    member_id = match.group(1)
                    # Get member name - first div contains kanji, second has hiragana
                    # We want only the kanji name
                    name = ""
                    divs = link.select("div")
                    if divs:
                        name = divs[0].get_text(strip=True)
                    if not name:
                        # Fallback: try img alt attribute
                        img = link.select_one("img")
                        if img:
                            name = str(img.get("alt", ""))
                    if name and member_id not in members:
                        members[member_id] = name

            return members

    async def get_blogs(
        self,
        member_id: str,
        since_date: datetime | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog entries for a specific member.

        Scrapes the member's blog list page and yields entries.
        Pagination is handled automatically.

        Args:
            member_id: The member's UUID (ct parameter).
            since_date: If provided, stop when reaching blogs before this date.

        Yields:
            BlogEntry objects for each blog post found.
        """
        page = 0
        seen_ids: set[str] = set()

        while True:
            url = f"{self.base_url}/s/s46/diary/blog/list"
            params = {"ima": "0000", "ct": member_id, "page": page}

            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "blog_list_fetch_failed",
                        status=resp.status,
                        member_id=member_id,
                        page=page,
                    )
                    break

                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")

                # Find all blog entries on this page
                blog_links = soup.select('a[href*="/diary/detail/"]')

                if not blog_links:
                    break

                found_new = False
                for link in blog_links:
                    href = link.get("href", "")
                    match = re.search(r"/diary/detail/(\d+)", href)
                    if not match:
                        continue

                    blog_id = match.group(1)
                    if blog_id in seen_ids:
                        continue

                    seen_ids.add(blog_id)
                    found_new = True

                    # Extract preview data from list for early date filtering
                    box = link.find_parent("li", class_="box")
                    if box:
                        date_elem = box.select_one(".date")
                        date_text = date_elem.get_text(strip=True) if date_elem else ""

                        # Check date filter early (from preview)
                        if since_date and date_text:
                            try:
                                # Format: "2026/1/08"
                                preview_date = datetime.strptime(date_text, "%Y/%m/%d")
                                preview_date = preview_date.replace(tzinfo=JST)
                                if preview_date < since_date:
                                    return
                            except ValueError:
                                pass

                    # Fetch full blog detail
                    try:
                        entry = await self.get_blog_detail(blog_id)
                        entry.member_id = member_id

                        # Check date filter again with actual date
                        if since_date and entry.published_at < since_date:
                            return

                        yield entry

                        # Polite delay between requests
                        await asyncio.sleep(0.5)
                    except ValueError as e:
                        logger.warning(
                            "blog_detail_fetch_failed",
                            blog_id=blog_id,
                            error=str(e),
                        )

                if not found_new:
                    break

                page += 1
                await asyncio.sleep(1)  # Polite delay between pages

    async def get_blog_detail(self, blog_id: str) -> BlogEntry:
        """Fetch the full content of a specific blog post.

        Args:
            blog_id: The unique identifier of the blog post.

        Returns:
            A BlogEntry with full content.

        Raises:
            ValueError: If the blog post cannot be parsed.
        """
        url = f"{self.base_url}/s/s46/diary/detail/{blog_id}"
        params = {"ima": "0000", "cd": "blog"}

        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to fetch blog {blog_id}: HTTP {resp.status}")

            html = await resp.text()
            return self._parse_blog_detail(html, blog_id, str(resp.url))

    def _parse_blog_detail(self, html: str, blog_id: str, url: str) -> BlogEntry:
        """Parse a blog detail page HTML into a BlogEntry.

        Args:
            html: Raw HTML content.
            blog_id: The blog ID.
            url: The full URL of the blog.

        Returns:
            Parsed BlogEntry.

        Raises:
            ValueError: If required elements are not found.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Try og:meta tags first (most reliable)
        title_meta = soup.select_one('meta[property="og:title"]')
        title = ""
        if title_meta:
            title = title_meta.get("content", "")
        else:
            # Fallback to page title structure
            title_elem = soup.select_one(".title, h1.title, h2.title")
            title = title_elem.get_text(strip=True) if title_elem else ""

        # Extract date
        date_elem = soup.select_one(".date")
        published_at = datetime.now(JST)
        if date_elem:
            date_text = date_elem.get_text(strip=True)
            # Try various formats
            for fmt in ["%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y.%m.%d %H:%M", "%Y.%m.%d"]:
                try:
                    published_at = datetime.strptime(date_text, fmt)
                    published_at = published_at.replace(tzinfo=JST)
                    break
                except ValueError:
                    continue

        # Extract member name
        name_elem = soup.select_one(".name, .prof-name, .blog-name")
        member_name = name_elem.get_text(strip=True) if name_elem else ""

        # Extract content - try several possible selectors
        content_selectors = [
            ".box-article",
            ".blog-detail-txt",
            ".article-body",
            ".entry-content",
            "article .txt",
        ]

        content = ""
        images: list[str] = []

        for selector in content_selectors:
            content_elem = soup.select_one(selector)
            if content_elem:
                content = str(content_elem)

                # Extract image URLs
                for img in content_elem.select("img"):
                    src = img.get("src", "")
                    if src:
                        # Make absolute URL
                        if src.startswith("/"):
                            src = f"{self.base_url}{src}"
                        elif not src.startswith("http"):
                            src = f"{self.base_url}/{src}"
                        images.append(src)
                break

        # Also check for og:image
        if not images:
            og_image = soup.select_one('meta[property="og:image"]')
            if og_image:
                img_url = og_image.get("content", "")
                if img_url:
                    images.append(img_url)

        return BlogEntry(
            id=blog_id,
            title=title,
            content=content,
            published_at=published_at,
            url=url,
            images=images,
            member_id="",
            member_name=member_name,
        )
