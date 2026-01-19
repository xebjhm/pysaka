"""Sakurazaka46 blog scraper implementation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import datetime

import structlog
from bs4 import BeautifulSoup

from .base import BaseBlogScraper, BlogEntry
from .config import (
    DETAIL_DELAY,
    FULL_CONTENT_PAGE_DELAY,
    MAX_PAGES_SAFETY_CAP,
    PAGE_DELAY,
    parse_jst_datetime,
)
from .hinatazaka import MemberInfo

logger = structlog.get_logger(__name__)


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

    async def get_members_with_thumbnails(self) -> list[MemberInfo]:
        """Fetch blog members with their profile thumbnail URLs.

        Scrapes the artist search page to get member profile images.
        No authentication required.

        Returns:
            List of MemberInfo objects with id, name, and thumbnail_url.
        """
        members: list[MemberInfo] = []
        seen_ids: set[str] = set()

        url = f"{self.base_url}/s/s46/search/artist"
        params = {"ima": "0000"}

        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(
                    "failed_to_fetch_members_with_thumbnails",
                    status=resp.status,
                    url=url,
                )
                return []

            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")

            # Find all member links in format /s/s46/artist/{ID}
            for link in soup.select('a[href*="/s/s46/artist/"]'):
                href = str(link.get("href", ""))
                match = re.search(r"/s/s46/artist/(\d+)", href)
                if not match:
                    continue

                member_id = match.group(1)
                if member_id in seen_ids:
                    continue
                seen_ids.add(member_id)

                # Find the image within the link
                img = link.select_one("img")
                thumbnail_url = ""
                if img:
                    src = img.get("src", "")
                    if src:
                        thumbnail_url = self.normalize_url(src)

                # Get member name - first div contains kanji
                name = ""
                divs = link.select("div")
                if divs:
                    name = divs[0].get_text(strip=True)
                if not name and img:
                    # Fallback: try img alt attribute
                    name = str(img.get("alt", ""))

                if name and thumbnail_url:
                    members.append(
                        MemberInfo(
                            id=member_id,
                            name=name,
                            thumbnail_url=thumbnail_url,
                        )
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

        Args:
            member_id: The member's UUID (ct parameter).
            since_date: If provided, stop when reaching blogs before this date.
            max_pages: Maximum pages to fetch per member.
            member_name: If provided, only yield blogs where author matches.
                The Sakurazaka list page sometimes includes "featured" blogs
                from other members - this filters them out.

        Yields:
            BlogEntry objects with id, title, published_at, url, images (thumbnail only).
            content will be empty - fetch with get_blog_detail() when needed.
        """
        page = 0
        seen_ids: set[str] = set()

        while page < max_pages:
            url = f"{self.base_url}/s/s46/diary/blog/list"
            params = {"ima": "0000", "ct": member_id, "page": page}

            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    break

                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")

                # Find all blog entry boxes
                boxes = soup.select("li.box")

                if not boxes:
                    break

                found_new = False
                for box in boxes:
                    link = box.select_one('a[href*="/diary/detail/"]')
                    if not link:
                        continue

                    href = link.get("href", "")
                    match = re.search(r"/diary/detail/(\d+)", href)
                    if not match:
                        continue

                    blog_id = match.group(1)
                    if blog_id in seen_ids:
                        continue

                    # Extract author name from list entry
                    # Filter out "featured" blogs that don't belong to this member
                    author_elem = box.select_one(".name")
                    author_name = author_elem.get_text(strip=True) if author_elem else ""
                    if member_name and author_name:
                        # Normalize spaces for comparison (full-width vs half-width)
                        normalized_author = author_name.replace(" ", "").replace("\u3000", "")
                        normalized_member = member_name.replace(" ", "").replace("\u3000", "")
                        if normalized_author != normalized_member:
                            # Blog belongs to different member, skip
                            logger.debug(
                                "skipping_featured_blog",
                                blog_id=blog_id,
                                expected=member_name,
                                actual=author_name,
                            )
                            continue

                    seen_ids.add(blog_id)
                    found_new = True

                    # Parse date from list
                    date_elem = box.select_one(".date")
                    date_text = date_elem.get_text(strip=True) if date_elem else ""
                    published_at = parse_jst_datetime(date_text)

                    if since_date and published_at < since_date:
                        return

                    # Parse title from list
                    title_elem = box.select_one(".title, .ttl, h3")
                    title = title_elem.get_text(strip=True) if title_elem else ""

                    # Extract blog thumbnail from CSS background-image on span.img
                    # Sakurazaka stores thumbnails in style="background-image: url(...)"
                    images: list[str] = []
                    thumbnail_span = box.select_one("span.img")
                    if thumbnail_span:
                        style = thumbnail_span.get("style", "")
                        bg_match = re.search(
                            r'background-image:\s*url\(["\']?([^"\')\s]+)["\']?\)', style
                        )
                        if bg_match:
                            images.append(self.normalize_url(bg_match.group(1)))

                    blog_url = self.normalize_url(href)

                    yield BlogEntry(
                        id=blog_id,
                        title=title,
                        content="",  # Empty - fetch with get_blog_detail() when needed
                        published_at=published_at,
                        url=blog_url,
                        images=images,
                        member_id=member_id,
                        member_name=author_name,
                    )

                if not found_new:
                    break

                page += 1
                await asyncio.sleep(PAGE_DELAY)

    async def get_blogs(
        self,
        member_id: str,
        since_date: datetime | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog entries with FULL content for a specific member.

        NOTE: This is SLOW - fetches full detail for each blog.
        For metadata sync, use get_blogs_metadata() instead.

        Args:
            member_id: The member's UUID (ct parameter).
            since_date: If provided, stop when reaching blogs before this date.

        Yields:
            BlogEntry objects for each blog post found.
        """
        page = 0
        seen_ids: set[str] = set()

        while page < MAX_PAGES_SAFETY_CAP:
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

                # Find all blog entry boxes (specific to member's blog list)
                # Use li.box to avoid picking up unrelated blog links
                boxes = soup.select("li.box")

                if not boxes:
                    break

                found_new = False
                for box in boxes:
                    link = box.select_one('a[href*="/diary/detail/"]')
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
                    found_new = True

                    # Extract preview data from list for early date filtering
                    date_elem = box.select_one(".date")
                    date_text = date_elem.get_text(strip=True) if date_elem else ""

                    # Check date filter early (from preview)
                    if since_date and date_text:
                        preview_date = parse_jst_datetime(date_text)
                        if preview_date < since_date:
                            return

                    # Fetch full blog detail
                    try:
                        entry = await self.get_blog_detail(blog_id)
                        entry.member_id = member_id

                        # Check date filter again with actual date
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

                if not found_new:
                    break

                page += 1
                await asyncio.sleep(FULL_CONTENT_PAGE_DELAY)

    async def get_blog_detail(self, blog_id: str, member_id: str | None = None) -> BlogEntry:
        """Fetch the full content of a specific blog post.

        Args:
            blog_id: The unique identifier of the blog post.
            member_id: Optional member ID (unused for Sakurazaka - direct URL access).

        Returns:
            A BlogEntry with full content.

        Raises:
            ValueError: If the blog post cannot be parsed.
        """
        # member_id is unused - Sakurazaka has direct detail URLs
        _ = member_id
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
        date_text = date_elem.get_text(strip=True) if date_elem else ""
        published_at = parse_jst_datetime(date_text)

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
                # Normalize URLs within the HTML content
                content = self.normalize_html_urls(str(content_elem))

                # Extract image URLs
                for img in content_elem.select("img"):
                    src = img.get("src", "")
                    if src:
                        images.append(self.normalize_url(src))
                break

        # Also check for og:image
        if not images:
            og_image = soup.select_one('meta[property="og:image"]')
            if og_image:
                img_url = og_image.get("content", "")
                if img_url:
                    images.append(self.normalize_url(img_url))

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

    async def get_blog_thumbnail(self, blog_id: str) -> str | None:
        """Fetch just the thumbnail (og:image) for a blog post.

        This is a lightweight alternative to get_blog_detail() when only
        the thumbnail is needed. Sakurazaka list pages don't show blog
        thumbnails, so this fetches the og:image from the detail page.

        Args:
            blog_id: The unique identifier of the blog post.

        Returns:
            The og:image URL if found, None otherwise.
        """
        url = f"{self.base_url}/s/s46/diary/detail/{blog_id}"
        params = {"ima": "0000", "cd": "blog"}

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None

                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")

                # Try og:image first (most reliable for thumbnails)
                og_image = soup.select_one('meta[property="og:image"]')
                if og_image:
                    img_url = og_image.get("content", "")
                    if img_url:
                        return self.normalize_url(img_url)

                # Fallback: first image in content
                content_elem = soup.select_one(".box-article, .blog-detail-txt, .article-body")
                if content_elem:
                    img = content_elem.select_one("img")
                    if img:
                        src = img.get("src", "")
                        if src:
                            return self.normalize_url(src)

                return None
        except Exception as e:
            logger.warning("get_blog_thumbnail_failed", blog_id=blog_id, error=str(e))
            return None
