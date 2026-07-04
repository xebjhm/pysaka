import base64
import json
import platform
import zlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import structlog

from .exceptions import SakaError

logger = structlog.get_logger()

SERVICE_NAME = "pysaka"

# Each credential is stored under its OWN keyring service ("pysaka:<group>")
# with a single fixed entry name. keyring's Windows backend always writes the
# newest credential for a service to the bare "service" target and relocates the
# previous occupant to "user@service" (see keyring backends/Windows.py). Sharing
# one service across many groups therefore churns every credential through a
# single target, letting them clobber one another -- e.g. a routine login/token
# re-save (which happens around an app update or reinstall) overwrites the stored
# LLM API key. One credential per service removes that shared target entirely.
_ENTRY_USERNAME = "credential"


def _service_for(group: str) -> str:
    """Keyring service name isolating a single credential group."""
    return f"{SERVICE_NAME}:{group}"


def _compress_data(data: str) -> str:
    """Compress and base64-encode data for storage in size-limited backends."""
    compressed = zlib.compress(data.encode("utf-8"), level=9)
    return base64.b64encode(compressed).decode("ascii")


def _decompress_data(data: str) -> str:
    """Decompress base64-encoded data."""
    try:
        compressed = base64.b64decode(data.encode("ascii"))
        return zlib.decompress(compressed).decode("utf-8")
    except Exception as e:
        # Fallback: data might not be compressed (legacy)
        logger.debug("Data not compressed (legacy format), using raw", error=str(e))
        return data


def is_windows() -> bool:
    return platform.system() == "Windows"


def get_user_data_dir() -> Path:
    """
    Get the platform-specific user data directory for pysaka.

    Returns:
        - Windows: %APPDATA%/pysaka
        - macOS: ~/Library/Application Support/pysaka
        - Linux: ~/.local/share/pysaka
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
        Path to `{user_data_dir}/auth_data` (e.g. ~/.local/share/pysaka/auth_data)
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
                keyring.set_password("pysaka_probe", "probe", "ok")
                keyring.delete_password("pysaka_probe", "probe")
            except Exception as e:
                logger.warning(f"Default keyring backend seems broken (headless?): {e}")

                # Try fallback
                try:
                    from keyrings.alt.file import PlaintextKeyring

                    keyring.set_keyring(PlaintextKeyring())
                    logger.warning("Switched to PlaintextKeyring (keyrings.alt) as fallback.")

                    # Verify fallback
                    keyring.set_password("pysaka_probe", "probe", "ok")
                    keyring.delete_password("pysaka_probe", "probe")
                except ImportError:
                    logger.error("keyrings.alt not found. Cannot provide fallback.")
                    raise e from None
                except Exception as fallback_error:
                    logger.error(f"Fallback backend also failed: {fallback_error}")
                    raise e from None

            self._keyring = keyring
        except ImportError:
            raise SakaError("keyring package is not installed.") from None

    def save(self, group: str, token_data: dict[str, Any]) -> None:
        # Keyring stores strings - compress JSON to fit Windows Credential Manager limits
        try:
            json_data = json.dumps(token_data)
            compressed = _compress_data(json_data)
            self._keyring.set_password(_service_for(group), _ENTRY_USERNAME, compressed)
        except Exception as e:
            raise SakaError(f"Failed to save credentials to keyring: {e}") from e

    def load(self, group: str) -> Optional[dict[str, Any]]:
        try:
            data = self._keyring.get_password(_service_for(group), _ENTRY_USERNAME)
            if data is None:
                # Fall back to the old shared-service layout and migrate it, so
                # users upgrading from a pre-fix version keep their credentials.
                data = self._migrate_legacy(group)
            if data:
                # Decompress (handles legacy uncompressed data automatically)
                json_data = _decompress_data(data)
                return json.loads(json_data)
        except Exception as e:
            logger.warning(f"Failed to load credentials for {group}: {e}")
        return None

    def _migrate_legacy(self, group: str) -> Optional[str]:
        """Read a credential written under the old shared-service layout
        (service=SERVICE_NAME, username=group) and move it to its own service."""
        legacy = self._keyring.get_password(SERVICE_NAME, group)
        if legacy is None:
            return None
        try:
            self._keyring.set_password(_service_for(group), _ENTRY_USERNAME, legacy)
            # delete_password's username guard only removes the matching entry,
            # so this never disturbs another group still on the shared service.
            self._keyring.delete_password(SERVICE_NAME, group)
            logger.info("Migrated credential to isolated keyring service", group=group)
        except Exception as e:
            logger.warning("Legacy credential migration failed", group=group, error=str(e))
        return legacy

    def delete(self, group: str) -> None:
        # Delete both the isolated credential and any legacy shared-service entry.
        for service, username in ((_service_for(group), _ENTRY_USERNAME), (SERVICE_NAME, group)):
            try:
                self._keyring.delete_password(service, username)
                logger.debug("Deleted credentials from keyring", service=service, group=group)
            except Exception as e:
                logger.debug(
                    "Keyring delete failed (may not exist)", service=service, group=group, error=str(e)
                )

        # Explicitly clean up keyrings.alt (Plaintext) residue for both layouts.
        # This handles cases where a user switched between Headless/GUI environments.
        try:
            import keyrings.alt.file

            alt_kr = keyrings.alt.file.PlaintextKeyring()
            for service, username in ((_service_for(group), _ENTRY_USERNAME), (SERVICE_NAME, group)):
                try:
                    alt_kr.delete_password(service, username)
                    logger.debug("Cleaned up residue from keyrings.alt", service=service, group=group)
                except Exception as e:
                    logger.debug(
                        "keyrings.alt delete failed (may not exist)", service=service, group=group, error=str(e)
                    )
        except ImportError:
            pass


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
            raise SakaError(f"Secure storage (keyring) is required but failed to initialize: {e}") from e

    def save_session(
        self,
        group: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        cookies: Optional[dict[Any, Any]] = None,
    ) -> None:
        data = {"access_token": access_token, "refresh_token": refresh_token, "cookies": cookies}
        self.store.save(group, data)
        logger.info(f"Session saved for {group}")

    def load_session(self, group: str) -> Optional[dict[str, Any]]:
        data = self.store.load(group)
        if data:
            logger.debug(
                "Session loaded",
                group=group,
                has_token=bool(data.get("access_token")),
                has_refresh=bool(data.get("refresh_token")),
            )
        else:
            logger.debug("No session found", group=group)
        return data

    def delete_session(self, group: str) -> None:
        self.store.delete(group)
        logger.info("Session deleted", group=group)


# Singleton instance
_token_manager: Optional[TokenManager] = None


def get_token_manager() -> TokenManager:
    """
    Get the singleton TokenManager instance.

    This avoids repeated keyring probe operations and log spam
    when TokenManager is accessed from multiple modules.
    """
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager()
    return _token_manager
