"""
Tests for auto-refresh mechanism using time-machine for time freezing.
"""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import time_machine

from pysaka.client import Client, Group
from pysaka.exceptions import RefreshFailedError


def _make_jwt(payload: dict) -> str:
    """Helper to create a test JWT (unsigned, for testing only)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.signature"


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock())
    session.post = MagicMock(return_value=AsyncMock())
    return session


@pytest.fixture
def client_with_auth_dir(tmp_path):
    """Create a client with auth_dir configured."""
    auth_dir = tmp_path / "auth_data"
    auth_dir.mkdir()
    return Client(group=Group.HINATAZAKA46, access_token="expired_token", auth_dir=str(auth_dir))


@pytest.fixture
def client_without_auth_dir():
    """Create a client without auth_dir."""
    return Client(group=Group.HINATAZAKA46, access_token="expired_token")


@pytest.mark.asyncio
async def test_headless_refresh_triggered_on_401(client_with_auth_dir, mock_session):
    """Test that headless refresh is attempted when API returns 401."""
    # Setup: API returns 401
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 401

    # Mock the headless refresh
    with patch("pysaka.auth.BrowserAuth") as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock(
            return_value={
                "access_token": "new_token_from_headless",
                "refresh_token": None,
                "cookies": {"session": "new_sess"},
                "app_id": "test",
                "user_agent": "test",
            }
        )

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        # Verify headless refresh was called
        mock_auth.refresh_token_headless.assert_called_once()
        assert result is True
        assert client_with_auth_dir.access_token == "new_token_from_headless"


@pytest.mark.asyncio
async def test_headless_refresh_updates_token(client_with_auth_dir, mock_session):
    """Test that token is properly updated after headless refresh."""
    original_token = client_with_auth_dir.access_token

    with patch("pysaka.auth.BrowserAuth") as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock(
            return_value={
                "access_token": "fresh_new_token",
                "refresh_token": None,
                "cookies": {"session": "fresh"},
                "app_id": "test",
                "user_agent": "test",
            }
        )

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        assert result is True
        assert client_with_auth_dir.access_token != original_token
        assert client_with_auth_dir.access_token == "fresh_new_token"
        assert client_with_auth_dir.headers["Authorization"] == "Bearer fresh_new_token"


@pytest.mark.asyncio
async def test_headless_refresh_skipped_without_auth_dir(client_without_auth_dir, mock_session):
    """PY-CORE-06: with no refresh credentials at all, refresh raises an auth
    error (not a silent False) so token-only callers can prompt re-login."""
    with patch("pysaka.auth.BrowserAuth") as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock()

        with pytest.raises(RefreshFailedError):
            await client_without_auth_dir.refresh_access_token(mock_session)

        # Headless refresh should NOT be called when auth_dir is None
        mock_auth.refresh_token_headless.assert_not_called()


@pytest.mark.asyncio
@time_machine.travel("2026-01-08 12:00:00", tick=False)
async def test_token_refresh_with_frozen_time(client_with_auth_dir, mock_session):
    """Test refresh mechanism with frozen time using time-machine."""
    import datetime

    # Verify time is frozen
    now = datetime.datetime.now()
    assert now.year == 2026
    assert now.month == 1
    assert now.day == 8

    with patch("pysaka.auth.BrowserAuth") as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock(
            return_value={
                "access_token": "token_at_frozen_time",
                "refresh_token": None,
                "cookies": {},
                "app_id": "test",
                "user_agent": "test",
            }
        )

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        assert result is True
        assert client_with_auth_dir.access_token == "token_at_frozen_time"


@pytest.mark.asyncio
async def test_headless_refresh_failure_raises_error(client_with_auth_dir, mock_session):
    """Test that headless refresh failure raises RefreshFailedError."""
    with patch("pysaka.auth.BrowserAuth") as mock_auth:
        # Simulate refresh failure (returns None)
        mock_auth.refresh_token_headless = AsyncMock(return_value=None)

        with pytest.raises(RefreshFailedError):
            await client_with_auth_dir.refresh_access_token(mock_session)

        # Token should remain unchanged
        assert client_with_auth_dir.access_token == "expired_token"


@pytest.mark.asyncio
async def test_headless_refresh_exception_raises_refresh_failed(client_with_auth_dir, mock_session):
    """Test that exceptions during headless refresh result in RefreshFailedError."""
    with patch("pysaka.auth.BrowserAuth") as mock_auth:
        # Simulate exception
        mock_auth.refresh_token_headless = AsyncMock(side_effect=Exception("Browser crash"))

        with pytest.raises(RefreshFailedError):
            await client_with_auth_dir.refresh_access_token(mock_session)


@pytest.mark.asyncio
async def test_refresh_contacts_server_for_unexpired_but_rejected_token_py_core_01():
    """PY-CORE-01: a single (uncontended) refresh request for a token that still
    looks valid (>300s of `exp` remaining) must still contact the server. The
    server can revoke a session while the JWT is unexpired; short-circuiting on
    the local expiry would re-send the rejected token forever and silently no-op
    a consumer's proactive refresh."""
    # Token with ~1h of `exp` left — well outside the 300s danger window.
    valid_looking = _make_jwt({"exp": int(time.time()) + 3600})
    client = Client(group=Group.HINATAZAKA46, access_token=valid_looking)
    client.cookies = {"session": "old_cookie"}
    client.refresh_token = None
    client.token_manager = None

    session = MagicMock()
    session.post = MagicMock(return_value=AsyncMock())
    mock_resp = session.post.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"access_token": "server_issued_token"})
    mock_resp.cookies = {}

    result = await client.refresh_access_token(session)

    # The server WAS contacted (no early-exit) and the token was replaced.
    assert result is True
    session.post.assert_called_once()
    assert client.access_token == "server_issued_token"


@pytest.mark.asyncio
async def test_refresh_if_needed_proactive_refresh_actually_refreshes_py_core_01():
    """PY-CORE-01: refresh_if_needed(min_seconds_remaining) with a threshold above
    300s must perform a real refresh (not report success without contacting the
    server) when the token sits between 300s and the threshold — the SakaDesk
    proactive-refresh scenario (10-minute threshold)."""
    # ~7 minutes remaining: inside a 10-minute (600s) threshold, but > the 300s
    # internal danger window that used to trigger the bogus early-exit.
    token = _make_jwt({"exp": int(time.time()) + 420})
    client = Client(group=Group.HINATAZAKA46, access_token=token)
    client.cookies = {"session": "old_cookie"}
    client.refresh_token = None
    client.token_manager = None

    session = MagicMock()
    session.post = MagicMock(return_value=AsyncMock())
    mock_resp = session.post.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"access_token": "refreshed_token"})
    mock_resp.cookies = {}

    refreshed = await client.refresh_if_needed(session, min_seconds_remaining=600)

    assert refreshed is True
    session.post.assert_called_once()
    assert client.access_token == "refreshed_token"


@pytest.mark.asyncio
async def test_refresh_single_flight_skips_when_concurrent_caller_succeeded_py_core_01():
    """PY-CORE-01 / PY-I5: the single-flight dedupe is preserved — when a
    concurrent caller already refreshed to a healthy token while we waited on the
    lock, the second caller skips its own POST."""
    import asyncio

    original = _make_jwt({"exp": int(time.time()) + 60})  # inside danger window
    client = Client(group=Group.HINATAZAKA46, access_token=original)
    client.cookies = {"session": "old_cookie"}
    client.refresh_token = None
    client.token_manager = None

    # The refreshed token is healthy (>300s), so the second caller's changed-token
    # early-exit fires.
    fresh = _make_jwt({"exp": int(time.time()) + 3600})
    post_count = 0

    def _make_post(*args, **kwargs):
        nonlocal post_count
        post_count += 1
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"access_token": fresh})
        resp.cookies = {}

        async def _slow_aenter(*a, **k):
            await asyncio.sleep(0)
            return resp

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=_slow_aenter)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session = MagicMock()
    session.post = MagicMock(side_effect=_make_post)

    results = await asyncio.gather(
        client.refresh_access_token(session),
        client.refresh_access_token(session),
    )

    assert all(results)
    assert post_count == 1


class TestRefreshIfNeeded:
    """PY-TEST-02: threshold logic of Client.refresh_if_needed."""

    @pytest.fixture
    def client(self):
        c = Client(group=Group.HINATAZAKA46, access_token="placeholder")
        c.token_manager = None
        return c

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.post = MagicMock(return_value=AsyncMock())
        return session

    @pytest.mark.asyncio
    async def test_no_refresh_when_token_well_above_threshold(self, client, mock_session):
        """remaining > threshold → no refresh, returns False."""
        client.access_token = _make_jwt({"exp": int(time.time()) + 3600})

        with patch.object(client, "refresh_access_token", new=AsyncMock()) as mock_refresh:
            result = await client.refresh_if_needed(mock_session, min_seconds_remaining=300)

        assert result is False
        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_when_token_within_threshold(self, client, mock_session):
        """remaining <= threshold → exactly one refresh, returns its result."""
        client.access_token = _make_jwt({"exp": int(time.time()) + 200})

        with patch.object(client, "refresh_access_token", new=AsyncMock(return_value=True)) as mock_refresh:
            result = await client.refresh_if_needed(mock_session, min_seconds_remaining=300)

        assert result is True
        mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_when_token_expired(self, client, mock_session):
        """Already-expired token (remaining <= 0) → refresh."""
        client.access_token = _make_jwt({"exp": int(time.time()) - 100})

        with patch.object(client, "refresh_access_token", new=AsyncMock(return_value=True)) as mock_refresh:
            result = await client.refresh_if_needed(mock_session, min_seconds_remaining=300)

        assert result is True
        mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_conservatively_when_expiry_unparseable(self, client, mock_session):
        """Unparseable token (remaining is None) → refresh conservatively."""
        client.access_token = "not-a-jwt"

        with patch.object(client, "refresh_access_token", new=AsyncMock(return_value=True)) as mock_refresh:
            result = await client.refresh_if_needed(mock_session, min_seconds_remaining=300)

        assert result is True
        mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_threshold_honored(self, client, mock_session):
        """A token 7 min out refreshes under a 10-min threshold but not a 5-min one."""
        client.access_token = _make_jwt({"exp": int(time.time()) + 420})

        with patch.object(client, "refresh_access_token", new=AsyncMock(return_value=True)) as mock_refresh:
            # 420s remaining > 300s threshold → skip.
            assert await client.refresh_if_needed(mock_session, min_seconds_remaining=300) is False
            mock_refresh.assert_not_called()

            # 420s remaining <= 600s threshold → refresh.
            assert await client.refresh_if_needed(mock_session, min_seconds_remaining=600) is True
            mock_refresh.assert_called_once()
