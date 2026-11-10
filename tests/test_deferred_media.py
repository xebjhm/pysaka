"""Deferred backfill / letter media (``media_queue`` + ``finalize_deferred_media``)."""

import json
from unittest.mock import AsyncMock, MagicMock

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

    assert report == {"backfill_marked": 1, "letters_marked": 0, "still_pending": 0}
    assert manager.sync_state["79_134"]["past_backfilled_at"] == NOW
    on_disk = json.loads((manager.state_file).read_text(encoding="utf-8"))
    assert on_disk["79_134"]["past_backfilled_at"] == NOW
    # Pending set is cleared: a second finalize does nothing.
    assert manager.finalize_deferred_media() == {"backfill_marked": 0, "letters_marked": 0, "still_pending": 0}


async def test_backfill_deferred_missing_file_stays_unflagged(manager):
    await _first_sync(manager)
    manager.client.get_past_messages.return_value = [
        _m(401, "2026-06-14T13:40:00Z", type="picture", file="https://cdn/p.jpg"),
    ]
    queue: list = []
    await manager.backfill_past_messages(AsyncMock(), GROUP, MEMBER, media_queue=queue)

    report = manager.finalize_deferred_media()  # never downloaded

    assert report == {"backfill_marked": 0, "letters_marked": 0, "still_pending": 1}
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

    assert manager.finalize_deferred_media() == {"backfill_marked": 0, "letters_marked": 0, "still_pending": 1}
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

    assert report == {"backfill_marked": 0, "letters_marked": 1, "still_pending": 0}
    assert manager.sync_state["79_134"]["letters_updated_at"] == "2026-01-01T00:03:00Z"


async def test_letters_deferred_failed_image_holds_cursor(manager):
    manager.client.get_letters.return_value = [_letter(1, "2026-01-01T00:01:00Z", url="fail")]
    queue: list = []
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER, media_queue=queue)
    await _download_all(manager, queue)  # the "fail" URL writes nothing

    assert manager.finalize_deferred_media() == {"backfill_marked": 0, "letters_marked": 0, "still_pending": 1}
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
    assert manager.finalize_deferred_media() == {"backfill_marked": 0, "letters_marked": 0, "still_pending": 0}
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
    assert manager.finalize_deferred_media() == {"backfill_marked": 0, "letters_marked": 0, "still_pending": 0}
