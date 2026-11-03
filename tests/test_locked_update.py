"""Thread-locked read-modify-write for messages.json (spec §3)."""

import json
import threading
from pathlib import Path

import pytest

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


def test_locked_update_corrupt_raises_unless_corrupt_ok(tmp_path):
    p = tmp_path / "messages.json"
    p.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        SyncManager._locked_update(p, lambda cur: (None, None))

    seen = []
    SyncManager._locked_update(p, lambda cur: (seen.append(cur), (None, None))[1], corrupt_ok=True)
    assert seen == [None]


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
