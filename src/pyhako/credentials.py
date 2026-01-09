
import base64
import json
import platform
import zlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import structlog

from .exceptions import HakoError

logger = structlog.get_logger()

SERVICE_NAME = "pyhako"

def _compress_data(data: str) -> str:
    """Compress and base64-encode data for storage in size-limited backends."""
    compressed = zlib.compress(data.encode('utf-8'), level=9)
    return base64.b64encode(compressed).decode('ascii')

def _decompress_data(data: str) -> str:
    """Decompress base64-encoded data."""
    try:
        compressed = base64.b64decode(data.encode('ascii'))
        return zlib.decompress(compressed).decode('utf-8')
    except Exception:
        # Fallback: data might not be compressed (legacy)
        return data

def is_windows() -> bool:
    return platform.system() == "Windows"

def get_user_data_dir() -> Path:
    """
    Get the platform-specific user data directory for pyhako.
    
    Returns:
        - Windows: %APPDATA%/pyhako
        - macOS: ~/Library/Application Support/pyhako
        - Linux: ~/.local/share/pyhako
    """
    system = platform.system()

    if system == "Windows":
        appdata = Path.home() / "AppData" / "Roaming" / SERVICE_NAME
    elif system == "Darwin":  # macOS
        appdata = Path.home() / "Library" / "Application Support" / SERVICE_NAME
    else:  # Linux and others
        appdata = Path.home() / ".local" / "share" / SERVICE_NAME

    appdata.mkdir(parents=True, exist_ok=True)
    return appdata

def get_auth_dir() -> Path:
    """
    Get the browser auth data directory for session persistence.
    
    Returns:
        Path to `{user_data_dir}/auth_data` (e.g. ~/.local/share/pyhako/auth_data)
    """
    auth_dir = get_user_data_dir() / "auth_data"
    auth_dir.mkdir(parents=True, exist_ok=True)
    return auth_dir

class CredentialStore(ABC):
    @abstractmethod
    def save(self, group: str, token_data: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def load(self, group: str) -> Optional[dict[str, Any]]:
        pass

    @abstractmethod
    def delete(self, group: str) -> None:
        pass

class KeyringStore(CredentialStore):
    def __init__(self):
        try:
            import keyring

            # Linux Headless Fallback Logic
            # We attempt to verify the backend works. If not, we try keyrings.alt.
            try:
                # Probe the backend with a write operation
                keyring.set_password("pyhako_probe", "probe", "ok")
                keyring.delete_password("pyhako_probe", "probe")
            except Exception as e:
                logger.warning(f"Default keyring backend seems broken (headless?): {e}")

                # Try fallback
                try:
                    from keyrings.alt.file import PlaintextKeyring
                    keyring.set_keyring(PlaintextKeyring())
                    logger.warning("Switched to PlaintextKeyring (keyrings.alt) as fallback.")

                    # Verify fallback
                    keyring.set_password("pyhako_probe", "probe", "ok")
                    keyring.delete_password("pyhako_probe", "probe")
                except ImportError:
                    logger.error("keyrings.alt not found. Cannot provide fallback.")
                    raise e from None
                except Exception as fallback_error:
                    logger.error(f"Fallback backend also failed: {fallback_error}")
                    raise e from None

            self._keyring = keyring
        except ImportError:
            raise HakoError("keyring package is not installed.") from None

    def save(self, group: str, token_data: dict[str, Any]) -> None:
        # Keyring stores strings - compress JSON to fit Windows Credential Manager limits
        try:
            json_data = json.dumps(token_data)
            compressed = _compress_data(json_data)
            self._keyring.set_password(SERVICE_NAME, group, compressed)
        except Exception as e:
             raise HakoError(f"Failed to save credentials to keyring: {e}") from e

    def load(self, group: str) -> Optional[dict[str, Any]]:
        try:
            data = self._keyring.get_password(SERVICE_NAME, group)
            if data:
                # Decompress (handles legacy uncompressed data automatically)
                json_data = _decompress_data(data)
                return json.loads(json_data)
        except Exception as e:
            logger.warning(f"Failed to load credentials for {group}: {e}")
        return None

    def delete(self, group: str) -> None:
        # 1. Delete from currently active backend
        try:
            self._keyring.delete_password(SERVICE_NAME, group)
        except Exception:
            pass # Use silence error if not found

        # 2. Explicitly try to clean up keyrings.alt (Plaintext)
        # This handles cases where user switched between Headless/GUI environments
        try:
            import keyrings.alt.file
            alt_kr = keyrings.alt.file.PlaintextKeyring()
            try:
                alt_kr.delete_password(SERVICE_NAME, group)
                logger.debug(f"Cleaned up residue from keyrings.alt for {group}")
            except Exception:
                pass
        except ImportError:
            pass

        # 3. Explicit check for Windows Credential Manager if on Windows but currently using fallback?
        # Usually checking current backend (Step 1) covers WCM on Windows,
        # as WCM is the priority backend.

class TokenManager:
    """
    Manages token storage using Keyring.
    Strictly requires a working keyring backend.
    """
    def __init__(self):
        try:
            self.store = KeyringStore()
            logger.debug("Using KeyringStore")
        except Exception as e:
            logger.error(f"Keyring initialization failed: {e}")
            raise HakoError(f"Secure storage (keyring) is required but failed to initialize: {e}") from e

    def save_session(self, group: str, access_token: str, refresh_token: str = None, cookies: dict = None) -> None:
        data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "cookies": cookies
        }
        self.store.save(group, data)
        logger.info(f"Session saved for {group}")

    def load_session(self, group: str) -> Optional[dict[str, Any]]:
        return self.store.load(group)

    def delete_session(self, group: str) -> None:
        self.store.delete(group)
