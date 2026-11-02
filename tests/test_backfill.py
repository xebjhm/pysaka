"""Pre-subscription backfill (spec §5)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka.client import Client, Group
from pysaka.exceptions import SessionExpiredError
from pysaka.manager import SyncManager

GROUP = {"id": 79, "name": "中川 智尋"}
MEMBER = {"id": 134, "name": "中川 智尋"}
NOW = "2026-11-03T05:00:00Z"


def _m(i, ts, **kw):
    return {"id": i, "type": "text", "text": f"m{i}", "member_id": 134, "published_at": ts, **kw}


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.SAKURAZAKA46
    client.get_messages = AsyncMock(return_value=[_m(500, "2026-06-15T12:00:00Z")])
    client.get_past_messages = AsyncMock()

    async def fake_download(session, url, path, timestamp=None):
        if "fail" in url:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return True

    client.download_file = AsyncMock(side_effect=fake_download)
    return SyncManager(client, tmp_path)


async def _first_sync(manager):
    await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])
    return manager._member_dir_path(GROUP, MEMBER) / "messages.json"


def _ids(path):
    with open(path, encoding="utf-8") as f:
        return [m["id"] for m in json.load(f)["messages"]]


async def test_backfill_merges_older_messages_and_sets_flag(manager, time_machine):
    time_machine.move_to(NOW)
    path = await _first_sync(manager)
    cursor_before = dict(manager.sync_state["79_134"])
    manager.client.get_past_messages.return_value = [
        _m(400, "2026-06-14T04:57:00Z"),
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
        _m(999, "2026-06-14T10:00:00Z", member_id=1),  # other member: ignored
    ]

    added = await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER)

    assert added == 2
    assert _ids(path) == [400, 401, 500]
    entry = manager.sync_state["79_134"]
    assert entry["last_sync_ts"] == cursor_before["last_sync_ts"]
    assert entry["last_message_id"] == cursor_before["last_message_id"]
    assert entry["total_messages"] == 3
    assert entry["past_backfilled_at"] == NOW
    member_dir = path.parent
    assert (member_dir / "picture" / "401.jpg").exists()
    assert manager.client.get_past_messages.await_args.args[1] == 79
    assert manager.client.get_past_messages.await_args.kwargs == {"strict": True}

    # Flag survives the next normal sync and makes later calls free.
    await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW
    manager.client.get_past_messages.reset_mock()
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    manager.client.get_past_messages.assert_not_awaited()


async def test_backfill_empty_response_sets_flag(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = []
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    assert "past_backfilled_at" in manager.sync_state["79_134"]


async def test_backfill_request_failure_does_not_set_flag(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = None
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    assert "past_backfilled_at" not in manager.sync_state["79_134"]


async def test_backfill_missing_media_does_not_set_flag_and_retries(manager):
    path = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/fail.jpg")
    ]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 1
    assert "past_backfilled_at" not in manager.sync_state["79_134"]
    assert _ids(path) == [401, 500]

    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/ok.jpg")
    ]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0  # already on disk
    assert "past_backfilled_at" in manager.sync_state["79_134"]


async def test_backfill_skipped_before_first_sync(manager):
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    manager.client.get_past_messages.assert_not_awaited()


async def test_backfill_corrupt_file_is_left_alone(manager):
    path = await _first_sync(manager)
    path.write_text("{corrupt", encoding="utf-8")
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    assert path.read_text(encoding="utf-8") == "{corrupt"
    assert "past_backfilled_at" not in manager.sync_state["79_134"]


async def test_backfill_does_not_fire_on_new_messages(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    # backfill has no callback parameter at all; a later normal sync must not
    # report the backfilled id as new either.
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER)
    seen = []
    await manager.sync_member(AsyncMock(), GROUP, MEMBER, [], on_new_messages=lambda i, m: seen.extend(m))
    assert seen == []


async def test_backfill_skipped_when_messages_file_missing(manager):
    await _first_sync(manager)
    (manager._member_dir_path(GROUP, MEMBER) / "messages.json").unlink()
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    manager.client.get_past_messages.assert_not_awaited()
    assert not (manager._member_dir_path(GROUP, MEMBER) / "messages.json").exists()


async def test_backfill_session_expired_propagates(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.side_effect = SessionExpiredError("expired")
    with pytest.raises(SessionExpiredError):
        await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER)
    assert "past_backfilled_at" not in manager.sync_state["79_134"]


async def test_backfill_unexpected_fetch_error_is_swallowed_without_flag(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.side_effect = RuntimeError("boom")
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    assert "past_backfilled_at" not in manager.sync_state["79_134"]


async def test_backfill_non_dict_messages_file_is_left_alone(manager):
    path = await _first_sync(manager)
    path.write_text("[]", encoding="utf-8")
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    assert path.read_text(encoding="utf-8") == "[]"
    assert "past_backfilled_at" not in manager.sync_state["79_134"]


async def test_backfill_writes_media_metadata(manager, monkeypatch):
    path = await _first_sync(manager)
    monkeypatch.setattr("pysaka.manager.get_media_dimensions", lambda p, t: (640, 480))
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg")
    ]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 1
    with open(path, encoding="utf-8") as f:
        msg = next(m for m in json.load(f)["messages"] if m["id"] == 401)
    assert (msg["width"], msg["height"]) == (640, 480)
    assert "past_backfilled_at" in manager.sync_state["79_134"]


def test_update_total_messages_touches_only_total(manager):
    manager.sync_state["79_134"] = {"last_message_id": 5, "last_sync_ts": "t", "total_messages": 1}
    manager.update_total_messages(79, 134, 9)
    assert manager.sync_state["79_134"] == {"last_message_id": 5, "last_sync_ts": "t", "total_messages": 9}
    manager.update_total_messages(79, 999, 2)
    assert manager.sync_state["79_999"] == {"total_messages": 2}
