"""Extended tests for pyhako.client module to improve coverage."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyhako import ApiError, Client, Group, SessionExpiredError


class TestClientInitialization:
    """Tests for Client initialization."""

    def test_client_init_with_all_groups(self):
        """Test client initialization with all supported groups."""
        for group in Group:
            client = Client(group=group)
            assert client.group == group
            assert group.value in client.api_base

    def test_client_init_with_string_groups(self):
        """Test client initialization with string group names."""
        for group_str in ["hinatazaka46", "nogizaka46", "sakurazaka46"]:
            client = Client(group=group_str)
            assert client.group.value == group_str

    def test_client_init_case_insensitive(self):
        """Test that string groups are case-insensitive."""
        client = Client(group="HINATAZAKA46")
        assert client.group == Group.HINATAZAKA46

    def test_client_init_with_custom_app_id(self):
        """Test client initialization with custom app_id."""
        client = Client(group=Group.NOGIZAKA46, app_id="custom_app_id")
        assert client.app_id == "custom_app_id"
        assert client.headers["x-talk-app-id"] == "custom_app_id"

    def test_client_init_with_custom_user_agent(self):
        """Test client initialization with custom user agent."""
        client = Client(group=Group.NOGIZAKA46, user_agent="CustomAgent/1.0")
        assert client.user_agent == "CustomAgent/1.0"

    def test_client_init_sets_auth_header(self):
        """Test that Authorization header is set when access_token provided."""
        client = Client(group=Group.NOGIZAKA46, access_token="test_token")
        assert client.headers["Authorization"] == "Bearer test_token"

    def test_client_init_without_auth_header(self):
        """Test that Authorization header is not set without access_token."""
        client = Client(group=Group.NOGIZAKA46)
        assert "Authorization" not in client.headers


class TestClientTokenStorage:
    """Tests for Client token storage integration."""

    def test_client_with_token_storage_enabled(self):
        """Test client with use_token_storage=True."""
        mock_tm = MagicMock()
        mock_tm.load_session.return_value = {
            "access_token": "stored_token",
            "refresh_token": "stored_refresh",
            "cookies": {"s": "v"}
        }

        with patch("pyhako.client.TokenManager", return_value=mock_tm):
            client = Client(group=Group.NOGIZAKA46, use_token_storage=True)

            assert client.access_token == "stored_token"
            assert client.refresh_token == "stored_refresh"
            assert client.cookies == {"s": "v"}
            assert client.token_manager is not None

    def test_client_token_storage_explicit_token_takes_precedence(self):
        """Test that explicit token overrides stored token."""
        mock_tm = MagicMock()
        mock_tm.load_session.return_value = {"access_token": "stored"}

        with patch("pyhako.client.TokenManager", return_value=mock_tm):
            client = Client(
                group=Group.NOGIZAKA46,
                access_token="explicit_token",
                use_token_storage=True
            )

            # Explicit token should not be overwritten
            assert client.access_token == "explicit_token"

    def test_client_token_storage_failure_is_handled(self):
        """Test that token storage failures are handled gracefully."""
        with patch("pyhako.client.TokenManager", side_effect=Exception("Storage failed")):
            # Should not raise, just log warning
            client = Client(group=Group.NOGIZAKA46, use_token_storage=True)
            assert client.token_manager is None


class TestClientUpdateToken:
    """Tests for Client.update_token method."""

    @pytest.mark.asyncio
    async def test_update_token_updates_header(self):
        """Test that update_token updates the Authorization header."""
        client = Client(group=Group.NOGIZAKA46, access_token="old_token")
        await client.update_token("new_token")

        assert client.access_token == "new_token"
        assert client.headers["Authorization"] == "Bearer new_token"

    @pytest.mark.asyncio
    async def test_update_token_with_refresh_token(self):
        """Test that update_token can also update refresh token."""
        client = Client(group=Group.NOGIZAKA46, access_token="old")
        await client.update_token("new_at", "new_rt")

        assert client.access_token == "new_at"
        assert client.refresh_token == "new_rt"

    @pytest.mark.asyncio
    async def test_update_token_saves_to_storage(self):
        """Test that update_token saves to storage if configured."""
        mock_tm = MagicMock()
        mock_tm.load_session.return_value = None

        with patch("pyhako.client.TokenManager", return_value=mock_tm):
            client = Client(group=Group.NOGIZAKA46, use_token_storage=True)
            client.access_token = "initial"

            await client.update_token("new_token")

            mock_tm.save_session.assert_called_once()


class TestClientSaveSession:
    """Tests for Client.save_session method."""

    def test_save_session_with_storage(self):
        """Test manual save_session with storage configured."""
        mock_tm = MagicMock()
        mock_tm.load_session.return_value = None

        with patch("pyhako.client.TokenManager", return_value=mock_tm):
            client = Client(
                group=Group.NOGIZAKA46,
                access_token="token",
                use_token_storage=True
            )
            client.save_session()

            mock_tm.save_session.assert_called_once()

    def test_save_session_without_storage(self):
        """Test save_session does nothing without storage."""
        client = Client(group=Group.NOGIZAKA46, access_token="token")
        # Should not raise
        client.save_session()


class TestClientFetchJson:
    """Tests for Client.fetch_json method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = AsyncMock()
        session.get.return_value.__aexit__.return_value = None
        session.post.return_value.__aenter__.return_value = AsyncMock()
        session.post.return_value.__aexit__.return_value = None
        return session

    @pytest.mark.asyncio
    async def test_fetch_json_with_params(self, client, mock_session):
        """Test fetch_json passes query parameters."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"data": "value"})

        result = await client.fetch_json(mock_session, "/test", {"key": "val"})

        assert result == {"data": "value"}
        mock_session.get.assert_called_once()
        call_kwargs = mock_session.get.call_args[1]
        assert call_kwargs["params"] == {"key": "val"}

    @pytest.mark.asyncio
    async def test_fetch_json_401_no_credentials(self, client, mock_session):
        """Test fetch_json handles 401 without credentials for refresh."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 401

        # No refresh token or cookies
        client.refresh_token = None
        client.cookies = None
        client.auth_dir = None

        result = await client.fetch_json(mock_session, "/test")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_json_unexpected_status(self, client, mock_session):
        """Test fetch_json handles unexpected status codes."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 403  # Forbidden

        result = await client.fetch_json(mock_session, "/test")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_json_generic_exception(self, client, mock_session):
        """Test fetch_json handles generic exceptions."""
        mock_session.get.return_value.__aenter__.side_effect = Exception("Random error")

        result = await client.fetch_json(mock_session, "/test")
        assert result is None


class TestClientRefreshToken:
    """Tests for Client.refresh_access_token method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.post.return_value.__aenter__.return_value = AsyncMock()
        session.post.return_value.__aexit__.return_value = None
        return session

    @pytest.mark.asyncio
    async def test_refresh_with_cookie_rotation(self, client, mock_session):
        """Test that refresh captures rotated cookies."""
        client.cookies = {"session": "old_cookie"}
        client.refresh_token = None

        mock_resp = mock_session.post.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"access_token": "new_token"})

        # Mock cookie rotation - cookies needs to be a dict-like object
        # that returns cookie objects with .value attribute when iterated
        mock_cookie = MagicMock()
        mock_cookie.value = "new_cookie"

        # Create a proper items() that returns tuples of (name, cookie_obj)
        mock_resp.cookies = {"session": mock_cookie}

        result = await client.refresh_access_token(mock_session)

        assert result is True
        assert client.cookies["session"] == "new_cookie"

    @pytest.mark.asyncio
    async def test_refresh_session_invalidated(self, client, mock_session):
        """Test that refresh raises SessionExpiredError on invalidation."""
        client.cookies = {"session": "invalid"}
        client.refresh_token = None

        mock_resp = mock_session.post.return_value.__aenter__.return_value
        mock_resp.status = 400
        mock_resp.json = AsyncMock(return_value={"code": "invalid_parameter"})

        with pytest.raises(SessionExpiredError):
            await client.refresh_access_token(mock_session)

    @pytest.mark.asyncio
    async def test_refresh_with_auth_dir_fallback(self, client, mock_session, tmp_path):
        """Test that refresh tries headless browser if auth_dir exists."""
        client.cookies = None
        client.refresh_token = None
        client.auth_dir = tmp_path  # Exists

        # Import path for BrowserAuth is pyhako.auth, not pyhako.client
        with patch("pyhako.auth.BrowserAuth.refresh_token_headless") as mock_refresh:
            mock_refresh.return_value = {
                "access_token": "headless_token",
                "cookies": {"s": "v"}
            }

            result = await client.refresh_access_token(mock_session)

            assert result is True
            assert client.access_token == "headless_token"


class TestClientDownloadFile:
    """Tests for Client.download_file method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46)

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = AsyncMock()
        session.get.return_value.__aexit__.return_value = None
        return session

    @pytest.mark.asyncio
    async def test_download_file_already_exists(self, client, mock_session, tmp_path):
        """Test that download skips if file exists."""
        existing_file = tmp_path / "existing.jpg"
        existing_file.write_bytes(b"content")

        result = await client.download_file(mock_session, "http://example.com/file.jpg", existing_file)

        assert result is True
        mock_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_file_empty_url(self, client, mock_session, tmp_path):
        """Test that download returns True for empty URL."""
        result = await client.download_file(mock_session, "", tmp_path / "file.jpg")
        assert result is True

    @pytest.mark.asyncio
    async def test_download_file_creates_parent_dirs(self, client, mock_session, tmp_path):
        """Test that download creates parent directories."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"file_content")

        filepath = tmp_path / "nested" / "dirs" / "file.jpg"

        with patch("aiofiles.open", new_callable=MagicMock) as mock_open:
            mock_file = AsyncMock()
            mock_open.return_value.__aenter__.return_value = mock_file

            result = await client.download_file(mock_session, "http://example.com/file.jpg", filepath)

            assert result is True
            assert filepath.parent.exists()


class TestClientGetMessages:
    """Tests for Client.get_messages method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test")

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = AsyncMock()
        session.get.return_value.__aexit__.return_value = None
        session.post.return_value.__aenter__.return_value = AsyncMock()
        session.post.return_value.__aexit__.return_value = None
        return session

    @pytest.mark.asyncio
    async def test_get_messages_with_since_id(self, client, mock_session):
        """Test that since_id filtering works."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "messages": [
                {"id": 100},
                {"id": 50},  # Below since_id
                {"id": 30},  # Below since_id
            ],
            "continuation": None
        })

        messages = await client.get_messages(mock_session, group_id=1, since_id=60)

        # Only id=100 should be included (others <= since_id)
        assert len(messages) == 1
        assert messages[0]["id"] == 100

    @pytest.mark.asyncio
    async def test_get_messages_with_progress_callback(self, client, mock_session):
        """Test that progress callback is called."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "messages": [{"id": 1, "published_at": "2024-01-01"}],
            "continuation": None
        })

        callback_calls = []

        def progress_cb(date, count):
            callback_calls.append((date, count))

        await client.get_messages(mock_session, group_id=1, progress_callback=progress_cb)

        assert len(callback_calls) == 1

    @pytest.mark.asyncio
    async def test_get_messages_with_async_progress_callback(self, client, mock_session):
        """Test that async progress callback is awaited."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "messages": [{"id": 1, "published_at": "2024-01-01"}],
            "continuation": None
        })

        callback_calls = []

        async def async_progress_cb(date, count):
            callback_calls.append((date, count))

        await client.get_messages(mock_session, group_id=1, progress_callback=async_progress_cb)

        assert len(callback_calls) == 1


class TestClientGetNews:
    """Tests for Client.get_news method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test")

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = AsyncMock()
        session.get.return_value.__aexit__.return_value = None
        return session

    @pytest.mark.asyncio
    async def test_get_news_empty_response(self, client, mock_session):
        """Test get_news handles missing announcements key."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={})

        result = await client.get_news(mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_news_with_count(self, client, mock_session):
        """Test get_news with custom count parameter."""
        mock_resp = mock_session.get.return_value.__aenter__.return_value
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"announcements": []})

        await client.get_news(mock_session, count=50)

        call_kwargs = mock_session.get.call_args[1]
        assert call_kwargs["params"]["count"] == 50
