
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyhako import Group
from pyhako.auth import BrowserAuth


@pytest.mark.asyncio
async def test_auth_initialization():
    """Verify static method structure."""
    assert hasattr(BrowserAuth, "login")

@pytest.mark.asyncio
async def test_invalid_group():
    with pytest.raises(ValueError):
        await BrowserAuth.login("invalid_group")

@pytest.fixture
def mock_playwright_env():
    with patch("pyhako.auth.async_playwright") as mock_pw:
        mock_ctx_mgr = AsyncMock()
        mock_pw.return_value = mock_ctx_mgr

        mock_p = MagicMock()
        mock_ctx_mgr.__aenter__.return_value = mock_p
        mock_ctx_mgr.__aexit__.return_value = None

        # Browser object itself (not awaitable, it's the result)
        mock_browser = MagicMock()

        # launch is an async method, so it should be AsyncMock
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

        # context logic
        mock_context = MagicMock()
        # new_context is async
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        mock_page = MagicMock()
        # new_page is async
        mock_context.new_page = AsyncMock(return_value=mock_page)

        # Configure other async methods
        mock_context.add_init_script = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[])
        mock_context.close = AsyncMock()

        mock_page.goto = AsyncMock()
        mock_page.close = AsyncMock()

        mock_browser.close = AsyncMock()

        yield mock_p, mock_browser, mock_context, mock_page

@pytest.mark.asyncio
async def test_login_timeout(mock_playwright_env):
    """Test login timeout behavior."""
    _, _, _, mock_page = mock_playwright_env

    # Mock asyncio.wait_for to raise TimeoutError
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await BrowserAuth.login(Group.NOGIZAKA46)

        assert result is None
        # Verify cleanup occurred
        mock_page.close.assert_not_called() # Logic: In timeout, it goes to finally
        # Actually in the code:
        # except asyncio.TimeoutError: logger.error...
        # finally: ... await browser.close()

        # We can't easily check logging without capturing logs, but we can check return None

@pytest.mark.asyncio
async def test_login_generic_error(mock_playwright_env):
    """Test generic error during login."""
    _, _, _, mock_page = mock_playwright_env

    mock_page.goto.side_effect = Exception("Navigation Failed")

    # Needs to fail inside the try/except block
    # The code catches navigation error and logs warning, then proceeds to wait_for

    with patch("asyncio.wait_for", side_effect=Exception("Catastrophic Failure")):
        result = await BrowserAuth.login(Group.NOGIZAKA46)
        assert result is None
