"""Tests for new API methods added for official app feature parity."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp

from pyhako import ApiError, Client, Group


class TestPostJson:
    """Tests for post_json helper method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_post_json_success(self, client):
        """Test successful POST request returns JSON."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"success": True})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        result = await client.post_json(mock_session, "/test/endpoint", {"key": "value"})
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_post_json_401_with_refresh_success(self, client):
        """Test POST returns data after successful token refresh on 401."""
        # First response is 401
        mock_response_401 = MagicMock()
        mock_response_401.status = 401
        mock_response_401.__aenter__ = AsyncMock(return_value=mock_response_401)
        mock_response_401.__aexit__ = AsyncMock(return_value=None)

        # Retry response is 200
        mock_response_200 = MagicMock()
        mock_response_200.status = 200
        mock_response_200.json = AsyncMock(return_value={"refreshed": True})
        mock_response_200.__aenter__ = AsyncMock(return_value=mock_response_200)
        mock_response_200.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=[mock_response_401, mock_response_200])

        with patch.object(client, 'refresh_access_token', new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = True
            result = await client.post_json(mock_session, "/test/endpoint")
            assert result == {"refreshed": True}
            mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_json_401_refresh_fails(self, client):
        """Test POST returns None when token refresh fails."""
        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        with patch.object(client, 'refresh_access_token', new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = False
            result = await client.post_json(mock_session, "/test/endpoint")
            assert result is None

    @pytest.mark.asyncio
    async def test_post_json_server_error(self, client):
        """Test POST raises ApiError on 5xx status."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        with pytest.raises(ApiError) as exc_info:
            await client.post_json(mock_session, "/test/endpoint")
        assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_post_json_unexpected_status(self, client):
        """Test POST returns None on unexpected status codes."""
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        result = await client.post_json(mock_session, "/test/endpoint")
        assert result is None

    @pytest.mark.asyncio
    async def test_post_json_network_error(self, client):
        """Test POST raises ApiError on network error."""
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=aiohttp.ClientError("Connection failed"))

        with pytest.raises(ApiError) as exc_info:
            await client.post_json(mock_session, "/test/endpoint")
        assert "Network error" in str(exc_info.value)


class TestDeleteJson:
    """Tests for delete_json helper method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_delete_json_success_200(self, client):
        """Test DELETE returns True on 200 status."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.delete = MagicMock(return_value=mock_response)

        result = await client.delete_json(mock_session, "/test/endpoint")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_json_success_204(self, client):
        """Test DELETE returns True on 204 status."""
        mock_response = MagicMock()
        mock_response.status = 204
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.delete = MagicMock(return_value=mock_response)

        result = await client.delete_json(mock_session, "/test/endpoint")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_json_401_with_refresh_success(self, client):
        """Test DELETE succeeds after token refresh on 401."""
        mock_response_401 = MagicMock()
        mock_response_401.status = 401
        mock_response_401.__aenter__ = AsyncMock(return_value=mock_response_401)
        mock_response_401.__aexit__ = AsyncMock(return_value=None)

        mock_response_200 = MagicMock()
        mock_response_200.status = 200
        mock_response_200.__aenter__ = AsyncMock(return_value=mock_response_200)
        mock_response_200.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.delete = MagicMock(side_effect=[mock_response_401, mock_response_200])

        with patch.object(client, 'refresh_access_token', new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = True
            result = await client.delete_json(mock_session, "/test/endpoint")
            assert result is True

    @pytest.mark.asyncio
    async def test_delete_json_401_refresh_fails(self, client):
        """Test DELETE returns False when token refresh fails."""
        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.delete = MagicMock(return_value=mock_response)

        with patch.object(client, 'refresh_access_token', new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = False
            result = await client.delete_json(mock_session, "/test/endpoint")
            assert result is False

    @pytest.mark.asyncio
    async def test_delete_json_other_status(self, client):
        """Test DELETE returns False on other status codes."""
        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.delete = MagicMock(return_value=mock_response)

        result = await client.delete_json(mock_session, "/test/endpoint")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_json_exception(self, client):
        """Test DELETE returns False on exception."""
        mock_session = MagicMock()
        mock_session.delete = MagicMock(side_effect=Exception("Connection error"))

        result = await client.delete_json(mock_session, "/test/endpoint")
        assert result is False


class TestGetLetters:
    """Tests for get_letters method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_get_letters_success(self, client):
        """Test get_letters returns letters list."""
        mock_letters = [{"id": 1, "content": "Letter 1"}, {"id": 2, "content": "Letter 2"}]

        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"letters": mock_letters}
            result = await client.get_letters(MagicMock(), 123)
            assert result == mock_letters
            mock_fetch.assert_called_once()
            call_args = mock_fetch.call_args
            assert "/groups/123/letters" in str(call_args)

    @pytest.mark.asyncio
    async def test_get_letters_with_updated_from(self, client):
        """Test get_letters passes updated_from parameter."""
        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"letters": []}
            await client.get_letters(MagicMock(), 123, updated_from="2024-01-01T00:00:00Z")
            call_args = mock_fetch.call_args
            params = call_args[0][2]  # Third arg is params
            assert params.get("updated_from") == "2024-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_get_letters_empty(self, client):
        """Test get_letters returns empty list when no letters."""
        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = None
            result = await client.get_letters(MagicMock(), 123)
            assert result == []


class TestGetPastMessages:
    """Tests for get_past_messages method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_get_past_messages_success(self, client):
        """Test get_past_messages returns messages list."""
        mock_messages = [{"id": 1, "content": "Old message"}]

        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"messages": mock_messages}
            result = await client.get_past_messages(MagicMock(), 123)
            assert result == mock_messages

    @pytest.mark.asyncio
    async def test_get_past_messages_empty(self, client):
        """Test get_past_messages returns empty list when no messages."""
        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {}
            result = await client.get_past_messages(MagicMock(), 123)
            assert result == []


class TestGetSubscriptionStreak:
    """Tests for get_subscription_streak method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_get_subscription_streak_success(self, client):
        """Test get_subscription_streak returns streak data."""
        mock_streak = {"consecutive_days": 30, "start_date": "2024-01-01"}
        mock_session = MagicMock()

        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_streak
            result = await client.get_subscription_streak(mock_session, 123)
            assert result == mock_streak
            mock_fetch.assert_called_once()
            call_args = mock_fetch.call_args[0]
            assert call_args[1] == "/groups/123/consecutive-subscription-day"


class TestGetMember:
    """Tests for get_member method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_get_member_success(self, client):
        """Test get_member returns member data."""
        mock_member = {"id": 123, "name": "Test Member"}

        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_member
            result = await client.get_member(MagicMock(), 123)
            assert result == mock_member


class TestGetAccount:
    """Tests for get_account method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_get_account_success(self, client):
        """Test get_account returns account data."""
        mock_account = {"id": 1, "email": "test@example.com"}
        mock_session = MagicMock()

        with patch.object(client, 'fetch_json', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_account
            result = await client.get_account(mock_session)
            assert result == mock_account
            mock_fetch.assert_called_once()
            call_args = mock_fetch.call_args[0]
            assert call_args[1] == "/account"


class TestAddFavorite:
    """Tests for add_favorite method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_add_favorite_success(self, client):
        """Test add_favorite returns True on success."""
        with patch.object(client, 'post_json', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {}  # API returns empty object on success
            result = await client.add_favorite(MagicMock(), 123)
            assert result is True
            mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_favorite_failure(self, client):
        """Test add_favorite returns False on failure."""
        with patch.object(client, 'post_json', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = None
            result = await client.add_favorite(MagicMock(), 123)
            assert result is False


class TestRemoveFavorite:
    """Tests for remove_favorite method."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.NOGIZAKA46, access_token="test_token")

    @pytest.mark.asyncio
    async def test_remove_favorite_success(self, client):
        """Test remove_favorite returns True on success."""
        with patch.object(client, 'delete_json', new_callable=AsyncMock) as mock_delete:
            mock_delete.return_value = True
            result = await client.remove_favorite(MagicMock(), 123)
            assert result is True

    @pytest.mark.asyncio
    async def test_remove_favorite_failure(self, client):
        """Test remove_favorite returns False on failure."""
        with patch.object(client, 'delete_json', new_callable=AsyncMock) as mock_delete:
            mock_delete.return_value = False
            result = await client.remove_favorite(MagicMock(), 123)
            assert result is False
