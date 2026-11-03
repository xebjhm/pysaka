"""One-time room history scan bookkeeping (spec §4)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

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
