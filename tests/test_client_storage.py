from unittest.mock import patch

import pytest

from pyhako import Client, Group


@pytest.fixture
def mock_token_manager():
    mock_tm = patch("pyhako.client.get_token_manager")
    with mock_tm as mock_fn:
        yield mock_fn

@pytest.mark.asyncio
async def test_client_init_auto_load(mock_token_manager):
    # Setup mock manager instance
    manager_instance = mock_token_manager.return_value
    manager_instance.load_session.return_value = {
        "access_token": "stored_token",
        "refresh_token": "stored_refresh",
        "cookies": {"s": "1"}
    }

    # Init client with storage
    client = Client(group=Group.HINATAZAKA46, use_token_storage=True)

    # Verify it tried to load
    mock_token_manager.assert_called_with()
    manager_instance.load_session.assert_called_with(Group.HINATAZAKA46.value)

    # Verify properties set
    assert client.access_token == "stored_token"
    assert client.refresh_token == "stored_refresh"
    assert client.cookies == {"s": "1"}
    assert client.headers["Authorization"] == "Bearer stored_token"

@pytest.mark.asyncio
async def test_client_update_token_auto_save(mock_token_manager):
    # Configure mock to return None to avoid auto-loading junk mocks
    mock_token_manager.return_value.load_session.return_value = None

    client = Client(group=Group.HINATAZAKA46, use_token_storage=True)
    manager_instance = mock_token_manager.return_value

    # Call update_token
    await client.update_token("new_token_123")

    # Verify save called
    manager_instance.save_session.assert_called_with(
        Group.HINATAZAKA46.value,
        "new_token_123",
        None, # Refresh token wasn't set locally
        None  # Cookies weren't set locally
    )

@pytest.mark.asyncio
async def test_client_manual_save(mock_token_manager):
    client = Client(
        group=Group.HINATAZAKA46,
        access_token="manual_token",
        use_token_storage=True
    )
    manager_instance = mock_token_manager.return_value

    client.save_session()

    manager_instance.save_session.assert_called_with(
        Group.HINATAZAKA46.value,
        "manual_token",
        None,
        None
    )
