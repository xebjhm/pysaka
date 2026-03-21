import base64
import json
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

MEDIA_EXTENSIONS: dict[str, str] = {
    'image': 'jpg', 'picture': 'jpg',
    'voice': 'm4a',
    'movie': 'mp4', 'video': 'mp4'
}

def sanitize_name(name: str) -> str:
    """
    Sanitize directory names to be filesystem-safe.

    Args:
        name: The raw input string.

    Returns:
        Safe string with '/' replaced by '_', but preserving spaces for readability.
    """
    return name.replace('/', '_').strip()

def get_media_extension(url: Optional[str], msg_type: str) -> str:
    """
    Determine file extension from URL or fallback to type default.

    Args:
        url: The download URL.
        msg_type: The message type (e.g., 'picture').

    Returns:
        The detected or default extension (no dot).
    """
    if url:
        parsed = urlparse(url)
        path = parsed.path
        if '.' in path:
            ext = path.split('.')[-1].lower()
            if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'm4a', 'mp3', 'wav', 'mp4', 'mov', 'webm']:
                return ext
    return MEDIA_EXTENSIONS.get(msg_type, 'bin')

def parse_jwt_expiry(token: str) -> Optional[int]:
    """
    Extract expiry timestamp from a JWT token.

    Decodes the JWT payload (without verification) to extract the 'exp' claim.
    This is sufficient for client-side token lifetime management.

    Args:
        token: A JWT token string (header.payload.signature format).

    Returns:
        Unix timestamp (seconds since epoch) when the token expires,
        or None if the token is invalid or has no 'exp' claim.

    Example:
        >>> token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3MDAwMDAwMDB9.sig"
        >>> exp = parse_jwt_expiry(token)
        >>> if exp and exp > time.time():
        ...     print("Token is valid")
    """
    if not token:
        return None

    try:
        parts = token.split('.')
        if len(parts) < 2:
            return None

        # JWT payload is base64url encoded
        payload = parts[1]
        # Add padding for base64 decode
        payload += '=' * (4 - len(payload) % 4)
        decoded = base64.b64decode(payload)
        data = json.loads(decoded)

        if 'exp' in data:
            return int(data['exp'])
    except Exception:
        pass

    return None


def get_jwt_remaining_seconds(token: str) -> Optional[int]:
    """
    Get seconds remaining until a JWT token expires.

    Convenience function that wraps parse_jwt_expiry() and calculates
    the time remaining from now.

    Args:
        token: A JWT token string.

    Returns:
        Seconds remaining until expiry (can be negative if expired),
        or None if the token is invalid or has no 'exp' claim.

    Example:
        >>> remaining = get_jwt_remaining_seconds(token)
        >>> if remaining is not None and remaining < 300:
        ...     # Token expires in less than 5 minutes
        ...     await refresh_token()
    """
    exp_timestamp = parse_jwt_expiry(token)
    if exp_timestamp is None:
        return None

    now = datetime.now(timezone.utc).timestamp()
    return int(exp_timestamp - now)


def is_jwt_expired(token: str) -> bool:
    """
    Check if a JWT token is expired.

    Args:
        token: A JWT token string.

    Returns:
        True if the token is expired or cannot be parsed, False otherwise.
        Returns True on parse failure as a safe default (treat unknown as expired).

    Example:
        >>> if is_jwt_expired(access_token):
        ...     raise SessionExpiredError("Token has expired")
    """
    remaining = get_jwt_remaining_seconds(token)
    if remaining is None:
        return True  # Assume expired if can't parse
    return remaining <= 0


def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Normalizes a raw API message into the standard export format.
    Handles type mapping (image->picture, movie->video) and field selection.

    Args:
        msg: Raw API message dictionary.

    Returns:
        Normalized message dictionary.
    """
    # Map type to spec: text, video, picture, voice
    raw_type = msg.get('type')
    msg_type = 'text'
    if raw_type in ['image', 'picture']:
        msg_type = 'picture'
    elif raw_type in ['video', 'movie']:
        msg_type = 'video'
    elif raw_type in ['voice']:
        msg_type = 'voice'

    return {
        "id": msg['id'],
        "timestamp": msg.get('published_at'), # ISO string from API
        "type": msg_type,
        "is_favorite": msg.get('is_favorite', False),
        "content": msg.get('text'),
        # raw type useful for extension determination later
        "_raw_type": raw_type
    }
