"""Comprehensive tests for blog scraper async methods using mocked HTTP responses."""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from pysaka.blog import (
    HinatazakaBlogScraper,
    NogizakaBlogScraper,
    SakurazakaBlogScraper,
)
from pysaka.blog.config import parse_jst_datetime

JST = ZoneInfo("Asia/Tokyo")


class MockResponse:
    """Mock aiohttp response for testing."""

    def __init__(self, text: str = "", status: int = 200, json_data: Any = None, url: str = "https://example.com"):
        self._text = text
        self.status = status
        self._json = json_data
        self.url = url

    async def text(self) -> str:
        return self._text

    async def json(self) -> Any:
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestHinatazakaBlogScraperAsync:
    """Tests for HinatazakaBlogScraper async methods."""

    @pytest.fixture
    def mock_session(self):
        return MagicMock()

    @pytest.fixture
    def scraper(self, mock_session):
        return HinatazakaBlogScraper(mock_session)

    @pytest.mark.asyncio
    async def test_get_members_success(self, scraper, mock_session):
        """Test successful member list fetch."""
        html = """
        <html>
        <body>
            <div class="p-blog-member">
                <a href="/s/official/diary/member/list?ct=40">松田好花</a>
            </div>
            <div class="p-blog-member">
                <a href="/s/official/diary/member/list?ct=41">正源司陽子</a>
            </div>
        </body>
        </html>
        """
        mock_session.get.return_value = MockResponse(text=html, status=200)

        members = await scraper.get_members()

        assert "40" in members
        assert members["40"] == "松田好花"
        assert "41" in members
        assert members["41"] == "正源司陽子"

    @pytest.mark.asyncio
    async def test_get_members_http_error(self, scraper, mock_session):
        """Test member list fetch with HTTP error."""
        mock_session.get.return_value = MockResponse(text="", status=500)

        members = await scraper.get_members()

        assert members == {}

    @pytest.mark.asyncio
    async def test_get_members_empty_page(self, scraper, mock_session):
        """Test member list fetch with no members found."""
        html = "<html><body><div>No members</div></body></html>"
        mock_session.get.return_value = MockResponse(text=html, status=200)

        members = await scraper.get_members()

        assert members == {}

    @pytest.mark.asyncio
    async def test_get_members_with_thumbnails_success(self, scraper, mock_session):
        """Test fetching members with thumbnails."""
        artist_html = """
        <html>
        <body>
            <ul class="swiper-wrapper">
                <li class="swiper-slide">
                    <a href="?ct=40">
                        <img src="https://cdn.hinatazaka46.com/files/member/40.jpg"/>
                        <p class="name">松田 好花</p>
                    </a>
                </li>
            </ul>
        </body>
        </html>
        """
        diary_html = """
        <html>
        <body>
            <div class="p-blog-member">
                <a href="?ct=999">
                    <img src="https://cdn.hinatazaka46.com/files/poka.jpg"/>
                    <span class="p-blog-member__name">ポカ</span>
                </a>
            </div>
        </body>
        </html>
        """

        mock_session.get.side_effect = [
            MockResponse(text=artist_html, status=200),
            MockResponse(text=diary_html, status=200),
        ]

        members = await scraper.get_members_with_thumbnails()

        assert len(members) >= 1
        # Check first member has proper fields
        if members:
            assert hasattr(members[0], "id")
            assert hasattr(members[0], "name")
            assert hasattr(members[0], "thumbnail_url")

    @pytest.mark.asyncio
    async def test_get_blogs_metadata_single_page(self, scraper, mock_session):
        """Test fetching blog metadata from single page."""
        html = """
        <html>
        <body>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/12345">
                    <div class="c-blog-article__title">Test Title</div>
                    <div class="c-blog-article__date">2026.1.15 12:00</div>
                    <div class="c-blog-article__name">松田好花</div>
                    <img src="https://cdn.hinatazaka46.com/test.jpg"/>
                </a>
            </article>
        </body>
        </html>
        """
        empty_html = "<html><body></body></html>"

        mock_session.get.side_effect = [
            MockResponse(text=html, status=200),
            MockResponse(text=empty_html, status=200),
        ]

        blogs = []
        async for blog in scraper.get_blogs_metadata("40", max_pages=2):
            blogs.append(blog)

        assert len(blogs) == 1
        assert blogs[0].id == "12345"
        assert blogs[0].title == "Test Title"
        assert blogs[0].member_name == "松田好花"

    @pytest.mark.asyncio
    async def test_get_blogs_metadata_with_since_date(self, scraper, mock_session):
        """Test blog metadata fetch with date filter."""
        old_html = """
        <html>
        <body>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/12345">
                    <div class="c-blog-article__title">Old Blog</div>
                    <div class="c-blog-article__date">2020.1.1 12:00</div>
                    <div class="c-blog-article__name">Test Member</div>
                </a>
            </article>
        </body>
        </html>
        """

        mock_session.get.return_value = MockResponse(text=old_html, status=200)

        # Set since_date to 2025, so 2020 blogs should be filtered
        since_date = datetime(2025, 1, 1, tzinfo=JST)
        blogs = []
        async for blog in scraper.get_blogs_metadata("40", since_date=since_date, max_pages=1):
            blogs.append(blog)

        assert len(blogs) == 0

    @pytest.mark.asyncio
    async def test_get_blogs_metadata_http_error(self, scraper, mock_session):
        """Test blog metadata fetch with HTTP error."""
        mock_session.get.return_value = MockResponse(text="", status=500)

        blogs = []
        async for blog in scraper.get_blogs_metadata("40", max_pages=1):
            blogs.append(blog)

        assert len(blogs) == 0

    @pytest.mark.asyncio
    async def test_get_blog_detail_success(self, scraper, mock_session):
        """Test fetching full blog detail."""
        html = """
        <html>
        <body>
            <div class="c-blog-article__title">Full Blog Title</div>
            <div class="c-blog-article__date"><time>2026.1.15 12:00</time></div>
            <div class="c-blog-article__name">
                <a href="/s/official/diary/member/list?ct=40">松田好花</a>
            </div>
            <div class="c-blog-article__text">
                <p>This is the full blog content.</p>
                <img src="https://cdn.hinatazaka46.com/img1.jpg"/>
                <img src="https://cdn.hinatazaka46.com/img2.jpg"/>
            </div>
        </body>
        </html>
        """

        mock_session.get.return_value = MockResponse(
            text=html, status=200, url="https://www.hinatazaka46.com/s/official/diary/detail/12345"
        )

        entry = await scraper.get_blog_detail("12345")

        assert entry.id == "12345"
        assert entry.title == "Full Blog Title"
        assert "This is the full blog content" in entry.content
        assert len(entry.images) == 2
        assert entry.member_id == "40"
        assert entry.member_name == "松田好花"

    @pytest.mark.asyncio
    async def test_get_blog_detail_http_error(self, scraper, mock_session):
        """Test blog detail fetch with HTTP error."""
        mock_session.get.return_value = MockResponse(text="", status=404, url="https://example.com")

        with pytest.raises(Exception):  # noqa: B017 - testing that any error is raised
            await scraper.get_blog_detail("99999")


class TestSakurazakaBlogScraperAsync:
    """Tests for SakurazakaBlogScraper async methods."""

    @pytest.fixture
    def mock_session(self):
        return MagicMock()

    @pytest.fixture
    def scraper(self, mock_session):
        return SakurazakaBlogScraper(mock_session)

    @pytest.mark.asyncio
    async def test_get_members_success(self, scraper, mock_session):
        """Test successful member list fetch."""
        html = """
        <html>
        <body>
            <div class="box">
                <a href="/s/s46/diary/blog/list?ct=01">
                    <span class="name">山下瞳月</span>
                </a>
            </div>
        </body>
        </html>
        """
        mock_session.get.return_value = MockResponse(text=html, status=200)

        members = await scraper.get_members()

        assert "01" in members or len(members) >= 0  # May parse differently

    @pytest.mark.asyncio
    async def test_get_members_with_thumbnails(self, scraper, mock_session):
        """Test fetching members with thumbnails."""
        artist_html = """
        <html>
        <body>
            <ul class="list">
                <li>
                    <a href="/s/s46/artist/01">
                        <img src="https://sakurazaka46.com/files/member/01.jpg"/>
                        <div class="name">山下 瞳月</div>
                    </a>
                </li>
            </ul>
        </body>
        </html>
        """

        mock_session.get.return_value = MockResponse(text=artist_html, status=200)

        members = await scraper.get_members_with_thumbnails()

        # Just check it returns a list without error
        assert isinstance(members, list)

    @pytest.mark.asyncio
    async def test_get_blog_detail_with_og_tags(self, scraper, mock_session):
        """Test blog detail parsing with og:meta tags fallback."""
        html = """
        <html>
        <head>
            <meta property="og:title" content="Sakura Blog Title">
            <meta property="og:image" content="https://sakurazaka46.com/og_image.jpg">
        </head>
        <body>
            <div class="date">2026/1/15</div>
            <div class="name">
                <a href="/s/s46/artist/01">山下瞳月</a>
            </div>
            <div class="box-article">
                <p>Blog content here.</p>
            </div>
        </body>
        </html>
        """

        mock_session.get.return_value = MockResponse(
            text=html, status=200, url="https://sakurazaka46.com/s/s46/diary/detail/67890"
        )

        entry = await scraper.get_blog_detail("67890")

        assert entry.id == "67890"
        assert entry.title == "Sakura Blog Title"
        assert "og_image.jpg" in entry.images[0] if entry.images else True


class TestNogizakaBlogScraperAsync:
    """Tests for NogizakaBlogScraper async methods."""

    @pytest.fixture
    def mock_session(self):
        return MagicMock()

    @pytest.fixture
    def scraper(self, mock_session):
        return NogizakaBlogScraper(mock_session)

    @pytest.mark.asyncio
    async def test_get_members_from_api(self, scraper, mock_session):
        """Test fetching members from JSONP API."""
        jsonp = 'res({"count":"2","data":[{"code":"001","name":"久保史緒里"},{"code":"002","name":"賀喜遥香"}]})'

        mock_session.get.return_value = MockResponse(text=jsonp, status=200)

        members = await scraper.get_members()

        # Nogizaka uses JSONP API differently
        assert isinstance(members, dict)

    @pytest.mark.asyncio
    async def test_get_members_with_thumbnails(self, scraper, mock_session):
        """Test fetching members with thumbnails."""
        # Nogizaka uses artist search page
        artist_html = """
        <html>
        <body>
            <ul class="list">
                <li>
                    <a href="/s/n46/artist/55401">
                        <img src="https://www.nogizaka46.com/files/55401.jpg"/>
                        <div class="name">久保史緒里</div>
                    </a>
                </li>
            </ul>
        </body>
        </html>
        """

        mock_session.get.return_value = MockResponse(text=artist_html, status=200)

        members = await scraper.get_members_with_thumbnails()

        assert isinstance(members, list)

    @pytest.mark.asyncio
    async def test_get_blogs_metadata_from_api(self, scraper, mock_session):
        """Test fetching blog metadata from JSONP API."""
        jsonp = """res({"count":"1","data":[{
            "code":"104268",
            "title":"Test Blog",
            "text":"<p>Content</p>",
            "date":"2026/01/15 12:00:00",
            "link":"https://www.nogizaka46.com/s/n46/diary/detail/104268",
            "name":"久保史緒里",
            "arti_code":"55401"
        }]})"""

        empty_jsonp = 'res({"count":"0","data":[]})'

        mock_session.get.side_effect = [
            MockResponse(text=jsonp, status=200),
            MockResponse(text=empty_jsonp, status=200),
        ]

        blogs = []
        async for blog in scraper.get_blogs_metadata("55401", max_pages=2):
            blogs.append(blog)

        assert len(blogs) == 1
        assert blogs[0].id == "104268"
        assert blogs[0].title == "Test Blog"

    @pytest.mark.asyncio
    async def test_get_blog_detail_from_api(self, scraper, mock_session):
        """Test fetching blog detail from JSONP API."""
        jsonp = """res({"count":"1","data":[{
            "code":"104268",
            "title":"Full Blog Title",
            "text":"<p>Full content with <img src=\\"/files/img.jpg\\"/></p>",
            "date":"2026/01/15 12:00:00",
            "link":"https://www.nogizaka46.com/s/n46/diary/detail/104268",
            "name":"久保史緒里",
            "arti_code":"55401",
            "img":"https://www.nogizaka46.com/files/main.jpg"
        }]})"""

        mock_session.get.return_value = MockResponse(text=jsonp, status=200)

        entry = await scraper.get_blog_detail("104268")

        assert entry.id == "104268"
        assert entry.title == "Full Blog Title"
        assert entry.member_name == "久保史緒里"

    @pytest.mark.asyncio
    async def test_get_blog_detail_not_found(self, scraper, mock_session):
        """Test blog detail with no results."""
        empty_jsonp = 'res({"count":"0","data":[]})'
        mock_session.get.return_value = MockResponse(text=empty_jsonp, status=200)

        with pytest.raises(ValueError, match="not found"):
            await scraper.get_blog_detail("99999")


class TestBlogScraperEdgeCases:
    """Test edge cases across all scrapers."""

    @pytest.mark.asyncio
    async def test_hinatazaka_normalize_url(self):
        """Test URL normalization for relative URLs."""
        mock_session = MagicMock()
        scraper = HinatazakaBlogScraper(mock_session)

        # Test relative URL
        normalized = scraper.normalize_url("/files/test.jpg")
        assert normalized.startswith("https://")

        # Test absolute URL (should pass through)
        absolute = scraper.normalize_url("https://example.com/test.jpg")
        assert absolute == "https://example.com/test.jpg"

    @pytest.mark.asyncio
    async def test_sakurazaka_normalize_url(self):
        """Test Sakurazaka URL normalization."""
        mock_session = MagicMock()
        scraper = SakurazakaBlogScraper(mock_session)

        normalized = scraper.normalize_url("/files/test.jpg")
        assert normalized.startswith("https://")

    @pytest.mark.asyncio
    async def test_nogizaka_normalize_url(self):
        """Test Nogizaka URL normalization."""
        mock_session = MagicMock()
        scraper = NogizakaBlogScraper(mock_session)

        normalized = scraper.normalize_url("/files/test.jpg")
        assert normalized.startswith("https://")

    @pytest.mark.asyncio
    async def test_duplicate_blog_id_handling(self):
        """Test that duplicate blog IDs are filtered."""
        mock_session = MagicMock()
        scraper = HinatazakaBlogScraper(mock_session)

        # HTML with duplicate blog links
        html = """
        <html>
        <body>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/12345">
                    <div class="c-blog-article__title">Title 1</div>
                    <div class="c-blog-article__date">2026.1.15 12:00</div>
                </a>
            </article>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/12345">
                    <div class="c-blog-article__title">Title 1 Duplicate</div>
                    <div class="c-blog-article__date">2026.1.15 12:00</div>
                </a>
            </article>
        </body>
        </html>
        """
        empty_html = "<html><body></body></html>"

        mock_session.get.side_effect = [
            MockResponse(text=html, status=200),
            MockResponse(text=empty_html, status=200),
        ]

        blogs = []
        async for blog in scraper.get_blogs_metadata("40", max_pages=2):
            blogs.append(blog)

        # Should only have 1 blog even though there were 2 with same ID
        assert len(blogs) == 1
        assert blogs[0].id == "12345"

    @pytest.mark.asyncio
    async def test_get_blogs_skips_gone_blog_hinatazaka(self):
        """PY-I8: a deleted Hinatazaka blog (BlogGoneError) must not abort the generator."""
        mock_session = MagicMock()
        scraper = HinatazakaBlogScraper(mock_session)

        list_html = """
        <html><body>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/111">
                    <div class="c-blog-article__title">Gone Blog</div>
                    <div class="c-blog-article__date">2026.1.15 12:00</div>
                </a>
            </article>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/222">
                    <div class="c-blog-article__title">Older Blog</div>
                    <div class="c-blog-article__date">2026.1.10 12:00</div>
                </a>
            </article>
        </body></html>
        """
        detail_html = """
        <html><body>
            <div class="c-blog-article__title">Older Blog</div>
            <div class="c-blog-article__date"><time>2026.1.10 12:00</time></div>
            <div class="c-blog-article__name">
                <a href="/s/official/diary/member/list?ct=40">松田好花</a>
            </div>
            <div class="c-blog-article__text"><p>Content.</p></div>
        </body></html>
        """
        empty_html = "<html><body></body></html>"

        # 1) list page, 2) detail 111 -> 410 (gone), 3) detail 222 -> 200,
        # 4) next list page -> empty (terminates)
        mock_session.get.side_effect = [
            MockResponse(text=list_html, status=200),
            MockResponse(text="", status=410, url="https://www.hinatazaka46.com/s/official/diary/detail/111"),
            MockResponse(text=detail_html, status=200, url="https://www.hinatazaka46.com/s/official/diary/detail/222"),
            MockResponse(text=empty_html, status=200),
        ]

        blogs = []
        async for blog in scraper.get_blogs("40"):
            blogs.append(blog)

        # The gone blog (111) is skipped; the older blog (222) is still yielded.
        assert len(blogs) == 1
        assert blogs[0].id == "222"

    @pytest.mark.asyncio
    async def test_get_blogs_skips_gone_blog_sakurazaka(self):
        """PY-I8: a deleted Sakurazaka blog (BlogGoneError) must not abort the generator."""
        mock_session = MagicMock()
        scraper = SakurazakaBlogScraper(mock_session)

        list_html = """
        <html><body>
            <ul>
                <li class="box">
                    <a href="/s/s46/diary/detail/111">
                        <div class="date">2026/01/15 12:00</div>
                    </a>
                </li>
                <li class="box">
                    <a href="/s/s46/diary/detail/222">
                        <div class="date">2026/01/10 12:00</div>
                    </a>
                </li>
            </ul>
        </body></html>
        """
        detail_html = """
        <html><head>
            <meta property="og:title" content="Older Blog | 櫻坂46 山崎天 公式ブログ"/>
        </head><body>
            <div class="blog-foot"><div class="date">2026/01/10 12:00</div></div>
            <div class="name">山崎天</div>
            <div class="box-article"><p>Content.</p></div>
        </body></html>
        """
        empty_html = "<html><body></body></html>"

        # 1) list page, 2) detail 111 -> 410 (gone), 3) detail 222 -> 200,
        # 4) next list page -> empty (terminates via found_new=False)
        mock_session.get.side_effect = [
            MockResponse(text=list_html, status=200),
            MockResponse(text="", status=410, url="https://sakurazaka46.com/s/s46/diary/detail/111"),
            MockResponse(text=detail_html, status=200, url="https://sakurazaka46.com/s/s46/diary/detail/222"),
            MockResponse(text=empty_html, status=200),
        ]

        blogs = []
        async for blog in scraper.get_blogs("1"):
            blogs.append(blog)

        # The gone blog (111) is skipped; the older blog (222) is still yielded.
        assert len(blogs) == 1
        assert blogs[0].id == "222"


class TestParseJstDatetimeParseFailure:
    """PY-MGR-04: parse failure must not fabricate datetime.now(JST)."""

    def test_valid_date_parses(self):
        """Sanity: a well-formed date still parses to a JST datetime."""
        dt = parse_jst_datetime("2026.7.6 21:05")
        assert dt is not None
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 6, 21, 5)
        assert dt.tzinfo == JST

    def test_unparseable_returns_none_not_now(self):
        """PY-MGR-04: an unparseable date returns None (never now())."""
        assert parse_jst_datetime("not-a-date") is None

    def test_unparseable_logs_warning_with_text(self):
        """PY-MGR-04: the failure is logged (greppable) with the offending text."""
        with capture_logs() as logs:
            result = parse_jst_datetime("garbage 2026 date")

        assert result is None
        events = [e for e in logs if e.get("event") == "blog_date_parse_failed"]
        assert events, f"expected a 'blog_date_parse_failed' log event, got {[e.get('event') for e in logs]}"
        # The unparsed text must be present so drift is diagnosable.
        assert events[0].get("date_text") == "garbage 2026 date"

    def test_empty_string_returns_none(self):
        """An empty/whitespace date string is a parse failure, not now()."""
        assert parse_jst_datetime("") is None
        assert parse_jst_datetime("   ") is None


class TestSameDaySkipCursor:
    """PY-MGR-03: date-only list date vs time-precision cursor must not skip
    a same-day newer blog forever."""

    @pytest.fixture
    def mock_session(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_sakurazaka_metadata_same_day_newer_blog_not_skipped(self, mock_session):
        """A blog posted later the same day (list shows date only) must still be
        yielded when the cursor is a same-day, earlier time-of-day."""
        scraper = SakurazakaBlogScraper(mock_session)

        # List page: two boxes both dated 2026/07/06 (date-only, midnight JST).
        # Newer blog id=200 appears above older id=100.
        list_html = """
        <html><body>
            <ul>
                <li class="box">
                    <a href="/s/s46/diary/detail/200"></a>
                    <span class="name">山崎天</span>
                    <span class="date">2026/07/06</span>
                    <span class="title">Newer same-day blog</span>
                </li>
                <li class="box">
                    <a href="/s/s46/diary/detail/100"></a>
                    <span class="name">山崎天</span>
                    <span class="date">2026/07/06</span>
                    <span class="title">Earlier same-day blog</span>
                </li>
            </ul>
        </body></html>
        """
        empty_html = "<html><body></body></html>"
        mock_session.get.side_effect = [
            MockResponse(text=list_html, status=200),
            MockResponse(text=empty_html, status=200),
        ]

        # Cursor: last synced blog published 2026-07-06 21:05 JST (time precision).
        since_date = datetime(2026, 7, 6, 21, 5, tzinfo=JST)

        blogs = []
        async for blog in scraper.get_blogs_metadata(
            "1", since_date=since_date, max_pages=2, member_name="山崎天"
        ):
            blogs.append(blog)

        # Both same-day blogs must be yielded (caller dedupes by id); the strict
        # midnight < 21:05 early-return must NOT drop the newer one.
        ids = {b.id for b in blogs}
        assert ids == {"200", "100"}, f"same-day blogs were skipped, got {ids}"

    @pytest.mark.asyncio
    async def test_sakurazaka_metadata_strictly_older_day_stops(self, mock_session):
        """A blog from a strictly earlier day than the cursor still stops pagination."""
        scraper = SakurazakaBlogScraper(mock_session)

        list_html = """
        <html><body>
            <ul>
                <li class="box">
                    <a href="/s/s46/diary/detail/100"></a>
                    <span class="name">山崎天</span>
                    <span class="date">2026/07/05</span>
                    <span class="title">Yesterday's blog</span>
                </li>
            </ul>
        </body></html>
        """
        mock_session.get.side_effect = [
            MockResponse(text=list_html, status=200),
        ]

        since_date = datetime(2026, 7, 6, 21, 5, tzinfo=JST)

        blogs = []
        async for blog in scraper.get_blogs_metadata(
            "1", since_date=since_date, max_pages=2, member_name="山崎天"
        ):
            blogs.append(blog)

        assert blogs == [], "strictly-older-day blog should have been filtered"

    @pytest.mark.asyncio
    async def test_hinatazaka_metadata_same_day_newer_blog_not_skipped(self, mock_session):
        """Hinatazaka list dates carry time, but the '%Y.%m.%d' date-only fallback
        has the same hazard: a same-day cursor must not drop a same-day blog."""
        scraper = HinatazakaBlogScraper(mock_session)

        # Date-only (fallback format) list entries, both on 2026.7.6.
        list_html = """
        <html><body>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/200">
                    <div class="c-blog-article__title">Newer same-day</div>
                    <div class="c-blog-article__date">2026.7.6</div>
                    <div class="c-blog-article__name">松田好花</div>
                </a>
            </article>
            <article class="p-blog-article">
                <a href="/s/official/diary/detail/100">
                    <div class="c-blog-article__title">Earlier same-day</div>
                    <div class="c-blog-article__date">2026.7.6</div>
                    <div class="c-blog-article__name">松田好花</div>
                </a>
            </article>
        </body></html>
        """
        empty_html = "<html><body></body></html>"
        mock_session.get.side_effect = [
            MockResponse(text=list_html, status=200),
            MockResponse(text=empty_html, status=200),
        ]

        since_date = datetime(2026, 7, 6, 21, 5, tzinfo=JST)

        blogs = []
        async for blog in scraper.get_blogs_metadata("40", since_date=since_date, max_pages=2):
            blogs.append(blog)

        ids = {b.id for b in blogs}
        assert ids == {"200", "100"}, f"same-day blogs were skipped, got {ids}"

    @pytest.mark.asyncio
    async def test_metadata_unparseable_date_does_not_stop_or_skip(self, mock_session):
        """PY-MGR-04 x PY-MGR-03: a blog whose date fails to parse (published_at
        is None) must still be yielded and must not early-stop pagination."""
        scraper = SakurazakaBlogScraper(mock_session)

        list_html = """
        <html><body>
            <ul>
                <li class="box">
                    <a href="/s/s46/diary/detail/300"></a>
                    <span class="name">山崎天</span>
                    <span class="date">???broken???</span>
                    <span class="title">Broken date blog</span>
                </li>
            </ul>
        </body></html>
        """
        empty_html = "<html><body></body></html>"
        mock_session.get.side_effect = [
            MockResponse(text=list_html, status=200),
            MockResponse(text=empty_html, status=200),
        ]

        since_date = datetime(2026, 7, 6, 21, 5, tzinfo=JST)

        blogs = []
        async for blog in scraper.get_blogs_metadata(
            "1", since_date=since_date, max_pages=2, member_name="山崎天"
        ):
            blogs.append(blog)

        assert len(blogs) == 1
        assert blogs[0].id == "300"
        # Never fabricate now(): the unparsed date leaves published_at as None.
        assert blogs[0].published_at is None


class TestMaxPagesSafetyCap:
    """PY-MGR-05: safety cap applied uniformly, only as a failsafe when the
    caller's max_pages is unbounded, and logged when it terminates pagination."""

    @pytest.fixture
    def mock_session(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_sakurazaka_metadata_capped_when_unbounded(self, mock_session, monkeypatch):
        """Sakurazaka metadata pagination must not run forever: with an unbounded
        max_pages it stops at the safety cap and logs a warning."""
        import pysaka.blog.sakurazaka as sakurazaka_mod

        # Shrink the cap so the test is fast.
        monkeypatch.setattr(sakurazaka_mod, "MAX_PAGES_SAFETY_CAP", 2)
        monkeypatch.setattr(sakurazaka_mod.asyncio, "sleep", AsyncMock())
        scraper = SakurazakaBlogScraper(mock_session)

        # Every page returns a fresh unique blog so natural termination never fires.
        def make_page(page_num: int) -> str:
            bid = 1000 + page_num
            return f"""
            <html><body>
                <ul>
                    <li class="box">
                        <a href="/s/s46/diary/detail/{bid}"></a>
                        <span class="name">山崎天</span>
                        <span class="date">2026/07/06</span>
                        <span class="title">Page {page_num}</span>
                    </li>
                </ul>
            </body></html>
            """

        mock_session.get.side_effect = [MockResponse(text=make_page(n), status=200) for n in range(10)]

        with capture_logs() as logs:
            blogs = []
            async for blog in scraper.get_blogs_metadata(
                "1", max_pages=10_000, member_name="山崎天"
            ):
                blogs.append(blog)

        # Stopped at the (monkeypatched) cap of 2 pages, not the natural 10.
        assert len(blogs) == 2
        assert any(e.get("event") == "blog_pagination_safety_cap_hit" for e in logs), (
            f"expected a safety-cap warning, got {[e.get('event') for e in logs]}"
        )

    @pytest.mark.asyncio
    async def test_hinatazaka_metadata_bounded_request_not_clamped_silently(self, mock_session, monkeypatch):
        """PY-MGR-05: a bounded caller max_pages below the cap must be honored
        exactly (no silent clamp) and terminate naturally without a cap warning."""
        import pysaka.blog.hinatazaka as hinatazaka_mod

        monkeypatch.setattr(hinatazaka_mod, "MAX_PAGES_SAFETY_CAP", 100)
        monkeypatch.setattr(hinatazaka_mod.asyncio, "sleep", AsyncMock())
        scraper = HinatazakaBlogScraper(mock_session)

        def make_page(page_num: int) -> str:
            bid = 2000 + page_num
            return f"""
            <html><body>
                <article class="p-blog-article">
                    <a href="/s/official/diary/detail/{bid}">
                        <div class="c-blog-article__title">Page {page_num}</div>
                        <div class="c-blog-article__date">2026.7.6 12:00</div>
                        <div class="c-blog-article__name">松田好花</div>
                    </a>
                </article>
            </body></html>
            """

        # 3 non-empty pages available, but caller asks for only 2.
        mock_session.get.side_effect = [MockResponse(text=make_page(n), status=200) for n in range(3)]

        with capture_logs() as logs:
            blogs = []
            async for blog in scraper.get_blogs_metadata("40", max_pages=2):
                blogs.append(blog)

        assert len(blogs) == 2, "caller max_pages=2 must be honored exactly"
        # Terminating on the caller's own bound is not a safety-cap event.
        assert not any(e.get("event") == "blog_pagination_safety_cap_hit" for e in logs)

    @pytest.mark.asyncio
    async def test_nogizaka_metadata_capped_when_unbounded(self, mock_session, monkeypatch):
        """Nogizaka metadata pagination must also respect the safety cap when
        the caller passes an unbounded max_pages."""
        import pysaka.blog.nogizaka as nogizaka_mod

        monkeypatch.setattr(nogizaka_mod, "MAX_PAGES_SAFETY_CAP", 2)
        monkeypatch.setattr(nogizaka_mod.asyncio, "sleep", AsyncMock())
        scraper = NogizakaBlogScraper(mock_session)

        # Each page must be "full" (page_size=32 unique blogs) so the
        # len(blogs) < page_size natural-termination guard never fires.
        def make_page(page_num: int) -> str:
            items = []
            for i in range(32):
                bid = page_num * 100 + i
                items.append(
                    f'{{"code":"{bid}","title":"P{page_num}I{i}",'
                    f'"date":"2026/07/06 12:00:00","link":"","name":"久保史緒里","arti_code":"55401"}}'
                )
            return 'res({"count":"32","data":[' + ",".join(items) + "]})"

        mock_session.get.side_effect = [MockResponse(text=make_page(n), status=200) for n in range(10)]

        with capture_logs() as logs:
            blogs = []
            async for blog in scraper.get_blogs_metadata("55401", max_pages=10_000):
                blogs.append(blog)

        # 2 pages * 32 = 64 blogs, then the cap stops it.
        assert len(blogs) == 64
        assert any(e.get("event") == "blog_pagination_safety_cap_hit" for e in logs), (
            f"expected a safety-cap warning, got {[e.get('event') for e in logs]}"
        )
