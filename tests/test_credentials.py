
import sys
from unittest.mock import MagicMock, patch

import pytest

from pyhako.credentials import TokenManager
from pyhako.exceptions import HakoError


@pytest.fixture
def mock_keyring():
    mock = MagicMock()
    with patch.dict(sys.modules, {"keyring": mock}):
        yield mock

def test_token_manager_init_keyring():
    with patch("pyhako.credentials.KeyringStore") as mock_store_cls:
        tm = TokenManager()
        mock_store_cls.assert_called_once()
        assert tm.store == mock_store_cls.return_value

def test_token_manager_init_fail():
    # Force KeyringStore to raise Exception (mimicking ImportError or other init fail)
    with patch("pyhako.credentials.KeyringStore", side_effect=ImportError("fail")):
        with pytest.raises(HakoError) as e:
            TokenManager()
        assert "Secure storage (keyring) is required" in str(e.value)

def test_keyring_save_load(mock_keyring):
    # Enable KeyringStore to succeed init
    with patch.dict(sys.modules, {"keyring": mock_keyring}):
        # Mock Windows backend if needed
        with patch.dict(sys.modules, {"keyring.backends.Windows": MagicMock()}):
             tm = TokenManager()

    # Inject our mock into the instance
    tm.store._keyring = mock_keyring

    # Setup mock behaviors
    stored_data = {}
    def set_password(service, username, password):
        stored_data[username] = password
    def get_password(service, username):
        return stored_data.get(username)

    mock_keyring.set_password.side_effect = set_password
    mock_keyring.get_password.side_effect = get_password

    # Test valid save/load
    tm.save_session("group1", "token123", "refresh123", {"s": "1"})

    # Check what was stored (compressed string)
    assert "group1" in stored_data
    # Data is now compressed - verify by loading
    loaded = tm.load_session("group1")
    assert loaded["access_token"] == "token123"
    assert loaded["cookies"] == {"s": "1"}

    # Test delete
    tm.delete_session("group1")
    mock_keyring.delete_password.assert_called_with("pyhako", "group1")
