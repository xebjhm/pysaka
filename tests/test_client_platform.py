from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka import Client, Group
from pysaka.client import GROUP_CONFIG


def test_group_config_has_verified_mobile_hosts():
    assert GROUP_CONFIG[Group.NOGIZAKA46]["mobile_api_base"] == "https://api.n46.glastonr.net/v2"
    assert GROUP_CONFIG[Group.HINATAZAKA46]["mobile_api_base"] == "https://api.kh.glastonr.net/v2"
    assert GROUP_CONFIG[Group.SAKURAZAKA46]["mobile_api_base"] == "https://api.s46.glastonr.net/v2"
    # Yodel has no known mobile host
    assert GROUP_CONFIG[Group.YODEL]["mobile_api_base"] is None


def test_android_profile_nogizaka():
    c = Client(group=Group.NOGIZAKA46, platform="android")
    assert c.api_base == "https://api.n46.glastonr.net/v2"
    assert c.headers["x-talk-app-platform"] == "android"
    assert c.headers["user-agent"] == "Dart/3.7 (dart:io)"
    assert c.headers["accept-language"] == "ja-JP;q=1.0,en-US;q=0.9"
    assert c.headers["accept"] == "application/json"
    assert c.headers["content-type"] == "application/json"
    assert c.headers["x-talk-app-id"] == "jp.co.sonymusic.communication.nogizaka 2.5"
    assert "origin" not in c.headers
    assert "referer" not in c.headers


def test_web_profile_unchanged_regression():
    c = Client(group=Group.NOGIZAKA46)  # default platform="web"
    assert c.api_base == "https://api.message.nogizaka46.com/v2"
    assert c.headers["x-talk-app-platform"] == "web"
    assert c.headers["user-agent"].startswith("Mozilla/5.0")
    assert c.headers["origin"] == "https://message.nogizaka46.com"
    assert c.headers["referer"] == "https://message.nogizaka46.com/"
    assert c.headers["accept-language"] == "ja,en-US;q=0.9,en;q=0.8"


def test_android_yodel_falls_back_to_web_host():
    c = Client(group=Group.YODEL, platform="android")
    assert c.api_base == "https://api.service.yodel-app.com/v2"
    assert c.headers["x-talk-app-platform"] == "android"
    assert "origin" not in c.headers


def test_unknown_platform_defaults_to_web():
    c = Client(group=Group.NOGIZAKA46, platform="bogus")
    assert c.headers["x-talk-app-platform"] == "web"
    assert c.api_base == "https://api.message.nogizaka46.com/v2"


def test_android_with_token_sets_bearer():
    c = Client(group=Group.NOGIZAKA46, platform="android", access_token="TKN")
    assert c.headers["Authorization"] == "Bearer TKN"


def test_explicit_user_agent_overrides_android_default():
    c = Client(group=Group.NOGIZAKA46, platform="android", user_agent="Custom/1.0")
    assert c.headers["user-agent"] == "Custom/1.0"


# --- Query-param platform tests ---


@pytest.fixture
def _mock_session():
    session = MagicMock()
    session.get.return_value.__aenter__.return_value = AsyncMock()
    session.get.return_value.__aexit__.return_value = None
    return session


@pytest.mark.asyncio
async def test_get_news_android_sends_platform_android(_mock_session):
    """Android client must send ?platform=android in get_news."""
    c = Client(group=Group.NOGIZAKA46, platform="android", access_token="TKN")
    mock_resp = _mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"announcements": [{"id": 1}]})

    result = await c.get_news(_mock_session)

    call_kwargs = _mock_session.get.call_args[1]
    assert call_kwargs["params"]["platform"] == "android"
    assert result == [{"id": 1}]


@pytest.mark.asyncio
async def test_get_news_web_sends_platform_web(_mock_session):
    """Web client must send ?platform=web in get_news."""
    c = Client(group=Group.NOGIZAKA46, platform="web", access_token="TKN")
    mock_resp = _mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"announcements": []})

    await c.get_news(_mock_session)

    call_kwargs = _mock_session.get.call_args[1]
    assert call_kwargs["params"]["platform"] == "web"


@pytest.mark.asyncio
async def test_android_skips_cookie_refresh_for_purity():
    """Absolute fingerprint purity: android mode must never POST web session cookies
    on token refresh — a real Flutter client only uses the refresh_token grant. With no
    refresh_token and android platform, refresh is a no-op (cookie fallback is web-only)."""
    c = Client(group=Group.NOGIZAKA46, platform="android", cookies={"session": "x"})
    session = MagicMock()
    result = await c.refresh_access_token(session)
    assert result is False
    session.post.assert_not_called()
