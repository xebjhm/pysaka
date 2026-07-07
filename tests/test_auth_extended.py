"""Extended tests for pysaka.auth module to improve coverage."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pysaka import Group
from pysaka.auth import BrowserAuth


class TestBrowserAuthLogin:
    """Tests for BrowserAuth.login method."""

    @pytest.mark.asyncio
    async def test_login_with_string_group(self):
        """Test that string group is converted to Group enum."""
        with patch("pysaka.auth.async_playwright") as mock_pw:
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
        with patch("pysaka.auth.async_playwright") as mock_pw:
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
                result = await BrowserAuth.login(Group.NOGIZAKA46, user_data_dir="/tmp/test_auth")
                assert result is None

    @pytest.mark.asyncio
    async def test_login_captures_token_from_response(self):
        """Test that token is captured from API response."""
        with patch("pysaka.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            mock_browser = AsyncMock()
            mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

            mock_context = AsyncMock()
            mock_browser.new_context = AsyncMock(return_value=mock_context)
            mock_context.add_init_script = AsyncMock()
            mock_context.cookies = AsyncMock(
                return_value=[{"name": "session", "value": "sess123", "domain": "message.hinatazaka46.com"}]
            )
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
            login_task = asyncio.create_task(BrowserAuth.login(Group.HINATAZAKA46, headless=True))

            # Let the task start
            await asyncio.sleep(0.05)

            # Simulate a response with token
            if captured_handler:
                mock_request = MagicMock()
                mock_request.url = "https://api.message.hinatazaka46.com/v2/profile"
                mock_request.headers = {
                    "Authorization": "Bearer test_token_123",
                    "x-talk-app-id": "test_app_id",
                    "user-agent": "test_ua",
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
        result = await BrowserAuth.refresh_token_headless(Group.NOGIZAKA46, non_existent_path)
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_headless_timeout(self, tmp_path):
        """Test headless refresh timeout."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with patch("pysaka.auth.async_playwright") as mock_pw:
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
                result = await BrowserAuth.refresh_token_headless(Group.NOGIZAKA46, auth_dir)
                assert result is None

    @pytest.mark.asyncio
    async def test_refresh_headless_browser_not_installed(self, tmp_path):
        """Test handling when playwright browser is not installed."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with patch("pysaka.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            # Simulate browser not installed error
            mock_p.chromium.launch_persistent_context = AsyncMock(side_effect=Exception("Executable doesn't exist"))

            # Browser error should result in None
            result = await BrowserAuth.refresh_token_headless(Group.NOGIZAKA46, auth_dir)
            assert result is None

    @pytest.mark.asyncio
    async def test_refresh_headless_success(self, tmp_path):
        """Test successful headless refresh."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with patch("pysaka.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx

            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            mock_context = AsyncMock()
            mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
            mock_context.pages = []
            mock_context.cookies = AsyncMock(
                return_value=[{"name": "session", "value": "refreshed_sess", "domain": "message.sakurazaka46.com"}]
            )
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
            refresh_task = asyncio.create_task(BrowserAuth.refresh_token_headless(Group.SAKURAZAKA46, auth_dir))

            await asyncio.sleep(0.05)

            # Trigger response
            if captured_handler:
                mock_request = MagicMock()
                mock_request.url = "https://api.message.sakurazaka46.com/v2/foo"
                mock_request.headers = {
                    "Authorization": "Bearer refreshed_token",
                    "x-talk-app-id": "app123",
                    "user-agent": "ua123",
                }

                mock_response = MagicMock()
                mock_response.status = 200
                mock_response.request = mock_request

                await captured_handler(mock_response)

            result = await refresh_task

            assert result is not None
            assert result["access_token"] == "refreshed_token"
            assert result["cookies"]["session"] == "refreshed_sess"

    @pytest.mark.asyncio
    async def test_refresh_headless_system_channel_never_downloads(self, tmp_path):
        """When a system browser channel is requested (the desktop GUI mode), a
        missing browser must NOT trigger the runtime Chromium download — that
        download runs a Node subprocess that pops a console window in a packaged
        (console=False) app. The refresh tries the requested channel, falls back
        to Edge (always present on Windows), then fails closed (returns None) so
        the app prompts a normal re-login instead of downloading anything."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with (
            patch("pysaka.auth.async_playwright") as mock_pw,
            patch("playwright.__main__.main") as mock_install,
        ):
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx
            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p
            # Neither the requested channel nor the Edge fallback is launchable.
            mock_p.chromium.launch_persistent_context = AsyncMock(side_effect=Exception("Executable doesn't exist"))

            result = await BrowserAuth.refresh_token_headless(Group.NOGIZAKA46, auth_dir, channel="chrome")

            assert result is None
            mock_install.assert_not_called()  # the scary runtime download never ran
            channels = [call.kwargs.get("channel") for call in mock_p.chromium.launch_persistent_context.call_args_list]
            assert channels == ["chrome", "msedge"]

    @pytest.mark.asyncio
    async def test_refresh_headless_env_channel_pins_system_browser(self, tmp_path, monkeypatch):
        """PYSAKA_BROWSER_CHANNEL lets the desktop app pin the system browser once
        at startup, covering every Client refresh path without threading a param
        through each call site."""
        monkeypatch.setenv("PYSAKA_BROWSER_CHANNEL", "chrome")
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with (
            patch("pysaka.auth.async_playwright") as mock_pw,
            patch("playwright.__main__.main") as mock_install,
        ):
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx
            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p
            mock_p.chromium.launch_persistent_context = AsyncMock(side_effect=Exception("Executable doesn't exist"))

            result = await BrowserAuth.refresh_token_headless(Group.NOGIZAKA46, auth_dir)

            assert result is None
            mock_install.assert_not_called()
            first_channel = mock_p.chromium.launch_persistent_context.call_args_list[0].kwargs.get("channel")
            assert first_channel == "chrome"

    @pytest.mark.asyncio
    async def test_refresh_headless_success_threads_system_channel(self, tmp_path):
        """On success the requested system channel is passed straight through to
        the persistent-context launch (so the refresh reuses the same browser the
        interactive login created the profile with)."""
        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()

        with patch("pysaka.auth.async_playwright") as mock_pw:
            mock_ctx = AsyncMock()
            mock_pw.return_value = mock_ctx
            mock_p = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_p

            mock_context = AsyncMock()
            mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
            mock_context.pages = []
            mock_context.cookies = AsyncMock(
                return_value=[{"name": "session", "value": "s", "domain": "message.sakurazaka46.com"}]
            )
            mock_context.close = AsyncMock()
            mock_page = AsyncMock()
            mock_context.new_page = AsyncMock(return_value=mock_page)
            mock_page.goto = AsyncMock()

            captured_handler = None

            def capture_on(event, handler):
                nonlocal captured_handler
                if event == "response":
                    captured_handler = handler

            mock_page.on = capture_on

            refresh_task = asyncio.create_task(
                BrowserAuth.refresh_token_headless(Group.SAKURAZAKA46, auth_dir, channel="chrome")
            )
            await asyncio.sleep(0.05)

            if captured_handler:
                mock_request = MagicMock()
                mock_request.url = "https://api.message.sakurazaka46.com/v2/foo"
                mock_request.headers = {"Authorization": "Bearer tok", "x-talk-app-id": "a", "user-agent": "u"}
                mock_response = MagicMock()
                mock_response.status = 200
                mock_response.request = mock_request
                await captured_handler(mock_response)

            result = await refresh_task

            assert result is not None
            assert result["access_token"] == "tok"
            assert mock_p.chromium.launch_persistent_context.call_args.kwargs.get("channel") == "chrome"
