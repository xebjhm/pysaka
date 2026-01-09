"""
Tests for auto-refresh mechanism using time-machine for time freezing.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import time_machine

from pyhako.client import Client, Group


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
    return Client(
        group=Group.HINATAZAKA46,
        access_token="expired_token",
        auth_dir=str(auth_dir)
    )


@pytest.fixture
def client_without_auth_dir():
    """Create a client without auth_dir."""
    return Client(
        group=Group.HINATAZAKA46,
        access_token="expired_token"
    )


@pytest.mark.asyncio
async def test_headless_refresh_triggered_on_401(client_with_auth_dir, mock_session):
    """Test that headless refresh is attempted when API returns 401."""
    # Setup: API returns 401
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 401

    # Mock the headless refresh
    with patch('pyhako.auth.BrowserAuth') as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock(return_value={
            "access_token": "new_token_from_headless",
            "refresh_token": None,
            "cookies": {"session": "new_sess"},
            "app_id": "test",
            "user_agent": "test"
        })

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        # Verify headless refresh was called
        mock_auth.refresh_token_headless.assert_called_once()
        assert result is True
        assert client_with_auth_dir.access_token == "new_token_from_headless"


@pytest.mark.asyncio
async def test_headless_refresh_updates_token(client_with_auth_dir, mock_session):
    """Test that token is properly updated after headless refresh."""
    original_token = client_with_auth_dir.access_token

    with patch('pyhako.auth.BrowserAuth') as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock(return_value={
            "access_token": "fresh_new_token",
            "refresh_token": None,
            "cookies": {"session": "fresh"},
            "app_id": "test",
            "user_agent": "test"
        })

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        assert result is True
        assert client_with_auth_dir.access_token != original_token
        assert client_with_auth_dir.access_token == "fresh_new_token"
        assert client_with_auth_dir.headers["Authorization"] == "Bearer fresh_new_token"


@pytest.mark.asyncio
async def test_headless_refresh_skipped_without_auth_dir(client_without_auth_dir, mock_session):
    """Test that headless refresh is skipped when no auth_dir configured."""
    with patch('pyhako.auth.BrowserAuth') as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock()

        result = await client_without_auth_dir.refresh_access_token(mock_session)

        # Headless refresh should NOT be called when auth_dir is None
        mock_auth.refresh_token_headless.assert_not_called()
        assert result is False


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

    with patch('pyhako.auth.BrowserAuth') as mock_auth:
        mock_auth.refresh_token_headless = AsyncMock(return_value={
            "access_token": "token_at_frozen_time",
            "refresh_token": None,
            "cookies": {},
            "app_id": "test",
            "user_agent": "test"
        })

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        assert result is True
        assert client_with_auth_dir.access_token == "token_at_frozen_time"


@pytest.mark.asyncio
async def test_headless_refresh_failure_handled_gracefully(client_with_auth_dir, mock_session):
    """Test that headless refresh failure is handled gracefully."""
    with patch('pyhako.auth.BrowserAuth') as mock_auth:
        # Simulate refresh failure (returns None)
        mock_auth.refresh_token_headless = AsyncMock(return_value=None)

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        # Should return False but not crash
        assert result is False
        # Token should remain unchanged
        assert client_with_auth_dir.access_token == "expired_token"


@pytest.mark.asyncio
async def test_headless_refresh_exception_handled(client_with_auth_dir, mock_session):
    """Test that exceptions during headless refresh are caught."""
    with patch('pyhako.auth.BrowserAuth') as mock_auth:
        # Simulate exception
        mock_auth.refresh_token_headless = AsyncMock(side_effect=Exception("Browser crash"))

        result = await client_with_auth_dir.refresh_access_token(mock_session)

        # Should return False and not propagate exception
        assert result is False
