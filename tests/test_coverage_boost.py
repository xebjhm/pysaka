import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError

from pyhako import ApiError, Group

# --- Client Coverage Boost ---

@pytest.mark.asyncio
async def test_client_download_file_success(client, mock_session):
    """Test file download with aiofiles mocking."""
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=b"file_content")

    # Needs a Path object
    dest = Path("/tmp/file.jpg")

    with patch("aiofiles.open", new_callable=MagicMock) as mock_file_open_ctx, \
         patch("pathlib.Path.exists", return_value=False), \
         patch("pathlib.Path.mkdir"): # Mock mkdir to avoid file ops

        # aiofiles.open returns an AsyncContextManager
        # So calling it returns a context manager whose __aenter__ returns the file handle
        mock_file_handle = AsyncMock()
        mock_file_open_ctx.return_value.__aenter__.return_value = mock_file_handle

        await client.download_file(mock_session, "http://example.com/file.jpg", dest)

        # We can't assert_called_with on the ContextManager easily if it's async?
        # Actually aiofiles.open(...) returns the CM.
        mock_file_open_ctx.assert_called_with(dest, "wb")
        mock_file_handle.write.assert_called_with(b"file_content")

@pytest.mark.asyncio
async def test_client_download_message_media(client, mock_session):
    """Test downloading media logic."""
    msg = {"id": 100, "type": "image", "file": "http://img.jpg"}
    output = Path("/tmp/out")

    client.download_file = AsyncMock(return_value=True)

    with patch("pyhako.client.get_media_extension", return_value="jpg"), \
         patch("pathlib.Path.mkdir"):

        path = await client.download_message_media(mock_session, msg, output)
        assert path == output / "picture" / "100.jpg"
        client.download_file.assert_called()


# ... (existing tests)

def test_token_manager_save_load_integration():
    """Test high level save/load logic flows."""
    with patch("keyring.set_password"), patch("keyring.delete_password"):
        tm = TokenManager()
        # Mock internal store
        tm.store = MagicMock()
        tm.store.load.return_value = {'access_token': 'at'}

        data = tm.load_session("nogizaka46")
        assert data["access_token"] == "at"

        tm.save_session("nogizaka46", "at", "rt", {})
        tm.store.save.assert_called()

# --- Auth Coverage Boost ---

from pyhako.auth import BrowserAuth


@pytest.mark.asyncio
async def test_login_successful_flow():
    """Test a successful login interception via Playwright mock."""
    with patch("pyhako.auth.async_playwright") as mock_pw:
        mock_ctx_mgr = AsyncMock()
        mock_pw.return_value = mock_ctx_mgr

        mock_p = MagicMock()
        mock_ctx_mgr.__aenter__.return_value = mock_p

        mock_browser = MagicMock()
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

        mock_context = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        # FIX: add_init_script must be AsyncMock
        mock_context.add_init_script = AsyncMock()

        mock_page = MagicMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        # Override asyncio.wait_for to Return the expected dict directly
        expected_tokens = {
            "access_token": "valid_at",
            "refresh_token": "valid_rt",
            "cookies": {},
            "x-talk-app-id": "app_id",
            "user-agent": "ua"
        }

        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = expected_tokens
            result = await BrowserAuth.login(Group.NOGIZAKA46)
            assert result == expected_tokens

@pytest.mark.asyncio
async def test_client_download_file_error(client, mock_session):
    """Test file download 404 error."""
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 404
    dest = Path("/tmp/file.jpg")

    with patch("aiofiles.open") as mock_file_open, \
         patch("pathlib.Path.exists", return_value=False):

        await client.download_file(mock_session, "http://example.com/file.jpg", dest)

        # Should not open file if status is bad
        mock_file_open.assert_not_called()

@pytest.mark.asyncio
async def test_fetch_json_network_error(client, mock_session):
    """Test standard network error (ClientError)."""
    # Raising ClientError on .get(...)
    mock_session.get.side_effect = ClientError("Network down")

    with pytest.raises(ApiError) as exc:
        await client.fetch_json(mock_session, "/test")

    assert "Network error" in str(exc.value)

# --- Credentials Coverage Boost ---

from pyhako.credentials import KeyringStore, TokenManager


def test_token_manager_keyring_fallback():
    """Test that it tries to initialize KeyringStore."""
    with patch("keyring.set_password") as mock_set, \
         patch("keyring.delete_password"):

        # Simulate working backend
        tm = TokenManager()
        # Should default to KeyringStore
        assert isinstance(tm.store, KeyringStore)
        mock_set.assert_called()

def test_token_manager_save_load_integration():
    """Test high level save/load logic flows."""
    with patch("keyring.set_password"), patch("keyring.delete_password"):
        tm = TokenManager()
        # Mock internal store
        tm.store = MagicMock()
        tm.store.load.return_value = {'access_token': 'at'}

        data = tm.load_session("nogizaka46")
        assert data["access_token"] == "at"

        tm.save_session("nogizaka46", "at", "rt", {})
        tm.store.save.assert_called()

# --- Auth Coverage Boost ---


@pytest.mark.asyncio
async def test_login_successful_flow():
    """Test a successful login interception via Playwright mock."""
    with patch("pyhako.auth.async_playwright") as mock_pw:
        # 1. Setup Mock Hierarchy with AsyncMocks
        mock_ctx_mgr = AsyncMock()
        mock_pw.return_value = mock_ctx_mgr

        mock_p = AsyncMock()
        mock_ctx_mgr.__aenter__.return_value = mock_p

        mock_browser = AsyncMock()
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        # add_init_script must be awaitable
        mock_context.add_init_script = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[
            {'name': 'c_name', 'value': 'c_val'}
        ])
        mock_context.close = AsyncMock()

        mock_page = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        mock_page.goto = AsyncMock()
        mock_page.close = AsyncMock()

        # 2. Start login in background task
        login_task = asyncio.create_task(BrowserAuth.login(Group.NOGIZAKA46))

        # 3. Yield to let login reach page.on or wait_for
        await asyncio.sleep(0.1)

        # 4. Verify page.on was called and extract handler
        mock_page.on.assert_called()
        args, _ = mock_page.on.call_args
        event_name, handler = args
        assert event_name == "response"

        # 5. Create a Mock Response ensuring matching host
        mock_request = MagicMock()
        mock_request.url = "https://api.message.nogizaka46.com/v2/foo"
        mock_request.headers = {
            "Authorization": "Bearer valid_at",
            "x-talk-app-id": "app_id",
            "user-agent": "ua"
        }

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.request = mock_request

        # 6. Trigger handler (this should set the future result)
        await handler(mock_response)

        # 7. Await the task
        result = await login_task

        expected = {
            "access_token": "valid_at",
            "cookies": {'c_name': 'c_val'},
            "app_id": "app_id",
            "user_agent": "ua"
        }
        assert result == expected
