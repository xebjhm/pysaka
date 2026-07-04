import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka.client import Client, Group
from pysaka.manager import SyncManager


@pytest.fixture
def mock_client():
    client = MagicMock(spec=Client)
    client.get_messages = AsyncMock()
    client.download_file = AsyncMock()
    # Set a valid group for GROUP_CONFIG lookup
    client.group = Group.NOGIZAKA46
    return client


@pytest.fixture
def sync_manager(mock_client, tmp_path):
    return SyncManager(mock_client, tmp_path)


@pytest.mark.asyncio
async def test_load_save_sync_state(sync_manager):
    sync_manager.sync_state = {"test_key": {"data": 123}}
    sync_manager.save_sync_state()

    # Verify file written
    assert sync_manager.state_file.exists()

    # Create new instance to test load
    new_manager = SyncManager(sync_manager.client, sync_manager.output_dir)
    assert new_manager.sync_state["test_key"]["data"] == 123


@pytest.mark.asyncio
async def test_update_sync_state(sync_manager):
    sync_manager.update_sync_state(1, 100, 500, 10)

    key = "1_100"
    assert key in sync_manager.sync_state
    assert sync_manager.sync_state[key]["last_message_id"] == 500
    assert sync_manager.sync_state[key]["total_messages"] == 10
    assert sync_manager.get_last_id(1, 100) == 500


@pytest.mark.asyncio
async def test_sync_member_no_messages(sync_manager):
    session = AsyncMock()
    group = {"id": 1, "name": "G"}
    member = {"id": 100, "name": "M"}
    media_queue = []

    sync_manager.client.get_messages.return_value = []

    count = await sync_manager.sync_member(session, group, member, media_queue)
    assert count == 0
    assert len(media_queue) == 0


@pytest.mark.asyncio
async def test_sync_member_flow(sync_manager):
    session = AsyncMock()
    group = {"id": 1, "name": "Grp", "subscription": {"state": "active"}}
    member = {"id": 10, "name": "Mem", "portrait": "url"}
    media_queue = []

    # Mock API response
    sync_manager.client.get_messages.return_value = [
        {"id": 101, "type": "text", "text": "Hello", "member_id": 10, "published_at": "2023-01-01T10:00:00Z"},
        {"id": 102, "type": "image", "file": "http://img.jpg", "member_id": 10, "published_at": "2023-01-01T11:00:00Z"},
    ]

    count = await sync_manager.sync_member(session, group, member, media_queue)

    assert count == 2

    # Check media queue
    assert len(media_queue) == 1
    assert media_queue[0]["url"] == "http://img.jpg"
    assert str(media_queue[0]["path"]).endswith(".jpg")

    # Check messages.json content
    # output_dir is already service-specific, so path is: messages/GID GName/MID MName
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    json_path = member_dir / "messages.json"
    assert json_path.exists()

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
        assert len(data["messages"]) == 2
        assert data["messages"][0]["content"] == "Hello"
        assert data["messages"][1]["type"] == "picture"


@pytest.mark.asyncio
async def test_sync_member_write_failure_does_not_advance_cursor(sync_manager, monkeypatch):
    """If the messages.json write fails, the cursor must NOT advance — otherwise the
    next sync would skip the unwritten messages permanently."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp", "subscription": {"state": "active"}}
    member = {"id": 10, "name": "Mem", "portrait": "url"}

    sync_manager.client.get_messages.return_value = [
        {"id": 101, "type": "text", "text": "Hi", "member_id": 10, "published_at": "2023-01-01T10:00:00Z"},
    ]
    # Simulate a persistent write failure (e.g. Windows lock exhausting retries).
    write_mock = MagicMock(return_value=False)
    monkeypatch.setattr(SyncManager, "_atomic_write_json", staticmethod(write_mock))

    count = await sync_manager.sync_member(session, group, member, [])

    write_mock.assert_called_once()  # the write was actually attempted (not an early exit)
    assert count == 0  # member sync aborted, not reported as success
    assert "1_10" not in sync_manager.sync_state  # cursor was NOT advanced
    assert sync_manager.get_last_ts(1, 10) is None


@pytest.mark.asyncio
async def test_sync_member_get_messages_error_does_not_advance_cursor(sync_manager):
    """If get_messages raises (e.g. it fails closed on an incomplete pagination),
    the member sync must abort without advancing the cursor, so the next run
    re-fetches from the last good point instead of skipping the un-fetched gap."""
    from pysaka.exceptions import ApiError

    session = AsyncMock()
    group = {"id": 1, "name": "Grp", "subscription": {"state": "active"}}
    member = {"id": 10, "name": "Mem", "portrait": "url"}

    sync_manager.client.get_messages.side_effect = ApiError("pagination aborted before cursor")

    count = await sync_manager.sync_member(session, group, member, [])

    assert count == 0  # aborted, not reported as success
    assert "1_10" not in sync_manager.sync_state  # cursor was NOT advanced
    assert sync_manager.get_last_ts(1, 10) is None


@pytest.mark.asyncio
async def test_sync_member_prepare_failure_holds_cursor(sync_manager):
    """A message that fails to normalize must NOT let the cursor advance past it.
    The next sync fetches published_at >= cursor, so an un-clamped cursor would
    filter the failed message out permanently (silent data loss)."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem", "portrait": "url"}
    media_queue: list = []

    prefetched = [
        # Valid, newer message — normalizes fine.
        {"id": 101, "type": "text", "text": "Hi", "member_id": 10, "published_at": "2023-01-01T10:00:00Z"},
        # Malformed: no "id" -> normalize_message raises KeyError. Older timestamp.
        {"type": "text", "text": "Broken", "member_id": 10, "published_at": "2023-01-01T09:00:00Z"},
    ]

    count = await sync_manager.sync_member(session, group, member, media_queue, prefetched_messages=prefetched)

    # Only the valid message is persisted...
    assert count == 1
    # ...but the cursor is held at the FAILED message's timestamp, not the newer
    # valid one, so the failed message is re-fetched (>= cursor) on the next sync
    # instead of being skipped forever.
    assert sync_manager.get_last_ts(1, 10) == "2023-01-01T09:00:00Z"


@pytest.mark.asyncio
async def test_process_media_queue(sync_manager):
    session = AsyncMock()
    queue = [{"url": "u1", "path": Path("p1"), "timestamp": "t1"}, {"url": "u2", "path": Path("p2"), "timestamp": "t2"}]
    sync_manager.client.download_file.return_value = True

    callback = MagicMock()
    await sync_manager.process_media_queue(session, queue, concurrency=2, progress_callback=callback)

    assert sync_manager.client.download_file.call_count == 2
    assert callback.call_count == 2


@pytest.mark.asyncio
async def test_sync_member_prefetched_skips_api(sync_manager):
    """When prefetched_messages is provided, no API call is made."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem", "portrait": "url"}
    media_queue = []

    prefetched = [
        {"id": 201, "type": "text", "text": "Hi", "member_id": 10, "published_at": "2023-06-01T10:00:00Z"},
        {"id": 202, "type": "text", "text": "Bye", "member_id": 20, "published_at": "2023-06-01T11:00:00Z"},
    ]

    count = await sync_manager.sync_member(session, group, member, media_queue, prefetched_messages=prefetched)

    assert count == 1  # Only member_id=10
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_prefetched_respects_last_id(sync_manager):
    """Prefetched path filters by this member's own last_id."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    media_queue = []

    # Set last_ts cursor — messages with published_at < last_ts should be skipped
    sync_manager.update_sync_state(1, 10, 300, 5, last_ts="2023-01-01T03:00:00Z")

    prefetched = [
        {"id": 299, "type": "text", "text": "Old", "member_id": 10, "published_at": "2023-01-01T01:00:00Z"},
        {"id": 300, "type": "text", "text": "Boundary", "member_id": 10, "published_at": "2023-01-01T02:00:00Z"},
        {"id": 301, "type": "text", "text": "New", "member_id": 10, "published_at": "2023-01-01T03:00:00Z"},
        {"id": 302, "type": "text", "text": "Also new", "member_id": 10, "published_at": "2023-01-01T04:00:00Z"},
    ]

    count = await sync_manager.sync_member(session, group, member, media_queue, prefetched_messages=prefetched)

    assert count == 2  # Only id=301 (>= last_ts) and id=302
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_prefetched_no_last_id_takes_all(sync_manager):
    """When last_id is None (first sync), all prefetched messages for the member are used."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    media_queue = []

    prefetched = [
        {"id": 1, "type": "text", "text": "First", "member_id": 10, "published_at": "2023-01-01T01:00:00Z"},
        {"id": 2, "type": "text", "text": "Second", "member_id": 10, "published_at": "2023-01-01T02:00:00Z"},
        {"id": 3, "type": "text", "text": "Other", "member_id": 99, "published_at": "2023-01-01T03:00:00Z"},
    ]

    count = await sync_manager.sync_member(session, group, member, media_queue, prefetched_messages=prefetched)

    assert count == 2  # member_id=10 only, but all of them
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_prefetched_empty_returns_zero(sync_manager):
    """Prefetched with no matching messages returns 0."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    media_queue = []

    prefetched = [
        {"id": 100, "type": "text", "text": "Other member", "member_id": 20, "published_at": "2023-01-01T01:00:00Z"},
    ]

    count = await sync_manager.sync_member(session, group, member, media_queue, prefetched_messages=prefetched)

    assert count == 0
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_cursor_held_behind_message_with_undownloaded_media(sync_manager):
    """A queued-but-not-downloaded image must hold the cursor behind it, even
    when a newer text message exists — so an interrupted media phase self-heals."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    queue: list = []
    prefetched = [
        {"id": 100, "type": "text", "text": "hi", "member_id": 10, "published_at": "2026-01-01T00:00:00Z"},
        {"id": 101, "type": "image", "file": "http://img.jpg", "member_id": 10, "published_at": "2026-01-02T00:00:00Z"},
        {"id": 102, "type": "text", "text": "newest", "member_id": 10, "published_at": "2026-01-03T00:00:00Z"},
    ]
    await sync_manager.sync_member(session, group, member, queue, prefetched_messages=prefetched)
    # Image 101 was queued (file absent) -> cursor clamped to its ts, NOT 102's.
    assert any(item["message_id"] == 101 for item in queue)
    assert sync_manager.get_last_ts(1, 10) == "2026-01-02T00:00:00Z"


@pytest.mark.asyncio
async def test_cursor_held_behind_zero_byte_media_stub(sync_manager):
    """A zero-byte stub for an image must be treated as missing and re-queued,
    holding the cursor behind it — consistent with scan_member_media and download_file."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    (member_dir / "picture").mkdir(parents=True)
    (member_dir / "picture" / "101.jpg").write_bytes(b"")  # Zero-byte stub (incomplete download)
    queue: list = []
    prefetched = [
        {"id": 101, "type": "image", "file": "http://img.jpg", "member_id": 10, "published_at": "2026-01-02T00:00:00Z"},
        {"id": 102, "type": "text", "text": "newer", "member_id": 10, "published_at": "2026-01-03T00:00:00Z"},
    ]
    await sync_manager.sync_member(session, group, member, queue, prefetched_messages=prefetched)
    # Image 101 zero-byte stub is re-queued despite the file existing.
    assert any(item["message_id"] == 101 for item in queue)
    # Cursor held behind the 0-byte stub, NOT at 102's timestamp.
    assert sync_manager.get_last_ts(1, 10) == "2026-01-02T00:00:00Z"


@pytest.mark.asyncio
async def test_cursor_advances_fully_when_all_media_present(sync_manager):
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    (member_dir / "picture").mkdir(parents=True)
    (member_dir / "picture" / "101.jpg").write_bytes(b"IMG")  # already on disk -> not queued
    queue: list = []
    prefetched = [
        {"id": 101, "type": "image", "file": "http://img.jpg", "member_id": 10, "published_at": "2026-01-02T00:00:00Z"},
        {"id": 102, "type": "text", "text": "later", "member_id": 10, "published_at": "2026-01-03T00:00:00Z"},
    ]
    await sync_manager.sync_member(session, group, member, queue, prefetched_messages=prefetched)
    assert queue == []
    assert sync_manager.get_last_ts(1, 10) == "2026-01-03T00:00:00Z"  # full advance


def test_scan_member_media_finds_absent_and_zero_byte(sync_manager):
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    (member_dir / "picture").mkdir(parents=True)
    # 101 present & non-empty; 102 zero-byte; 103 absent; 104 text (ignored)
    (member_dir / "picture" / "101.jpg").write_bytes(b"IMG")
    (member_dir / "picture" / "102.jpg").write_bytes(b"")
    (member_dir / "messages.json").write_text(
        json.dumps(
            {
                "messages": [
                    {"id": 101, "type": "picture", "media_file": "messages/1 Grp/10 Mem/picture/101.jpg"},
                    {"id": 102, "type": "picture", "media_file": "messages/1 Grp/10 Mem/picture/102.jpg"},
                    {"id": 103, "type": "picture", "media_file": "messages/1 Grp/10 Mem/picture/103.jpg"},
                    {"id": 104, "type": "text", "content": "hi"},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = sync_manager.scan_member_media(member_dir)
    assert result["checked"] == 3
    assert sorted(d["message_id"] for d in result["missing"]) == [102, 103]
    assert all(isinstance(d["path"], Path) for d in result["missing"])
    assert result["unresolved"] == []


def test_scan_member_media_counts_media_without_media_file_as_unresolved(sync_manager):
    """A media-type message with no recorded media_file (the media URL was absent
    at sync time — e.g. an expired-media stub) must be surfaced as `unresolved`,
    not silently ignored. Otherwise the check reports 'all media present' while
    such media is genuinely absent and unrecorded — a false completeness claim."""
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    (member_dir / "picture").mkdir(parents=True)
    (member_dir / "picture" / "201.jpg").write_bytes(b"IMG")
    (member_dir / "messages.json").write_text(
        json.dumps(
            {
                "messages": [
                    {"id": 201, "type": "picture", "media_file": "messages/1 Grp/10 Mem/picture/201.jpg"},
                    {"id": 202, "type": "video"},  # media type, no media_file (no url at sync)
                    {"id": 203, "type": "picture", "media_file": ""},  # empty media_file
                    {"id": 204, "type": "text", "content": "hi"},  # non-media, ignored
                ]
            }
        ),
        encoding="utf-8",
    )

    result = sync_manager.scan_member_media(member_dir)
    assert result["checked"] == 1  # only 201 has a checkable media_file
    assert result["missing"] == []  # 201 is present on disk
    # 202 and 203: media type but no usable media_file, reported with details
    assert sorted(u["message_id"] for u in result["unresolved"]) == [202, 203]
    assert {u["media_type"] for u in result["unresolved"]} == {"video", "picture"}


def test_scan_member_media_missing_file_returns_empty(sync_manager):
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    assert sync_manager.scan_member_media(member_dir) == {"checked": 0, "missing": [], "unresolved": []}


def test_scan_member_media_non_dict_json_returns_empty(sync_manager):
    # A valid-but-non-dict messages.json (e.g. "[]" or "null") must degrade to
    # the empty result rather than raising AttributeError on data.get(...).
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    (member_dir / "messages.json").write_text(json.dumps([]), encoding="utf-8")

    assert sync_manager.scan_member_media(member_dir) == {"checked": 0, "missing": [], "unresolved": []}


@pytest.mark.asyncio
async def test_reconcile_downloads_missing_from_timeline(sync_manager):
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    (member_dir / "picture").mkdir(parents=True)
    dest = member_dir / "picture" / "103.jpg"
    missing = [{"message_id": 103, "media_type": "picture", "path": dest, "timestamp": "2026-01-03T00:00:00Z"}]
    timeline = [
        {"id": 103, "file": "https://cdn/fresh-103.jpg", "type": "picture"},
        {"id": 999, "file": "https://cdn/other.jpg"},
    ]
    (member_dir / "messages.json").write_text(
        json.dumps(
            {"messages": [{"id": 103, "type": "picture", "media_file": "messages/1 Grp/10 Mem/picture/103.jpg"}]}
        ),
        encoding="utf-8",
    )

    async def fake_dl(session, url, path, timestamp=None, **kw):
        Path(path).write_bytes(b"FRESHIMG")
        return True

    sync_manager.client.download_file = AsyncMock(side_effect=fake_dl)

    report = await sync_manager.reconcile_member_media(AsyncMock(), member_dir, missing, timeline)
    assert report == {"repaired": 1, "failed": 0, "still_missing": 0}
    assert dest.read_bytes() == b"FRESHIMG"


@pytest.mark.asyncio
async def test_reconcile_no_timeline_match_is_still_missing(sync_manager):
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    missing = [
        {"message_id": 103, "media_type": "picture", "path": member_dir / "picture" / "103.jpg", "timestamp": None}
    ]
    report = await sync_manager.reconcile_member_media(AsyncMock(), member_dir, missing, timeline_messages=[])
    assert report == {"repaired": 0, "failed": 0, "still_missing": 1}
