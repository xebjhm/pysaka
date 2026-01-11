"""Base classes for blog scraping across all groups."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp


@dataclass
class BlogEntry:
    """Represents a single blog post from any group's official site.

    Attributes:
        id: Unique identifier for the blog post.
        title: Title of the blog post.
        content: HTML content of the blog post.
        published_at: Publication timestamp.
        url: Full URL to the original blog post.
        images: List of image URLs found in the content.
        member_id: ID of the member who wrote the post.
        member_name: Name of the member who wrote the post.
    """

    id: str
    title: str
    content: str
    published_at: datetime
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

    @abstractmethod
    async def get_members(self) -> dict[str, str]:
        """Fetch available blog members from the official site.

        Returns:
            Dictionary mapping member_id to member_name.
        """
        pass

    @abstractmethod
    def get_blogs(
        self,
        member_id: str,
        since_date: datetime | None = None,
    ) -> AsyncIterator[BlogEntry]:
        """Yield blog entries for a specific member.

        This is an async generator that yields BlogEntry objects.
        It handles pagination internally and can filter by date.

        Args:
            member_id: The ID of the member whose blogs to fetch.
            since_date: If provided, only yield blogs published after this date.

        Yields:
            BlogEntry objects for each blog post found.
        """
        pass

    @abstractmethod
    async def get_blog_detail(self, blog_id: str) -> BlogEntry:
        """Fetch the full content of a specific blog post.

        Some APIs return truncated content in list views. This method
        fetches the complete blog post content.

        Args:
            blog_id: The unique identifier of the blog post.

        Returns:
            A BlogEntry with full content.

        Raises:
            ValueError: If the blog post is not found.
        """
        pass
