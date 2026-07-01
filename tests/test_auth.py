import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pysaka import Group
from pysaka.auth import BrowserAuth


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
    with patch("pysaka.auth.async_playwright") as mock_pw:
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
        mock_page.close.assert_not_called()  # Logic: In timeout, it goes to finally
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


@pytest.mark.asyncio
async def test_login_captures_refresh_token_from_signin_response(mock_playwright_env):
    """The refresh_token in the /v2/signin response body is captured and returned
    (previously it was discarded — only the Bearer access_token was scraped)."""
    _, _, mock_context, mock_page = mock_playwright_env

    handlers = {}
    mock_page.on.side_effect = lambda ev, cb: handlers.__setitem__(ev, cb)
    mock_context.on.side_effect = lambda ev, cb: handlers.__setitem__(ev, cb)

    async def drive(*args, **kwargs):
        # 1) The signin response carrying the long-lived refresh_token.
        signin = MagicMock()
        signin.status = 200
        signin.request = MagicMock()
        signin.request.url = "https://api.message.nogizaka46.com/v2/signin"
        signin.request.headers = {}
        signin.json = AsyncMock(return_value={"access_token": "AT", "refresh_token": "RT123", "expires_in": 3600})
        await handlers["response"](signin)
        # 2) A subsequent authed request carrying the Bearer (completes login).
        authed = MagicMock()
        authed.status = 200
        authed.request = MagicMock()
        authed.request.url = "https://api.message.nogizaka46.com/v2/messages"
        authed.request.headers = {
            "authorization": "Bearer AT",
            "x-talk-app-id": "app",
            "user-agent": "ua",
        }
        await handlers["response"](authed)

    mock_page.goto.side_effect = drive

    result = await asyncio.wait_for(BrowserAuth.login(Group.NOGIZAKA46), timeout=5)
    assert result is not None
    assert result["access_token"] == "AT"
    assert result["refresh_token"] == "RT123"


def _authed_response():
    authed = MagicMock()
    authed.status = 200
    authed.request = MagicMock()
    authed.request.url = "https://api.message.nogizaka46.com/v2/messages"
    authed.request.headers = {
        "authorization": "Bearer AT",
        "x-talk-app-id": "app",
        "user-agent": "ua",
    }
    return authed


@pytest.mark.asyncio
async def test_login_closes_browser_exactly_once_on_success(mock_playwright_env):
    """Cleanup must happen exactly once. The success path no longer closes the
    browser itself — the ``finally`` is the single cleanup site — so there is no
    redundant double-close (and no stray page.close())."""
    _, mock_browser, mock_context, mock_page = mock_playwright_env

    handlers: dict[str, object] = {}
    mock_page.on.side_effect = lambda ev, cb: handlers.__setitem__(ev, cb)
    mock_context.on.side_effect = lambda ev, cb: handlers.__setitem__(ev, cb)

    async def drive(*args, **kwargs):
        await handlers["response"](_authed_response())

    mock_page.goto.side_effect = drive

    result = await asyncio.wait_for(BrowserAuth.login(Group.NOGIZAKA46), timeout=5)

    assert result is not None
    assert result["access_token"] == "AT"
    # Non-persistent path: closed once via the finally, never double-closed,
    # and no redundant page.close().
    assert mock_browser.close.await_count == 1
    assert mock_page.close.await_count == 0
    assert mock_context.close.await_count == 0


@pytest.mark.asyncio
async def test_login_persistent_returns_creds_when_context_close_fails(tmp_path):
    """Faithful Windows repro: persistent context whose ``context.close()`` raises
    'Target page, context or browser has been closed' during cleanup. login() must
    still return the captured credentials, and close the context exactly once."""
    with patch("pysaka.auth.async_playwright") as mock_pw:
        mock_ctx_mgr = AsyncMock()
        mock_pw.return_value = mock_ctx_mgr
        mock_p = MagicMock()
        mock_ctx_mgr.__aenter__.return_value = mock_p
        mock_ctx_mgr.__aexit__.return_value = None

        mock_context = MagicMock()
        mock_page = MagicMock()
        # Persistent launch returns the context directly; reuse its first page.
        mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_context.pages = [mock_page]
        mock_context.add_init_script = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[])
        # Teardown raises, exactly like Windows persistent-context close.
        mock_context.close = AsyncMock(
            side_effect=Exception("BrowserContext.close: Target page, context or browser has been closed")
        )

        mock_page.close = AsyncMock()
        mock_page.evaluate = AsyncMock()

        handlers: dict[str, object] = {}
        mock_page.on.side_effect = lambda ev, cb: handlers.__setitem__(ev, cb)
        mock_context.on.side_effect = lambda ev, cb: handlers.__setitem__(ev, cb)

        async def drive(*args, **kwargs):
            await handlers["response"](_authed_response())

        mock_page.goto = AsyncMock(side_effect=drive)

        result = await asyncio.wait_for(
            BrowserAuth.login(Group.NOGIZAKA46, user_data_dir=str(tmp_path)),
            timeout=5,
        )

        assert result is not None, "captured credentials were discarded by a close error"
        assert result["access_token"] == "AT"
        # Closed once (the finally) — not twice — even though it raised.
        assert mock_context.close.await_count == 1


@pytest.mark.asyncio
async def test_login_returns_promptly_when_browser_closed(mock_playwright_env):
    """If the user closes the browser before a token is captured, login() must
    return promptly so the caller's lock is released — instead of hanging until
    the 300s interactive timeout. Regression test for the manual-close deadlock.
    """
    _, _, mock_context, mock_page = mock_playwright_env

    # Capture event handlers registered via page.on(...) / context.on(...)
    handlers: dict[str, object] = {}

    def register(event, cb):
        handlers[event] = cb

    mock_page.on.side_effect = register
    mock_context.on.side_effect = register

    # Simulate the user closing the window during navigation: fire the "close"
    # handler. No token will ever be captured.
    def goto_then_close(*args, **kwargs):
        close_cb = handlers.get("close")
        if close_cb:
            close_cb()

    mock_page.goto.side_effect = goto_then_close

    # Must finish well within the interactive timeout; otherwise the lock stays held.
    try:
        result = await asyncio.wait_for(BrowserAuth.login(Group.NOGIZAKA46), timeout=5)
    except asyncio.TimeoutError:
        pytest.fail("login() hung after the browser was closed (lock would stay held)")

    assert result is None
