from typing import Optional


class HakoError(Exception):
    """Base exception for PyHako library."""
    pass

class AuthError(HakoError):
    """Authentication related errors."""
    pass

class ApiError(HakoError):
    """API request related errors."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code

class SessionExpiredError(AuthError):
    """
    Raised when the session has been invalidated server-side.

    This typically happens when:
    - The user logged in from another device/browser
    - The session was explicitly revoked on the server

    User message: "Authentication session has expired. Please log in again to continue using the service."
    """
    pass


class RefreshFailedError(AuthError):
    """
    Raised when all token refresh attempts have failed unexpectedly.

    This indicates a potential bug or unexpected server behavior,
    as opposed to SessionExpiredError which is an expected scenario.

    User message: "Authentication failed unexpectedly. Please log in again to continue using the service."
    Optional: Include "Report issue" action since this is unexpected.
    """
    pass
