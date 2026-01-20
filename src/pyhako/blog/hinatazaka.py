"""Hinatazaka46 blog scraper implementation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import datetime

import structlog
from bs4 import BeautifulSoup

from .base import BaseBlogScraper, BlogEntry, MemberInfo
from .config import (
    DETAIL_DELAY,
    FULL_CONTENT_PAGE_DELAY,
    MAX_PAGES_SAFETY_CAP,
    PAGE_DELAY,
    parse_jst_datetime,
)

logger = structlog.get_logger(__name__)


class HinatazakaBlogScraper(BaseBlogScraper):
    """Scraper for Hinatazaka46 official blog.

    Uses HTML scraping from www.hinatazaka46.com.

    The member blog list page uses a ct parameter that pre-filters
    .p-blog-article elements to show only the target member's blogs.
    Pagination continues until no articles are found.

    Attributes:
        base_url: Base URL for Hinatazaka46 official site.
    """

    base_url = "https://www.hinatazaka46.com"

    async def get_members(self) -> dict[str, str]:
        """Fetch available blog members from the public website.

        Scrapes the member list page to get member IDs and names.
        No authentication required.

        Returns:
            Dictionary mapping member_id (ct parameter) to member_name.
        """
        url = f"{self.base_url}/s/official/diary/member"
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
            # Look for member links in the member list
            for link in soup.select('a[href*="ct="]'):
                href = str(link.get("href", ""))
                match = re.search(r"ct=(\d+)", href)
                if match:
                    member_id = match.group(1)
                    # Get member name from the link text or parent element
                    name = link.get_text(strip=True)
                    if not name:
                        # Try parent element
                        parent = link.find_parent("div")
                        if parent:
                            name_elem = parent.select_one(".name, .p-blog-member__name")
                            if name_elem:
                                name = name_elem.get_text(strip=True)
                    if name and member_id not in members:
                        members[member_id] = name

            return members

    async def get_members_with_thumbnails(self) -> list[MemberInfo]:
        """Fetch blog members with their profile thumbnail URLs.

        Scrapes the artist search page to get member profile images.
        Also scrapes the diary member page to catch mascots like ポカ.
        No authentication required.

        Returns:
            List of MemberInfo objects with id, name, and thumbnail_url.
        """
        members: list[MemberInfo] = []
        seen_ids: set[str] = set()

        # First, scrape the artist search page
        artist_url = f"{self.base_url}/s/official/search/artist"
        params = {"ima": "0000"}

        async with self.session.get(artist_url, params=params) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")

                # Find all member links with artist IDs
                for link in soup.select('a[href*="/artist/"]'):
                    href = str(link.get("href", ""))
                    match = re.search(r"/artist/(\d+)", href)
                    if not match:
                        continue

                    member_id = match.group(1)
                    if member_id in seen_ids:
                        continue
                    seen_ids.add(member_id)

                    # Find the image within or near the link
                    img = link.select_one("img")
                    if not img:
                        # Try parent container
                        parent = link.find_parent("li") or link.find_parent("div")
                        if parent:
                            img = parent.select_one("img")

                    thumbnail_url = ""
                    if img:
                        src = img.get("src", "")
                        if src:
                            thumbnail_url = self.normalize_url(src)

                    # Get member name from link text or nearby elements
                    name = link.get_text(strip=True)
                    if not name or name.isspace():
                        # Try to find name in parent or sibling elements
                        parent = link.find_parent("li") or link.find_parent("div")
                        if parent:
                            # Look for text content
                            for child in parent.stripped_strings:
                                if child and not child.isspace():
                                    name = child
                                    break

                    if name and thumbnail_url:
                        members.append(
                            MemberInfo(
                                id=member_id,
                                name=name,
                                thumbnail_url=thumbnail_url,
                            )
                        )
            else:
                logger.warning(
                    "failed_to_fetch_artist_list",
                    status=resp.status,
                    url=artist_url,
                )

        # Also scrape the diary member page to catch mascots like ポカ
        diary_url = f"{self.base_url}/s/official/diary/member"
        async with self.session.get(diary_url, params=params) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")

                # Find member items with ct parameter (blog member list)
                for link in soup.select('a[href*="ct="]'):
                    href = str(link.get("href", ""))
                    match = re.search(r"ct=(\d+)", href)
                    if not match:
                        continue

                    member_id = match.group(1)
                    if member_id in seen_ids:
                        continue
                    seen_ids.add(member_id)

                    # Find the container for this member
                    container = link.find_parent("li") or link.find_parent("div")
                    if not container:
                        continue

                    # Find the image
                    img = container.select_one("img")
                    thumbnail_url = ""
                    if img:
                        src = img.get("src", "")
                        if src:
                            thumbnail_url = self.normalize_url(src)

                    # Get member name
                    name_elem = container.select_one(".name, .p-blog-member__name")
                    name = name_elem.get_text(strip=True) if name_elem else ""
                    if not name:
                        name = link.get_text(strip=True)

                    if name and thumbnail_url:
                        members.append(
                            MemberInfo(
                                id=member_id,
                                name=name,
                                thumbnail_url=thumbnail_url,
                            )
                        )
            else:
                logger.warning(
                    "failed_to_fetch_diary_member_list",
                    status=resp.status,
                    url=diary_url,
                )

        logger.info(
            "fetched_members_with_thumbnails",
            count=len(members),
        )
        return members

    async def get_blogs_metadata(
        self,
        member_id: str,
        since_date: datetime | None = None,
        max_pages: int = 3,
        member_name: str | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog metadata (lightweight) for a specific member.

        FAST: Parses list pages only, NO detail fetches required.
        Use this for sync_blog_metadata() to quickly index blogs.

        The ct parameter filters .p-blog-article elements to show only
        the target member's blogs. Pagination continues until no articles found.

        Args:
            member_id: The member's UUID (ct parameter).
            since_date: If provided, stop when reaching blogs before this date.
            max_pages: Maximum pages to fetch (default 3).
                       Use a high value to fetch complete blog history.
            member_name: Not used for Hinatazaka (list is already filtered by ct).
                       Included for interface compatibility.

        Yields:
            BlogEntry objects with id, title, published_at, url, images.
            content will be empty - fetch with get_blog_detail() when needed.
        """
        page = 0
        seen_ids: set[str] = set()
        effective_max = min(max_pages, MAX_PAGES_SAFETY_CAP)

        while page < effective_max:
            url = f"{self.base_url}/s/official/diary/member/list"
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

                # .p-blog-article elements are pre-filtered by ct parameter
                # All articles on this page belong to the target member
                articles = soup.select(".p-blog-article")

                if not articles:
                    break

                for article in articles:
                    link = article.select_one('a[href*="/diary/detail/"]')
                    if not link:
                        continue

                    href = link.get("href", "")
                    match = re.search(r"/diary/detail/(\d+)", href)
                    if not match:
                        continue

                    blog_id = match.group(1)
                    if blog_id in seen_ids:
                        continue
                    seen_ids.add(blog_id)

                    # Get member name from article (for metadata)
                    name_elem = article.select_one(".c-blog-article__name")
                    member_name = name_elem.get_text(strip=True) if name_elem else ""

                    # Parse title
                    title_elem = article.select_one(".c-blog-article__title")
                    title = title_elem.get_text(strip=True) if title_elem else ""

                    # Extract date (format: "2024.1.23 16:03")
                    date_elem = article.select_one(".c-blog-article__date")
                    date_text = date_elem.get_text(strip=True) if date_elem else ""
                    published_at = parse_jst_datetime(date_text)

                    # Check date filter
                    if since_date and published_at < since_date:
                        return

                    # Extract first image if present
                    images: list[str] = []
                    img = article.select_one("img")
                    if img:
                        src = img.get("src", "")
                        if src:
                            images.append(self.normalize_url(src))

                    blog_url = self.normalize_url(href)

                    yield BlogEntry(
                        id=blog_id,
                        title=title,
                        content="",  # Empty - fetch with get_blog_detail() when needed
                        published_at=published_at,
                        url=blog_url,
                        images=images,
                        member_id=member_id,
                        member_name=member_name,
                    )

                page += 1
                await asyncio.sleep(PAGE_DELAY)

    async def get_blogs(
        self,
        member_id: str,
        since_date: datetime | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog entries with FULL content for a specific member.

        NOTE: This is SLOW - fetches full detail for each blog found.
        For metadata sync, use get_blogs_metadata() instead.

        The ct parameter filters .p-blog-article elements to show only
        the target member's blogs. Pagination continues until no articles found.

        Args:
            member_id: The member's UUID (ct parameter).
            since_date: If provided, stop when reaching blogs before this date.

        Yields:
            BlogEntry objects with full content for each blog post found.
        """
        page = 0
        seen_ids: set[str] = set()

        while page < MAX_PAGES_SAFETY_CAP:
            url = f"{self.base_url}/s/official/diary/member/list"
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

                # .p-blog-article elements are pre-filtered by ct parameter
                # All articles on this page belong to the target member
                articles = soup.select(".p-blog-article")

                if not articles:
                    break

                for article in articles:
                    link = article.select_one('a[href*="/diary/detail/"]')
                    if not link:
                        continue

                    href = link.get("href", "")
                    match = re.search(r"/diary/detail/(\d+)", href)
                    if not match:
                        continue

                    blog_id = match.group(1)
                    if blog_id in seen_ids:
                        continue
                    seen_ids.add(blog_id)

                    # Fetch full blog detail
                    try:
                        entry = await self.get_blog_detail(blog_id)
                        entry.member_id = member_id

                        # Check date filter
                        if since_date and entry.published_at < since_date:
                            return

                        yield entry

                        # Polite delay between requests
                        await asyncio.sleep(DETAIL_DELAY)
                    except ValueError as e:
                        logger.warning(
                            "blog_detail_fetch_failed",
                            blog_id=blog_id,
                            error=str(e),
                        )

                page += 1
                await asyncio.sleep(FULL_CONTENT_PAGE_DELAY)

    async def get_blog_detail(self, blog_id: str, member_id: str | None = None) -> BlogEntry:
        """Fetch the full content of a specific blog post.

        Args:
            blog_id: The unique identifier of the blog post.
            member_id: Optional member ID (unused for Hinatazaka - direct URL access).

        Returns:
            A BlogEntry with full content.

        Raises:
            ValueError: If the blog post cannot be parsed.
        """
        # member_id is unused - Hinatazaka has direct detail URLs
        _ = member_id
        url = f"{self.base_url}/s/official/diary/detail/{blog_id}"
        params = {"ima": "0000", "cd": "member"}

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

        # Extract title
        title_elem = soup.select_one(".c-blog-article__title")
        title = title_elem.get_text(strip=True) if title_elem else ""

        # Extract date
        date_elem = soup.select_one(".c-blog-article__date time")
        date_text = date_elem.get_text(strip=True) if date_elem else ""
        published_at = parse_jst_datetime(date_text)

        # Extract member name
        name_elem = soup.select_one(".c-blog-article__name a")
        member_name = name_elem.get_text(strip=True) if name_elem else ""

        # Extract member ID from name link
        member_id = ""
        if name_elem:
            href = name_elem.get("href", "")
            ct_match = re.search(r"ct=(\d+)", href)
            if ct_match:
                member_id = ct_match.group(1)

        # Extract content
        content_elem = soup.select_one(".c-blog-article__text")
        content = ""
        images: list[str] = []

        if content_elem:
            # Get raw HTML content and normalize URLs within it
            content = self.normalize_html_urls(str(content_elem))

            # Extract image URLs
            for img in content_elem.select("img"):
                src = img.get("src", "")
                if src:
                    images.append(self.normalize_url(src))

        return BlogEntry(
            id=blog_id,
            title=title,
            content=content,
            published_at=published_at,
            url=url,
            images=images,
            member_id=member_id,
            member_name=member_name,
        )
