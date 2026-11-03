"""Room author (member 0), author derivation, member folder renames, ignore_cursor (spec §3, §4)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from pysaka import roster
from pysaka.client import Client, Group
from pysaka.manager import SyncManager

GROUP = {"id": 43, "name": "日向坂46", "thumbnail": "https://cdn/g.png"}


def _m(i, ts, member_id=..., **kw):
    msg = {"id": i, "type": "text", "text": f"m{i}", "published_at": ts, **kw}
    if member_id is not ...:
        msg["member_id"] = member_id
    return msg


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.HINATAZAKA46
    client.get_messages = AsyncMock(return_value=[])
    client.get_past_messages = AsyncMock(return_value=[])
    client.get_letters = AsyncMock(return_value=[])
    client.download_file = AsyncMock(return_value=True)
    return SyncManager(client, tmp_path)


def _ids(path):
    with open(path, encoding="utf-8") as f:
        return sorted(m["id"] for m in json.load(f)["messages"])


def test_author_id_maps_missing_to_room_author():
    assert roster.author_id({"member_id": 22}) == 22
    assert roster.author_id({"member_id": "22"}) == 22
    assert roster.author_id({}) == roster.ROOM_AUTHOR_ID
    assert roster.author_id({"member_id": None}) == roster.ROOM_AUTHOR_ID
    assert roster.author_id({"member_id": True}) == roster.ROOM_AUTHOR_ID
    assert roster.author_id({"member_id": "²"}) == roster.ROOM_AUTHOR_ID  # isdigit() but not int()-able


def test_extra_author_ids_order_and_room_author_last():
    msgs = [_m(1, "t", 98), _m(2, "t", 22), _m(3, "t"), _m(4, "t", 79), _m(5, "t", 22)]
    assert roster.extra_author_ids([79], msgs) == [22, 98, roster.ROOM_AUTHOR_ID]
    assert roster.extra_author_ids([79, 22, 98], msgs[:2] + msgs[3:]) == []


def test_room_author_uses_room_identity():
    assert roster.room_author(GROUP) == {"id": 0, "name": "日向坂46", "thumbnail": "https://cdn/g.png"}


async def test_member_zero_takes_messages_without_member_id(manager):
    timeline = [_m(10, "2022-04-01T03:00:10Z"), _m(11, "2022-04-01T03:05:10Z", 79)]
    await manager.sync_member(AsyncMock(), GROUP, roster.room_author(GROUP), [], prefetched_messages=timeline)
    await manager.sync_member(AsyncMock(), GROUP, {"id": 79, "name": "日向坂46"}, [], prefetched_messages=timeline)

    room = manager.output_dir / "messages" / "43 日向坂46"
    assert _ids(room / "0 日向坂46" / "messages.json") == [10]
    assert _ids(room / "79 日向坂46" / "messages.json") == [11]


async def test_member_zero_skips_profile_backfill_and_letters(manager):
    manager.archive_profile_images = AsyncMock()
    await manager.sync_member(AsyncMock(), GROUP, roster.room_author(GROUP), [], prefetched_messages=[_m(10, "t")])
    manager.archive_profile_images.assert_not_awaited()
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, roster.room_author(GROUP)) == 0
    manager.client.get_past_messages.assert_not_awaited()
    assert await manager.sync_letters(AsyncMock(), GROUP, roster.room_author(GROUP)) == 0
    manager.client.get_letters.assert_not_awaited()


async def test_member_folder_renamed_when_name_changes(manager):
    old = manager.output_dir / "messages" / "43 日向坂46" / "22 22"
    old.mkdir(parents=True)
    with open(old / "messages.json", "w", encoding="utf-8") as f:
        json.dump({"messages": [{"id": 5, "timestamp": "2021-09-16T06:37:53Z", "type": "text"}]}, f)

    await manager.sync_member(
        AsyncMock(), GROUP, {"id": 22, "name": "潮 紗理菜"}, [], prefetched_messages=[_m(6, "2021-09-17T00:00:00Z", 22)]
    )

    room = manager.output_dir / "messages" / "43 日向坂46"
    assert not old.exists()
    assert _ids(room / "22 潮 紗理菜" / "messages.json") == [5, 6]


def test_member_folder_untouched_when_old_and_new_both_exist(manager):
    room = manager.output_dir / "messages" / "43 日向坂46"
    (room / "22 22").mkdir(parents=True)
    (room / "22 潮 紗理菜").mkdir()

    result = manager._ensure_member_dir(room, 22, "潮 紗理菜")

    assert result == room / "22 潮 紗理菜"
    assert (room / "22 22").is_dir()
    assert (room / "22 潮 紗理菜").is_dir()


async def test_ignore_cursor_merges_rows_older_than_cursor(manager):
    member = {"id": 79, "name": "日向坂46"}
    await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=[_m(20, "2026-01-01T00:00:00Z", 79)])
    older = _m(15, "2025-01-01T00:00:00Z", 79, state="canceled")
    timeline = [older, _m(20, "2026-01-01T00:00:00Z", 79)]

    await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=timeline)
    path = manager._member_dir_path(GROUP, member) / "messages.json"
    assert _ids(path) == [20]  # cursor hides the older row

    await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=timeline, ignore_cursor=True)
    assert _ids(path) == [15, 20]
    assert manager.get_last_ts(43, 79) == "2026-01-01T00:00:00Z"


async def test_member_zero_from_unfiltered_fetch_without_prefetched(manager):
    manager.client.get_messages = AsyncMock(
        return_value=[_m(10, "2022-04-01T03:00:10Z"), _m(11, "2022-04-01T03:05:10Z", 79)]
    )
    await manager.sync_member(AsyncMock(), GROUP, roster.room_author(GROUP), [])
    await manager.sync_member(AsyncMock(), GROUP, {"id": 79, "name": "日向坂46"}, [])

    room = manager.output_dir / "messages" / "43 日向坂46"
    assert _ids(room / "0 日向坂46" / "messages.json") == [10]
    assert _ids(room / "79 日向坂46" / "messages.json") == [11]


async def test_sync_letters_renames_member_folder_instead_of_splitting(manager):
    old = manager.output_dir / "messages" / "43 日向坂46" / "22 22"
    old.mkdir(parents=True)
    with open(old / "messages.json", "w", encoding="utf-8") as f:
        json.dump({"messages": [{"id": 5, "timestamp": "2021-09-16T06:37:53Z", "type": "text"}]}, f)
    letter = {
        "id": 1,
        "member_id": 22,
        "text": "手紙",
        "file": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    manager.client.get_letters = AsyncMock(return_value=[letter])

    added = await manager.sync_letters(AsyncMock(), GROUP, {"id": 22, "name": "潮 紗理菜"})

    room = manager.output_dir / "messages" / "43 日向坂46"
    assert added == 1
    assert [p.name for p in room.iterdir()] == ["22 潮 紗理菜"]
    assert (room / "22 潮 紗理菜" / "messages.json").exists()
    assert (room / "22 潮 紗理菜" / "letters.json").exists()


async def test_backfill_uses_author_rule_for_real_members(manager):
    member = {"id": 79, "name": "日向坂46"}
    await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=[_m(20, "2026-01-01T00:00:00Z", 79)])
    manager.client.get_past_messages = AsyncMock(
        return_value=[
            _m(1, "2020-01-01T00:00:00Z", "79"),
            _m(2, "2020-01-02T00:00:00Z"),
            _m(3, "2020-01-03T00:00:00Z", 5),
        ]
    )

    assert await manager.backfill_past_messages(AsyncMock(), GROUP, member) == 1
    assert _ids(manager._member_dir_path(GROUP, member) / "messages.json") == [1, 20]


async def test_ignore_cursor_does_not_fire_on_new_messages(manager):
    member = {"id": 79, "name": "日向坂46"}
    await manager.sync_member(AsyncMock(), GROUP, member, [], prefetched_messages=[_m(20, "2026-01-01T00:00:00Z", 79)])
    older = _m(15, "2025-01-01T00:00:00Z", 79, state="canceled")
    callback = MagicMock()

    await manager.sync_member(
        AsyncMock(), GROUP, member, [], prefetched_messages=[older], ignore_cursor=True, on_new_messages=callback
    )

    assert _ids(manager._member_dir_path(GROUP, member) / "messages.json") == [15, 20]
    callback.assert_not_called()


def test_ensure_member_dir_logs_and_reraises_rename_failure(manager, monkeypatch):
    room = manager.output_dir / "messages" / "43 日向坂46"
    (room / "22 22").mkdir(parents=True)

    def boom(self, target):
        raise OSError("locked")

    monkeypatch.setattr(Path, "rename", boom)
    with structlog.testing.capture_logs() as logs, pytest.raises(OSError):
        manager._ensure_member_dir(room, 22, "潮 紗理菜")

    warning = [e for e in logs if e["event"] == "Member folder rename failed"]
    assert warning and warning[0]["member_id"] == 22
    assert warning[0]["old"] == "22 22" and warning[0]["new"] == "22 潮 紗理菜"
    assert (room / "22 22").is_dir()
