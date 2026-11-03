"""Thread-locked read-modify-write for messages.json (spec §3)."""

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from pysaka.client import Client, Group
from pysaka.manager import SyncManager


def _write(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _read(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_locked_update_writes_mutated_data(tmp_path):
    p = tmp_path / "messages.json"
    _write(p, {"messages": [{"id": 1}]})

    def mutate(current):
        current["messages"].append({"id": 2, "content": "日本語 🎉"})
        return current, "done"

    ok, result = SyncManager._locked_update(p, mutate)

    assert ok is True and result == "done"
    assert [m["id"] for m in _read(p)["messages"]] == [1, 2]
    assert _read(p)["messages"][1]["content"] == "日本語 🎉"


def test_locked_update_none_data_skips_write(tmp_path):
    p = tmp_path / "messages.json"
    _write(p, {"messages": []})
    before = p.stat().st_mtime_ns

    ok, result = SyncManager._locked_update(p, lambda cur: (None, 42))

    assert (ok, result) == (True, 42)
    assert p.stat().st_mtime_ns == before


def test_locked_update_missing_file_passes_none(tmp_path):
    p = tmp_path / "messages.json"
    seen = []

    def mutate(current):
        seen.append(current)
        return {"messages": []}, None

    ok, _ = SyncManager._locked_update(p, mutate)
    assert seen == [None] and ok is True and p.exists()


def test_locked_update_corrupt_raises_unless_discard(tmp_path):
    p = tmp_path / "messages.json"
    p.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        SyncManager._locked_update(p, lambda cur: (None, None))

    seen = []
    with capture_logs() as logs:
        SyncManager._locked_update(p, lambda cur: (seen.append(cur), (None, None))[1], on_corrupt="discard")
    assert seen == [None]
    assert p.read_text(encoding="utf-8") == "{not json"  # discard does not move the file
    assert logs == []  # the caller (sync_member) logs the corrupt file itself


def test_locked_update_quarantine_moves_unreadable_file_aside(tmp_path, time_machine):
    time_machine.move_to("2026-11-03T12:34:56Z", tick=False)
    p = tmp_path / "index.json"
    p.write_text("{not json 日本語", encoding="utf-8")
    seen = []

    def mutate(current):
        seen.append(current)
        return {"version": 1}, "rebuilt"

    with capture_logs() as logs:
        ok, result = SyncManager._locked_update(p, mutate, on_corrupt="quarantine")

    assert (ok, result) == (True, "rebuilt")
    assert seen == [None]
    kept = tmp_path / "index.json.corrupt-20261103T123456Z"
    assert kept.read_text(encoding="utf-8") == "{not json 日本語"
    assert _read(p) == {"version": 1}
    (entry,) = [e for e in logs if e["log_level"] == "warning"]
    assert entry["file"] == "index.json"  # file name only, never the full path
    assert entry["kept_as"] == kept.name
    assert entry["error_type"] == "JSONDecodeError"
    assert str(tmp_path) not in repr(logs)


def test_locked_update_reports_write_failure(tmp_path, monkeypatch):
    p = tmp_path / "messages.json"
    monkeypatch.setattr(SyncManager, "_atomic_write_json", staticmethod(lambda target, data: False))

    ok, result = SyncManager._locked_update(p, lambda cur: ({"messages": []}, "r"))
    assert (ok, result) == (False, "r")


def test_locked_update_serializes_writers_on_same_file(tmp_path):
    """A second writer must not enter its read-modify-write while the first holds
    the lock — otherwise one write overwrites the other (spec §3 race)."""
    p = tmp_path / "messages.json"
    _write(p, {"messages": [{"id": 1, "is_favorite": False}]})
    a_inside = threading.Event()
    release_a = threading.Event()
    b_inside = threading.Event()

    def mutate_a(current):  # simulates the favorite toggle
        a_inside.set()
        assert release_a.wait(timeout=5)
        current["messages"][0]["is_favorite"] = True
        return current, None

    def mutate_b(current):  # simulates sync appending a new message
        b_inside.set()
        current["messages"].append({"id": 2})
        return current, None

    ta = threading.Thread(target=SyncManager._locked_update, args=(p, mutate_a))
    ta.start()
    assert a_inside.wait(timeout=5)
    tb = threading.Thread(target=SyncManager._locked_update, args=(p, mutate_b))
    tb.start()

    assert not b_inside.wait(timeout=0.3)  # B is blocked while A holds the lock
    release_a.set()
    ta.join(timeout=5)
    tb.join(timeout=5)

    data = _read(p)
    assert [m["id"] for m in data["messages"]] == [1, 2]  # B's message kept
    assert data["messages"][0]["is_favorite"] is True  # A's change kept


def test_locked_update_different_files_do_not_block(tmp_path):
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    inside = threading.Event()
    release = threading.Event()

    def slow(current):
        inside.set()
        assert release.wait(timeout=5)
        return None, None

    t = threading.Thread(target=SyncManager._locked_update, args=(p1, slow))
    t.start()
    assert inside.wait(timeout=5)
    ok, res = SyncManager._locked_update(p2, lambda cur: (None, "free"))
    assert res == "free"
    release.set()
    t.join(timeout=5)


async def test_update_message_fields_sets_fields(tmp_path):
    p = tmp_path / "messages.json"
    _write(p, {"messages": [{"id": 7, "is_favorite": False}, {"id": 8}]})

    assert await SyncManager.update_message_fields(p, 7, {"is_favorite": True}) is True
    assert _read(p)["messages"][0]["is_favorite"] is True
    assert "is_favorite" not in _read(p)["messages"][1]


async def test_update_message_fields_unknown_id_or_missing_file(tmp_path):
    p = tmp_path / "messages.json"
    assert await SyncManager.update_message_fields(p, 1, {"x": 1}) is False
    _write(p, {"messages": [{"id": 1}]})
    before = p.stat().st_mtime_ns
    assert await SyncManager.update_message_fields(p, 99, {"x": 1}) is False
    assert p.stat().st_mtime_ns == before


def test_update_message_fields_sync_from_worker_thread(tmp_path):
    p = tmp_path / "messages.json"
    _write(p, {"messages": [{"id": 3}]})
    assert SyncManager.update_message_fields_sync(p, 3, {"is_favorite": True}) is True
    assert _read(p)["messages"][0]["is_favorite"] is True


async def test_update_message_fields_write_failure_returns_false(tmp_path, monkeypatch):
    p = tmp_path / "messages.json"
    _write(p, {"messages": [{"id": 1}]})
    monkeypatch.setattr(SyncManager, "_atomic_write_json", staticmethod(lambda target, data: False))
    assert await SyncManager.update_message_fields(p, 1, {"x": 1}) is False


# --- Lock-key canonicalization -------------------------------------------------


def _assert_second_path_blocks(first: Path, second: Path) -> None:
    """Hold the lock via ``first``; a writer via ``second`` must wait for it."""
    a_inside = threading.Event()
    release_a = threading.Event()
    b_inside = threading.Event()

    def mutate_a(current):
        a_inside.set()
        assert release_a.wait(timeout=5)
        current["messages"][0]["is_favorite"] = True
        return current, None

    def mutate_b(current):
        b_inside.set()
        current["messages"].append({"id": 2})
        return current, None

    ta = threading.Thread(target=SyncManager._locked_update, args=(first, mutate_a))
    ta.start()
    assert a_inside.wait(timeout=5)
    tb = threading.Thread(target=SyncManager._locked_update, args=(second, mutate_b))
    tb.start()

    assert not b_inside.wait(timeout=0.3)  # same file, same lock
    release_a.set()
    ta.join(timeout=5)
    tb.join(timeout=5)

    data = _read(first)
    assert [m["id"] for m in data["messages"]] == [1, 2]
    assert data["messages"][0]["is_favorite"] is True


def test_lock_key_canonicalizes_dot_dot_segments(tmp_path):
    (tmp_path / "x").mkdir()
    p = tmp_path / "x" / "messages.json"
    _write(p, {"messages": [{"id": 1, "is_favorite": False}]})

    _assert_second_path_blocks(p, tmp_path / "x" / ".." / "x" / "messages.json")


def test_lock_key_follows_symlinked_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    _write(real / "messages.json", {"messages": [{"id": 1, "is_favorite": False}]})

    _assert_second_path_blocks(real / "messages.json", link / "messages.json")


# --- Production writers share the lock ------------------------------------------

GROUP = {"id": 79, "name": "G"}
MEMBER = {"id": 134, "name": "M"}


def _manager(tmp_path) -> SyncManager:
    client = MagicMock(spec=Client)
    client.group = Group.SAKURAZAKA46
    return SyncManager(client, tmp_path)


def _seed_member(manager: SyncManager) -> Path:
    path = manager._member_dir_path(GROUP, MEMBER) / "messages.json"
    path.parent.mkdir(parents=True)
    _write(path, {"messages": [{"id": 1, "type": "text", "timestamp": "2026-06-15T00:00:00Z"}]})
    manager.sync_state["79_134"] = {"total_messages": 1}
    return path


async def _favorite(manager, path):
    return await asyncio.to_thread(SyncManager.update_message_fields_sync, path, 1, {"is_favorite": True})


async def _metadata(manager, path):
    await manager.update_message_metadata(path, {1: {"width": 640, "height": 480}})


async def _backfill(manager, path):
    manager.client.get_past_messages = AsyncMock(
        return_value=[{"id": 0, "type": "text", "text": "前", "member_id": 134, "published_at": "2026-06-14T00:00:00Z"}]
    )
    return await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER)


@pytest.mark.parametrize("writer", [_favorite, _metadata, _backfill], ids=["fields", "metadata", "backfill"])
async def test_production_writers_wait_for_the_file_lock(tmp_path, writer):
    manager = _manager(tmp_path)
    path = _seed_member(manager)
    holding = threading.Event()
    release = threading.Event()

    def hold(current):
        holding.set()
        assert release.wait(timeout=5)
        current["messages"].append({"id": 2, "type": "text", "timestamp": "2026-06-16T00:00:00Z"})
        return current, None

    holder = threading.Thread(target=SyncManager._locked_update, args=(path, hold))
    holder.start()
    assert holding.wait(timeout=5)
    try:
        task = asyncio.create_task(writer(manager, path))
        await asyncio.sleep(0.3)
        assert not task.done()  # blocked on the lock the holder owns
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=5)
    await asyncio.to_thread(holder.join, 5)

    ids = {m["id"] for m in _read(path)["messages"]}
    assert {1, 2} <= ids  # the holder's write was not lost to the writer's


async def test_sync_merge_and_favorite_toggles_lose_nothing(tmp_path):
    """Sync appends a new message per run while another thread favorites the
    existing ones: no id and no favorite may be lost."""
    manager = _manager(tmp_path)
    path = manager._member_dir_path(GROUP, MEMBER) / "messages.json"
    path.parent.mkdir(parents=True)
    seeded = list(range(1, 21))
    _write(
        path,
        {
            "messages": [
                {
                    "id": i,
                    "type": "text",
                    "content": f"m{i}",
                    "is_favorite": False,
                    "timestamp": f"2026-01-01T00:00:{i:02d}Z",
                }
                for i in seeded
            ]
        },
    )
    new_ids = iter(range(101, 121))

    async def fetch_one_new(*args, **kwargs):
        i = next(new_ids)
        return [
            {
                "id": i,
                "type": "text",
                "text": f"新{i}",
                "member_id": 134,
                "published_at": f"2026-02-01T00:00:{i - 100:02d}Z",
            }
        ]

    manager.client.get_messages = AsyncMock(side_effect=fetch_one_new)
    manager.client.download_file = AsyncMock(return_value=True)

    def toggle_all():
        for i in seeded:
            assert SyncManager.update_message_fields_sync(path, i, {"is_favorite": True})

    toggler = threading.Thread(target=toggle_all)
    toggler.start()
    for _ in range(20):
        await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])
    await asyncio.to_thread(toggler.join, 10)

    messages = _read(path)["messages"]
    assert {m["id"] for m in messages} == set(seeded) | set(range(101, 121))
    assert all(m["is_favorite"] for m in messages if m["id"] in seeded)
