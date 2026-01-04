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
