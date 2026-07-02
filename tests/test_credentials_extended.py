"""Extended tests for pysaka.credentials module to improve coverage."""

from unittest.mock import MagicMock, patch

import pytest

from pysaka.credentials import (
    PLAINTEXT_FALLBACK_ENV,
    KeyringStore,
    NoSecureKeyringError,
    TokenManager,
    _compress_data,
    _decompress_data,
    get_auth_dir,
    get_user_data_dir,
    is_windows,
)
from pysaka.exceptions import SakaError


class TestCompressionFunctions:
    """Tests for compression/decompression utilities."""

    def test_compress_decompress_roundtrip(self):
        """Test that data can be compressed and decompressed."""
        original = '{"access_token": "secret123", "cookies": {"session": "abc"}}'
        compressed = _compress_data(original)
        decompressed = _decompress_data(compressed)
        assert decompressed == original

    def test_compress_produces_smaller_output(self):
        """Test that compression actually reduces size for large data."""
        # Large repetitive data compresses well
        original = '{"token": "' + "a" * 1000 + '"}'
        compressed = _compress_data(original)
        # Compressed should be shorter for repetitive data
        assert len(compressed) < len(original)

    def test_decompress_handles_uncompressed_data(self):
        """Test that decompress falls back to returning data as-is if not compressed."""
        raw_json = '{"token": "value"}'
        # This is not compressed, should return as-is
        result = _decompress_data(raw_json)
        assert result == raw_json

    def test_decompress_handles_invalid_data(self):
        """Test that invalid compressed data returns original."""
        invalid = "not_valid_base64_or_compressed"
        result = _decompress_data(invalid)
        # Should return original on failure
        assert result == invalid


class TestPlatformFunctions:
    """Tests for platform detection functions."""

    def test_is_windows(self):
        """Test is_windows function."""
        with patch("platform.system", return_value="Windows"):
            assert is_windows() is True

        with patch("platform.system", return_value="Linux"):
            assert is_windows() is False

        with patch("platform.system", return_value="Darwin"):
            assert is_windows() is False


class TestGetUserDataDir:
    """Tests for get_user_data_dir function."""

    def test_get_user_data_dir_windows(self, tmp_path):
        """Test Windows data directory path."""
        mock_home = tmp_path / "home"
        mock_home.mkdir()

        with patch("platform.system", return_value="Windows"), \
             patch("pathlib.Path.home", return_value=mock_home):
            result = get_user_data_dir()
            assert "AppData" in str(result)
            assert "Roaming" in str(result)
            assert "pysaka" in str(result)

    def test_get_user_data_dir_macos(self, tmp_path):
        """Test macOS data directory path."""
        mock_home = tmp_path / "home"
        mock_home.mkdir()

        with patch("platform.system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=mock_home):
            result = get_user_data_dir()
            assert "Library" in str(result)
            assert "Application Support" in str(result)
            assert "pysaka" in str(result)

    def test_get_user_data_dir_linux(self, tmp_path):
        """Test Linux data directory path."""
        mock_home = tmp_path / "home"
        mock_home.mkdir()

        with patch("platform.system", return_value="Linux"), \
             patch("pathlib.Path.home", return_value=mock_home):
            result = get_user_data_dir()
            assert ".local" in str(result)
            assert "share" in str(result)
            assert "pysaka" in str(result)

    def test_get_user_data_dir_creates_directory(self, tmp_path):
        """Test that the directory is created if it doesn't exist."""
        mock_home = tmp_path / "home"
        mock_home.mkdir()

        with patch("platform.system", return_value="Linux"), \
             patch("pathlib.Path.home", return_value=mock_home):
            result = get_user_data_dir()
            # Directory should be created
            assert result.exists()


class TestGetAuthDir:
    """Tests for get_auth_dir function."""

    def test_get_auth_dir_creates_subdirectory(self, tmp_path):
        """Test that auth_data subdirectory is created."""
        mock_home = tmp_path / "home"
        mock_home.mkdir()

        with patch("platform.system", return_value="Linux"), \
             patch("pathlib.Path.home", return_value=mock_home):
            result = get_auth_dir()
            assert "auth_data" in str(result)
            assert result.exists()


class TestKeyringStore:
    """Tests for KeyringStore class."""

    def test_keyring_store_init_success(self):
        """Test successful KeyringStore initialization."""
        with patch("keyring.set_password"), \
             patch("keyring.delete_password"):
            store = KeyringStore()
            assert store._keyring is not None

    def test_keyring_store_init_refuses_plaintext_by_default(self, monkeypatch):
        """Without opt-in, a broken backend must raise NoSecureKeyringError.

        This enforces the opt-in contract (PY-I7/SEC-3): we no longer silently
        downgrade to the insecure PlaintextKeyring on disk.
        """
        monkeypatch.delenv(PLAINTEXT_FALLBACK_ENV, raising=False)
        set_keyring_called = {"count": 0}

        def broken_set(service, key, val):
            raise Exception("Default keyring broken")

        def track_set_keyring(_kr):
            set_keyring_called["count"] += 1

        with patch("keyring.set_password", side_effect=broken_set), \
             patch("keyring.delete_password"), \
             patch("keyring.set_keyring", side_effect=track_set_keyring):
            with pytest.raises(NoSecureKeyringError) as exc:
                KeyringStore()

        # Guidance on how to opt in is included in the message.
        assert PLAINTEXT_FALLBACK_ENV in str(exc.value)
        assert "allow_plaintext_fallback" in str(exc.value)
        # The insecure fallback backend was never installed.
        assert set_keyring_called["count"] == 0

    def test_keyring_store_init_fallback_opt_in_flag(self, monkeypatch):
        """With allow_plaintext_fallback=True, a broken backend uses the fallback."""
        monkeypatch.delenv(PLAINTEXT_FALLBACK_ENV, raising=False)
        call_count = {"set": 0}

        def mock_set(service, key, val):
            call_count["set"] += 1
            if call_count["set"] == 1:
                raise Exception("Default keyring broken")
            # Subsequent calls (probing the fallback) succeed.

        mock_plaintext = MagicMock()
        mock_plaintext.file_path = None  # skip permission tightening in test

        with patch("keyring.set_password", side_effect=mock_set), \
             patch("keyring.delete_password"), \
             patch("keyring.set_keyring") as mock_set_keyring, \
             patch.dict(
                 "sys.modules",
                 {"keyrings.alt.file": MagicMock(PlaintextKeyring=lambda: mock_plaintext)},
             ):
            store = KeyringStore(allow_plaintext_fallback=True)

        # Fallback backend was installed and verified.
        mock_set_keyring.assert_called_once()
        assert call_count["set"] >= 2
        assert store._plaintext_kr is mock_plaintext

    def test_keyring_store_init_fallback_opt_in_env(self, monkeypatch):
        """The PYSAKA_ALLOW_PLAINTEXT_KEYRING env var also enables the fallback."""
        monkeypatch.setenv(PLAINTEXT_FALLBACK_ENV, "1")
        call_count = {"set": 0}

        def mock_set(service, key, val):
            call_count["set"] += 1
            if call_count["set"] == 1:
                raise Exception("Default keyring broken")

        mock_plaintext = MagicMock()
        mock_plaintext.file_path = None

        with patch("keyring.set_password", side_effect=mock_set), \
             patch("keyring.delete_password"), \
             patch("keyring.set_keyring") as mock_set_keyring, \
             patch.dict(
                 "sys.modules",
                 {"keyrings.alt.file": MagicMock(PlaintextKeyring=lambda: mock_plaintext)},
             ):
            KeyringStore()  # no flag; env var opts in

        mock_set_keyring.assert_called_once()

    def test_keyring_store_fallback_sets_posix_permissions(self, monkeypatch, tmp_path):
        """On POSIX, the plaintext store file gets 0600 permissions when used."""
        import os as _os

        monkeypatch.setenv(PLAINTEXT_FALLBACK_ENV, "1")
        store_file = tmp_path / "plaintext.keyring"
        store_file.write_text("dummy")
        _os.chmod(store_file, 0o644)

        call_count = {"set": 0}

        def mock_set(service, key, val):
            call_count["set"] += 1
            if call_count["set"] == 1:
                raise Exception("Default keyring broken")

        mock_plaintext = MagicMock()
        mock_plaintext.file_path = str(store_file)

        with patch("keyring.set_password", side_effect=mock_set), \
             patch("keyring.delete_password"), \
             patch("keyring.set_keyring"), \
             patch("pysaka.credentials.is_windows", return_value=False), \
             patch.dict(
                 "sys.modules",
                 {"keyrings.alt.file": MagicMock(PlaintextKeyring=lambda: mock_plaintext)},
             ):
            KeyringStore()

        mode = store_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_keyring_store_save_compresses_data(self):
        """Test that save compresses data before storing."""
        stored_data = {}

        def mock_set(service, key, val):
            stored_data[key] = val

        with patch("keyring.set_password", side_effect=mock_set), \
             patch("keyring.delete_password"):
            store = KeyringStore()

            token_data = {"access_token": "test", "cookies": {"s": "v"}}
            store.save("group1", token_data)

            # Data should be compressed (base64 encoded)
            assert "group1" in stored_data
            # The stored value should be different from raw JSON
            import json
            raw_json = json.dumps(token_data)
            assert stored_data["group1"] != raw_json

    def test_keyring_store_load_decompresses_data(self):
        """Test that load decompresses data."""
        import json
        token_data = {"access_token": "test123", "cookies": {}}
        compressed = _compress_data(json.dumps(token_data))

        with patch("keyring.set_password"), \
             patch("keyring.delete_password"), \
             patch("keyring.get_password", return_value=compressed):
            store = KeyringStore()
            result = store.load("group1")

            assert result["access_token"] == "test123"

    def test_keyring_store_load_returns_none_on_error(self):
        """Test that load returns None if get_password fails."""
        with patch("keyring.set_password"), \
             patch("keyring.delete_password"), \
             patch("keyring.get_password", side_effect=Exception("Error")):
            store = KeyringStore()
            result = store.load("group1")
            assert result is None

    def test_keyring_store_delete_cleans_all_backends(self):
        """Test that delete attempts to clean multiple backends."""
        delete_calls = []

        def track_delete(service, key):
            delete_calls.append((service, key))

        mock_alt = MagicMock()
        mock_alt.delete_password = MagicMock()

        with patch("keyring.set_password"), \
             patch("keyring.delete_password", side_effect=track_delete), \
             patch.dict("sys.modules", {"keyrings.alt.file": MagicMock(PlaintextKeyring=lambda: mock_alt)}):
            store = KeyringStore()
            store.delete("group1")

            # Should have called delete on main keyring
            assert ("pysaka", "group1") in delete_calls


class TestTokenManager:
    """Tests for TokenManager class."""

    def test_token_manager_requires_keyring(self):
        """Test that TokenManager raises if keyring fails."""
        with patch("pysaka.credentials.KeyringStore", side_effect=Exception("No keyring")):
            with pytest.raises(SakaError) as exc:
                TokenManager()
            assert "Secure storage" in str(exc.value)

    def test_token_manager_propagates_no_secure_keyring_error(self):
        """NoSecureKeyringError must propagate unwrapped so opt-in guidance survives."""
        with patch(
            "pysaka.credentials.KeyringStore",
            side_effect=NoSecureKeyringError("no secure backend; opt in via flag"),
        ):
            with pytest.raises(NoSecureKeyringError) as exc:
                TokenManager()
            # Original guidance preserved (not wrapped in a generic message).
            assert "opt in" in str(exc.value)

    def test_token_manager_passes_fallback_flag(self):
        """allow_plaintext_fallback is forwarded to KeyringStore."""
        with patch("pysaka.credentials.KeyringStore") as mock_store_cls:
            TokenManager(allow_plaintext_fallback=True)
            mock_store_cls.assert_called_once_with(allow_plaintext_fallback=True)

    def test_token_manager_save_session(self):
        """Test save_session method."""
        mock_store = MagicMock()

        with patch("pysaka.credentials.KeyringStore", return_value=mock_store):
            tm = TokenManager()
            tm.save_session("group1", "token", "refresh", {"c": "v"})

            mock_store.save.assert_called_once()
            call_args = mock_store.save.call_args[0]
            assert call_args[0] == "group1"
            assert call_args[1]["access_token"] == "token"
            assert call_args[1]["refresh_token"] == "refresh"
            assert call_args[1]["cookies"] == {"c": "v"}

    def test_token_manager_load_session(self):
        """Test load_session method."""
        mock_store = MagicMock()
        mock_store.load.return_value = {"access_token": "loaded"}

        with patch("pysaka.credentials.KeyringStore", return_value=mock_store):
            tm = TokenManager()
            result = tm.load_session("group1")

            assert result["access_token"] == "loaded"
            mock_store.load.assert_called_once_with("group1")

    def test_token_manager_delete_session(self):
        """Test delete_session method."""
        mock_store = MagicMock()

        with patch("pysaka.credentials.KeyringStore", return_value=mock_store):
            tm = TokenManager()
            tm.delete_session("group1")

            mock_store.delete.assert_called_once_with("group1")
