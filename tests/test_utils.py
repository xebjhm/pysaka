import base64
import json
import time

from pysaka.utils import (
    get_jwt_remaining_seconds,
    get_media_extension,
    is_jwt_expired,
    normalize_message,
    parse_jwt_expiry,
    sanitize_name,
)


def test_sanitize_name_windows_invalid_chars():
    """PY-M6: strip the full Windows-invalid set, not just '/'."""
    # All reserved characters get replaced with '_'.
    assert sanitize_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    # Control characters are stripped too.
    assert sanitize_name("name\x00\x1f") == "name__"
    # Trailing dots and spaces (silently dropped by Windows) are removed.
    assert sanitize_name("member name... ") == "member name"
    assert sanitize_name("group.") == "group"
    # Interior spaces and dots are preserved for readability.
    assert sanitize_name("Kobayashi Yui") == "Kobayashi Yui"
    assert sanitize_name("v2.5 update") == "v2.5 update"


def test_get_media_extension():
    # Test valid extension extraction
    assert get_media_extension("http://example.com/file.jpg", "image") == "jpg"
    assert get_media_extension("http://example.com/file.mp4?query=1", "video") == "mp4"

    # Test backup map
    assert get_media_extension("http://example.com/file", "image") == "jpg"
    assert get_media_extension(None, "voice") == "m4a"

def test_normalize_message():
    raw_msg = {
        "id": 123,
        "published_at": "2023-01-01T00:00:00Z",
        "text": "Hello",
        "type": "image",
        "file": "http://img.com/a.jpg"
    }

    normalized = normalize_message(raw_msg)

    assert normalized['id'] == 123
    assert normalized['type'] == 'picture'  # Mapping check
    assert normalized['content'] == 'Hello'
    assert normalized['is_favorite'] is False
    assert '_raw_type' in normalized # Internal field presence check

def test_normalize_message_types():
    assert normalize_message({"id":1, "type": "movie"})['type'] == 'video'
    assert normalize_message({"id":1, "type": "voice"})['type'] == 'voice'
    assert normalize_message({"id":1, "type": "text"})['type'] == 'text'


# ============================================================================
# JWT Utility Tests
# ============================================================================

def _make_jwt(payload: dict) -> str:
    """Helper to create a test JWT (unsigned, for testing only)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.signature"


class TestParseJwtExpiry:
    """Tests for parse_jwt_expiry function."""

    def test_valid_token_with_exp(self):
        exp_time = int(time.time()) + 3600  # 1 hour from now
        token = _make_jwt({"exp": exp_time, "sub": "user123"})
        assert parse_jwt_expiry(token) == exp_time

    def test_valid_token_without_exp(self):
        token = _make_jwt({"sub": "user123"})
        assert parse_jwt_expiry(token) is None

    def test_empty_token(self):
        assert parse_jwt_expiry("") is None
        assert parse_jwt_expiry(None) is None

    def test_invalid_token_format(self):
        assert parse_jwt_expiry("not.a.valid.jwt.token") is None
        assert parse_jwt_expiry("single_part") is None

    def test_malformed_payload(self):
        # Invalid base64 in payload
        assert parse_jwt_expiry("header.not_valid_base64!@#.signature") is None

    def test_base64url_payload_with_special_chars(self):
        """Real JWT payloads use base64url ('-'/'_'); must decode correctly.

        Regression test for PY-I6: previously decoded with base64.b64decode,
        which fails on '-'/'_' → parse_jwt_expiry returned None for real tokens.
        """
        exp_time = int(time.time()) + 3600
        # Find a filler value whose base64url-encoded JSON contains a
        # base64url-only char ('-' or '_' — i.e. the '+'/'/' substitutions).
        # These bytes are exactly what base64.b64decode chokes on.
        for i in range(500):
            payload = {"exp": exp_time, "pad": "?" * i}
            payload_b64 = base64.urlsafe_b64encode(
                json.dumps(payload).encode()
            ).rstrip(b"=").decode()
            if "-" in payload_b64 or "_" in payload_b64:
                break
        else:  # pragma: no cover - sanity guard
            raise AssertionError("Could not construct a base64url payload for the test")

        token = f"header.{payload_b64}.signature"
        assert parse_jwt_expiry(token) == exp_time


class TestGetJwtRemainingSeconds:
    """Tests for get_jwt_remaining_seconds function."""

    def test_future_expiry(self):
        exp_time = int(time.time()) + 3600  # 1 hour from now
        token = _make_jwt({"exp": exp_time})
        remaining = get_jwt_remaining_seconds(token)
        # Should be roughly 3600 seconds (allow for test execution time)
        assert remaining is not None
        assert 3590 <= remaining <= 3610

    def test_past_expiry(self):
        exp_time = int(time.time()) - 3600  # 1 hour ago
        token = _make_jwt({"exp": exp_time})
        remaining = get_jwt_remaining_seconds(token)
        assert remaining is not None
        assert remaining < 0

    def test_no_exp_claim(self):
        token = _make_jwt({"sub": "user123"})
        assert get_jwt_remaining_seconds(token) is None

    def test_empty_token(self):
        assert get_jwt_remaining_seconds("") is None


class TestIsJwtExpired:
    """Tests for is_jwt_expired function."""

    def test_valid_token_not_expired(self):
        exp_time = int(time.time()) + 3600  # 1 hour from now
        token = _make_jwt({"exp": exp_time})
        assert is_jwt_expired(token) is False

    def test_expired_token(self):
        exp_time = int(time.time()) - 3600  # 1 hour ago
        token = _make_jwt({"exp": exp_time})
        assert is_jwt_expired(token) is True

    def test_invalid_token_treated_as_expired(self):
        # Invalid tokens should be treated as expired (safe default)
        assert is_jwt_expired("invalid") is True
        assert is_jwt_expired("") is True

    def test_token_without_exp_treated_as_expired(self):
        token = _make_jwt({"sub": "user123"})
        assert is_jwt_expired(token) is True
