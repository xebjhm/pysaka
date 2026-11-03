"""One-time room history scan bookkeeping (spec §4)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from pysaka import roster
from pysaka.client import Client, Group
from pysaka.manager import SyncManager

GROUP = {"id": 43, "name": "日向坂46", "thumbnail": None}
NOW = "2026-11-03T12:00:00Z"


def _m(i, member_id=None, ts="2022-04-01T03:00:10Z"):
    msg = {"id": i, "type": "text", "text": f"m{i}", "published_at": ts}
    if member_id is not None:
        msg["member_id"] = member_id
    return msg


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.HINATAZAKA46
    client.download_file = AsyncMock(return_value=True)
    return SyncManager(client, tmp_path)


def test_flag_roundtrip_and_key_shape(manager, time_machine):
    time_machine.move_to(NOW)
    assert manager.history_scanned(43) is False
    manager.mark_history_scanned(43)
    assert manager.history_scanned(43) is True
    with open(manager.output_dir / "sync_state.json", encoding="utf-8") as f:
        assert json.load(f)["room_43"] == {"history_scanned_at": NOW}


def test_flag_gone_after_sync_state_deleted(manager, tmp_path):
    manager.mark_history_scanned(43)
    (manager.output_dir / "sync_state.json").unlink()
    fresh = SyncManager(manager.client, tmp_path)
    assert fresh.history_scanned(43) is False


async def test_complete_when_every_message_stored(manager):
    timeline = [_m(1, 22), _m(2), _m(3, 79)]
    for member in ({"id": 22, "name": "a"}, roster.room_author(GROUP), {"id": 79, "name": "日向坂46"}):
        await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=timeline)
    assert await manager.room_archive_complete(GROUP, timeline) is True


async def test_incomplete_when_an_author_was_not_synced(manager):
    timeline = [_m(1, 22), _m(2, 98)]
    await manager.sync_member(AsyncMock(), GROUP, {"id": 22, "name": "a"}, [], prefetched_messages=timeline)
    assert await manager.room_archive_complete(GROUP, timeline) is False


async def test_incomplete_on_unreadable_file(manager):
    d = manager.output_dir / "messages" / "43 日向坂46" / "22 a"
    d.mkdir(parents=True)
    (d / "messages.json").write_text("{broken", encoding="utf-8")
    assert await manager.room_archive_complete(GROUP, [_m(1, 22)]) is False


async def test_empty_timeline_is_complete(manager):
    assert await manager.room_archive_complete(GROUP, []) is True


async def test_incomplete_on_non_object_file(manager):
    d = manager.output_dir / "messages" / "43 日向坂46" / "22 a"
    d.mkdir(parents=True)
    (d / "messages.json").write_text("[]", encoding="utf-8")
    assert await manager.room_archive_complete(GROUP, [_m(1, 22)]) is False


async def test_complete_when_an_empty_text_message_was_skipped(manager):
    # sync stores neither text nor media for id 2 (skipped), so it must not be expected.
    timeline = [
        _m(1, 22),
        {"id": 2, "type": "text", "text": "", "member_id": 22, "published_at": "2022-04-01T03:00:20Z"},
    ]
    member = {"id": 22, "name": "a"}
    await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=timeline, ignore_cursor=True)
    assert await manager.room_archive_complete(GROUP, timeline) is True


async def test_incomplete_when_an_empty_canceled_message_is_missing(manager):
    # A canceled empty message is stored by sync, so it is still expected.
    canceled = {"id": 2, "type": "text", "text": "", "member_id": 22, "state": "canceled", "published_at": "t"}
    d = manager.output_dir / "messages" / "43 日向坂46" / "22 a"
    d.mkdir(parents=True)
    (d / "messages.json").write_text(json.dumps({"messages": [{"id": 1}]}), encoding="utf-8")
    assert await manager.room_archive_complete(GROUP, [_m(1, 22), canceled]) is False


async def test_messages_without_id_are_ignored(manager):
    timeline = [_m(1, 22), {"type": "text", "text": "x", "member_id": 22}, "not a dict"]
    await manager.sync_member(AsyncMock(), GROUP, {"id": 22, "name": "a"}, [], prefetched_messages=[_m(1, 22)])
    assert await manager.room_archive_complete(GROUP, timeline) is True


async def test_null_messages_list_counts_as_empty(manager):
    d = manager.output_dir / "messages" / "43 日向坂46" / "22 a"
    d.mkdir(parents=True)
    (d / "messages.json").write_text('{"messages": null}', encoding="utf-8")
    assert await manager.room_archive_complete(GROUP, []) is True
    with structlog.testing.capture_logs() as logs:
        assert await manager.room_archive_complete(GROUP, [_m(1, 22)]) is False
    assert [e["member_id"] for e in logs if e["event"] == "Room archive incomplete"] == [22]


@pytest.mark.parametrize(
    ("body", "reason"),
    [('{"messages": {"1": 1}}', "messages_not_a_list"), ("[]", "not_an_object"), ("{broken", "unreadable")],
)
async def test_early_false_paths_log_reason(manager, body, reason):
    d = manager.output_dir / "messages" / "43 日向坂46" / "22 a"
    d.mkdir(parents=True)
    (d / "messages.json").write_text(body, encoding="utf-8")
    with structlog.testing.capture_logs() as logs:
        assert await manager.room_archive_complete(GROUP, [_m(1, 22)]) is False
    warnings = [e for e in logs if e["event"] == "Room archive unreadable"]
    expected = {"event": "Room archive unreadable", "log_level": "warning", "group_id": 43, "member_id": 22}
    expected["reason"] = reason
    if reason == "unreadable":
        expected["error_type"] = "JSONDecodeError"
    assert warnings == [expected]


def test_save_sync_state_returns_true_on_success(manager):
    assert manager.save_sync_state() is True


def test_mark_history_scanned_returns_true_and_logs_when_saved(manager, time_machine):
    time_machine.move_to(NOW)
    with structlog.testing.capture_logs() as logs:
        assert manager.mark_history_scanned(43) is True
    assert [e["event"] for e in logs if e["event"].startswith("Room history scan")] == ["Room history scan recorded"]


def test_mark_history_scanned_returns_false_when_save_fails(manager, monkeypatch):
    """A failed save must not log success: warn and return False. The in-memory
    flag stays set, but the room is scanned again after a restart."""
    monkeypatch.setattr(SyncManager, "_atomic_write_json", staticmethod(lambda target, data, retries=5: False))
    assert manager.save_sync_state() is False
    with structlog.testing.capture_logs() as logs:
        assert manager.mark_history_scanned(43) is False
    events = [e for e in logs if e["event"].startswith("Room history scan")]
    assert [(e["event"], e["log_level"], e["group_id"]) for e in events] == [
        ("Room history scan not saved", "warning", 43)
    ]
    assert manager.history_scanned(43) is True
    assert SyncManager(manager.client, manager.output_dir).history_scanned(43) is False
