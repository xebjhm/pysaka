from .auth import BrowserAuth
from .client import Client, Group
from .config import (
    MEDIA_DOWNLOAD_CONCURRENCY_INCREMENTAL,
    MEDIA_DOWNLOAD_CONCURRENCY_INITIAL,
)
from .credentials import get_auth_dir, get_user_data_dir
from .exceptions import ApiError, AuthError, HakoError, SessionExpiredError
from .logging import configure_logging
from .manager import SyncManager
from .utils import (
    get_jwt_remaining_seconds,
    is_jwt_expired,
    parse_jwt_expiry,
    sanitize_name,
)

# Logging should be configured by the application, not the library
# configure_logging()

__all__ = [
    "Client",
    "Group",
    "BrowserAuth",
    "sanitize_name",
    "SyncManager",
    "HakoError",
    "AuthError",
    "ApiError",
    "SessionExpiredError",
    "get_user_data_dir",
    "get_auth_dir",
    "configure_logging",
    # JWT utilities
    "parse_jwt_expiry",
    "get_jwt_remaining_seconds",
    "is_jwt_expired",
    # Config exports
    "MEDIA_DOWNLOAD_CONCURRENCY_INITIAL",
    "MEDIA_DOWNLOAD_CONCURRENCY_INCREMENTAL",
]
