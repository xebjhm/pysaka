"""Deferred backfill / letter media (``media_queue`` + ``finalize_deferred_media``)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pysaka.client import Client, Group
from pysaka.manager import SyncManager

GROUP = {"id": 79, "name": "中川 智尋"}
MEMBER = {"id": 134, "name": "中川 智尋"}
NOW = "2026-11-10T05:00:00Z"


def _m(i, ts, **kw):
    return {"id": i, "type": "text", "text": f"m{i} 🎉", "member_id": 134, "published_at": ts, **kw}


def _letter(i, upd, member_id=134, url="ok"):
    return {
        "id": i,
        "group_id": 79,
        "member_id": member_id,
        "text": f"手紙 {i}",
        "file": f"https://cdn/{url}/letters/{i}.jpg" if url else None,
        "thumbnail_width": 300,
        "thumbnail_height": 480,
        "is_favorite": False,
        "created_at": upd,
        "updated_at": upd,
    }


async def _fake_download(session, url, path, timestamp=None):
    if "fail" in url:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return True


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.SAKURAZAKA46
    client.get_messages = AsyncMock(return_value=[_m(500, "2026-06-15T12:00:00Z")])
    client.get_past_messages = AsyncMock()
    client.get_letters = AsyncMock(return_value=[])
    client.download_file = AsyncMock(side_effect=_fake_download)
    return SyncManager(client, tmp_path)


async def _first_sync(manager):
    await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])
    manager.client.download_file.reset_mock()
    return manager._member_dir_path(GROUP, MEMBER)


async def _download_all(manager, queue):
    for item in queue:
        await _fake_download(None, item["url"], item["path"])


def _ids(path):
    with open(path, encoding="utf-8") as f:
        return [m["id"] for m in json.load(f)["messages"]]


# --- backfill_past_messages(media_queue=...) ---------------------------------


async def test_backfill_with_queue_downloads_nothing_and_defers_flag(manager, time_machine):
    time_machine.move_to(NOW)
    member_dir = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(400, "2026-06-14T04:57:00Z"),
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]
    queue: list = []

    added = await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)

    assert added == 2
    assert _ids(member_dir / "messages.json") == [400, 401, 500]
    manager.client.download_file.assert_not_awaited()
    assert len(queue) == 1
    item = queue[0]
    assert item["message_id"] == 401 and item["member_dir"] == member_dir
    assert item["path"] == member_dir / "picture" / "401.jpg"
    assert "past_backfilled_at" not in manager.sync_state["79_134"]

    await _download_all(manager, queue)
    report = manager.finalize_deferred_media()

    assert report == {"backfill_marked": 1, "letters_marked": 0, "still_pending": 0, "saved": True}
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW
    on_disk = json.loads((manager.state_file).read_text(encoding="utf-8"))
    assert on_disk["79_134"]["past_backfilled_at"] == NOW
    # Pending set is cleared: a second finalize does nothing.
    assert manager.finalize_deferred_media() == {
        "backfill_marked": 0,
        "letters_marked": 0,
        "still_pending": 0,
        "saved": True,
    }


async def test_backfill_deferred_missing_file_stays_unflagged(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]
    queue: list = []
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)

    report = manager.finalize_deferred_media()  # never downloaded

    assert report == {"backfill_marked": 0, "letters_marked": 0, "still_pending": 1, "saved": True}
    assert "past_backfilled_at" not in manager.sync_state["79_134"]
    # Next sync retries the backfill (flag unset).
    manager.client.get_past_messages.reset_mock()
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=[])
    manager.client.get_past_messages.assert_awaited_once()


async def test_backfill_deferred_without_media_flags_at_once(manager, time_machine):
    time_machine.move_to(NOW)
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [_m(400, "2026-06-14T04:57:00Z")]
    queue: list = []

    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)

    assert queue == []
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW
    assert manager.finalize_deferred_media()["backfill_marked"] == 0


async def test_backfill_deferred_normalize_failure_never_flags(manager, monkeypatch):
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]
    real_prepare = manager.prepare_messages

    def prepare_with_failure(messages, member_dir, queue, **kw):
        processed, _, extra = real_prepare(messages, member_dir, queue, **kw)
        return processed, "2026-06-14T13:40:00Z", extra

    monkeypatch.setattr(manager, "prepare_messages", prepare_with_failure)
    queue: list = []
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await _download_all(manager, queue)

    assert manager.finalize_deferred_media() == {
        "backfill_marked": 0,
        "letters_marked": 0,
        "still_pending": 1,
        "saved": True,
    }
    assert "past_backfilled_at" not in manager.sync_state["79_134"]


async def test_backfill_without_queue_keeps_inline_download(manager, time_machine):
    time_machine.move_to(NOW)
    member_dir = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]

    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER)

    assert (member_dir / "picture" / "401.jpg").exists()
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW
    assert manager.finalize_deferred_media()["backfill_marked"] == 0


# --- sync_letters(media_queue=...) -------------------------------------------


async def test_letters_with_queue_defer_images_and_cursor(manager):
    member_dir = manager._member_dir_path(GROUP, MEMBER)
    manager.client.get_letters.return_value = [
        _letter(1, "2026-01-01T00:01:00Z"),
        _letter(2, "2026-01-01T00:02:00Z", url=None),
        _letter(3, "2026-01-01T00:03:00Z", member_id=7),
    ]
    queue: list = []

    added = await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue)

    assert added == 2
    manager.client.download_file.assert_not_awaited()
    assert len(queue) == 1
    item = queue[0]
    assert item["path"] == member_dir / "letters" / "1.jpg"
    assert item["media_type"] == "picture" and item["kind"] == "letter"
    assert "member_dir" not in item  # dimensions must never go into messages.json
    assert "letters_updated_at" not in (manager.sync_state.get("79_134") or {})

    await _download_all(manager, queue)
    report = manager.finalize_deferred_media()

    assert report == {"backfill_marked": 0, "letters_marked": 1, "still_pending": 0, "saved": True}
    assert manager.sync_state["79_134"]["letters_updated_at"] == "2026-01-01T00:03:00Z"


async def test_letters_deferred_failed_image_holds_cursor(manager):
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z", url="fail")]
    queue: list = []
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await _download_all(manager, queue)  # the "fail" URL writes nothing

    assert manager.finalize_deferred_media() == {
        "backfill_marked": 0,
        "letters_marked": 0,
        "still_pending": 1,
        "saved": True,
    }
    assert "letters_updated_at" not in (manager.sync_state.get("79_134") or {})


async def test_letters_deferred_present_image_is_not_queued(manager):
    member_dir = manager._member_dir_path(GROUP, MEMBER)
    (member_dir / "letters").mkdir(parents=True)
    (member_dir / "letters" / "1.jpg").write_bytes(b"card")
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z")]
    queue: list = []

    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue)

    assert queue == []
    assert manager.sync_state["79_134"]["letters_updated_at"] == "2026-01-01T00:01:00Z"


async def test_letters_deferred_incomplete_fetch_records_nothing(manager, monkeypatch):
    monkeypatch.setattr(SyncManager, "_LETTER_PAGE", 1)
    monkeypatch.setattr(SyncManager, "_LETTER_MAX_PAGES", 1)
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z")]
    queue: list = []

    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await _download_all(manager, queue)

    assert len(queue) == 1  # the image is still fetched
    assert manager.finalize_deferred_media() == {
        "backfill_marked": 0,
        "letters_marked": 0,
        "still_pending": 0,
        "saved": True,
    }
    assert "letters_updated_at" not in (manager.sync_state.get("79_134") or {})


# --- process_media_queue(item_callback=...) ----------------------------------


@pytest.mark.parametrize("is_async", [True, False])
async def test_item_callback_reports_failures_progress_counts_successes(manager, tmp_path, is_async):
    queue = [
        {"url": "https://cdn/ok/1.jpg", "path": tmp_path / "a.jpg", "timestamp": None, "message_id": 1},
        {"url": "https://cdn/fail/2.jpg", "path": tmp_path / "b.jpg", "timestamp": None, "message_id": 2},
        {"url": "https://cdn/ok/3.jpg", "path": tmp_path / "c.jpg", "timestamp": None, "message_id": 3},
    ]
    outcomes: list[bool] = []
    progress: list[int] = []

    if is_async:

        async def on_item(ok):
            outcomes.append(ok)

    else:

        def on_item(ok):
            outcomes.append(ok)

    await manager.process_media_queue(
        AsyncMock(), queue, progress_callback=lambda c, t: progress.append(c), item_callback=on_item
    )

    assert sorted(outcomes) == [False, True, True]
    assert sorted(progress) == [1, 2]  # successes only, unchanged


async def test_finalize_with_nothing_pending(manager):
    assert manager.finalize_deferred_media() == {
        "backfill_marked": 0,
        "letters_marked": 0,
        "still_pending": 0,
        "saved": True,
    }


# --- review follow-ups (release v0.4.8) --------------------------------------


async def test_finalize_never_moves_the_letters_cursor_back_and_skips_the_save(manager):
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z")]
    queue: list = []
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await _download_all(manager, queue)
    manager.sync_state.setdefault("79_134", {})["letters_updated_at"] = "2026-05-01T00:00:00Z"
    manager.save_sync_state = MagicMock(return_value=True)

    report = manager.finalize_deferred_media()

    assert report == {"backfill_marked": 0, "letters_marked": 1, "still_pending": 0, "saved": True}
    assert manager.sync_state["79_134"]["letters_updated_at"] == "2026-05-01T00:00:00Z"
    manager.save_sync_state.assert_not_called()


async def test_finalize_backfill_without_state_entry_stays_pending(manager):
    from structlog.testing import capture_logs

    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]
    queue: list = []
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await _download_all(manager, queue)
    del manager.sync_state["79_134"]

    with capture_logs() as logs:
        report = manager.finalize_deferred_media()

    assert report["still_pending"] == 1 and report["backfill_marked"] == 0
    (entry,) = [e for e in logs if e["event"] == "Deferred media left pending"]
    assert entry == {
        "event": "Deferred media left pending",
        "log_level": "info",
        "state_key": "79_134",
        "kind": "backfill",
        "reason": "no_state",
        "missing": 0,
    }


async def test_partial_download_keeps_the_entry_pending_with_its_reason(manager):
    from structlog.testing import capture_logs

    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/ok/a.jpg"),
        _m(402, "2026-06-14T13:41:00Z", type="picture", file="https://cdn/fail/b.jpg"),
    ]
    queue: list = []
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    assert len(queue) == 2
    await _download_all(manager, queue)  # the "fail" one writes nothing

    with capture_logs() as logs:
        report = manager.finalize_deferred_media()

    assert report["still_pending"] == 1
    assert "past_backfilled_at" not in manager.sync_state["79_134"]
    (entry,) = [e for e in logs if e["event"] == "Deferred media left pending"]
    assert (entry["reason"], entry["missing"]) == ("media_missing", 1)


async def test_mixed_queue_through_the_real_process_media_queue(manager):
    member_dir = await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z")]
    queue: list = []
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    assert len(queue) == 2

    import pysaka.manager as mgr

    with patch.object(mgr, "get_media_dimensions", return_value=(640, 480)):
        metadata = await manager.process_media_queue(AsyncMock(), queue)

    # Only the backfill item yields metadata, keyed for update_message_metadata.
    assert metadata == {member_dir: {401: {"width": 640, "height": 480}}}
    report = manager.finalize_deferred_media()
    assert report == {"backfill_marked": 1, "letters_marked": 1, "still_pending": 0, "saved": True}
    on_disk = json.loads(manager.state_file.read_text(encoding="utf-8"))["79_134"]
    assert "past_backfilled_at" in on_disk
    assert on_disk["letters_updated_at"] == "2026-01-01T00:01:00Z"


async def test_failed_deferred_letter_is_requeued_and_completes_on_retry(manager):
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z", url="fail")]
    first: list = []
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=first)
    await _download_all(manager, first)
    assert manager.finalize_deferred_media()["still_pending"] == 1

    manager.client.get_letters.reset_mock()
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z", url="ok")]
    second: list = []
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=second)

    assert len(second) == 1  # the image is queued again with a fresh URL
    assert manager.client.get_letters.await_args.kwargs["updated_from"] is None  # cursor was held
    await _download_all(manager, second)
    assert manager.finalize_deferred_media()["letters_marked"] == 1
    assert manager.sync_state["79_134"]["letters_updated_at"] == "2026-01-01T00:01:00Z"


async def test_letters_write_failure_with_queue_leaves_nothing_pending(manager, monkeypatch):
    monkeypatch.setattr(manager, "_locked_update", lambda path, mutate: (False, 0))
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z")]
    queue: list = []

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue) == 0

    assert queue == []
    assert manager.finalize_deferred_media() == {
        "backfill_marked": 0,
        "letters_marked": 0,
        "still_pending": 0,
        "saved": True,
    }


async def test_finalize_reports_a_failed_save(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]
    queue: list = []
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await _download_all(manager, queue)
    manager.save_sync_state = MagicMock(return_value=False)

    report = manager.finalize_deferred_media()

    assert report["backfill_marked"] == 1 and report["saved"] is False


@pytest.mark.parametrize("which", ["progress", "item"])
async def test_a_raising_callback_never_stops_the_queue(manager, tmp_path, which):
    from structlog.testing import capture_logs

    queue = [
        {"url": f"https://cdn/ok/{i}.jpg", "path": tmp_path / f"{i}.jpg", "timestamp": None, "message_id": i}
        for i in range(3)
    ]

    def boom(*args):
        raise RuntimeError("callback bug")

    kwargs = {"progress_callback": boom} if which == "progress" else {"item_callback": boom}
    with capture_logs() as logs:
        await manager.process_media_queue(AsyncMock(), queue, **kwargs)

    assert all(item["path"].exists() for item in queue)
    failures = [e for e in logs if e["event"] == "Media queue callback failed"]
    assert len(failures) == 3 and failures[0]["callback"] == which
    assert failures[0]["error_type"] == "RuntimeError"


@pytest.mark.parametrize(
    "letters,url,expected",
    [
        ([("2026-01-01T00:01:00Z", "ok")], "ok", "advanced"),
        ([("2026-01-01T00:01:00Z", "fail")], None, "held"),
    ],
)
async def test_letters_log_says_what_happened_to_the_cursor(manager, letters, url, expected):
    from structlog.testing import capture_logs

    manager.client.get_letters.return_value = [_letter(i + 1, ts, url=u) for i, (ts, u) in enumerate(letters)]
    with capture_logs() as logs:
        await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    (entry,) = [e for e in logs if e["event"] == "Letters synced"]
    assert entry["cursor"] == expected
