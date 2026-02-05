"""Configuration constants for blog scraping.

Centralized location for tunable parameters to make it easy
to adjust sync behavior without modifying scraper code.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# Japan Standard Time - used by all scrapers
JST = ZoneInfo("Asia/Tokyo")

# Standard date formats used across blog sites (Hinatazaka format as primary)
# All scrapers should use parse_jst_datetime() with these formats
DATE_FORMATS = [
    "%Y.%m.%d %H:%M",  # Hinatazaka: "2024.1.23 16:03"
    "%Y/%m/%d %H:%M:%S",  # Nogizaka: "2026/01/08 20:17:04"
    "%Y/%m/%d %H:%M",  # Sakurazaka variant
    "%Y/%m/%d",  # Date only (Sakurazaka list)
    "%Y.%m.%d",  # Date only (Hinatazaka fallback)
]


def parse_jst_datetime(
    date_text: str,
    formats: list[str] | None = None,
) -> datetime:
    """Parse a date string into a JST datetime.

    Tries multiple formats in order until one succeeds.
    Falls back to current time if all formats fail.

    Args:
        date_text: The date string to parse.
        formats: List of strptime format strings to try.
                 Defaults to DATE_FORMATS if not provided.

    Returns:
        Parsed datetime with JST timezone, or current JST time on failure.
    """
    if formats is None:
        formats = DATE_FORMATS

    for fmt in formats:
        try:
            dt = datetime.strptime(date_text.strip(), fmt)
            return dt.replace(tzinfo=JST)
        except ValueError:
            continue

    # Fallback to current time
    return datetime.now(JST)


# Pagination safety cap - prevents infinite loops if server behaves unexpectedly
# This is a failsafe, not a functional limit. Normal termination is via empty page.
MAX_PAGES_SAFETY_CAP = 100

# Concurrency settings for blog metadata sync (Stage 1)
# Higher values = faster sync but more server load
SYNC_CONCURRENCY_INITIAL = 20  # First sync: aggressive to build index quickly
SYNC_CONCURRENCY_INCREMENTAL = 5  # Incremental sync: gentle on server

# Concurrency settings for blog content download (Stage 2)
DOWNLOAD_CONCURRENCY_INITIAL = 20  # First download: aggressive to build cache quickly
DOWNLOAD_CONCURRENCY_INCREMENTAL = 5  # Incremental: gentle on server

# Concurrency for image downloads within a blog
IMAGE_DOWNLOAD_CONCURRENCY = 5

# Delay between page requests (seconds)
PAGE_DELAY = 0.3

# Delay between blog detail requests (seconds)
DETAIL_DELAY = 0.5

# Delay between pages when fetching full content (seconds)
FULL_CONTENT_PAGE_DELAY = 1.0
