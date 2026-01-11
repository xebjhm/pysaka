from .auth import BrowserAuth
from .client import Client, Group
from .credentials import get_auth_dir, get_user_data_dir
from .exceptions import ApiError, AuthError, HakoError, SessionExpiredError
from .logging import configure_logging
from .manager import SyncManager
from .utils import sanitize_name

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
]
