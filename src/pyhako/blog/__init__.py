"""Blog scraping module for Sakamichi groups.

This module provides scrapers for fetching blog posts from the official
websites of Hinatazaka46, Nogizaka46, and Sakurazaka46.

Example usage:
    >>> import aiohttp
    >>> from pyhako.client import Group
    >>> from pyhako.blog import BlogEntry, get_scraper
    >>>
    >>> async with aiohttp.ClientSession() as session:
    ...     scraper = get_scraper(Group.HINATAZAKA46, session)
    ...     members = await scraper.get_members()
    ...     async for entry in scraper.get_blogs(member_id):
    ...         print(entry.title)
"""

from typing import TYPE_CHECKING

import aiohttp

from pyhako.client import Group

from .base import BaseBlogScraper, BlogEntry, BlogGoneError, MemberInfo
from .config import (
    DOWNLOAD_CONCURRENCY_INCREMENTAL,
    DOWNLOAD_CONCURRENCY_INITIAL,
    IMAGE_DOWNLOAD_CONCURRENCY,
    JST,
    MAX_PAGES_SAFETY_CAP,
    SYNC_CONCURRENCY_INCREMENTAL,
    SYNC_CONCURRENCY_INITIAL,
    parse_jst_datetime,
)
from .hinatazaka import HinatazakaBlogScraper
from .nogizaka import NogizakaBlogScraper
from .sakurazaka import SakurazakaBlogScraper

if TYPE_CHECKING:
    pass

__all__ = [
    "BlogEntry",
    "BlogGoneError",
    "BaseBlogScraper",
    "HinatazakaBlogScraper",
    "MemberInfo",
    "NogizakaBlogScraper",
    "SakurazakaBlogScraper",
    "get_scraper",
    # Config exports
    "JST",
    "MAX_PAGES_SAFETY_CAP",
    "SYNC_CONCURRENCY_INITIAL",
    "SYNC_CONCURRENCY_INCREMENTAL",
    "DOWNLOAD_CONCURRENCY_INITIAL",
    "DOWNLOAD_CONCURRENCY_INCREMENTAL",
    "IMAGE_DOWNLOAD_CONCURRENCY",
    "parse_jst_datetime",
]


def get_scraper(group: Group, session: aiohttp.ClientSession) -> BaseBlogScraper:
    """Get the appropriate blog scraper for a group.

    Factory function that returns the correct scraper implementation
    based on the specified group.

    Args:
        group: The target Sakamichi group.
        session: An active aiohttp ClientSession for making requests.

    Returns:
        A BaseBlogScraper subclass instance for the specified group.

    Raises:
        ValueError: If the group is not supported.

    Example:
        >>> scraper = get_scraper(Group.NOGIZAKA46, session)
        >>> members = await scraper.get_members()
    """
    if group == Group.HINATAZAKA46:
        return HinatazakaBlogScraper(session)
    elif group == Group.NOGIZAKA46:
        return NogizakaBlogScraper(session)
    elif group == Group.SAKURAZAKA46:
        return SakurazakaBlogScraper(session)
    else:
        raise ValueError(f"Unsupported group: {group}")
