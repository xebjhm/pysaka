"""Member birthdays parsed from the official sites (Memories M1).

Hinatazaka's fixture reproduces the structure verified live. Sakurazaka's is
trimmed from a live page. Nogizaka's member API answered HTTP 202 with no body
when probed, so its JSON fixture is synthetic (the ``birthday`` key is assumed).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from pysaka.blog import HinatazakaBlogScraper, NogizakaBlogScraper, SakurazakaBlogScraper
from pysaka.blog.base import BaseBlogScraper, MemberInfo, parse_jp_birthday
from pysaka.blog.hinatazaka import parse_birthday_from_profile as hinata_parse
from pysaka.blog.nogizaka import birthdays_from_member_json
from pysaka.blog.sakurazaka import parse_birthday_from_profile as sakura_parse


class MockResponse:
    """Minimal aiohttp response double (same shape as test_blog_scrapers)."""

    def __init__(self, text: str = "", status: int = 200):
        self._text = text
        self.status = status

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


FIX = Path(__file__).parent / "fixtures" / "blog"


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr("pysaka.blog.base.BIRTHDAY_REQUEST_DELAY", 0)


def test_parse_japanese_date():
    assert parse_jp_birthday("2002年9月10日") == "2002-09-10"
    assert parse_jp_birthday(" 2003年12月1日 ") == "2003-12-01"
    assert parse_jp_birthday("2003/08/08") == "2003-08-08"
    assert parse_jp_birthday("不明") is None
    assert parse_jp_birthday("") is None
    assert parse_jp_birthday("2003年2月30日") is None


def test_hinatazaka_profile_parser():
    html = (FIX / "hinatazaka_artist.html").read_text(encoding="utf-8")
    assert hinata_parse(html) == "2002-09-10"


def test_hinatazaka_profile_without_birthday():
    assert hinata_parse("<html><body>no table</body></html>") is None


def test_hinatazaka_label_without_value():
    assert hinata_parse("<table><tr><td>生年月日</td></tr></table>") is None


def test_sakurazaka_profile_parser():
    html = (FIX / "sakurazaka_artist.html").read_text(encoding="utf-8")
    assert sakura_parse(html) == "2004-12-22"


def test_sakurazaka_profile_without_birthday():
    assert sakura_parse("<html><body><dl><dt>星座</dt><dd>x</dd></dl></body></html>") is None


def test_nogizaka_json_parser():
    data = json.loads((FIX / "nogizaka_member.json").read_text(encoding="utf-8"))
    assert birthdays_from_member_json(data) == {"テスト 一": "2003-08-08"}


def test_nogizaka_json_parser_tolerates_junk():
    assert birthdays_from_member_json({}) == {}
    assert birthdays_from_member_json({"data": ["x", {"name": "a"}]}) == {}


@pytest.mark.asyncio
async def test_base_default_returns_empty():
    class _S(BaseBlogScraper):
        async def get_members(self):
            return {}

        async def get_blogs_metadata(self, *a, **k):
            yield  # pragma: no cover

        async def get_blogs(self, *a, **k):
            yield  # pragma: no cover

        async def get_blog_detail(self, *a, **k):
            raise NotImplementedError

    assert await _S(MagicMock()).get_member_birthdays() == {}


def _profile_router(pages: dict[str, tuple[int, str]]):
    """Return a session.get side effect serving profile pages by member id."""

    def get(url, params=None):
        member_id = url.rsplit("/", 1)[-1]
        status, text = pages[member_id]
        return MockResponse(text=text, status=status)

    return get


HINATA_OK = (FIX / "hinatazaka_artist.html").read_text(encoding="utf-8")
SAKURA_OK = (FIX / "sakurazaka_artist.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls, ok_html",
    [(HinatazakaBlogScraper, HINATA_OK), (SakurazakaBlogScraper, SAKURA_OK)],
)
async def test_profile_scrapers_birthdays(cls, ok_html):
    session = MagicMock()
    scraper = cls(session)

    async def members():
        return [MemberInfo("1", "A", "u"), MemberInfo("2", "B", "u")]

    scraper.get_members_with_thumbnails = members
    session.get.side_effect = _profile_router({"1": (200, ok_html), "2": (200, "<html></html>")})
    result = await scraper.get_member_birthdays()
    assert list(result) == ["A"] and len(result["A"]) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [HinatazakaBlogScraper, SakurazakaBlogScraper])
async def test_profile_scrapers_failure_logged(cls):
    session = MagicMock()
    scraper = cls(session)

    async def members():
        return [MemberInfo("1", "A", "u")]

    scraper.get_members_with_thumbnails = members
    session.get.side_effect = _profile_router({"1": (503, "")})
    with capture_logs() as logs:
        assert await scraper.get_member_birthdays() == {}
    failed = [e for e in logs if e["event"] == "birthday_fetch_failed"]
    assert failed and failed[0]["member_id"] == "1" and failed[0]["error_type"] == "http_503"


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [HinatazakaBlogScraper, SakurazakaBlogScraper])
async def test_profile_scrapers_no_members_unavailable(cls):
    scraper = cls(MagicMock())

    async def members():
        return []

    scraper.get_members_with_thumbnails = members
    with capture_logs() as logs:
        assert await scraper.get_member_birthdays() == {}
    assert any(e["event"] == "birthdays_unavailable" for e in logs)


@pytest.mark.asyncio
async def test_profile_fetch_concurrency_capped():
    session = MagicMock()
    scraper = HinatazakaBlogScraper(session)
    active = peak = 0

    class Slow(MockResponse):
        async def __aenter__(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            import asyncio

            await asyncio.sleep(0.01)
            return self

        async def __aexit__(self, *a):
            nonlocal active
            active -= 1

    async def members():
        return [MemberInfo(str(i), f"M{i}", "u") for i in range(12)]

    scraper.get_members_with_thumbnails = members
    session.get.side_effect = lambda url, params=None: Slow(text=HINATA_OK)
    result = await scraper.get_member_birthdays()
    assert len(result) == 12
    assert peak == 4


@pytest.mark.asyncio
async def test_nogizaka_birthdays_success():
    session = MagicMock()
    text = "res(" + (FIX / "nogizaka_member.json").read_text(encoding="utf-8") + ")"
    session.get.return_value = MockResponse(text=text, status=200)
    assert await NogizakaBlogScraper(session).get_member_birthdays() == {"テスト 一": "2003-08-08"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status, body", [(202, ""), (200, "not json")])
async def test_nogizaka_birthdays_unavailable(status, body):
    session = MagicMock()
    session.get.return_value = MockResponse(text=body, status=status)
    with capture_logs() as logs:
        assert await NogizakaBlogScraper(session).get_member_birthdays() == {}
    assert any(e["event"] == "birthdays_unavailable" for e in logs)
    if status != 200:
        assert [e for e in logs if e["event"] == "birthdays_unavailable"][0]["status"] == status
