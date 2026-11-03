"""Log breadcrumbs for 0.4.6 room roster / scan decisions: events and fields, never message text."""

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from pysaka import roster
from pysaka.client import Client, Group
from pysaka.manager import SyncManager, _error_origin

GROUP = {"id": 43, "name": "日向坂46", "thumbnail": None}
SECRET = "秘密の本文🔒SECRET"
API = [{"id": 79, "name": "日向坂46"}]


def _m(i, member_id=None, ts="2026-01-01T00:00:00Z", **kw):
    msg = {"id": i, "type": "text", "text": SECRET, "published_at": ts, "updated_at": ts, **kw}
    if member_id is not None:
        msg["member_id"] = member_id
    return msg


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.HINATAZAKA46
    client.get_member = AsyncMock(return_value=None)
    client.get_past_messages = AsyncMock(return_value=[])
    client.get_letters = AsyncMock(return_value=[])
    client.download_file = AsyncMock(return_value=True)
    return SyncManager(client, tmp_path)


def _events(logs, event):
    return [e for e in logs if e["event"] == event]


def _no_text(logs):
    assert SECRET not in repr(logs)


def _write_local(manager, room, folder, content):
    d = manager.output_dir / "messages" / room / folder
    d.mkdir(parents=True)
    (d / "messages.json").write_text(content, encoding="utf-8")


async def test_extra_room_members_logs_summary_and_source_per_id(manager):
    _write_local(manager, "7 other", "30 本名", json.dumps({"member": {"id": 30, "name": "本名"}, "messages": []}))
    manager.client.get_member = AsyncMock(
        side_effect=lambda s, mid: {"id": mid, "name": "API名"} if mid == 40 else None
    )
    msgs = [_m(1, 20), _m(2, 30), _m(3, 40), _m(4, 50), _m(5)]
    with capture_logs() as logs:
        out = await manager.extra_room_members(AsyncMock(), GROUP, API, msgs, {20: {"id": 20, "name": "ヒント"}})

    assert [m["id"] for m in out] == [20, 30, 40, 50, 0]
    (info,) = _events(logs, "Extra room members")
    assert info["log_level"] == "info"
    assert info["group_id"] == 43 and info["member_ids"] == [20, 30, 40, 50, 0] and info["count"] == 5
    sources = {e["member_id"]: e["source"] for e in _events(logs, "Room member resolved")}
    assert sources == {20: "hint", 30: "local", 40: "api", 0: "room"}
    assert all(e["log_level"] == "debug" for e in _events(logs, "Room member resolved"))
    (warn,) = _events(logs, "Room member name unknown, using id as name")
    assert warn["log_level"] == "warning" and warn["member_id"] == 50 and warn["source"] == "fallback"
    _no_text(logs)


async def test_extra_room_members_silent_without_extras(manager):
    with capture_logs() as logs:
        assert await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 79)]) == []
    assert not _events(logs, "Extra room members")


async def test_extra_room_members_second_lookup_is_cached(manager):
    manager.client.get_member = AsyncMock(return_value={"id": 40, "name": "API名"})
    await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 40)])
    with capture_logs() as logs:
        await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 40)])
    (resolved,) = _events(logs, "Room member resolved")
    assert resolved["source"] == "api" and resolved["cached"] is True
    assert manager.client.get_member.await_count == 1


async def test_unusable_member_id_is_reported(manager):
    msgs = [_m(1, "abc"), _m(2, 1.5), _m(3, 0), _m(4, "0"), _m(5), _m(6, True), _m(7, False)]
    with capture_logs() as logs:
        out = await manager.extra_room_members(AsyncMock(), GROUP, API, msgs)
    assert [m["id"] for m in out] == [0]
    (warn,) = _events(logs, "Messages with unusable member_id attributed to room author")
    assert warn["group_id"] == 43 and warn["count"] == 4 and warn["id_types"] == ["bool", "float", "str"]
    _no_text(logs)


async def test_unreadable_local_member_block_logs_error_type(manager, tmp_path):
    _write_local(manager, "7 other", "30 30", "{broken")
    with capture_logs() as logs:
        await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 30)])
    (warn,) = _events(logs, "Local member block unreadable")
    assert warn["member_id"] == 30 and warn["error_type"] == "JSONDecodeError"
    assert warn["file"].endswith("messages.json") and not warn["file"].startswith(("/", "\\"))
    assert str(tmp_path) not in repr(logs)
    assert _events(logs, "Room member name unknown, using id as name")


async def test_mark_history_scanned_logs_info(manager, time_machine):
    time_machine.move_to("2026-11-03T12:00:00Z")
    with capture_logs() as logs:
        manager.mark_history_scanned(43)
    (info,) = _events(logs, "Room history scan recorded")
    assert info["log_level"] == "info" and info["group_id"] == 43
    assert info["history_scanned_at"] == "2026-11-03T12:00:00Z"


async def test_room_archive_complete_logs_debug_with_counts(manager):
    timeline = [_m(1, 22), _m(2), _m(3, 79), _m(4, 79, text="")]
    for member in ({"id": 22, "name": "a"}, roster.room_author(GROUP), {"id": 79, "name": "日向坂46"}):
        await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=timeline)
    with capture_logs() as logs:
        assert await manager.room_archive_complete(GROUP, timeline) is True
    (dbg,) = _events(logs, "Room archive complete")
    assert dbg["log_level"] == "debug"
    assert dbg["group_id"] == 43 and dbg["checked"] == 3 and dbg["authors"] == 3 and dbg["skipped"] == 1
    _no_text(logs)


async def test_room_archive_unreadable_logs_error_type(manager):
    _write_local(manager, "43 日向坂46", "22 a", "{broken")
    with capture_logs() as logs:
        assert await manager.room_archive_complete(GROUP, [_m(1, 22)]) is False
    (warn,) = _events(logs, "Room archive unreadable")
    assert warn["reason"] == "unreadable" and warn["error_type"] == "JSONDecodeError"


MEMBER = {"id": 138, "name": "蔵盛 妃那乃"}
CGROUP = {"id": 86, "name": "蔵盛 妃那乃"}


def _c(i, state="published", updated="2026-07-03T11:15:42Z", **kw):
    msg = {
        "id": i,
        "member_id": 138,
        "type": "text",
        "published_at": "2026-07-03T11:15:42Z",
        "updated_at": updated,
        "state": state,
        **kw,
    }
    if state == "published":
        msg.setdefault("text", SECRET)
    return msg


async def _sync(manager, rows, **kw):
    await manager.sync_member(AsyncMock(), CGROUP, MEMBER, [], prefetched_messages=rows, **kw)


async def test_stored_message_turning_canceled_is_logged(manager):
    await _sync(manager, [_c(1)])
    with capture_logs() as logs:
        await _sync(manager, [_c(1, state="canceled", updated="2026-07-03T11:16:36Z")], ignore_cursor=True)
    (info,) = _events(logs, "Stored message turned canceled")
    assert info["log_level"] == "info"
    assert info["group_id"] == 86 and info["member_id"] == 138 and info["message_id"] == 1
    assert info["canceled_at"] == "2026-07-03T11:16:36Z" and info["content_kept"] is True
    assert not _events(logs, "Canceled message stored without earlier copy")
    _no_text(logs)


async def test_canceled_without_captured_content_logs_content_kept_false(manager):
    await _sync(manager, [_c(2, type="picture", text=None, file="https://cdn/p.jpg?sig=SECRETURL")])  # no text captured
    with capture_logs() as logs:
        await _sync(manager, [_c(2, state="canceled", type="picture")], ignore_cursor=True)
    (info,) = _events(logs, "Stored message turned canceled")
    assert info["message_id"] == 2 and info["content_kept"] is False
    assert "SECRETURL" not in repr(logs)


async def test_canceled_skeleton_first_stored_is_debug(manager):
    with capture_logs() as logs:
        await _sync(manager, [_c(1, state="canceled")])
    (dbg,) = _events(logs, "Canceled message stored without earlier copy")
    assert dbg["log_level"] == "debug" and dbg["message_id"] == 1 and dbg["member_id"] == 138
    assert not _events(logs, "Stored message turned canceled")


async def test_already_canceled_row_is_not_logged_again(manager):
    await _sync(manager, [_c(1, state="canceled")])
    with capture_logs() as logs:
        await _sync(manager, [_c(1, state="canceled")], ignore_cursor=True)
    assert not _events(logs, "Stored message turned canceled")
    assert not _events(logs, "Canceled message stored without earlier copy")


async def test_ignore_cursor_logs_exclusions_below_the_floor(manager):
    member = {"id": 79, "name": "日向坂46"}
    await _sync_room(manager, member, [_m(20, 79, ts="2026-01-01T00:00:00Z")])
    pic = {"id": 15, "type": "picture", "file": "https://cdn/15.jpg?sig=SECRETURL", "member_id": 79}
    old_pic = {**pic, "published_at": "2020-01-01T00:00:00Z"}
    bad = {"id": 16, "member_id": 79, "published_at": "2020-01-02T00:00:00Z", "type": "text", "text": SECRET}
    del bad["id"]  # normalize_message raises KeyError -> a failed row
    with capture_logs() as logs:
        await _sync_room(manager, member, [old_pic, bad, _m(20, 79, ts="2026-01-01T00:00:00Z")], ignore_cursor=True)
    (dbg,) = _events(logs, "Old rows excluded from cursor clamp")
    assert dbg["log_level"] == "debug" and dbg["pending_media"] == 1 and dbg["failed"] == 1
    assert dbg["cursor_floor"] == "2026-01-01T00:00:00Z" and dbg["member_dir"].startswith("79 ")
    (scan,) = _events(logs, "Cursor ignored for room scan")
    assert scan["group_id"] == 43 and scan["member_id"] == 79 and scan["count"] == 3
    scan_logs = _events(logs, "Old rows excluded from cursor clamp") + [scan]
    assert "SECRETURL" not in repr(scan_logs)
    _no_text(scan_logs)
    _no_text(logs)


async def _sync_room(manager, member, rows, **kw):
    await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=rows, **kw)


async def test_normal_sync_does_not_log_cursor_exclusions(manager):
    with capture_logs() as logs:
        await _sync_room(manager, {"id": 79, "name": "日向坂46"}, [_m(1, 79)])
    assert not _events(logs, "Old rows excluded from cursor clamp")
    assert not _events(logs, "Cursor ignored for room scan")


async def test_member_zero_guards_log_debug(manager):
    room = roster.room_author(GROUP)
    with capture_logs() as logs:
        await manager.backfill_past_messages(AsyncMock(), GROUP, room)
        await manager.sync_letters(AsyncMock(), GROUP, room)
        await manager.sync_member(AsyncMock(), GROUP, room, [], prefetched_messages=[_m(1)])
    for event in (
        "Backfill skipped: room author",
        "Letters skipped: room author",
        "Profile images skipped: room author",
    ):
        (entry,) = _events(logs, event)
        assert entry["log_level"] == "debug" and entry["group_id"] == 43 and entry["member_id"] == 0
    _no_text(logs)


async def test_member_folder_rename_failure_logs_error_type(manager, monkeypatch):
    from pathlib import Path

    old = manager.output_dir / "messages" / "43 日向坂46" / "22 22"
    old.mkdir(parents=True)

    def boom(self, target):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "rename", boom)
    with capture_logs() as logs, pytest.raises(PermissionError):
        manager._ensure_member_dir(old.parent, 22, "潮 紗理菜")
    (warn,) = _events(logs, "Member folder rename failed")
    assert warn["error_type"] == "PermissionError"


@pytest.mark.parametrize(
    "raw,reason,error_type",
    [
        ("soon", "unparsable", "ValueError"),
        ("nan", "not_finite_or_negative", None),
        ("-5", "not_finite_or_negative", None),
    ],
)
async def test_rate_limit_warning_says_why_retry_after_was_ignored(client, mock_session, raw, reason, error_type):
    from pysaka import RateLimitedError

    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 429
    resp.headers = {"Retry-After": raw}
    with capture_logs() as logs, pytest.raises(RateLimitedError):
        await client.fetch_json(mock_session, "/groups")
    (warn,) = _events(logs, "API rate limited")
    assert warn["retry_after"] is None and warn["retry_after_ignored"] == reason
    assert warn.get("error_type") == error_type
    assert raw not in repr(warn)


async def test_rate_limit_warning_has_no_ignored_field_for_valid_header(client, mock_session):
    from pysaka import RateLimitedError

    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 429
    resp.headers = {"Retry-After": "30"}
    with capture_logs() as logs, pytest.raises(RateLimitedError):
        await client.fetch_json(mock_session, "/groups")
    (warn,) = _events(logs, "API rate limited")
    assert warn["retry_after"] == 30.0 and "retry_after_ignored" not in warn


async def test_get_member_non_object_body_is_logged(client, mock_session):
    client.fetch_json = AsyncMock(return_value=["not", "an", "object"])
    with capture_logs() as logs:
        assert await client.get_member(mock_session, 22) is None
    (warn,) = _events(logs, "Member lookup returned a non-object body")
    assert warn["member_id"] == 22 and warn["found_type"] == "list"

    client.fetch_json = AsyncMock(return_value=None)
    with capture_logs() as logs:
        assert await client.get_member(mock_session, 22) is None
    assert not _events(logs, "Member lookup returned a non-object body")


async def test_prepare_error_logs_type_not_message_text(manager, monkeypatch):
    def boom(msg):
        raise ValueError(f"bad row: {msg['text']} https://cdn/x.jpg?sig=SECRETURL")

    monkeypatch.setattr("pysaka.manager.normalize_message", boom)
    with capture_logs() as logs:
        await _sync(manager, [_c(1)])
    (err,) = _events(logs, "Prepare error")
    assert err["message_id"] == 1 and err["error_type"] == "ValueError" and "error" not in err
    _no_text(logs)
    assert "SECRETURL" not in repr(logs)


async def test_sync_member_error_logs_type_not_text_or_traceback(manager, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"merge failed on {SECRET}")

    monkeypatch.setattr(SyncManager, "_merge_messages", staticmethod(boom))
    with capture_logs() as logs:
        await _sync(manager, [_c(1)])
    (err,) = _events(logs, "Error syncing member")
    assert err["error_type"] == "RuntimeError" and err["member_id"] == 138 and err["group_id"] == 86
    assert "error" not in err and "exc_info" not in err
    _no_text(logs)


async def test_update_message_metadata_error_logs_type_only(manager, monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError(f"cannot write {SECRET}")

    monkeypatch.setattr(SyncManager, "_locked_update", staticmethod(boom))
    (tmp_path / "messages.json").write_text("{}", encoding="utf-8")
    with capture_logs() as logs:
        await manager.update_message_metadata(tmp_path / "messages.json", {1: {"width": 1}})
    (err,) = _events(logs, "Failed to update message metadata")
    assert err["error_type"] == "OSError" and "error" not in err
    _no_text(logs)


async def test_fetch_json_unexpected_error_logs_type_only(client, mock_session):
    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 200
    resp.json = AsyncMock(side_effect=ValueError(f"bad body: {SECRET}"))
    with capture_logs() as logs:
        assert await client.fetch_json(mock_session, "/groups/1/timeline") is None
    (err,) = _events(logs, "Unexpected error fetching")
    assert err["endpoint"] == "/groups/1/timeline" and err["error_type"] == "ValueError"
    _no_text(logs)


async def test_download_message_media_error_logs_type_only(client, mock_session, tmp_path):
    client.download_file = AsyncMock(side_effect=RuntimeError("https://cdn/p.jpg?sig=SECRETURL " + SECRET))
    message = {"id": 1, "type": "picture", "file": "https://cdn/p.jpg?sig=SECRETURL"}
    with capture_logs() as logs:
        assert await client.download_message_media(mock_session, message, tmp_path) is None
    (err,) = _events(logs, "Message media download error")
    assert err["error_type"] == "RuntimeError"
    _no_text(logs)
    assert "SECRETURL" not in repr(logs)


async def test_prepare_error_logs_text_free_origin(manager, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")  # not under output_dir: relative_to raises in manager.py
    row = {"id": 1, "type": "picture", "text": SECRET, "file": "https://cdn/p.jpg?sig=SECRETURL"}
    with capture_logs() as logs:
        processed, failed_ts, _ = manager.prepare_messages([row], elsewhere, [])
    assert processed == [] and failed_ts == ""
    (err,) = _events(logs, "Prepare error")
    assert err["error_type"] == "ValueError" and err["message_id"] == 1
    assert re.fullmatch(r"pathlib\.py:\d+:relative_to", err["origin"])  # innermost frame is stdlib
    assert re.fullmatch(r"manager\.py:\d+:prepare_messages", err["pysaka_frame"])
    _no_text(logs)
    assert (
        "SECRETURL" not in repr(logs) and str(elsewhere) not in repr(logs) and str(manager.output_dir) not in repr(logs)
    )


async def test_sync_member_error_logs_text_free_origin(manager):
    await _sync(manager, [_c(1)])  # sets the cursor
    with capture_logs() as logs:
        await _sync(manager, [_c(2, published_at=5)])  # int vs str cursor: TypeError inside manager.py
    (err,) = _events(logs, "Error syncing member")
    assert err["error_type"] == "TypeError" and err["member_id"] == 138 and err["group_id"] == 86
    assert re.fullmatch(r"manager\.py:\d+:fetch_for_member", err["origin"])
    assert re.match(r"manager\.py:\d+:", err["pysaka_frame"])
    assert "exc_info" not in err and "error" not in err
    _no_text(logs)


def test_error_origin_has_basenames_only_and_cause_type():
    def inner():
        try:
            raise KeyError(SECRET)
        except KeyError as cause:
            raise RuntimeError(f"wrapped {SECRET}") from cause

    try:
        inner()
    except RuntimeError as e:
        info = _error_origin(e)
    assert re.fullmatch(r"test_v046_logging\.py:\d+:inner", info["origin"])
    assert info["cause_type"] == "KeyError"
    assert SECRET not in repr(info) and "/" not in repr(info)

    assert _error_origin(ValueError(SECRET)) == {}  # never raised: no traceback, no cause


async def test_network_and_timeout_errors_log_redacted_url_and_type_only(client, mock_session):
    import asyncio

    import aiohttp

    from pysaka import ApiError

    url_secret = "SECRETURL"
    for exc, label in (
        (aiohttp.ClientError(f"boom https://x/y?sig={url_secret}"), "Network error"),
        (asyncio.TimeoutError(), "Timeout"),
    ):
        mock_session.get.side_effect = exc
        with capture_logs() as logs, pytest.raises(ApiError) as raised:
            await client.fetch_json(mock_session, "/groups?x=1")
        assert url_secret not in repr(logs) and url_secret not in str(raised.value)
        (err,) = [e for e in logs if e["log_level"] == "error"]
        assert err["error_type"] == type(exc).__name__ and "?" not in err["url"]
        assert label in str(raised.value) and type(exc).__name__ in str(raised.value)


async def test_download_ultimately_failed_logs_type_not_text(client, mock_session, tmp_path):
    mock_session.get.side_effect = RuntimeError("https://cdn/p.jpg?sig=SECRETURL")
    with capture_logs() as logs:
        assert (
            await client.download_file(mock_session, "https://cdn/p.jpg?sig=SECRETURL", tmp_path / "p.jpg", retries=1)
            is False
        )
    (err,) = _events(logs, "Download ultimately failed")
    assert err["error_type"] == "RuntimeError" and "error" not in err
    assert "SECRETURL" not in repr(logs)


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [(403, b"x", "status 403"), (200, b"", "empty body")],
)
async def test_download_ultimately_failed_logs_reason_for_non_exceptions(
    client, mock_session, tmp_path, status, body, reason
):
    """A failure that is not an exception is logged as ``reason``; ``error_type``
    only ever holds an exception type name."""
    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.headers = {}
    with capture_logs() as logs:
        assert (
            await client.download_file(mock_session, "https://cdn/p.jpg?sig=S", tmp_path / "p.jpg", retries=1) is False
        )
    (err,) = _events(logs, "Download ultimately failed")
    assert err["reason"] == reason and "error_type" not in err


def test_manager_file_errors_log_short_path_and_type(manager, monkeypatch, tmp_path):
    (tmp_path / "sync_state.json").write_text("{broken", encoding="utf-8")
    with capture_logs() as logs:
        SyncManager(manager.client, tmp_path)
    (err,) = _events(logs, "Failed to load sync state")
    assert err["file"] == f"{tmp_path.name}/sync_state.json" and err["error_type"] == "JSONDecodeError"
    assert str(tmp_path) not in repr(logs)

    member_dir = tmp_path / "m"
    member_dir.mkdir()
    (member_dir / "messages.json").write_text("{broken", encoding="utf-8")
    with capture_logs() as logs:
        assert manager.scan_member_media(member_dir)["error"] == "manifest_unreadable"
    (warn,) = _events(logs, "saka.scan.manifest_unreadable")
    assert warn["file"] == "m/messages.json" and warn["error_type"] == "JSONDecodeError"
    assert str(tmp_path) not in repr(logs)

    def boom(src, dst):
        raise PermissionError(f"{SECRET} {tmp_path}")

    monkeypatch.setattr("pysaka.manager.os.replace", boom)
    with capture_logs() as logs:
        assert SyncManager._atomic_write_json(tmp_path / "out.json", {"a": 1}) is False
    (err,) = [e for e in logs if e["log_level"] == "error"]
    assert err["error_type"] == "PermissionError" and err["file"] == f"{tmp_path.name}/out.json"
    assert str(tmp_path) not in repr(logs)
    _no_text(logs)
