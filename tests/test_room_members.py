"""extra_room_members: authors outside the API member list (spec §3)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka import RateLimitedError, roster
from pysaka.client import Client, Group
from pysaka.manager import SyncManager

GROUP = {"id": 79, "name": "18th Single ひなた坂46 LIVE", "thumbnail": "https://cdn/g.png"}
API = [{"id": 120, "name": "石塚 瑶季"}]


def _m(i, member_id=None, **kw):
    msg = {"id": i, "published_at": "2026-02-18T13:48:09Z", "type": "text", "text": "hi", **kw}
    if member_id is not None:
        msg["member_id"] = member_id
    return msg


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.HINATAZAKA46
    client.get_member = AsyncMock(return_value=None)
    return SyncManager(client, tmp_path)


def _write_local(manager, room, folder, block):
    d = manager.output_dir / "messages" / room / folder
    d.mkdir(parents=True)
    with open(d / "messages.json", "w", encoding="utf-8") as f:
        json.dump({"member": block, "messages": []}, f, ensure_ascii=False)


async def test_returns_only_extras_in_order(manager):
    manager.client.get_member.side_effect = lambda s, mid: {"id": mid, "name": f"n{mid}"}
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 98), _m(2, 120), _m(3), _m(4, 29)])
    assert [m["id"] for m in out] == [29, 98, roster.ROOM_AUTHOR_ID]
    assert out[-1] == roster.room_author(GROUP)


async def test_name_hint_wins_without_api_call(manager):
    out = await manager.extra_room_members(
        AsyncMock(), GROUP, API, [_m(1, 89)], {89: {"id": 89, "name": "上村 ひなの", "thumbnail": "t"}}
    )
    assert out == [{"id": 89, "name": "上村 ひなの", "thumbnail": "t"}]
    manager.client.get_member.assert_not_awaited()


async def test_local_member_block_used(manager):
    _write_local(manager, "53 上村 ひなの", "89 上村 ひなの", {"id": 89, "name": "上村 ひなの", "thumbnail": "t89"})
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 89)])
    assert out[0]["name"] == "上村 ひなの" and out[0]["thumbnail"] == "t89"
    manager.client.get_member.assert_not_awaited()


async def test_fallback_name_folder_does_not_block_resolution(manager):
    _write_local(manager, "79 18th Single ひなた坂46 LIVE", "29 29", {"id": 29, "name": "29"})
    manager.client.get_member.return_value = {"id": 29, "name": "加藤 史帆", "thumbnail": "t29", "birthday": "x"}
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 29)])
    assert out == [{"id": 29, "name": "加藤 史帆", "thumbnail": "t29", "phone_image": None, "portrait": None}]


async def test_get_member_cached_and_unknown_falls_back(manager):
    await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 63)])
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(2, 63)])
    assert out == [{"id": 63, "name": "63"}]
    assert manager.client.get_member.await_count == 1


async def test_rate_limit_propagates(manager):
    manager.client.get_member.side_effect = RateLimitedError("Rate limited at /members/63")
    with pytest.raises(RateLimitedError):
        await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 63)])


async def test_exception_is_not_cached(manager):
    manager.client.get_member.side_effect = [
        RateLimitedError("Rate limited at /members/63"),
        {"id": 63, "name": "n63"},
    ]
    with pytest.raises(RateLimitedError):
        await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 63)])
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 63)])
    assert out[0]["name"] == "n63"


async def test_unreadable_local_file_falls_through_to_api(manager):
    d = manager.output_dir / "messages" / "53 room" / "63 x"
    d.mkdir(parents=True)
    (d / "messages.json").write_text("{not json", encoding="utf-8")
    other = manager.output_dir / "messages" / "54 room" / "63 y"
    other.mkdir(parents=True)
    (other / "messages.json").write_text("[]", encoding="utf-8")
    manager.client.get_member.return_value = {"id": 63, "name": "n63"}
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 63)])
    assert out[0]["name"] == "n63"


@pytest.mark.parametrize("bad_name", [123, ["上村 ひなの"], {"ja": "上村"}, True])
async def test_local_member_block_with_non_string_name_is_ignored(manager, bad_name):
    _write_local(manager, "53 上村 ひなの", "89 上村 ひなの", {"id": 89, "name": bad_name})
    manager.client.get_member.return_value = {"id": 89, "name": "上村 ひなの"}
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_m(1, 89)])
    assert out[0]["name"] == "上村 ひなの"
    manager.client.get_member.assert_awaited_once()


def _empty(i, member_id=None, **kw):
    return {**_m(i, member_id), "text": "", **kw}


async def test_skipped_empty_rows_create_no_extra_author(manager):
    from structlog.testing import capture_logs

    msgs = [_m(1, 98, text="hi"), _empty(2), _empty(3, 29), _m(4, 120, text="x")]
    with capture_logs() as logs:
        out = await manager.extra_room_members(AsyncMock(), GROUP, API, msgs)
    assert [m["id"] for m in out] == [98]  # no member 0, no 29 (only skipped-empty rows)
    (dbg,) = [e for e in logs if e["event"] == "Skipped-empty rows ignored for extra members"]
    assert dbg["log_level"] == "debug" and dbg["ignored"] == 2 and dbg["group_id"] == 79


async def test_canceled_rows_still_create_authors(manager):
    msgs = [_empty(1, state="canceled"), _empty(2, 29, state="canceled")]
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, msgs)
    assert [m["id"] for m in out] == [29, roster.ROOM_AUTHOR_ID]


async def test_author_with_empty_and_real_rows_is_kept(manager):
    out = await manager.extra_room_members(AsyncMock(), GROUP, API, [_empty(1, 29), _m(2, 29, text="hi")])
    assert [m["id"] for m in out] == [29]


async def test_empty_row_with_media_is_not_skipped(manager):
    out = await manager.extra_room_members(
        AsyncMock(), GROUP, API, [_empty(1, 29, type="picture", file="https://cdn/p.jpg")]
    )
    assert [m["id"] for m in out] == [29]
