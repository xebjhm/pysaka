"""Server ids and media URLs never lead outside the archive (security audit 2026-11-13)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from pysaka.client import Client, Group
from pysaka.manager import SyncManager


def _client() -> Client:
    return Client(group=Group.HINATAZAKA46, access_token="test_token")


@pytest.mark.asyncio
async def test_get_messages_drops_rows_whose_id_is_not_an_integer():
    client = _client()
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 12},
                    {"id": "11"},
                    {"id": "../x"},
                    {"id": True},
                    {"id": None},
                    {"id": 10},
                ],
                "continuation": None,
            }
        ]
    )
    with capture_logs() as logs:
        msgs = await client.get_messages(MagicMock(), group_id=1)
    assert [m["id"] for m in msgs] == [10, 11, 12]
    assert all(type(m["id"]) is int for m in msgs)
    (refused,) = [e for e in logs if e["event"] == "Timeline ids refused"]
    assert (refused["group_id"], refused["count"]) == (1, 3)
    assert "../x" not in repr(logs)


GROUP = {"id": 34, "name": "g"}
MEMBER = {"id": 58, "name": "m"}


@pytest.fixture
def mock_client():
    client = MagicMock(spec=Client)
    client.get_messages = AsyncMock(return_value=[])
    client.get_past_messages = AsyncMock(return_value=[])
    client.download_file = AsyncMock(return_value=True)
    client.group = Group.NOGIZAKA46
    return client


@pytest.fixture
def manager(mock_client, tmp_path):
    return SyncManager(mock_client, tmp_path / "out")


@pytest.mark.parametrize("bad_id", ["../../../x", "/abs", True, None, -3, "3 "])
@pytest.mark.asyncio
async def test_sync_member_refuses_a_bad_group_id_before_touching_disk(manager, mock_client, tmp_path, bad_id):
    queue: list = []
    with capture_logs() as logs:
        n = await manager.sync_member(MagicMock(), {"id": bad_id, "name": "g"}, MEMBER, queue, prefetched_messages=[])
    assert n == 0 and queue == []
    assert not (tmp_path / "out" / "messages").exists()
    assert {p.name for p in tmp_path.iterdir()} <= {"out"}
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["group"]
    mock_client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_refuses_a_bad_member_id(manager):
    with capture_logs() as logs:
        n = await manager.sync_member(MagicMock(), GROUP, {"id": "../m", "name": "m"}, [], prefetched_messages=[])
    assert n == 0
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["member"]


@pytest.mark.asyncio
async def test_numeric_string_ids_name_the_same_folders_as_ints(manager):
    with patch.object(manager, "_archive_profile_images_safely", AsyncMock()):
        await manager.sync_member(
            MagicMock(), {"id": "34", "name": "g"}, {"id": "58", "name": "m"}, [], prefetched_messages=[]
        )
    assert (manager.output_dir / "messages" / "34 g" / "58 m" / "picture").is_dir()


@pytest.mark.asyncio
async def test_backfill_and_letters_refuse_bad_ids(manager, mock_client):
    bad = {"id": "../m", "name": "m"}
    assert await manager.backfill_past_messages(MagicMock(), GROUP, bad) == 0
    assert await manager.sync_letters(MagicMock(), GROUP, bad) == 0
    mock_client.get_past_messages.assert_not_called()


@pytest.mark.asyncio
async def test_room_archive_complete_is_false_for_a_bad_group_id(manager):
    assert await manager.room_archive_complete({"id": "../..", "name": "g"}, [{"id": 1}]) is False


def test_path_builders_refuse_anything_but_int_ids(manager):
    with pytest.raises(ValueError):
        manager._member_dir_path({"id": "../x", "name": "g"}, MEMBER)
    with pytest.raises(ValueError):
        manager._member_dir_path(GROUP, {"id": "58", "name": "m"})
    with pytest.raises(ValueError):
        manager._ensure_group_dir({"id": "34", "name": "g"})
    with pytest.raises(ValueError):
        manager._ensure_member_dir(manager.output_dir, "58", "m")
