from typing import Optional


class SakaError(Exception):
    """Base exception for pysaka library."""

    pass


class AuthError(SakaError):
    """Authentication related errors."""

    pass


class ApiError(SakaError):
    """API request related errors."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class RateLimitedError(ApiError):
    """The API answered 429 Too Many Requests.

    Raised instead of returning None so a rate-limited response is never mistaken
    for "no data" (an empty group list, timeline or member list). Retry later.

    Attributes:
        retry_after: Seconds from the ``Retry-After`` header; None when the
            header is absent, not a number, not finite or negative. For a
            timeline request refused during the 429 cooldown (never sent) it is
            the remaining cooldown in seconds.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message, 429)
        self.retry_after = retry_after


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
