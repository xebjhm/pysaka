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


@pytest.mark.parametrize("payload", [{}, {"messages": None}, {"messages": "oops"}])
async def test_backfill_malformed_200_does_not_set_flag(manager, payload):
    """A 200 without a ``messages`` list must not read as "nothing before the
    subscription", or the flag would be set and the backfill never retried."""
    await _first_sync(manager)
    real = Client(group=Group.SAKURAZAKA46, access_token="t")
    real.fetch_json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    manager.client.get_past_messages = real.get_past_messages

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


async def test_backfill_non_dict_messages_file_logs_its_type(manager):
    from structlog.testing import capture_logs

    path = await _first_sync(manager)
    path.write_text('["秘密"]', encoding="utf-8")
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]

    with capture_logs() as logs:
        assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0

    (entry,) = [e for e in logs if e["log_level"] == "warning"]
    assert entry["event"] == "Backfill skipped: messages.json is not an object"
    assert entry["found_type"] == "list"
    assert "秘密" not in repr(logs)


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


async def test_corrupt_recovery_clears_flag_so_backfill_reruns(manager, time_machine):
    """Recovery rebuilds messages.json from the timeline, which never holds the
    pre-subscription messages, so the flag must be cleared or they stay lost."""
    time_machine.move_to(NOW)
    path = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 1
    assert _ids(path) == [400, 500]
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW

    path.write_text("{corrupt", encoding="utf-8")
    await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])

    assert _ids(path) == [500]  # the past message is gone from disk...
    assert "past_backfilled_at" not in manager.sync_state["79_134"]  # ...so the flag must be too
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 1
    assert _ids(path) == [400, 500]
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW


async def test_count_mismatch_recovery_clears_flag(manager, time_machine):
    time_machine.move_to(NOW)
    path = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER)
    manager.sync_state["79_134"]["total_messages"] = 99  # file now holds fewer than recorded

    await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])

    assert "past_backfilled_at" not in manager.sync_state["79_134"]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0  # already merged: idempotent
    assert _ids(path) == [400, 500]
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW


async def test_backfill_partial_normalize_failure_merges_valid_and_retries(manager, time_machine):
    """One past message fails to normalize (a non-string media URL): the valid one
    is merged but the flag stays unset; a later call with a fixed payload adds the
    missing message and sets the flag."""
    time_machine.move_to(NOW)
    path = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(400, "2026-06-14T04:57:00Z"),
        _m(401, "2026-06-14T13:40:00Z", type="picture", file=12345),  # breaks prepare_messages
    ]

    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 1
    assert _ids(path) == [400, 500]
    assert "past_backfilled_at" not in manager.sync_state["79_134"]

    manager.client.get_past_messages.return_value = [
        _m(400, "2026-06-14T04:57:00Z"),
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p401.jpg"),
    ]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 1
    assert _ids(path) == [400, 401, 500]
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW
    assert manager.sync_state["79_134"]["total_messages"] == 3


async def test_backfill_write_failure_returns_zero_without_flag(manager, monkeypatch):
    path = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    monkeypatch.setattr(SyncManager, "_atomic_write_json", staticmethod(lambda target, data: False))

    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    assert _ids(path) == [500]
    assert "past_backfilled_at" not in manager.sync_state["79_134"]


async def test_backfill_messages_file_vanishing_under_the_lock_is_skipped_quietly(manager, monkeypatch):
    from structlog.testing import capture_logs

    path = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    original = SyncManager._locked_update

    def vanish_then_update(p, mutate, **kwargs):
        p.unlink()
        return original(p, mutate, **kwargs)

    monkeypatch.setattr(SyncManager, "_locked_update", staticmethod(vanish_then_update))
    with capture_logs() as logs:
        assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 0
    assert not path.exists()  # nothing recreated
    assert not [e for e in logs if e["log_level"] == "warning"]
    assert "past_backfilled_at" not in manager.sync_state["79_134"]
