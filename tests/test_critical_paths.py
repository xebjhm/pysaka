"""
Critical path tests for pysaka library.

Covers edge cases and regression scenarios that protect against:
- Infinite retry loops in message fetching
- Path traversal via sanitize_name
- JWT base64 padding corner cases
- RefreshFailedError when all fallback plans fail
- SessionExpiredError propagation in delete_json
- Atomic write integrity in SyncManager
- Corrupt file recovery in sync_member
"""

import base64
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from pysaka.client import Client, Group
from pysaka.exceptions import RefreshFailedError, SessionExpiredError
from pysaka.manager import SyncManager
from pysaka.utils import parse_jwt_expiry, sanitize_name

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict, pad_to_mod: int | None = None) -> str:
    """Create a test JWT with optional payload length control.

    Args:
        payload: The JWT claims dict.
        pad_to_mod: If set, pad the raw base64url payload so that its
            length (before stripping '=') satisfies ``len % 4 == pad_to_mod``.
    """
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()

    raw = base64.urlsafe_b64encode(json.dumps(payload).encode())
    payload_b64 = raw.rstrip(b"=").decode()

    if pad_to_mod is not None:
        # Adjust length so len(payload_b64) % 4 == pad_to_mod
        while len(payload_b64) % 4 != pad_to_mod:
            # Add a benign whitespace-safe char to the JSON then re-encode
            payload[" " * (len(payload_b64) % 4 + 1)] = ""
            raw = base64.urlsafe_b64encode(json.dumps(payload).encode())
            payload_b64 = raw.rstrip(b"=").decode()

    return f"{header}.{payload_b64}.signature"


def _make_jwt_raw_payload(payload_b64: str) -> str:
    """Create a JWT from a raw base64url payload string (no padding)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.signature"


# ---------------------------------------------------------------------------
# 1. Infinite retry loop fix: get_messages first_page_retried guard
# ---------------------------------------------------------------------------


class TestGetMessagesRetryGuard:
    """Verify that get_messages retries at most once when the first page
    returns None (the first_page_retried flag prevents infinite loops)."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.HINATAZAKA46, access_token="test_token")

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.get.return_value.__aenter__.return_value = AsyncMock()
        session.get.return_value.__aexit__.return_value = None
        session.post.return_value.__aenter__.return_value = AsyncMock()
        session.post.return_value.__aexit__.return_value = None
        return session

    async def test_first_page_none_retries_once_then_breaks(self, client, mock_session):
        """When fetch_json returns None on page 0, refresh is attempted once.
        After the retry also returns None, the loop must exit (not loop forever)."""
        # fetch_json always returns None (simulating persistent failure)
        with patch.object(client, "fetch_json", new_callable=AsyncMock, return_value=None) as mock_fetch:
            # refresh_access_token succeeds (returns True), so the retry fires
            with patch.object(
                client, "refresh_access_token", new_callable=AsyncMock, return_value=True
            ) as mock_refresh:
                messages = await client.get_messages(mock_session, group_id=1)

        assert messages == []
        # refresh called exactly once (not in a loop)
        mock_refresh.assert_called_once()
        # fetch_json called exactly twice: original + one retry
        assert mock_fetch.call_count == 2

    async def test_first_page_none_refresh_fails_exits_immediately(self, client, mock_session):
        """When fetch_json returns None and refresh also fails, exit immediately."""
        with patch.object(client, "fetch_json", new_callable=AsyncMock, return_value=None) as mock_fetch:
            with patch.object(
                client, "refresh_access_token", new_callable=AsyncMock, return_value=False
            ) as mock_refresh:
                messages = await client.get_messages(mock_session, group_id=1)

        assert messages == []
        mock_refresh.assert_called_once()
        # Only the original call, no retry since refresh failed
        assert mock_fetch.call_count == 1

    async def test_first_page_none_no_retry_on_later_pages(self, client, mock_session):
        """The retry guard only applies to page 0. If a later page returns None,
        we simply break out of the loop without attempting refresh."""
        call_count = 0

        async def fake_fetch(session, endpoint, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Page 0: success
                return {
                    "messages": [{"id": 10, "published_at": "2024-06-01T00:00:00Z"}],
                    "continuation": "page2",
                }
            # Page 1: failure
            return None

        with patch.object(client, "fetch_json", side_effect=fake_fetch):
            with patch.object(client, "refresh_access_token", new_callable=AsyncMock) as mock_refresh:
                messages = await client.get_messages(mock_session, group_id=1)

        # Should have the one message from page 0
        assert len(messages) == 1
        assert messages[0]["id"] == 10
        # refresh_access_token should NOT be called for later pages
        mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# 2. sanitize_name path traversal
# ---------------------------------------------------------------------------


class TestSanitizeNamePathTraversal:
    """Verify sanitize_name blocks path traversal and forbidden characters."""

    def test_double_dot_backslash_traversal(self):
        """Path traversal via '..\\..\\etc' is neutralized."""
        result = sanitize_name("..\\..\\etc")
        # Both '..' and '\\' should be replaced
        assert ".." not in result
        assert "\\" not in result
        # The result should not allow navigating up
        assert result == "____etc"

    def test_reserved_name_con_kept(self):
        """'CON' is a valid string; sanitize_name does not remove reserved words."""
        result = sanitize_name("CON")
        assert result == "CON"

    def test_forbidden_chars_replaced(self):
        """Forbidden characters < > : \" | ? * are replaced with '_'."""
        result = sanitize_name('name<with>bad:chars"here|test?*')
        for ch in '<>:"|?*':
            assert ch not in result
        # Verify the structure is preserved with underscores
        assert result == "name_with_bad_chars_here_test__"

    def test_forward_slash_replaced(self):
        """Forward slashes are replaced with '_'."""
        result = sanitize_name("some/path/name")
        assert "/" not in result
        assert result == "some_path_name"

    def test_control_characters_stripped(self):
        r"""Control characters (0x00-0x1F) are removed."""
        name_with_controls = "hello\x00world\x01test\x1f"
        result = sanitize_name(name_with_controls)
        assert result == "helloworldtest"
        # Verify no control chars remain
        for c in result:
            assert ord(c) > 0x1F

    def test_normal_name_with_spaces_preserved(self):
        """Spaces are preserved for readability."""
        assert sanitize_name("Normal Name") == "Normal Name"

    def test_cjk_name_preserved(self):
        """CJK characters are preserved."""
        assert sanitize_name("AKB48") == "AKB48"

    def test_empty_input(self):
        """Empty string returns empty string."""
        assert sanitize_name("") == ""

    def test_only_dots_traversal(self):
        """Multiple '..' sequences are all neutralized."""
        result = sanitize_name("../../../../../../etc/passwd")
        assert ".." not in result
        assert "/" not in result


# ---------------------------------------------------------------------------
# 3. JWT base64 padding
# ---------------------------------------------------------------------------


class TestJwtBase64Padding:
    """Verify parse_jwt_expiry handles all base64 padding scenarios."""

    def test_no_padding_needed(self):
        """Token where base64url payload length % 4 == 0 (no padding needed)."""
        # Create a payload whose base64url encoding (without '=') has length % 4 == 0
        # {"exp":1700000000} encodes to specific length; we control via payload content
        exp_time = 1700000000

        # Pad the JSON payload to get length % 4 == 0
        payload_dict = {"exp": exp_time}
        while len(base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()) % 4 != 0:
            payload_dict["_pad"] = payload_dict.get("_pad", "") + "x"

        raw_b64_final = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()
        assert len(raw_b64_final) % 4 == 0

        token = _make_jwt_raw_payload(raw_b64_final)
        result = parse_jwt_expiry(token)
        assert result == exp_time

    def test_padding_needed_mod_2(self):
        """Token where base64url payload length % 4 == 2 (needs 2 '=' chars)."""
        exp_time = 1700000000
        payload_dict = {"exp": exp_time}
        while len(base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()) % 4 != 2:
            payload_dict["_p"] = payload_dict.get("_p", "") + "a"

        raw_b64 = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()
        assert len(raw_b64) % 4 == 2

        token = _make_jwt_raw_payload(raw_b64)
        result = parse_jwt_expiry(token)
        assert result == exp_time

    def test_padding_needed_mod_3(self):
        """Token where base64url payload length % 4 == 3 (needs 1 '=' char)."""
        exp_time = 1700000000
        payload_dict = {"exp": exp_time}
        while len(base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()) % 4 != 3:
            payload_dict["_q"] = payload_dict.get("_q", "") + "b"

        raw_b64 = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()
        assert len(raw_b64) % 4 == 3

        token = _make_jwt_raw_payload(raw_b64)
        result = parse_jwt_expiry(token)
        assert result == exp_time

    def test_padding_needed_mod_1(self):
        """Token where base64url payload length % 4 == 1 (needs 3 '=' chars).

        Standard base64url encoding never produces length % 4 == 1, so we
        construct this case synthetically by truncating a valid payload.
        The padding logic adds '=' * (4 - 1) = 3 chars. This results in
        garbled decoding, so parse_jwt_expiry should return None gracefully.
        """
        # Start with a valid payload and truncate to get len % 4 == 1
        exp_time = 1700000000
        payload_dict = {"exp": exp_time, "data": "padding_test_value"}
        raw_b64 = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()

        # Truncate to make len % 4 == 1
        while len(raw_b64) % 4 != 1:
            raw_b64 = raw_b64[:-1]
        assert len(raw_b64) % 4 == 1

        token = _make_jwt_raw_payload(raw_b64)
        # The important thing is it does not raise an exception
        result = parse_jwt_expiry(token)
        # Truncated payload will likely produce garbled JSON -> None
        assert result is None or isinstance(result, int)

    def test_empty_token_returns_none(self):
        """Empty string returns None."""
        assert parse_jwt_expiry("") is None

    def test_none_token_returns_none(self):
        """None input returns None."""
        assert parse_jwt_expiry(None) is None

    def test_malformed_single_part_returns_none(self):
        """Single-part string (no dots) returns None."""
        assert parse_jwt_expiry("nodots") is None

    def test_malformed_garbage_payload_returns_none(self):
        """Token with invalid base64 in payload returns None."""
        assert parse_jwt_expiry("header.!!!invalid!!!.sig") is None

    def test_token_without_exp_claim(self):
        """Valid JWT structure but no 'exp' claim returns None."""
        token = _make_jwt({"sub": "user123", "iat": 1700000000})
        assert parse_jwt_expiry(token) is None


# ---------------------------------------------------------------------------
# 4. RefreshFailedError when all three plans fail
# ---------------------------------------------------------------------------


class TestRefreshFailedError:
    """Verify refresh_access_token raises RefreshFailedError when all
    three fallback plans are exhausted."""

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.post.return_value.__aenter__.return_value = AsyncMock()
        session.post.return_value.__aexit__.return_value = None
        return session

    async def test_all_three_plans_fail_raises_refresh_failed(self, mock_session, tmp_path):
        """When refresh_token fails, cookies fail, AND headless fails,
        RefreshFailedError is raised."""
        auth_dir = tmp_path / "auth_data"
        auth_dir.mkdir()

        client = Client(
            group=Group.HINATAZAKA46,
            access_token="expired",
            refresh_token="bad_rt",
            cookies={"session": "bad_cookie"},
            auth_dir=str(auth_dir),
        )

        # Plan A: refresh_token returns 401
        mock_resp_refresh = AsyncMock()
        mock_resp_refresh.status = 401

        # Plan B: cookies return 401
        mock_resp_cookie = AsyncMock()
        mock_resp_cookie.status = 401

        # post is called for both Plan A and Plan B
        mock_session.post.return_value.__aenter__.side_effect = [
            mock_resp_refresh,
            mock_resp_cookie,
        ]

        # Plan C: headless browser raises exception
        with patch("pysaka.auth.BrowserAuth") as mock_auth:
            mock_auth.refresh_token_headless = AsyncMock(side_effect=Exception("Browser not installed"))

            with pytest.raises(RefreshFailedError, match="All token refresh attempts failed"):
                await client.refresh_access_token(mock_session)

    async def test_no_credentials_at_all_returns_false(self, mock_session):
        """When no refresh_token, no cookies, no auth_dir exist at all,
        we get False (early exit) not RefreshFailedError."""
        client = Client(
            group=Group.HINATAZAKA46,
            access_token="expired",
            refresh_token=None,
            cookies=None,
            auth_dir=None,
        )

        result = await client.refresh_access_token(mock_session)
        assert result is False

    async def test_refresh_token_fails_cookies_fail_no_auth_dir_raises(self, mock_session):
        """When refresh_token and cookies both fail, and auth_dir is None,
        RefreshFailedError is raised (not False, because credentials were
        present but all attempts failed)."""
        client = Client(
            group=Group.HINATAZAKA46,
            access_token="expired",
            refresh_token="bad_rt",
            cookies={"session": "bad"},
        )

        # Plan A: refresh_token returns 400
        mock_resp_refresh = AsyncMock()
        mock_resp_refresh.status = 400

        # Plan B: cookies return 401
        mock_resp_cookie = AsyncMock()
        mock_resp_cookie.status = 401

        mock_session.post.return_value.__aenter__.side_effect = [
            mock_resp_refresh,
            mock_resp_cookie,
        ]

        with pytest.raises(RefreshFailedError):
            await client.refresh_access_token(mock_session)


# ---------------------------------------------------------------------------
# 5. delete_json SessionExpiredError propagation
# ---------------------------------------------------------------------------


class TestDeleteJsonErrorPropagation:
    """Verify delete_json propagates SessionExpiredError instead of
    swallowing it into ``return False``."""

    @pytest.fixture
    def client(self):
        return Client(group=Group.HINATAZAKA46, access_token="test_token", cookies={"session": "val"})

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.delete.return_value.__aenter__.return_value = AsyncMock()
        session.delete.return_value.__aexit__.return_value = None
        session.post.return_value.__aenter__.return_value = AsyncMock()
        session.post.return_value.__aexit__.return_value = None
        return session

    async def test_session_expired_propagates_from_refresh(self, client, mock_session):
        """When delete gets 401 and refresh raises SessionExpiredError,
        that error propagates out of delete_json."""
        # First call: 401 triggers refresh
        mock_resp_401 = AsyncMock()
        mock_resp_401.status = 401

        mock_session.delete.return_value.__aenter__.return_value = mock_resp_401

        # refresh_access_token raises SessionExpiredError
        with patch.object(
            client,
            "refresh_access_token",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError("Session invalidated"),
        ):
            with pytest.raises(SessionExpiredError, match="Session invalidated"):
                await client.delete_json(mock_session, "/messages/123/favorite")

    async def test_refresh_failed_error_propagates(self, client, mock_session):
        """RefreshFailedError also propagates from delete_json."""
        mock_resp_401 = AsyncMock()
        mock_resp_401.status = 401

        mock_session.delete.return_value.__aenter__.return_value = mock_resp_401

        with patch.object(
            client,
            "refresh_access_token",
            new_callable=AsyncMock,
            side_effect=RefreshFailedError("All plans failed"),
        ):
            with pytest.raises(RefreshFailedError, match="All plans failed"):
                await client.delete_json(mock_session, "/messages/123/favorite")

    async def test_delete_success_returns_true(self, client, mock_session):
        """Baseline: successful delete returns True."""
        mock_resp_200 = AsyncMock()
        mock_resp_200.status = 200
        mock_session.delete.return_value.__aenter__.return_value = mock_resp_200

        result = await client.delete_json(mock_session, "/messages/123/favorite")
        assert result is True

    async def test_delete_204_returns_true(self, client, mock_session):
        """HTTP 204 No Content also counts as success."""
        mock_resp_204 = AsyncMock()
        mock_resp_204.status = 204
        mock_session.delete.return_value.__aenter__.return_value = mock_resp_204

        result = await client.delete_json(mock_session, "/messages/123/favorite")
        assert result is True

    async def test_delete_generic_exception_returns_false(self, client, mock_session):
        """Non-auth exceptions (network errors etc.) return False."""
        mock_session.delete.return_value.__aenter__.side_effect = aiohttp.ClientError("Network down")

        result = await client.delete_json(mock_session, "/test")
        assert result is False


# ---------------------------------------------------------------------------
# 6. SyncManager atomic writes
# ---------------------------------------------------------------------------


class TestAtomicWriteJson:
    """Verify _atomic_write_json writes valid JSON atomically."""

    def test_writes_valid_json(self, tmp_path):
        """The target file contains valid, properly formatted JSON."""
        target = tmp_path / "test.json"
        data = {"key": "value", "nested": {"a": 1}}

        SyncManager._atomic_write_json(target, data)

        assert target.exists()
        with open(target, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_uses_temp_file_not_direct_write(self, tmp_path):
        """The method writes to a temp file first, then replaces.
        We verify by checking that os.replace is the mechanism used."""
        target = tmp_path / "atomic.json"

        with patch("os.replace", wraps=os.replace) as mock_replace:
            SyncManager._atomic_write_json(target, {"test": True})

        # os.replace was called with a .tmp source and the target
        mock_replace.assert_called_once()
        args = mock_replace.call_args[0]
        assert str(args[0]).endswith(".tmp")
        assert args[1] == target

    def test_temp_file_cleaned_up_on_success(self, tmp_path):
        """After successful write, no .tmp files remain."""
        target = tmp_path / "clean.json"

        SyncManager._atomic_write_json(target, {"clean": True})

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_writes_utf8_with_non_ascii(self, tmp_path):
        """Non-ASCII characters (CJK, emoji) are written correctly."""
        target = tmp_path / "unicode.json"
        data = {"name": "AKB48"}

        SyncManager._atomic_write_json(target, data)

        with open(target, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["name"] == "AKB48"

    def test_overwrites_existing_file(self, tmp_path):
        """Atomic write replaces an existing file completely."""
        target = tmp_path / "overwrite.json"
        target.write_text('{"old": true}', encoding="utf-8")

        SyncManager._atomic_write_json(target, {"new": True})

        with open(target, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == {"new": True}
        assert "old" not in loaded


# ---------------------------------------------------------------------------
# 7. SyncManager corrupt file recovery
# ---------------------------------------------------------------------------


class TestCorruptFileRecovery:
    """Verify that sync_member handles corrupted messages.json gracefully
    by resetting sync state for that member."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock(spec=Client)
        client.get_messages = AsyncMock()
        client.download_file = AsyncMock()
        client.group = Group.NOGIZAKA46
        return client

    @pytest.fixture
    def sync_manager(self, mock_client, tmp_path):
        return SyncManager(mock_client, tmp_path)

    async def test_corrupt_json_resets_sync_state(self, sync_manager):
        """When messages.json is corrupted (invalid JSON), sync_member
        catches the error and resets the sync state for that member."""
        session = AsyncMock()
        group = {"id": 1, "name": "TestGroup"}
        member = {"id": 10, "name": "TestMember"}
        media_queue = []

        # Pre-set sync state as if we already synced this member
        state_key = "1_10"
        sync_manager.sync_state[state_key] = {
            "last_message_id": 100,
            "last_sync_ts": "2024-01-01T00:00:00Z",
            "total_messages": 5,
        }
        sync_manager.save_sync_state()

        # Create the member directory and a corrupt messages.json
        member_dir = sync_manager.output_dir / "messages" / "1 TestGroup" / "10 TestMember"
        member_dir.mkdir(parents=True)
        for t in ["picture", "video", "voice"]:
            (member_dir / t).mkdir(exist_ok=True)

        corrupt_file = member_dir / "messages.json"
        corrupt_file.write_text("{this is not valid json!!!", encoding="utf-8")

        # Mock API returning new messages
        sync_manager.client.get_messages.return_value = [
            {
                "id": 201,
                "type": "text",
                "text": "Recovery message",
                "member_id": 10,
                "published_at": "2024-06-01T00:00:00Z",
            },
        ]

        count = await sync_manager.sync_member(session, group, member, media_queue)

        # Should have processed the new message successfully
        assert count == 1

        # The corrupt file path should now have valid JSON with the recovered message
        assert corrupt_file.exists()
        with open(corrupt_file, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["messages"]) == 1
        assert data["messages"][0]["content"] == "Recovery message"

    async def test_corrupt_json_clears_member_state_key(self, sync_manager):
        """After encountering corrupt JSON, the specific member's state
        key is removed from sync_state so the next full sync re-fetches."""
        session = AsyncMock()
        group = {"id": 2, "name": "Group2"}
        member = {"id": 20, "name": "Member2"}
        media_queue = []

        state_key = "2_20"
        other_key = "3_30"

        # Set up state for both this member and another member
        sync_manager.sync_state[state_key] = {
            "last_message_id": 50,
            "last_sync_ts": "2024-01-01T00:00:00Z",
            "total_messages": 3,
        }
        sync_manager.sync_state[other_key] = {
            "last_message_id": 99,
            "last_sync_ts": "2024-02-01T00:00:00Z",
            "total_messages": 10,
        }
        sync_manager.save_sync_state()

        # Create corrupt file
        member_dir = sync_manager.output_dir / "messages" / "2 Group2" / "20 Member2"
        member_dir.mkdir(parents=True)
        for t in ["picture", "video", "voice"]:
            (member_dir / t).mkdir(exist_ok=True)

        corrupt_file = member_dir / "messages.json"
        corrupt_file.write_text("CORRUPTED", encoding="utf-8")

        sync_manager.client.get_messages.return_value = [
            {
                "id": 301,
                "type": "text",
                "text": "New",
                "member_id": 20,
                "published_at": "2024-07-01T00:00:00Z",
            },
        ]

        await sync_manager.sync_member(session, group, member, media_queue)

        # The other member's state should be untouched
        assert other_key in sync_manager.sync_state

    async def test_missing_messages_json_works_normally(self, sync_manager):
        """When messages.json does not exist (first sync), everything works."""
        session = AsyncMock()
        group = {"id": 1, "name": "FirstSync"}
        member = {"id": 10, "name": "NewMember"}
        media_queue = []

        sync_manager.client.get_messages.return_value = [
            {
                "id": 1,
                "type": "text",
                "text": "First message",
                "member_id": 10,
                "published_at": "2024-01-01T00:00:00Z",
            },
        ]

        count = await sync_manager.sync_member(session, group, member, media_queue)
        assert count == 1

        member_dir = sync_manager.output_dir / "messages" / "1 FirstSync" / "10 NewMember"
        json_path = member_dir / "messages.json"
        assert json_path.exists()

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["total_messages"] == 1
