"""Extended tests for pyhako.auth module to improve coverage."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyhako import Group
from pyhako.auth import BrowserAuth


class TestBrowserAuthLogin:
    """Tests for BrowserAuth.login method."""

    @pytest.mark.asyncio
    async def test_login_with_string_group(self):
        """Test that string group is converted to Group enum."""
        with patch("pyhako.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            mock_browser = AsyncMock()
            mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.add_init_script = AsyncMock()
            mock_context.cookies = AsyncMock(return_value=[])
            mock_context.close = AsyncMock()

            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.close = AsyncMock()
            mock_browser.close = AsyncMock()

            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                # Should work with string group
                result = await BrowserAuth.login("nogizaka46")
                assert result is None  # Timeout

    @pytest.mark.asyncio
    async def test_login_with_persistent_context(self):
        """Test login with user_data_dir for persistent context."""
        with patch("pyhako.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            # For persistent context, launch_persistent_context is used
            mock_context = AsyncMock()
            mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
            mock_context.pages = []
            mock_context.add_init_script = AsyncMock()
            mock_context.cookies = AsyncMock(return_value=[])
            mock_context.close = AsyncMock()
            mock_context.clear_cookies = AsyncMock()

            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.close = AsyncMock()
            mock_page.evaluate = AsyncMock()

            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await BrowserAuth.login(
                    Group.NOGIZAKA46,
                    user_data_dir="/tmp/test_auth"
                )
                assert result is None

    @pytest.mark.asyncio
    async def test_login_captures_token_from_response(self):
        """Test that token is captured from API response."""
        with patch("pyhako.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            mock_browser = AsyncMock()
            mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.add_init_script = AsyncMock()
            mock_context.cookies = AsyncMock(return_value=[
                {"name": "session", "value": "sess123", "domain": "message.hinatazaka46.com"}
            ])
            mock_context.close = AsyncMock()

            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()
            mock_page.close = AsyncMock()
            mock_browser.close = AsyncMock()

            # Store the handler when page.on is called
            captured_handler = None

            def capture_on(event, handler):
                nonlocal captured_handler
                if event == "response":
                    captured_handler = handler

            mock_page.on = capture_on

            # Start login as a task
            login_task = asyncio.create_task(
                BrowserAuth.login(Group.HINATAZAKA46, headless=True)
            )

            # Let the task start
            await asyncio.sleep(0.05)

            # Simulate a response with token
            if captured_handler:
                mock_request = MagicMock()
                mock_request.url = "https://api.message.hinatazaka46.com/v2/profile"
                mock_request.headers = {
                    "Authorization": "Bearer test_token_123",
                    "x-talk-app-id": "test_app_id",
                    "user-agent": "test_ua"
                }

                mock_response = MagicMock()
                mock_response.status = 200
                mock_response.request = mock_request

                await captured_handler(mock_response)

            result = await login_task

            assert result is not None
            assert result["access_token"] == "test_token_123"
            assert result["app_id"] == "test_app_id"
            assert result["cookies"]["session"] == "sess123"


class TestBrowserAuthRefreshHeadless:
    """Tests for BrowserAuth.refresh_token_headless method."""

    @pytest.mark.asyncio
    async def test_refresh_headless_auth_dir_not_exists(self, tmp_path):
        """Test that refresh fails if auth_dir doesn't exist."""
        non_existent_path = tmp_path / "non_existent"
        result = await BrowserAuth.refresh_token_headless(
            Group.NOGIZAKA46,
            non_existent_path
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_headless_timeout(self, tmp_path):
        """Test headless refresh timeout."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with patch("pyhako.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            mock_context = AsyncMock()
            mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
            mock_context.pages = []
            mock_context.cookies = AsyncMock(return_value=[])
            mock_context.close = AsyncMock()

            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()

            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await BrowserAuth.refresh_token_headless(
                    Group.NOGIZAKA46,
                    auth_dir
                )
                assert result is None

    @pytest.mark.asyncio
    async def test_refresh_headless_browser_not_installed(self, tmp_path):
        """Test handling when playwright browser is not installed."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with patch("pyhako.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            # Simulate browser not installed error
            mock_p.chromium.launch_persistent_context = AsyncMock(
                side_effect=Exception("Executable doesn't exist")
            )

            # Browser error should result in None
            result = await BrowserAuth.refresh_token_headless(
                Group.NOGIZAKA46,
                auth_dir
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_refresh_headless_success(self, tmp_path):
        """Test successful headless refresh."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with patch("pyhako.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            mock_context = AsyncMock()
            mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
            mock_context.pages = []
            mock_context.cookies = AsyncMock(return_value=[
                {"name": "session", "value": "refreshed_sess", "domain": "message.sakurazaka46.com"}
            ])
            mock_context.close = AsyncMock()

            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()

            # Capture response handler
            captured_handler = None

            def capture_on(event, handler):
                nonlocal captured_handler
                if event == "response":
                    captured_handler = handler

            mock_page.on = capture_on

            # Start refresh as task
            refresh_task = asyncio.create_task(
                BrowserAuth.refresh_token_headless(Group.SAKURAZAKA46, auth_dir)
            )

            await asyncio.sleep(0.05)

            # Trigger response
            if captured_handler:
                mock_request = MagicMock()
                mock_request.url = "https://api.message.sakurazaka46.com/v2/foo"
                mock_request.headers = {
                    "Authorization": "Bearer refreshed_token",
                    "x-talk-app-id": "app123",
                    "user-agent": "ua123"
                }

                mock_response = MagicMock()
                mock_response.status = 200
                mock_response.request = mock_request

                await captured_handler(mock_response)

            result = await refresh_task

            assert result is not None
            assert result["access_token"] == "refreshed_token"
            assert result["cookies"]["session"] == "refreshed_sess"
