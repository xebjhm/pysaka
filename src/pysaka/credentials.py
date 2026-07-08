import base64
import json
import os
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


# Environment variable that opts in to the insecure plaintext keyring fallback.
# Set to "1"/"true"/"yes"/"on" to allow it when no secure backend is available.
PLAINTEXT_FALLBACK_ENV = "PYSAKA_ALLOW_PLAINTEXT_KEYRING"


class NoSecureKeyringError(SakaError):
    """
    Raised when no secure OS keyring backend is available and the insecure
    plaintext fallback has not been explicitly opted in to.

    The plaintext fallback (``keyrings.alt.file.PlaintextKeyring``) only
    obfuscates data (base64+zlib) and is NOT encryption, so credentials would be
    recoverable by any user-level read. To avoid silently downgrading, we refuse
    to persist unless the caller opts in via the ``allow_plaintext_fallback``
    flag or the ``PYSAKA_ALLOW_PLAINTEXT_KEYRING`` environment variable.
    """

    pass


def _plaintext_fallback_env_enabled() -> bool:
    """Return True if the plaintext fallback env var opts in to the fallback."""
    return os.getenv(PLAINTEXT_FALLBACK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


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
    # Class default so instances built via __new__ (e.g. in tests that inject a
    # fake backend) still expose the attribute; __init__ overrides it with the
    # PlaintextKeyring instance only when the insecure fallback is actually used.
    _plaintext_kr: Any = None

    def __init__(self, allow_plaintext_fallback: bool = False):
        """
        Args:
            allow_plaintext_fallback: If True (or the ``PYSAKA_ALLOW_PLAINTEXT_KEYRING``
                environment variable is set), fall back to the insecure
                ``keyrings.alt.file.PlaintextKeyring`` when no secure OS backend is
                available. This only obfuscates credentials (base64+zlib), it is
                NOT encryption, so it is disabled by default. When not allowed and
                no secure backend exists, a :class:`NoSecureKeyringError` is raised
                instead of silently writing plaintext (PY-I7/SEC-3).
        """
        allow_plaintext = allow_plaintext_fallback or _plaintext_fallback_env_enabled()
        # Holds the PlaintextKeyring instance when the insecure fallback is in use,
        # so we can re-enforce restrictive file permissions on each save.
        self._plaintext_kr: Any = None
        try:
            import keyring

            # Linux Headless Fallback Logic
            # We attempt to verify the backend works. If not, we optionally try
            # keyrings.alt — but only when the caller has explicitly opted in.
            try:
                # Probe the backend with a write operation
                keyring.set_password("pysaka_probe", "probe", "ok")
                keyring.delete_password("pysaka_probe", "probe")
            except Exception as e:
                logger.warning(f"Default keyring backend seems broken (headless?): {e}")

                if not allow_plaintext:
                    # Do NOT silently downgrade to plaintext on disk. Refuse to
                    # persist unless the caller opts in explicitly (PY-I7/SEC-3).
                    logger.error(
                        "No secure keyring backend available and plaintext fallback "
                        "is not allowed. Credentials will not be written in plaintext."
                    )
                    raise NoSecureKeyringError(
                        "No secure keyring backend is available (headless/CI/container "
                        "or locked Secret Service). Refusing to store credentials in "
                        "plaintext. To opt in to the insecure plaintext fallback "
                        "(base64+zlib obfuscation, NOT encryption), pass "
                        "allow_plaintext_fallback=True or set the environment variable "
                        f"{PLAINTEXT_FALLBACK_ENV}=1."
                    ) from e

                # Opt-in plaintext fallback
                try:
                    from keyrings.alt.file import PlaintextKeyring

                    plaintext_kr = PlaintextKeyring()
                    keyring.set_keyring(plaintext_kr)
                    logger.warning(
                        "Switched to insecure PlaintextKeyring (keyrings.alt) fallback. "
                        "Credentials are only obfuscated (base64+zlib), NOT encrypted."
                    )

                    # Verify fallback
                    keyring.set_password("pysaka_probe", "probe", "ok")
                    keyring.delete_password("pysaka_probe", "probe")

                    # On POSIX, tighten permissions on the plaintext store file so
                    # it is not world/group-readable.
                    self._plaintext_kr = plaintext_kr
                    self._secure_plaintext_file(plaintext_kr)
                except ImportError:
                    logger.error("keyrings.alt not found. Cannot provide fallback.")
                    raise e from None
                except NoSecureKeyringError:
                    raise
                except Exception as fallback_error:
                    logger.error(f"Fallback backend also failed: {fallback_error}")
                    raise e from None

            self._keyring = keyring
        except ImportError:
            raise SakaError("keyring package is not installed.") from None

    @staticmethod
    def _secure_plaintext_file(plaintext_kr: Any) -> None:
        """
        On POSIX, set restrictive (0600) permissions on the PlaintextKeyring store
        file if it can be located. Best-effort: any failure is logged and ignored
        (e.g. Windows has no POSIX permissions, file may not exist yet).
        """
        if is_windows():
            return
        try:
            file_path = getattr(plaintext_kr, "file_path", None)
            if not file_path:
                return
            path = Path(file_path)
            if path.exists():
                os.chmod(path, 0o600)
                logger.debug("Restricted plaintext keyring file permissions to 0600", path=str(path))
        except Exception as e:
            logger.warning(f"Could not set restrictive permissions on plaintext keyring file: {e}")

    def save(self, group: str, token_data: dict[str, Any]) -> None:
        # Keyring stores strings - compress JSON to fit Windows Credential Manager limits
        try:
            json_data = json.dumps(token_data)
            compressed = _compress_data(json_data)
            self._keyring.set_password(_service_for(group), _ENTRY_USERNAME, compressed)
        except Exception as e:
            raise SakaError(f"Failed to save credentials to keyring: {e}") from e

        # When the insecure plaintext fallback is in use, re-enforce restrictive
        # permissions on the store file (it may have been (re)created by the write).
        if self._plaintext_kr is not None:
            self._secure_plaintext_file(self._plaintext_kr)

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
            # Greppable id so corrupt/undecodable stored data (a real problem) is
            # distinguishable in logs from the legitimate "never stored" case, which
            # returns None without logging here.
            logger.warning("saka.cred.load_failed", group=group, error=str(e))
        return None

    def _migrate_legacy(self, group: str) -> Optional[str]:
        """Read a credential written under the old shared-service layout
        (service=SERVICE_NAME, username=group) and move it to its own service."""
        legacy = self._keyring.get_password(SERVICE_NAME, group)
        if legacy is None:
            return None
        # Write the isolated copy FIRST. If this fails the migration did NOT happen:
        # surface it (raise) rather than returning the credential as if migrated, and
        # critically do NOT fall through to the delete below — deleting the legacy
        # entry after a failed write would destroy the only surviving copy. Raising
        # leaves the legacy entry intact so the next load retries the migration.
        try:
            self._keyring.set_password(_service_for(group), _ENTRY_USERNAME, legacy)
        except Exception as e:
            logger.error("saka.cred.migrate_write_failed", group=group, error=str(e))
            raise SakaError(f"Failed to migrate legacy credential for {group}: {e}") from e
        # Isolated copy is safely written; the legacy entry can now be removed.
        # delete_password's username guard only removes the matching entry, so this
        # never disturbs another group still on the shared service. A delete failure
        # only leaves harmless residue (the isolated copy is authoritative and new
        # saves never touch the shared service), so log it but don't fail the load.
        try:
            self._keyring.delete_password(SERVICE_NAME, group)
            logger.info("Migrated credential to isolated keyring service", group=group)
        except Exception as e:
            logger.warning("saka.cred.migrate_delete_failed", group=group, error=str(e))
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

    def __init__(self, allow_plaintext_fallback: bool = False):
        try:
            self.store = KeyringStore(allow_plaintext_fallback=allow_plaintext_fallback)
            logger.debug("Using KeyringStore")
        except NoSecureKeyringError:
            # Preserve the clear opt-in guidance instead of wrapping it in a
            # generic message (PY-I7/SEC-3).
            logger.error("Keyring initialization failed: no secure backend available")
            raise
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


def get_token_manager(allow_plaintext_fallback: bool = False) -> TokenManager:
    """
    Get the singleton TokenManager instance.

    This avoids repeated keyring probe operations and log spam
    when TokenManager is accessed from multiple modules.

    Args:
        allow_plaintext_fallback: Opt in to the insecure plaintext keyring
            fallback when no secure OS backend is available. Only applied when the
            singleton is first created (also honored via the
            ``PYSAKA_ALLOW_PLAINTEXT_KEYRING`` environment variable).
    """
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager(allow_plaintext_fallback=allow_plaintext_fallback)
    return _token_manager
