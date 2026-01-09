"""Tests for blog scraping module."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from pyhako.blog import (
    BlogEntry,
    HinatazakaBlogScraper,
    NogizakaBlogScraper,
    SakurazakaBlogScraper,
    get_scraper,
)
from pyhako.client import Group

JST = ZoneInfo("Asia/Tokyo")


class TestBlogEntry:
    """Tests for BlogEntry dataclass."""

    def test_blog_entry_creation(self):
        entry = BlogEntry(
            id="12345",
            title="Test Title",
            content="<p>Test content</p>",
            published_at=datetime(2026, 1, 8, 12, 0, tzinfo=JST),
            url="https://example.com/blog/12345",
            images=["https://example.com/img1.jpg"],
            member_id="42",
            member_name="Test Member",
        )
        assert entry.id == "12345"
        assert entry.title == "Test Title"
        assert entry.member_name == "Test Member"
        assert len(entry.images) == 1

    def test_blog_entry_defaults(self):
        entry = BlogEntry(
            id="1",
            title="",
            content="",
            published_at=datetime.now(JST),
            url="",
        )
        assert entry.images == []
        assert entry.member_id == ""
        assert entry.member_name == ""


class TestGetScraper:
    """Tests for get_scraper factory function."""

    def test_get_hinatazaka_scraper(self):
        mock_session = MagicMock()
        scraper = get_scraper(Group.HINATAZAKA46, mock_session)
        assert isinstance(scraper, HinatazakaBlogScraper)
        assert scraper.session is mock_session

    def test_get_nogizaka_scraper(self):
        mock_session = MagicMock()
        scraper = get_scraper(Group.NOGIZAKA46, mock_session)
        assert isinstance(scraper, NogizakaBlogScraper)

    def test_get_sakurazaka_scraper(self):
        mock_session = MagicMock()
        scraper = get_scraper(Group.SAKURAZAKA46, mock_session)
        assert isinstance(scraper, SakurazakaBlogScraper)


class TestHinatazakaBlogScraper:
    """Tests for HinatazakaBlogScraper."""

    @pytest.fixture
    def scraper(self):
        mock_session = MagicMock()
        return HinatazakaBlogScraper(mock_session)

    def test_base_url(self, scraper):
        assert scraper.base_url == "https://www.hinatazaka46.com"

    def test_parse_blog_detail(self, scraper):
        """Test parsing a blog detail page HTML."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Test</title></head>
        <body>
            <div class="c-blog-article__title">Test Blog Title</div>
            <div class="c-blog-article__date"><time>2026.1.8 12:00</time></div>
            <div class="c-blog-article__name">
                <a href="/s/official/diary/member/list?ct=42">Test Member</a>
            </div>
            <div class="c-blog-article__text">
                <p>Hello World</p>
                <img src="https://cdn.hinatazaka46.com/test.jpg"/>
            </div>
        </body>
        </html>
        """
        entry = scraper._parse_blog_detail(
            html, "67452", "https://www.hinatazaka46.com/s/official/diary/detail/67452"
        )

        assert entry.id == "67452"
        assert entry.title == "Test Blog Title"
        assert entry.member_name == "Test Member"
        assert entry.member_id == "42"
        assert "https://cdn.hinatazaka46.com/test.jpg" in entry.images
        assert entry.published_at.year == 2026
        assert entry.published_at.month == 1
        assert entry.published_at.day == 8


class TestNogizakaBlogScraper:
    """Tests for NogizakaBlogScraper."""

    @pytest.fixture
    def scraper(self):
        mock_session = MagicMock()
        return NogizakaBlogScraper(mock_session)

    def test_base_url(self, scraper):
        assert scraper.base_url == "https://www.nogizaka46.com"

    def test_parse_jsonp(self):
        """Test JSONP parsing."""
        from pyhako.blog.nogizaka import parse_jsonp

        jsonp = 'res({"count":"10","data":[]})'
        result = parse_jsonp(jsonp)
        assert result["count"] == "10"
        assert result["data"] == []

    def test_parse_jsonp_invalid(self):
        """Test JSONP parsing with invalid format."""
        from pyhako.blog.nogizaka import parse_jsonp

        with pytest.raises(ValueError):
            parse_jsonp('invalid({"data":[]})')

    def test_parse_blog_from_api(self, scraper):
        """Test parsing blog entry from API response."""
        blog_data = {
            "code": "104268",
            "title": "Test Title",
            "text": "<p>Content with <img src=\"/files/test.jpg\"/></p>",
            "date": "2026/01/08 20:17:04",
            "link": "https://www.nogizaka46.com/s/n46/diary/detail/104268",
            "name": "Test Member",
            "arti_code": "55401",
            "img": "https://www.nogizaka46.com/files/test_main.jpg",
        }
        entry = scraper._parse_blog_from_api(blog_data)

        assert entry.id == "104268"
        assert entry.title == "Test Title"
        assert entry.member_name == "Test Member"
        assert entry.member_id == "55401"
        assert len(entry.images) >= 1
        assert entry.published_at.year == 2026


class TestSakurazakaBlogScraper:
    """Tests for SakurazakaBlogScraper."""

    @pytest.fixture
    def scraper(self):
        mock_session = MagicMock()
        return SakurazakaBlogScraper(mock_session)

    def test_base_url(self, scraper):
        assert scraper.base_url == "https://sakurazaka46.com"

    def test_parse_blog_detail_from_og_tags(self, scraper):
        """Test parsing using og:meta tags fallback."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta property="og:title" content="Test OG Title">
            <meta property="og:image" content="https://sakurazaka46.com/test.jpg">
        </head>
        <body>
            <div class="date">2026/1/08</div>
            <div class="name">Test Member</div>
        </body>
        </html>
        """
        entry = scraper._parse_blog_detail(
            html, "67495", "https://sakurazaka46.com/s/s46/diary/detail/67495"
        )

        assert entry.id == "67495"
        assert entry.title == "Test OG Title"
        assert "https://sakurazaka46.com/test.jpg" in entry.images
