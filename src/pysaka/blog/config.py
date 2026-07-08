"""Configuration constants for blog scraping.

Centralized location for tunable parameters to make it easy
to adjust sync behavior without modifying scraper code.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger(__name__)

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
) -> datetime | None:
    """Parse a date string into a JST datetime.

    Tries multiple formats in order until one succeeds.

    Args:
        date_text: The date string to parse.
        formats: List of strptime format strings to try.
                 Defaults to DATE_FORMATS if not provided.

    Returns:
        Parsed datetime with JST timezone, or ``None`` if no format matched.

    Note:
        PY-MGR-04: on parse failure this returns ``None`` (and logs a warning
        with the offending text) rather than fabricating ``datetime.now(JST)``.
        A "now" fallback silently corrupts stored publish dates and poisons the
        ``max(published_at)`` incremental cursor. Callers must treat ``None`` as
        "unknown date" (never as a boundary for early-stop filtering).
    """
    if formats is None:
        formats = DATE_FORMATS

    for fmt in formats:
        try:
            dt = datetime.strptime(date_text.strip(), fmt)
            return dt.replace(tzinfo=JST)
        except ValueError:
            continue

    # PY-MGR-04: never fabricate now() for archival data. Log the unparsed text
    # (site date-format drift is exactly what scrapers are exposed to) so the
    # failure is diagnosable, and let the caller decide how to handle it.
    logger.warning("blog_date_parse_failed", date_text=date_text)
    return None


def is_before_since_date(
    published_at: datetime | None,
    since_date: datetime | None,
) -> bool:
    """Decide whether a list entry is old enough to stop incremental pagination.

    PY-MGR-03: list pages often expose only a *date* (midnight JST), while the
    ``since_date`` cursor stored by consumers is derived from the detail page's
    time-precision ``published_at``. A strict ``published_at < since_date``
    compare then drops a same-day-but-newer blog forever (e.g. list date
    2026/07/06 -> 00:00 JST < cursor 2026/07/06 21:05). To avoid that, compare
    at *day* granularity: only report "before" when the entry's calendar day is
    strictly earlier than the cursor's. Same-day entries are re-yielded and the
    caller dedupes by id.

    A ``None`` ``published_at`` (unparseable date, see PY-MGR-04) is never
    treated as "before": we cannot know it is older, so we keep it rather than
    silently truncating history.

    Args:
        published_at: The list entry's parsed publish datetime, or ``None``.
        since_date: The incremental cursor, or ``None`` for a full fetch.

    Returns:
        ``True`` if pagination should early-return, ``False`` otherwise.
    """
    if since_date is None or published_at is None:
        return False
    return published_at.date() < since_date.date()


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
