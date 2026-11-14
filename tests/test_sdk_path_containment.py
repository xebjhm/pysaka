"""Server ids and media URLs never lead outside the archive (security audit 2026-11-13)."""

from pathlib import Path
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
async def test_sync_member_refuses_a_bad_member_id(manager, mock_client, tmp_path):
    with capture_logs() as logs:
        n = await manager.sync_member(MagicMock(), GROUP, {"id": "../m", "name": "m"}, [], prefetched_messages=[])
    assert n == 0
    assert not (tmp_path / "out" / "messages").exists()
    assert {p.name for p in tmp_path.iterdir()} <= {"out"}
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["member"]
    mock_client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_numeric_string_ids_name_the_same_folders_as_ints(manager):
    with patch.object(manager, "_archive_profile_images_safely", AsyncMock()):
        await manager.sync_member(
            MagicMock(), {"id": "34", "name": "g"}, {"id": "58", "name": "m"}, [], prefetched_messages=[]
        )
    assert (manager.output_dir / "messages" / "34 g" / "58 m" / "picture").is_dir()


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.asyncio
async def test_backfill_refuses_a_bad_member_id_even_with_a_matching_state_entry(manager, mock_client, tmp_path):
    # Seed what the unguarded code would need to get past its early returns.
    manager.sync_state["34_../m"] = {"last_id": 1}
    (manager.output_dir / "messages" / "34 g").mkdir(parents=True)
    (manager.output_dir / "messages" / "m").mkdir()
    (manager.output_dir / "messages" / "m" / "messages.json").write_text("{}", encoding="utf-8")
    before = _tree(tmp_path)
    with capture_logs() as logs:
        n = await manager.backfill_past_messages(MagicMock(), GROUP, {"id": "../m", "name": "m"})
    assert n == 0
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["member"]
    mock_client.get_past_messages.assert_not_called()
    assert _tree(tmp_path) == before


@pytest.mark.asyncio
async def test_letters_refuse_a_bad_member_id_before_fetching_or_touching_disk(manager, mock_client, tmp_path):
    mock_client.get_letters = AsyncMock(return_value=[])
    with capture_logs() as logs:
        n = await manager.sync_letters(MagicMock(), GROUP, {"id": "../m", "name": "m"})
    assert n == 0
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["member"]
    mock_client.get_letters.assert_not_called()
    assert not (tmp_path / "out" / "messages").exists()
    assert {p.name for p in tmp_path.iterdir()} <= {"out"}


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


def _member_dir(manager) -> Path:
    member_dir = manager.output_dir / "messages" / "34 g" / "58 m"
    member_dir.mkdir(parents=True)
    return member_dir


def _row(mid, ts, url="https://cdn.example.com/a.jpg"):
    return {"id": mid, "type": "picture", "file": url, "published_at": ts, "text": None}


def test_rows_with_unsafe_ids_are_dropped_and_do_not_hold_the_cursor(manager):
    member_dir = _member_dir(manager)
    queue: list = []
    rows = [
        _row("../../../../escape", "2026-01-01T00:00:00Z"),
        _row("/abs/escape", "2026-01-01T00:00:01Z"),
        _row(7, "2026-01-02T00:00:00Z", "https://cdn.example.com/7.jpg"),
    ]
    with capture_logs() as logs, patch("pysaka.manager.media_file_is_present", return_value=False) as present:
        processed, failed_ts, pending_ts = manager.prepare_messages(rows, member_dir, queue)
    assert [p["id"] for p in processed] == [7]
    assert failed_ts is None
    assert pending_ts == "2026-01-02T00:00:00Z"
    assert [item["message_id"] for item in queue] == [7]
    assert queue[0]["path"] == member_dir / "picture" / "7.jpg"
    assert all(call.args[0].parent == member_dir / "picture" for call in present.call_args_list)
    (refused,) = [e for e in logs if e["event"] == "Messages refused"]
    assert (refused["reason"], refused["count"]) == ("bad_id", 2)
    assert "escape" not in repr(logs)


def test_numeric_string_message_ids_are_stored_as_ints(manager):
    member_dir = _member_dir(manager)
    queue: list = []
    with patch("pysaka.manager.media_file_is_present", return_value=False):
        processed, _, _ = manager.prepare_messages([_row("8", "2026-01-03T00:00:00Z")], member_dir, queue)
    assert processed[0]["id"] == 8 and queue[0]["message_id"] == 8
    assert processed[0]["media_file"] == str((member_dir / "picture" / "8.jpg").relative_to(manager.output_dir))
