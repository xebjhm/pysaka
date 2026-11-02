# pysaka 0.4.5 Archive Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship pysaka 0.4.5: a locked writer for `messages.json`, an `on_new_messages` sync callback, profile-image archiving with history, pre-subscription backfill, and (conditionally) the exact-multiple-of-200 pagination fix.

**Architecture:** All `messages.json` writes go through one synchronous, thread-locked read-modify-write primitive (`SyncManager._locked_update`) executed via `asyncio.to_thread`. `sync_member` becomes a thin wrapper around the existing body plus a never-failing profile-image archiving step. Backfill is a separate public method that owns its own media download and only sets a completion flag once everything is on disk. Cursor computation is untouched.

**Tech Stack:** Python ≥3.9, asyncio, aiohttp, aiofiles, structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`), `uv`.

**Spec:** `docs/superpowers/specs/2026-11-03-archive-integrity-v045-design.md`

## Global Constraints

- Run everything through `uv run` (`uv run pytest`, `uv run ruff check .`, `uv run mypy src`). Never `pip`.
- Commit with `uv run git commit` (bare `git commit` fails: pre-commit is only on the uv PATH).
- Commit messages: Conventional Commits, whole subject line ≤ 72 chars, imperative, no trailing period, body ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- `requires-python = ">=3.9"`: no `match`, no `X | Y` in runtime-evaluated annotations, no `asyncio.TaskGroup`.
- Every file I/O: `pathlib.Path` + `encoding="utf-8"`. Never `time.sleep()` in async code. No `print()`.
- Logging via `structlog` (`logger = structlog.get_logger()` already in `manager.py`); never log tokens or message text.
- 100 % coverage of new/changed lines (`addopts` already runs `--cov=pysaka --cov-report=term-missing`).
- Cursor fields (`last_message_id`, `last_sync_ts`) must never be changed by any new code path except the existing `update_sync_state` call in `sync_member`.
- Docstrings match code (Zero Drift); `docs/API.md` and `CHANGELOG.md` `[Unreleased]` updated in the same branch.
- Branch: `feat/archive-integrity-v045` (already created from `origin/dev`).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/pysaka/manager.py` (modify) | Lock registry, `_locked_update`, `update_message_fields`, `sync_member` wrapper + locked merge + callback, `update_message_metadata` via lock, `archive_profile_images`, `backfill_past_messages`, `update_total_messages`, `update_sync_state` preserving extra keys |
| `src/pysaka/profile_images.py` (create) | Pure helpers: URL extraction, safe relative paths, `index.json` merge. No I/O. |
| `src/pysaka/client.py` (modify) | `get_past_messages(..., strict=False)`; conditional `_timeline_first_id` + use in `get_messages` |
| `tests/test_locked_update.py` (create) | Lock primitive, `update_message_fields`, serialization test |
| `tests/test_profile_images.py` (create) | Pure helper tests + `archive_profile_images` + sync_member integration |
| `tests/test_backfill.py` (create) | Backfill lifecycle |
| `tests/test_manager.py` (modify) | Callback + locked merge tests for `sync_member` |
| `tests/test_client_new_apis.py` (modify) | `strict` flag tests |
| `tests/test_client.py` (modify, conditional Task 6) | 200-multiple regression |
| `docs/API.md`, `CHANGELOG.md` (modify) | Docs |

---

### Task 1: Thread-locked read-modify-write primitive + `update_message_fields`

**Files:**
- Modify: `src/pysaka/manager.py` (imports at top; new module-level lock registry after `_MEDIA_STRIPPING_STATES`; new static methods right after `_atomic_write_json`)
- Create: `tests/test_locked_update.py`

**Interfaces:**
- Produces:
  - `SyncManager._locked_update(path: Path, mutate: Callable[[Optional[Any]], tuple[Optional[Any], T]], *, corrupt_ok: bool = False) -> tuple[bool, T]` — synchronous; returns `(write_ok, result)`; `write_ok` is `True` when `mutate` returned `None` data (nothing to write).
  - `SyncManager.update_message_fields_sync(messages_file: Path, message_id: int, fields: dict[str, Any]) -> bool` (static, blocking — for callers already in a worker thread, e.g. SakaDesk favorites).
  - `async SyncManager.update_message_fields(messages_file: Path, message_id: int, fields: dict[str, Any]) -> bool` (static; runs the sync variant via `asyncio.to_thread`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_locked_update.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_locked_update.py -v --no-cov`
Expected: FAIL — `AttributeError: type object 'SyncManager' has no attribute '_locked_update'`.

- [ ] **Step 3: Implement**

In `src/pysaka/manager.py`, change the imports block to:

```python
import asyncio
import inspect
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
```

Below `_MEDIA_STRIPPING_STATES = frozenset(...)` add:

```python
T = TypeVar("T")

# One lock per messages.json (or any JSON file updated via _locked_update). Thread
# locks, not asyncio locks: SakaDesk's favorite toggle and the sync write both run
# their file work in worker threads (asyncio.to_thread), which an asyncio.Lock
# would not serialise. In-process only — cross-process writers are excluded by the
# consumer (SakaDesk's DataDirLock).
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """Return the process-wide lock for ``path`` (case-normalised on Windows)."""
    key = os.path.normcase(str(path.resolve()))
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock
```

Right after the `_atomic_write_json` static method add:

```python
    @staticmethod
    def _locked_update(
        path: Path,
        mutate: Callable[[Optional[Any]], tuple[Optional[Any], T]],
        *,
        corrupt_ok: bool = False,
    ) -> tuple[bool, T]:
        """Read-modify-write a JSON file under its process-wide lock.

        Synchronous and blocking — call via ``asyncio.to_thread`` from async code.
        ``mutate`` receives the parsed file (``None`` if the file is missing, or if
        it is unreadable and ``corrupt_ok`` is True) and returns ``(data, result)``.
        ``data is None`` means "nothing to write". Every writer of a given file must
        go through this method, otherwise a concurrent read-modify-write can
        overwrite the other's changes.

        Returns:
            ``(write_ok, result)``. ``write_ok`` is False only when a write was
            attempted and ``_atomic_write_json`` failed.

        Raises:
            ValueError: the file is unreadable JSON and ``corrupt_ok`` is False.
        """
        with _lock_for(path):
            current: Optional[Any] = None
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        current = json.load(f)
                except ValueError:
                    if not corrupt_ok:
                        raise
                    current = None
            data, result = mutate(current)
            if data is None:
                return True, result
            return SyncManager._atomic_write_json(path, data), result

    @staticmethod
    def update_message_fields_sync(messages_file: Path, message_id: int, fields: dict[str, Any]) -> bool:
        """Set ``fields`` on one message of a ``messages.json``, under the file lock.

        Blocking — for callers already running in a worker thread. From async
        code use :meth:`update_message_fields`. Use one of these instead of reading
        and rewriting the file yourself: a sync running at the same moment would
        otherwise lose either its new messages or your change.

        Returns:
            True if the message was found and the file was written; False if the
            file is missing, the id is absent, or the write failed.
        """

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], bool]:
            if not isinstance(current, dict):
                return None, False
            for msg in current.get("messages", []):
                if msg.get("id") == message_id:
                    msg.update(fields)
                    return current, True
            return None, False

        wrote, found = SyncManager._locked_update(messages_file, mutate)
        return found and wrote

    @staticmethod
    async def update_message_fields(messages_file: Path, message_id: int, fields: dict[str, Any]) -> bool:
        """Async form of :meth:`update_message_fields_sync` (runs it in a thread)."""
        return await asyncio.to_thread(SyncManager.update_message_fields_sync, messages_file, message_id, fields)
```

(`inspect` is used in Task 2; adding it now keeps the import block edit in one place. If ruff flags it unused before Task 2, add it in Task 2 instead.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_locked_update.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src
git add src/pysaka/manager.py tests/test_locked_update.py
uv run git commit -m "feat(manager): add thread-locked JSON read-modify-write

Adds SyncManager._locked_update and the public update_message_fields so
every writer of messages.json can share one per-file lock.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Route sync writes through the lock + `on_new_messages` callback

**Files:**
- Modify: `src/pysaka/manager.py` — `sync_member` (L152-394), `update_message_metadata` (L691-721), `update_sync_state` (L106-129)
- Modify: `tests/test_manager.py` (append tests)

**Interfaces:**
- Consumes: `SyncManager._locked_update` (Task 1).
- Produces:
  - `async SyncManager.sync_member(session, group, member, media_queue, progress_callback=None, prefetched_messages=None, *, on_new_messages: Optional[Callable[[dict[str, Any], list[dict[str, Any]]], Any]] = None) -> int` (return value unchanged: number of **processed** messages).
  - `on_new_messages(member_info, new_messages)` where `member_info = {"group_id": int, "group_name": str, "member_id": int, "member_name": str, "member_dir": Path, "phone_image": Optional[str]}` and `new_messages` are the normalized message dicts newly added to disk.
  - `SyncManager._member_dir_path(group: dict, member: dict) -> Path`
  - `SyncManager._type_counts(messages: list[dict]) -> dict[str, int]` (static)
  - `update_sync_state` now preserves keys it does not own (e.g. `past_backfilled_at`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_manager.py`)

```python
def _msg(i, ts, member_id=10, **kw):
    return {"id": i, "type": "text", "text": f"m{i}", "member_id": member_id, "published_at": ts, **kw}


@pytest.mark.asyncio
async def test_on_new_messages_receives_only_genuinely_new(sync_manager):
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "メンバー", "phone_image": "https://cdn/x/phone.jpg"}
    sync_manager.client.get_messages.return_value = [_msg(101, "2023-01-01T10:00:00Z")]
    await sync_manager.sync_member(session, group, member, [])

    # Second sync re-fetches the overlap (101) plus one new message (102).
    sync_manager.client.get_messages.return_value = [
        _msg(101, "2023-01-01T10:00:00Z"),
        _msg(102, "2023-01-01T11:00:00Z", type="voice", file="https://cdn/v.m4a"),
    ]
    calls = []

    async def on_new(info, msgs):
        calls.append((info, [m["id"] for m in msgs]))

    count = await sync_manager.sync_member(session, group, member, [], on_new_messages=on_new)

    assert count == 2  # unchanged contract: processed, not new
    assert len(calls) == 1
    info, ids = calls[0]
    assert ids == [102]
    assert info["group_id"] == 1 and info["member_id"] == 10
    assert info["member_name"] == "メンバー"
    assert info["phone_image"] == "https://cdn/x/phone.jpg"
    assert info["member_dir"] == sync_manager.output_dir / "messages" / "1 Grp" / "10 メンバー"


@pytest.mark.asyncio
async def test_on_new_messages_not_called_when_nothing_new(sync_manager):
    session = AsyncMock()
    group, member = {"id": 1, "name": "G"}, {"id": 10, "name": "M"}
    sync_manager.client.get_messages.return_value = [_msg(101, "2023-01-01T10:00:00Z")]
    await sync_manager.sync_member(session, group, member, [])
    called = MagicMock()
    await sync_manager.sync_member(session, group, member, [], on_new_messages=called)
    called.assert_not_called()


@pytest.mark.asyncio
async def test_on_new_messages_sync_callable_and_exception_swallowed(sync_manager):
    session = AsyncMock()
    group, member = {"id": 1, "name": "G"}, {"id": 10, "name": "M"}
    sync_manager.client.get_messages.return_value = [_msg(101, "2023-01-01T10:00:00Z")]

    def boom(info, msgs):
        raise RuntimeError("consumer bug")

    count = await sync_manager.sync_member(session, group, member, [], on_new_messages=boom)
    assert count == 1
    assert sync_manager.get_last_ts(1, 10) == "2023-01-01T10:00:00Z"  # cursor still advanced


@pytest.mark.asyncio
async def test_sync_merge_keeps_concurrent_favorite(sync_manager, monkeypatch):
    """A favorite written between sync's pre-read and its write must survive:
    the merge happens under the lock against the file as it is at write time."""
    session = AsyncMock()
    group, member = {"id": 1, "name": "G"}, {"id": 10, "name": "M"}
    sync_manager.client.get_messages.return_value = [_msg(101, "2023-01-01T10:00:00Z")]
    await sync_manager.sync_member(session, group, member, [])
    path = sync_manager.output_dir / "messages" / "1 G" / "10 M" / "messages.json"

    original_prepare = sync_manager.prepare_messages

    def prepare_then_favorite(messages, member_dir, queue):
        # Runs after sync's pre-read and before its write.
        def fav(current):
            current["messages"][0]["local_note"] = "kept"
            return current, None

        SyncManager._locked_update(path, fav)
        return original_prepare(messages, member_dir, queue)

    monkeypatch.setattr(sync_manager, "prepare_messages", prepare_then_favorite)
    sync_manager.client.get_messages.return_value = [_msg(102, "2023-01-01T11:00:00Z")]
    await sync_manager.sync_member(session, group, member, [])

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    by_id = {m["id"]: m for m in data["messages"]}
    assert set(by_id) == {101, 102}
    assert by_id[101]["local_note"] == "kept"


@pytest.mark.asyncio
async def test_on_new_messages_not_fired_during_recovery(sync_manager):
    session = AsyncMock()
    group, member = {"id": 1, "name": "G"}, {"id": 10, "name": "M"}
    member_dir = sync_manager.output_dir / "messages" / "1 G" / "10 M"
    member_dir.mkdir(parents=True)
    (member_dir / "messages.json").write_text("{corrupt", encoding="utf-8")
    sync_manager.client.get_messages.return_value = [_msg(101, "2023-01-01T10:00:00Z")]
    called = MagicMock()

    count = await sync_manager.sync_member(session, group, member, [], on_new_messages=called)

    assert count == 1
    called.assert_not_called()


def test_update_sync_state_preserves_unowned_keys(sync_manager):
    sync_manager.sync_state["1_100"] = {"past_backfilled_at": "2026-11-03T00:00:00Z"}
    sync_manager.update_sync_state(1, 100, 500, 10, last_ts="2026-11-03T01:00:00Z")
    entry = sync_manager.sync_state["1_100"]
    assert entry["past_backfilled_at"] == "2026-11-03T00:00:00Z"
    assert entry["last_message_id"] == 500 and entry["total_messages"] == 10


@pytest.mark.asyncio
async def test_update_message_metadata_goes_through_lock(sync_manager, tmp_path):
    p = tmp_path / "messages.json"
    p.write_text(json.dumps({"messages": [{"id": 1}, {"id": 2}]}), encoding="utf-8")
    await sync_manager.update_message_metadata(p, {1: {"width": 10, "height": None}})
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["messages"][0]["width"] == 10 and "height" not in data["messages"][0]
    before = p.stat().st_mtime_ns
    await sync_manager.update_message_metadata(p, {99: {"width": 1}})  # no match: no write
    assert p.stat().st_mtime_ns == before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_manager.py -v --no-cov -k "on_new_messages or concurrent_favorite or unowned_keys or metadata_goes_through"`
Expected: FAIL — `TypeError: sync_member() got an unexpected keyword argument 'on_new_messages'`, and the favorite/preserve tests fail on assertions.

- [ ] **Step 3: Implement**

3a. Replace `update_sync_state`'s body assignment so it merges instead of replacing:

```python
        key = f"{group_id}_{member_id}"
        # Merge, don't replace: other features keep their own keys in the entry
        # (e.g. backfill's past_backfilled_at), which a full replace would wipe on
        # every sync.
        entry = dict(self.sync_state.get(key) or {})
        entry.update(
            {
                "last_message_id": last_msg_id,
                "last_sync_ts": last_ts,
                "total_messages": count,
                "last_sync": datetime.now(timezone.utc).isoformat() + "Z",
            }
        )
        self.sync_state[key] = entry
        self.save_sync_state()
```

3b. Add helpers to the class (place right before `sync_member`):

```python
    def _member_dir_path(self, group: dict[str, Any], member: dict[str, Any]) -> Path:
        """The on-disk directory of a member's archive (same naming as sync_member)."""
        return (
            self.output_dir
            / "messages"
            / f"{group['id']} {sanitize_name(group['name'])}"
            / f"{member['id']} {sanitize_name(member['name'])}"
        )

    @staticmethod
    def _type_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"text": 0, "video": 0, "picture": 0, "voice": 0}
        for msg in messages:
            mtype = msg.get("type", "text")
            if mtype in counts:
                counts[mtype] += 1
        return counts

    @staticmethod
    async def _notify_new_messages(
        callback: Callable[[dict[str, Any], list[dict[str, Any]]], Any],
        member_info: dict[str, Any],
        new_messages: list[dict[str, Any]],
    ) -> None:
        """Invoke a consumer callback; its failures must never break a sync."""
        try:
            result = callback(member_info, new_messages)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error("on_new_messages callback failed", error=str(e), exc_info=True)
```

3c. Rename the existing `sync_member` to `_sync_member_messages`, add the keyword-only parameter `on_new_messages: Optional[Callable[[dict[str, Any], list[dict[str, Any]]], Any]] = None` to it, and add a new public `sync_member` above it:

```python
    async def sync_member(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        media_queue: list[dict[str, Any]],
        progress_callback: Optional[Any] = None,
        prefetched_messages: Optional[list[dict[str, Any]]] = None,
        *,
        on_new_messages: Optional[Callable[[dict[str, Any], list[dict[str, Any]]], Any]] = None,
    ) -> int:
        """
        Syncs messages for a member, prepares the media queue, and archives the
        member's profile images.

        Args:
            session: Active aiohttp session.
            group: Group object dict.
            member: Member object dict.
            media_queue: List to append media download tasks to.
            progress_callback: Optional callback for progress updates.
            prefetched_messages: Pre-fetched group timeline messages. When
                provided, the API call is skipped and messages are filtered
                from this list instead (using this member's own last_id).
            on_new_messages: Optional ``callback(member_info, new_messages)``
                (sync or async) called once after a successful write when at
                least one message was not on disk before. Not called during
                corrupt-file / count-mismatch recovery. Exceptions it raises are
                logged and swallowed.

        Returns:
            Number of messages processed in this run (includes re-fetched
            overlap, so it is not a count of new messages).
        """
        processed = await self._sync_member_messages(
            session,
            group,
            member,
            media_queue,
            progress_callback,
            prefetched_messages,
            on_new_messages=on_new_messages,
        )
        await self._archive_profile_images_safely(session, group, member)
        return processed
```

and, until Task 3 implements it, a no-op placeholder that Task 3 replaces in the same file (keeps this task green and reviewable on its own):

```python
    async def _archive_profile_images_safely(
        self, session: aiohttp.ClientSession, group: dict[str, Any], member: dict[str, Any]
    ) -> None:
        """Archive profile images; never raises (implemented in the next commit)."""
        return None
```

Update the `_sync_member_messages` docstring's first line to "Message part of :meth:`sync_member` (see there for arguments)." and keep its `Returns:` section.

3d. Inside `_sync_member_messages`, replace everything from `# Dedupe (Upsert: prefer fresh API data, ...` down to and including the `if not wrote: ... return 0` block with:

```python
            member_block = {
                "id": mid,
                "name": mname,
                "group_id": gid,
                "portrait": member.get("portrait"),
                "thumbnail": member.get("thumbnail"),
                "phone_image": member.get("phone_image"),
                "group_thumbnail": group.get("thumbnail"),
            }

            def mutate(current: Optional[Any]) -> tuple[Optional[Any], tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
                # Merge against the file as it is NOW, under the lock — not the copy
                # read before the API call. Another writer (e.g. a favorite toggle)
                # may have rewritten it since; merging the stale copy would drop
                # that change, and the other writer's stale copy would drop ours.
                base = current.get("messages", []) if isinstance(current, dict) else existing_msgs
                base_ids = {m.get("id") for m in base}
                new_msgs = [m for m in processed if m["id"] not in base_ids]
                # Dedupe (Upsert: prefer fresh API data, but preserve locally-derived
                # media metadata the API never returns — see _merge_messages).
                merged = self._merge_messages(base, processed)
                merged.sort(key=lambda x: x.get("timestamp") or "")
                export_data = {
                    "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
                    "member": member_block,
                    "total_messages": len(merged),
                    "message_type_counts": self._type_counts(merged),
                    "messages": merged,
                }
                return export_data, (merged, new_msgs)

            wrote, (merged, new_msgs) = await asyncio.to_thread(
                self._locked_update, existing_file, mutate, corrupt_ok=True
            )
            if not wrote:
                # The messages never hit disk — do NOT advance the cursor, or the next
                # sync would skip them permanently. Abort this member; retry next run.
                logger.error("Skipping cursor advance: messages.json write failed", member=mname)
                return 0
```

Keep the existing cursor/state block unchanged, then immediately after `self.update_sync_state(gid, mid, max_id, len(merged), last_ts=newest_ts)` and before `return len(processed)` add:

```python
            if on_new_messages is not None and new_msgs and not recovering:
                member_info = {
                    "group_id": gid,
                    "group_name": group["name"],
                    "member_id": mid,
                    "member_name": member["name"],
                    "member_dir": member_dir,
                    "phone_image": member.get("phone_image"),
                }
                await self._notify_new_messages(on_new_messages, member_info, new_msgs)
```

Delete the old `# Stats` / `type_counts` loop (now `_type_counts`).

3e. Replace the body of `update_message_metadata` (keep signature/docstring; add one docstring line "Runs under the file lock (see :meth:`_locked_update`).") with:

```python
        if not metadata or not messages_file.exists():
            return

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], None]:
            if not isinstance(current, dict):
                return None, None
            updated = False
            for msg in current.get("messages", []):
                msg_metadata = metadata.get(msg.get("id"))
                if msg_metadata:
                    for key, value in msg_metadata.items():
                        if value is not None:
                            msg[key] = value
                            updated = True
            return (current if updated else None), None

        try:
            wrote, _ = await asyncio.to_thread(self._locked_update, messages_file, mutate)
            if not wrote:
                logger.error("Failed to write message metadata", file=str(messages_file))
        except Exception as e:
            logger.error("Failed to update message metadata", file=str(messages_file), error=str(e))
```

If `aiofiles` is no longer used anywhere in `manager.py` after this, remove its import.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all PASS (existing `test_sync_member_write_failure_does_not_advance_cursor` still sees exactly one `_atomic_write_json` call). Coverage report shows no new missed lines in `manager.py`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src
git add src/pysaka/manager.py tests/test_manager.py
uv run git commit -m "fix(manager): merge messages.json under the file lock

sync_member and update_message_metadata now read-merge-write inside
_locked_update, so a concurrent favorite toggle can no longer drop newly
synced messages after the cursor advanced. Adds the on_new_messages
callback reporting messages that were not on disk before.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Profile-image archiving with history

**Files:**
- Create: `src/pysaka/profile_images.py`
- Modify: `src/pysaka/manager.py` (import; replace `_archive_profile_images_safely`; add `archive_profile_images`)
- Create: `tests/test_profile_images.py`

**Interfaces:**
- Consumes: `_locked_update`, `_member_dir_path` (Tasks 1–2), `Client.download_file(session, url, filepath) -> bool`, `media_file_is_present`.
- Produces:
  - `profile_images.KINDS = ("thumbnail", "phone_image", "group_thumbnail")`, `DIR_NAME = "profile_images"`, `INDEX_NAME = "index.json"`
  - `profile_images.image_urls(group: dict, member: dict) -> dict[str, str]`
  - `profile_images.relative_file(kind: str, url: str) -> Optional[str]`
  - `profile_images.merge_index(current: Optional[Any], archived: dict[str, tuple[str, str]], current_urls: dict[str, str], now_iso: str) -> Optional[dict]`
  - `async SyncManager.archive_profile_images(session, group, member, member_dir: Path) -> dict[str, int]` → `{"downloaded", "skipped", "failed"}`
  - On-disk `index.json`: `{"version": 1, "current": {kind: url}, "images": [{"kind", "file", "url", "first_seen"}]}` where `file` is relative to `profile_images/`.

Note for SakaDesk: the spec text says `current` maps kind → file; this plan stores kind → **URL** (always the latest API URL, even before its download succeeds). SakaDesk derives the current entry as the image whose `url` equals `current[kind]`, falling back to the remote URL when none matches. Update the spec's §2 example in Step 5's commit.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_profile_images.py`:

```python
"""Profile image archiving (spec §2)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka import profile_images as pi
from pysaka.client import Client, Group
from pysaka.manager import SyncManager

NOW = "2026-11-03T04:12:00Z"
GROUP = {"id": 79, "name": "中川 智尋", "thumbnail": "https://cdn/public/groups/thumbnails/79-20261001-010023.jpg"}
MEMBER = {
    "id": 134,
    "name": "中川 智尋",
    "thumbnail": "https://cdn/public/members/thumbnails/134-20260705-113257.jpeg",
    "phone_image": "https://cdn/public/members/phone-images/134-20260223-125348.jpeg?sig=abc",
}


def test_image_urls_skips_empty():
    urls = pi.image_urls({"thumbnail": ""}, {"thumbnail": "u1", "phone_image": None})
    assert urls == {"thumbnail": "u1"}


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cdn/a/134-20260705-113257.jpeg", "thumbnail/134-20260705-113257.jpeg"),
        ("https://cdn/a/x.jpeg?sig=1#f", "thumbnail/x.jpeg"),
        ("https://cdn/a/..", "thumbnail/_"),
        ("https://cdn/a/", None),
    ],
)
def test_relative_file(url, expected):
    assert pi.relative_file("thumbnail", url) == expected


def test_merge_index_creates_and_appends_history():
    first = pi.merge_index(None, {"thumbnail": ("thumbnail/a.jpg", "u/a.jpg")}, {"thumbnail": "u/a.jpg"}, NOW)
    assert first == {
        "version": 1,
        "current": {"thumbnail": "u/a.jpg"},
        "images": [{"kind": "thumbnail", "file": "thumbnail/a.jpg", "url": "u/a.jpg", "first_seen": NOW}],
    }
    assert pi.merge_index(first, {"thumbnail": ("thumbnail/a.jpg", "u/a.jpg")}, {"thumbnail": "u/a.jpg"}, NOW) is None

    later = "2026-12-01T00:00:00Z"
    second = pi.merge_index(first, {"thumbnail": ("thumbnail/b.jpg", "u/b.jpg")}, {"thumbnail": "u/b.jpg"}, later)
    assert [e["file"] for e in second["images"]] == ["thumbnail/a.jpg", "thumbnail/b.jpg"]
    assert second["images"][1]["first_seen"] == later
    assert second["current"] == {"thumbnail": "u/b.jpg"}


def test_merge_index_current_tracks_api_even_before_download():
    idx = pi.merge_index(None, {}, {"phone_image": "u/p.jpg"}, NOW)
    assert idx == {"version": 1, "current": {"phone_image": "u/p.jpg"}, "images": []}


def test_merge_index_nothing_to_record_returns_none():
    assert pi.merge_index(None, {}, {}, NOW) is None


def test_merge_index_replaces_invalid_existing():
    idx = pi.merge_index(["bad"], {}, {"thumbnail": "u"}, NOW)
    assert idx["current"] == {"thumbnail": "u"} and idx["images"] == []


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.SAKURAZAKA46
    client.get_messages = AsyncMock(return_value=[])

    async def fake_download(session, url, path, timestamp=None):
        if "fail" in url:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")
        return True

    client.download_file = AsyncMock(side_effect=fake_download)
    return SyncManager(client, tmp_path)


def _index(member_dir):
    with open(member_dir / "profile_images" / "index.json", encoding="utf-8") as f:
        return json.load(f)


async def test_archive_downloads_then_skips(manager, tmp_path, time_machine):
    time_machine.move_to(NOW)
    member_dir = tmp_path / "m"
    report = await manager.archive_profile_images(AsyncMock(), GROUP, MEMBER, member_dir)
    assert report == {"downloaded": 3, "skipped": 0, "failed": 0}
    assert (member_dir / "profile_images" / "phone_image" / "134-20260223-125348.jpeg").read_bytes() == b"img"
    idx = _index(member_dir)
    assert idx["current"]["phone_image"] == MEMBER["phone_image"]
    assert {e["first_seen"] for e in idx["images"]} == {NOW}

    report = await manager.archive_profile_images(AsyncMock(), GROUP, MEMBER, member_dir)
    assert report == {"downloaded": 0, "skipped": 3, "failed": 0}
    assert manager.client.download_file.await_count == 3


async def test_archive_failed_download_is_not_indexed_and_retried(manager, tmp_path):
    member_dir = tmp_path / "m"
    member = {**MEMBER, "thumbnail": "https://cdn/fail/t.jpg"}
    report = await manager.archive_profile_images(AsyncMock(), GROUP, member, member_dir)
    assert report["failed"] == 1
    idx = _index(member_dir)
    assert "thumbnail/t.jpg" not in {e["file"] for e in idx["images"]}
    assert idx["current"]["thumbnail"] == "https://cdn/fail/t.jpg"


async def test_archive_no_urls_writes_nothing(manager, tmp_path):
    member_dir = tmp_path / "m"
    report = await manager.archive_profile_images(AsyncMock(), {"id": 1, "name": "G"}, {"id": 2, "name": "M"}, member_dir)
    assert report == {"downloaded": 0, "skipped": 0, "failed": 0}
    assert not (member_dir / "profile_images").exists()


async def test_sync_member_archives_even_without_new_messages(manager):
    count = await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])
    assert count == 0
    member_dir = manager._member_dir_path(GROUP, MEMBER)
    assert len(_index(member_dir)["images"]) == 3


async def test_sync_member_survives_archiving_crash(manager, monkeypatch):
    monkeypatch.setattr(manager, "archive_profile_images", AsyncMock(side_effect=RuntimeError("disk full")))
    manager.client.get_messages.return_value = [
        {"id": 1, "type": "text", "text": "hi", "member_id": 134, "published_at": "2026-01-01T00:00:00Z"}
    ]
    assert await manager.sync_member(AsyncMock(), GROUP, MEMBER, []) == 1
    assert manager.get_last_ts(79, 134) == "2026-01-01T00:00:00Z"
```

`time_machine` is the `time-machine` pytest fixture. If `uv run python -c "import time_machine"` fails, add it first: `uv add --dev time-machine`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_profile_images.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'profile_images' from 'pysaka'`.

- [ ] **Step 3: Implement**

Create `src/pysaka/profile_images.py`:

```python
"""Pure helpers for archiving member profile images with history.

Three images describe a talk room member: the list icon (the room's
``group.thumbnail``), the chat icon (``member.thumbnail``) and the call screen
(``member.phone_image``). Their CDN basenames embed an upload timestamp and change
when the member changes the image, so archiving every distinct basename yields a
history. This module does no I/O; :meth:`pysaka.manager.SyncManager.archive_profile_images`
does the downloads and writes ``index.json``.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from .utils import sanitize_name

KINDS = ("thumbnail", "phone_image", "group_thumbnail")
DIR_NAME = "profile_images"
INDEX_NAME = "index.json"
INDEX_VERSION = 1


def image_urls(group: dict[str, Any], member: dict[str, Any]) -> dict[str, str]:
    """Map each kind to its current CDN URL, omitting kinds without one."""
    raw = {
        "thumbnail": member.get("thumbnail"),
        "phone_image": member.get("phone_image"),
        "group_thumbnail": group.get("thumbnail"),
    }
    return {kind: url for kind, url in raw.items() if url}


def relative_file(kind: str, url: str) -> Optional[str]:
    """Path of the archived file relative to ``profile_images/``, or None.

    Uses the URL's basename (query/fragment dropped) after ``sanitize_name``, so it
    can never contain a path separator or ``..`` and always stays inside the
    kind's directory.
    """
    basename = urlparse(url).path.rsplit("/", 1)[-1]
    safe = sanitize_name(basename)
    if not safe:
        return None
    return f"{kind}/{safe}"


def merge_index(
    current: Optional[Any],
    archived: dict[str, tuple[str, str]],
    current_urls: dict[str, str],
    now_iso: str,
) -> Optional[dict[str, Any]]:
    """Return the updated index, or None if nothing changed.

    Args:
        current: Parsed existing ``index.json`` (None or invalid → start fresh).
        archived: kind → (relative file, url) for images present on disk now.
        current_urls: kind → URL from the latest API data (recorded even when the
            download has not succeeded yet, so consumers know what is current).
        now_iso: UTC timestamp recorded as ``first_seen`` for new entries.
    """
    valid = isinstance(current, dict) and isinstance(current.get("images"), list)
    if not valid and not archived and not current_urls:
        return None
    index: dict[str, Any] = (
        current if valid else {"version": INDEX_VERSION, "current": {}, "images": []}
    )
    changed = not valid
    known = {(e.get("kind"), e.get("file")) for e in index["images"]}
    for kind, (rel, url) in archived.items():
        if (kind, rel) not in known:
            index["images"].append({"kind": kind, "file": rel, "url": url, "first_seen": now_iso})
            changed = True
    if index.get("current") != current_urls:
        index["current"] = dict(current_urls)
        changed = True
    return index if changed else None
```

In `src/pysaka/manager.py`: add `from . import profile_images` to the relative imports, then replace the Task 2 placeholder `_archive_profile_images_safely` and add `archive_profile_images`:

```python
    async def archive_profile_images(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        member_dir: Path,
    ) -> dict[str, int]:
        """Download the member's current profile images and record them in
        ``<member_dir>/profile_images/index.json``.

        Idempotent: an image already on disk (non-empty) is skipped. A failed
        download is not indexed and is retried on the next call.

        Returns:
            ``{"downloaded": n, "skipped": n, "failed": n}``.
        """
        report = {"downloaded": 0, "skipped": 0, "failed": 0}
        urls = profile_images.image_urls(group, member)
        root = member_dir / profile_images.DIR_NAME
        archived: dict[str, tuple[str, str]] = {}
        for kind, url in urls.items():
            rel = profile_images.relative_file(kind, url)
            if rel is None:
                report["failed"] += 1
                continue
            path = root / rel
            if media_file_is_present(path):
                report["skipped"] += 1
            elif await self.client.download_file(session, url, path):
                report["downloaded"] += 1
            else:
                report["failed"] += 1
                continue
            archived[kind] = (rel, url)

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], None]:
            return profile_images.merge_index(current, archived, urls, now_iso), None

        if urls or (root / profile_images.INDEX_NAME).exists():
            root.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._locked_update, root / profile_images.INDEX_NAME, mutate, corrupt_ok=True)
        return report

    async def _archive_profile_images_safely(
        self, session: aiohttp.ClientSession, group: dict[str, Any], member: dict[str, Any]
    ) -> None:
        """Run :meth:`archive_profile_images`; any failure is logged, never raised,
        so it cannot change sync_member's result or the cursor."""
        try:
            report = await self.archive_profile_images(session, group, member, self._member_dir_path(group, member))
            if report["downloaded"] or report["failed"]:
                logger.info("Profile images archived", member_id=member.get("id"), **report)
        except Exception as e:
            logger.warning("Profile image archiving failed", member_id=member.get("id"), error=str(e))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all PASS, no missed lines in `profile_images.py`.

- [ ] **Step 5: Fix the spec example, lint, commit**

In `docs/superpowers/specs/2026-11-03-archive-integrity-v045-design.md` §2, change the `current` example to map kinds to URLs and the bullet to: "`current` maps each kind to the URL from the fresh API data of the latest sync (recorded even before its download succeeds); consumers treat the image whose `url` matches as current and fall back to the remote URL otherwise."

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src
git add src/pysaka/profile_images.py src/pysaka/manager.py tests/test_profile_images.py docs/superpowers/specs/2026-11-03-archive-integrity-v045-design.md pyproject.toml uv.lock
uv run git commit -m "feat(manager): archive member profile images with history

Every sync downloads the list icon, chat icon and call-screen image when
their CDN basename is new and records them in profile_images/index.json.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Pre-subscription backfill

**Files:**
- Modify: `src/pysaka/client.py:1164-1181` (`get_past_messages`)
- Modify: `src/pysaka/manager.py` (add `update_total_messages`, `backfill_past_messages`)
- Modify: `tests/test_client_new_apis.py` (class `TestGetPastMessages`)
- Create: `tests/test_backfill.py`

**Interfaces:**
- Consumes: `_locked_update`, `_member_dir_path`, `_type_counts`, `prepare_messages`, `process_media_queue`, `update_message_metadata`, `_merge_messages`.
- Produces:
  - `async Client.get_past_messages(session, group_id, *, strict: bool = False) -> Optional[list[dict]]` — `strict=True` returns `None` when the request failed.
  - `SyncManager.update_total_messages(group_id: int, member_id: int, count: int) -> None`
  - `async SyncManager.backfill_past_messages(session, group: dict, member: dict) -> int` (messages added)
  - sync_state key `past_backfilled_at` (UTC `YYYY-MM-DDTHH:MM:SSZ`).

- [ ] **Step 1: Write the failing tests**

Append to `TestGetPastMessages` in `tests/test_client_new_apis.py`:

```python
    @pytest.mark.asyncio
    async def test_get_past_messages_strict_distinguishes_failure(self, client):
        with patch.object(client, "fetch_json", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = None
            assert await client.get_past_messages(MagicMock(), 123) == []
            assert await client.get_past_messages(MagicMock(), 123, strict=True) is None
            mock_fetch.return_value = {"messages": []}
            assert await client.get_past_messages(MagicMock(), 123, strict=True) == []
```

Create `tests/test_backfill.py`:

```python
"""Pre-subscription backfill (spec §5)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka.client import Client, Group
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
    manager.client.get_past_messages.return_value = [_m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/fail.jpg")]
    assert await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER) == 1
    assert "past_backfilled_at" not in manager.sync_state["79_134"]
    assert _ids(path) == [401, 500]

    manager.client.get_past_messages.return_value = [_m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/ok.jpg")]
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_backfill.py tests/test_client_new_apis.py -v --no-cov -k "backfill or past_messages"`
Expected: FAIL — `AttributeError: ... 'backfill_past_messages'` and `TypeError: ... unexpected keyword argument 'strict'`.

- [ ] **Step 3: Implement**

`src/pysaka/client.py` — replace `get_past_messages`:

```python
    async def get_past_messages(
        self, session: aiohttp.ClientSession, group_id: int, *, strict: bool = False
    ) -> Optional[list[dict[str, Any]]]:
        """
        Fetch historical messages (before user's subscription start date).

        Returns the roughly 1–1.5 days of messages immediately before the
        account's first subscription to the room. The server keeps returning
        them after the subscription lapses or is renewed. Does NOT mark messages
        as read.

        Args:
            session: Active aiohttp ClientSession.
            group_id: The ID of the group/member.
            strict: If True, return None when the request failed, so callers can
                tell a failure apart from a room with no earlier messages.

        Returns:
            List of historical message objects (possibly empty); None only when
            ``strict`` is True and the request failed.
        """
        data = await self.fetch_json(session, f"/groups/{group_id}/past_messages")
        if data is None and strict:
            return None
        if data and "messages" in data:
            return data["messages"]
        return []
```

`src/pysaka/manager.py` — add after `update_sync_state`:

```python
    def update_total_messages(self, group_id: int, member_id: int, count: int) -> None:
        """Update only ``total_messages`` for a member (cursor fields untouched)."""
        key = f"{group_id}_{member_id}"
        entry = dict(self.sync_state.get(key) or {})
        entry["total_messages"] = count
        self.sync_state[key] = entry
        self.save_sync_state()
```

and the backfill method (after `sync_member`):

```python
    async def backfill_past_messages(
        self, session: aiohttp.ClientSession, group: dict[str, Any], member: dict[str, Any]
    ) -> int:
        """Merge the room's pre-subscription messages into the member's archive.

        Runs once per member: after it completes, ``past_backfilled_at`` is set in
        the member's sync_state entry and later calls return immediately. Requires
        a prior successful :meth:`sync_member` (no entry or no ``messages.json`` →
        skipped). Downloads its own media, because Verify & Fix takes fresh URLs
        from the timeline, which never contains these messages. The flag is set
        only when the request succeeded and every queued file is on disk; on any
        failure the next call retries (merging is idempotent). Cursor fields are
        never touched and ``on_new_messages`` is never fired.

        Returns:
            Number of messages that were not in the archive before.
        """
        gid, mid = group["id"], member["id"]
        key = f"{gid}_{mid}"
        entry = self.sync_state.get(key)
        if not entry or entry.get("past_backfilled_at"):
            return 0
        member_dir = self._member_dir_path(group, member)
        messages_file = member_dir / "messages.json"
        if not messages_file.exists():
            return 0

        try:
            past = await self.client.get_past_messages(session, gid, strict=True)
        except (SessionExpiredError, RefreshFailedError):
            raise
        except Exception as e:
            logger.warning("Past message fetch failed", group_id=gid, error=str(e))
            return 0
        if past is None:
            logger.warning("Past message fetch failed", group_id=gid)
            return 0

        mine = [m for m in past if m.get("member_id") == mid]
        queue: list[dict[str, Any]] = []
        processed, earliest_failed_ts, _ = self.prepare_messages(mine, member_dir, queue)

        added = 0
        if processed:

            def mutate(current: Optional[Any]) -> tuple[Optional[Any], tuple[int, int]]:
                if not isinstance(current, dict):
                    return None, (0, -1)
                base = current.get("messages", [])
                base_ids = {m.get("id") for m in base}
                new_count = sum(1 for m in processed if m["id"] not in base_ids)
                merged = self._merge_messages(base, processed)
                merged.sort(key=lambda x: x.get("timestamp") or "")
                current["messages"] = merged
                current["total_messages"] = len(merged)
                current["message_type_counts"] = self._type_counts(merged)
                return current, (new_count, len(merged))

            try:
                wrote, (added, total) = await asyncio.to_thread(self._locked_update, messages_file, mutate)
            except ValueError:
                logger.warning("Backfill skipped: messages.json unreadable", group_id=gid, member_id=mid)
                return 0
            if not wrote or total < 0:
                return 0
            self.update_total_messages(gid, mid, total)

        metadata = await self.process_media_queue(session, queue)
        if metadata.get(member_dir):
            await self.update_message_metadata(messages_file, metadata[member_dir])

        complete = earliest_failed_ts is None and all(media_file_is_present(item["path"]) for item in queue)
        if complete:
            self.sync_state[key]["past_backfilled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.save_sync_state()
        logger.info("Past messages backfilled", group_id=gid, member_id=mid, added=added, complete=complete)
        return added
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all PASS; no new missed lines.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src
git add src/pysaka/client.py src/pysaka/manager.py tests/test_backfill.py tests/test_client_new_apis.py
uv run git commit -m "feat(manager): backfill messages from before the subscription

backfill_past_messages merges /past_messages into the archive once per
member, downloads its media itself, and never moves the cursor.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5 (conditional): Exact-multiple-of-200 frozen chat

Ship only if Step 1 proves a definitive end-of-timeline signal. Otherwise do Step 6 only.

**Files:**
- Create (scratch, not committed): `%TEMP%\saka-spike\asc_probe.py`
- Modify: `src/pysaka/client.py` (`get_messages`, new `_timeline_first_id`)
- Modify: `tests/test_client.py`

**Interfaces:**
- Produces: `async Client._timeline_first_id(session, group_id) -> Optional[int]`

- [ ] **Step 1: Live read-only probe (Windows, existing login, no refresh)**

Write `C:\Users\xtorker\AppData\Local\Temp\saka-spike\asc_probe.py` (from WSL: `/mnt/c/Users/xtorker/AppData/Local/Temp/saka-spike/asc_probe.py`):

```python
"""Read-only: does /timeline?order=asc&count=1 return the room's FIRST message?"""

import asyncio
import json
from pathlib import Path

import aiohttp
from pysaka import Client, Group
from pysaka.credentials import get_token_manager
from pysaka.utils import get_jwt_remaining_seconds

ROOMS = {Group.HINATAZAKA46: [53, 90], Group.SAKURAZAKA46: [79]}
OUT = Path(__file__).with_name("asc_probe_report.json")


async def _no_refresh(*_a, **_k):
    return False


async def main() -> None:
    tm = get_token_manager()
    report = {}
    for group, rooms in ROOMS.items():
        token = (tm.load_session(group.value) or {}).get("access_token")
        if not token or (get_jwt_remaining_seconds(token) or 0) < 300:
            report[group.value] = "token expired; skipped"
            continue
        client = Client(group, access_token=token)
        client.refresh_access_token = _no_refresh
        async with aiohttp.ClientSession() as s:
            for gid in rooms:
                asc = await client.fetch_json(s, f"/groups/{gid}/timeline", {"count": 1, "order": "asc", "clear_unread": "false"})
                desc_all = await client.get_messages(s, gid)
                first = min(desc_all, key=lambda m: m.get("published_at") or "") if desc_all else None
                report[f"{group.value}/{gid}"] = {
                    "asc_first": [(m["id"], m.get("published_at")) for m in (asc or {}).get("messages", [])],
                    "asc_has_continuation": bool((asc or {}).get("continuation")),
                    "true_first": (first["id"], first.get("published_at")) if first else None,
                }
                await asyncio.sleep(0.5)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


asyncio.run(main())
```

Run from WSL:

```bash
powershell.exe -NoProfile -Command "Set-Location \$env:TEMP\saka-spike; uv run --no-project --with pysaka==0.4.4 python -I asc_probe.py *> \$null"
cat /mnt/c/Users/xtorker/AppData/Local/Temp/saka-spike/asc_probe_report.json
```

Decision gate: proceed to Step 2 **only if** for every probed room `asc_first[0][0] == true_first[0]`. If any room differs, the API ignores `order=asc` (or sorts differently) → skip to Step 6.

- [ ] **Step 2: Write the failing regression test** (append to `tests/test_client.py`)

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from pysaka import ApiError, Client, Group


def _page(start_id, n, ts_fn):
    # Newest first (order=desc), like the API.
    return [{"id": i, "published_at": ts_fn(i)} for i in range(start_id + n - 1, start_id - 1, -1)]


def _ts(i):
    return f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"


@pytest.mark.asyncio
async def test_full_last_page_at_timeline_start_is_not_a_gap():
    """History is exactly 400 messages: the second page is full (200) and has no
    continuation. When the room's first message (order=asc probe) is in the
    collected set, this is the start of the timeline — return, don't raise."""
    client = Client(group=Group.HINATAZAKA46, access_token="t")
    pages = [
        {"messages": _page(201, 200, _ts), "continuation": "c1"},
        {"messages": _page(1, 200, _ts)},  # full, no continuation
    ]
    calls = []

    async def fetch(session, endpoint, params=None):
        calls.append(params)
        if params.get("order") == "asc":
            return {"messages": [{"id": 1, "published_at": _ts(1)}]}
        return pages.pop(0)

    client.fetch_json = AsyncMock(side_effect=fetch)
    msgs = await client.get_messages(MagicMock(), 5, since_ts="2025-12-31T00:00:00Z")
    assert len(msgs) == 400
    assert any(p.get("order") == "asc" for p in calls)


@pytest.mark.asyncio
async def test_full_last_page_still_raises_when_first_message_not_collected():
    client = Client(group=Group.HINATAZAKA46, access_token="t")
    pages = [{"messages": _page(201, 200, _ts)}]  # full, no continuation, but id 1 never seen

    async def fetch(session, endpoint, params=None):
        if params.get("order") == "asc":
            return {"messages": [{"id": 1, "published_at": _ts(1)}]}
        return pages.pop(0)

    client.fetch_json = AsyncMock(side_effect=fetch)
    with pytest.raises(ApiError):
        await client.get_messages(MagicMock(), 5, since_ts="2025-12-31T00:00:00Z")


@pytest.mark.asyncio
async def test_full_last_page_raises_when_probe_fails():
    client = Client(group=Group.HINATAZAKA46, access_token="t")
    pages = [{"messages": _page(1, 200, _ts)}]

    async def fetch(session, endpoint, params=None):
        if params.get("order") == "asc":
            return None
        return pages.pop(0)

    client.fetch_json = AsyncMock(side_effect=fetch)
    with pytest.raises(ApiError):
        await client.get_messages(MagicMock(), 5, since_ts="2025-12-31T00:00:00Z")
```

(Merge the imports into the file's existing import block rather than duplicating them. If `asyncio.sleep(0.5)` between pages slows the test, patch it: `monkeypatch.setattr("pysaka.client.asyncio.sleep", AsyncMock())`.)

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_client.py -v --no-cov -k full_last_page`
Expected: first test FAILS with `ApiError ... full or replayed page while the cursor ... is still unreached`; the other two PASS already.

- [ ] **Step 4: Implement**

Add to `Client` (near `get_messages`):

```python
    async def _timeline_first_id(self, session: aiohttp.ClientSession, group_id: int) -> Optional[int]:
        """Id of the room's very first message (``order=asc``, one item), or None
        if the probe failed. Used to tell the true start of a timeline apart from
        a dropped continuation on a full page."""
        data = await self.fetch_json(
            session,
            f"/groups/{group_id}/timeline",
            {"count": 1, "order": "asc", "clear_unread": "false"},
        )
        messages = (data or {}).get("messages") or []
        return messages[0].get("id") if messages else None
```

In `get_messages`, in the `if not current_continuation or repeated:` block, change

```python
                timeline_start = len(messages) < params["count"] and not replayed
```

to

```python
                timeline_start = len(messages) < params["count"] and not replayed
                if cursor_unreached and not timeline_start and not replayed and not repeated:
                    # A FULL last page with no continuation is ambiguous: dropped
                    # token, or a history that is an exact multiple of the page
                    # size. Ask the server for the room's first message; if we
                    # already hold it, nothing older exists (KNOWN_BUGS #1).
                    first_id = await self._timeline_first_id(session, group_id)
                    timeline_start = first_id is not None and first_id in all_messages
```

Update the method docstring's `Returns:` paragraph to add: "A full last page without a continuation is also accepted as the start of the timeline when the room's first message (probed with ``order=asc``) is among the collected messages."

- [ ] **Step 5: Run all tests, lint, commit**

```bash
uv run pytest -q
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src
git add src/pysaka/client.py tests/test_client.py
uv run git commit -m "fix(client): accept a full last page at the timeline start

A history that is an exact multiple of 200 no longer freezes the room:
an order=asc probe confirms the first message was already collected.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6 (only if the gate failed): record the outcome**

No code change. Note in the final report to the user: "200-multiple fix dropped from 0.4.5: `order=asc` probe did not return the room's first message (see asc_probe_report.json)". `SakaDesk/docs/KNOWN_BUGS.md` stays unchanged.

---

### Task 6: Docs and changelog

**Files:**
- Modify: `docs/API.md` (Sync Manager section after `sync_member`; Client `get_past_messages`)
- Modify: `CHANGELOG.md` (`[Unreleased]`)

- [ ] **Step 1: Update `docs/API.md`**

Replace the `sync_member` heading line and add the new entries under `## Sync Manager`:

```markdown
#### `sync_member(session, group: dict, member: dict, media_queue: list, progress_callback = None, prefetched_messages: list = None, *, on_new_messages = None) -> int`
Sync one member's messages, queue their media, and archive the member's profile images.
Returns the number of messages **processed** (re-fetched overlap included).
`on_new_messages(member_info, new_messages)` (sync or async) is called once after a successful
write when messages that were not on disk before were added. `member_info` has `group_id`,
`group_name`, `member_id`, `member_name`, `member_dir` (Path) and `phone_image`. Not called during
corrupt-file/count-mismatch recovery or by `backfill_past_messages`. Its exceptions are logged and swallowed.

#### `update_message_fields(messages_file: Path, message_id: int, fields: dict) -> bool` (async, static) / `update_message_fields_sync(...)` (blocking)
Set fields on one message under the file's lock. Use this for every local edit of `messages.json`
(e.g. favorites) so a concurrent sync cannot lose either change. Returns False if the file or id
is missing or the write failed.

#### `archive_profile_images(session, group: dict, member: dict, member_dir: Path) -> dict`
Download the current list icon (`group.thumbnail`), chat icon (`member.thumbnail`) and call-screen
image (`member.phone_image`) into `member_dir/profile_images/<kind>/<basename>` and record them in
`profile_images/index.json` (`{"version": 1, "current": {kind: url}, "images": [{"kind", "file", "url", "first_seen"}]}`).
Called automatically by `sync_member`. Returns `{"downloaded", "skipped", "failed"}`.

#### `backfill_past_messages(session, group: dict, member: dict) -> int`
Merge the room's pre-subscription messages (`Client.get_past_messages`) into the member's archive
and download their media. Runs once per member (`past_backfilled_at` in sync_state); never moves the
cursor. Returns the number of messages added.
```

and change the `get_past_messages` entry to:

```markdown
#### `get_past_messages(session, group_id: int, *, strict: bool = False) -> Optional[List[dict]]`
Messages from the ~1–1.5 days before the account's first subscription to the room (still returned
after the subscription lapses or is renewed). With `strict=True`, returns None when the request failed.
```

If Task 5 shipped, append to the `get_messages` entry: "A full last page without a continuation is accepted as the start of the timeline when the room's first message (an `order=asc` probe) was collected."

- [ ] **Step 2: Update `CHANGELOG.md` under `## [Unreleased]`**

```markdown
### Added
- `SyncManager.update_message_fields(messages_file, message_id, fields)` (and the blocking
  `update_message_fields_sync`) update one message under a per-file lock. Use them for any
  local edit of `messages.json`.
- `SyncManager.sync_member(..., on_new_messages=callback)` reports the messages that were not
  on disk before, with the member's `phone_image`, for notifications.
- `sync_member` now archives the member's list icon, chat icon and call-screen image into
  `profile_images/` with an `index.json` history (`SyncManager.archive_profile_images`).
- `SyncManager.backfill_past_messages(session, group, member)` adds the room's
  pre-subscription messages and their media once per member, without moving the cursor.
- `Client.get_past_messages(..., strict=True)` returns None on a failed request instead of `[]`.

### Fixed
- **A favorite (or any local `messages.json` edit) made while a sync was writing the same
  file could drop the newly synced messages permanently**, because the cursor had already
  advanced past them. `sync_member` and `update_message_metadata` now merge under the same
  per-file lock as `update_message_fields`.
- `SyncManager.update_sync_state` keeps keys it does not own in the member's entry instead of
  replacing the whole entry.
```

If Task 5 shipped, add under `### Fixed`:

```markdown
- `Client.get_messages` no longer raises `ApiError` forever for a room whose whole history is an
  exact multiple of 200 messages: a full last page without a continuation is accepted when an
  `order=asc` probe shows the room's first message was collected.
```

- [ ] **Step 3: Final verification and commit**

```bash
uv run pytest -q
uv run ruff check . && uv run mypy src
git add docs/API.md CHANGELOG.md
uv run git commit -m "docs: document 0.4.5 sync, archive and backfill APIs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: tests PASS; coverage shows no missed lines in changed code.

- [ ] **Step 4: Hand-off**

Do **not** release yet. Push the branch and open a PR to `dev` (PR body sections `## Why`, `## What`, `## Testing`, `## Changelog`). Release 0.4.5 via `/release pysaka 0.4.5` only after SakaDesk sub-project A has been verified against this branch.
