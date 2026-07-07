import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka.client import Client, Group
from pysaka.exceptions import RefreshFailedError, SessionExpiredError
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


class TestMergeMessages:
    """The re-fetch upsert must not drop locally-derived media metadata.

    Each sync re-fetches an overlapping window and rebuilds messages from the
    API (which never carries width/height/is_muted/media_duration). A naive
    whole-object upsert therefore erased is_muted/media_duration on every
    re-sync. _merge_messages must carry those forward from the stored record.
    """

    def test_preserves_local_derived_fields_when_api_omits_them(self):
        existing = [
            {
                "id": 1,
                "type": "video",
                "is_favorite": True,
                "width": 540,
                "height": 720,
                "is_muted": True,
                "media_duration": 2.4,
            }
        ]
        # Fresh API rebuild: server fields only, no locally-derived metadata.
        processed = [{"id": 1, "type": "video", "is_favorite": False}]

        merged = {m["id"]: m for m in SyncManager._merge_messages(existing, processed)}

        assert merged[1]["is_muted"] is True  # preserved
        assert merged[1]["media_duration"] == 2.4  # preserved
        assert merged[1]["width"] == 540 and merged[1]["height"] == 720
        assert merged[1]["is_favorite"] is False  # server field: fresh value wins

    def test_fresh_value_wins_when_present(self):
        existing = [{"id": 1, "type": "video", "is_muted": True}]
        processed = [{"id": 1, "type": "video", "is_muted": False}]  # re-derived

        merged = {m["id"]: m for m in SyncManager._merge_messages(existing, processed)}

        assert merged[1]["is_muted"] is False  # do not clobber a fresh derivation

    def test_new_message_passes_through_and_existing_untouched(self):
        existing = [{"id": 1, "type": "text", "content": "old"}]
        processed = [{"id": 2, "type": "text", "content": "new"}]

        merged = {m["id"]: m for m in SyncManager._merge_messages(existing, processed)}

        assert merged[1]["content"] == "old"
        assert merged[2]["content"] == "new"


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

    # Pre-existing on-disk history matching the recorded count, so the
    # integrity check does not treat this as data loss (PY-I3 recovery).
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True, exist_ok=True)
    (member_dir / "messages.json").write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "id": i,
                        "type": "text",
                        "content": f"old{i}",
                        "timestamp": f"2023-01-01T0{i}:00:00Z",
                    }
                    for i in range(1, 6)
                ]
            }
        ),
        encoding="utf-8",
    )

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
    # A readable manifest carries no error signal.
    assert result["error"] is None


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


def test_scan_member_media_excludes_canceled_from_unresolved(sync_manager):
    """A withdrawn post (state 'canceled') legitimately has no media, so it must NOT
    be reported as unresolved — only genuinely-missing published media should be.
    The state stays recorded on disk."""
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    (member_dir / "messages.json").write_text(
        json.dumps(
            {
                "messages": [
                    {"id": 301, "type": "video"},  # published (no state), no media_file -> unresolved
                    {"id": 302, "type": "video", "state": "canceled"},  # withdrawn -> excluded
                    {"id": 303, "type": "picture", "state": "canceled"},  # withdrawn -> excluded
                ]
            }
        ),
        encoding="utf-8",
    )

    result = sync_manager.scan_member_media(member_dir)
    assert [u["message_id"] for u in result["unresolved"]] == [301]


def test_scan_member_media_surfaces_unexpected_state_as_unresolved(sync_manager):
    """Only a genuinely-withdrawn state ('canceled') strips media legitimately. Any
    OTHER non-published state (e.g. a transient 'processing') on a media message with
    no media_file must STILL be surfaced as unresolved — otherwise an unexpected
    state would silently hide a real gap, overclaiming completeness."""
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    (member_dir / "messages.json").write_text(
        json.dumps(
            {
                "messages": [
                    {"id": 401, "type": "video", "state": "processing"},  # unexpected -> surfaced
                    {"id": 402, "type": "picture", "state": "canceled"},  # withdrawn -> excluded
                ]
            }
        ),
        encoding="utf-8",
    )

    result = sync_manager.scan_member_media(member_dir)
    assert [u["message_id"] for u in result["unresolved"]] == [401]


def test_scan_member_media_missing_file_signals_absent_manifest(sync_manager):
    # An absent messages.json must be distinguishable from a fully-synced member
    # (checked=0, missing=[]): callers get a distinct 'manifest_missing' error id
    # rather than a result that looks identical to "all present".
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    assert sync_manager.scan_member_media(member_dir) == {
        "checked": 0,
        "missing": [],
        "unresolved": [],
        "error": "manifest_missing",
    }


def test_scan_member_media_non_dict_json_signals_invalid_manifest(sync_manager):
    # A valid-but-non-dict messages.json (e.g. "[]" or "null") must degrade to an
    # empty result rather than raising AttributeError on data.get(...) — and now
    # carry a distinct 'manifest_invalid' error id so callers can tell it apart
    # from a genuinely-synced member.
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    (member_dir / "messages.json").write_text(json.dumps([]), encoding="utf-8")

    assert sync_manager.scan_member_media(member_dir) == {
        "checked": 0,
        "missing": [],
        "unresolved": [],
        "error": "manifest_invalid",
    }


def test_scan_member_media_unreadable_manifest_signals_error(sync_manager):
    # A corrupt/unreadable messages.json (invalid JSON) must NOT be reported as an
    # empty-but-clean result — that would be indistinguishable from "fully synced".
    # It carries a distinct 'manifest_unreadable' error id so callers can surface it.
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True)
    (member_dir / "messages.json").write_text("{ this is not valid json", encoding="utf-8")

    result = sync_manager.scan_member_media(member_dir)
    assert result == {
        "checked": 0,
        "missing": [],
        "unresolved": [],
        "error": "manifest_unreadable",
    }


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


@pytest.mark.asyncio
async def test_sync_member_recovers_full_history_on_count_mismatch(sync_manager):
    """PY-I3: a count mismatch forces a full re-fetch (since_ts=None) so the
    complete history is recovered, not truncated to only the new messages."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    media_queue = []

    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True, exist_ok=True)
    json_path = member_dir / "messages.json"

    # On-disk file is truncated: only 1 message, but state expects 3.
    json_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"id": 3, "type": "text", "content": "C", "timestamp": "2023-01-03T00:00:00Z"},
                ]
            }
        ),
        encoding="utf-8",
    )
    sync_manager.update_sync_state(1, 10, 3, 3, last_ts="2023-01-03T00:00:00Z")

    full_history = [
        {"id": 1, "type": "text", "text": "A", "member_id": 10, "published_at": "2023-01-01T00:00:00Z"},
        {"id": 2, "type": "text", "text": "B", "member_id": 10, "published_at": "2023-01-02T00:00:00Z"},
        {"id": 3, "type": "text", "text": "C", "member_id": 10, "published_at": "2023-01-03T00:00:00Z"},
    ]

    async def fake_get_messages(sess, gid, since_ts=None, progress_callback=None):
        # Incremental fetch (bounded by cursor) returns nothing new; the full
        # re-fetch (since_ts=None) returns the entire history.
        if since_ts is None:
            return list(full_history)
        return [m for m in full_history if m["published_at"] >= since_ts]

    sync_manager.client.get_messages.side_effect = fake_get_messages

    await sync_manager.sync_member(session, group, member, media_queue)

    # File must contain the COMPLETE history, not just the truncated single msg.
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["messages"]) == 3
    assert {m["id"] for m in data["messages"]} == {1, 2, 3}
    # A full re-fetch (since_ts=None) must have occurred.
    assert any(
        call.kwargs.get("since_ts") is None for call in sync_manager.client.get_messages.call_args_list
    )
    # Recovered state reflects the full count.
    assert sync_manager.sync_state["1_10"]["total_messages"] == 3


@pytest.mark.asyncio
async def test_sync_member_recovers_on_corrupt_file(sync_manager):
    """PY-I3: a corrupt messages.json forces a full re-fetch to recover."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}
    media_queue = []

    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True, exist_ok=True)
    json_path = member_dir / "messages.json"
    json_path.write_text("{ this is not valid json", encoding="utf-8")

    # Prior cursor so the incremental fetch would otherwise return nothing.
    sync_manager.update_sync_state(1, 10, 2, 2, last_ts="2023-01-02T00:00:00Z")

    full_history = [
        {"id": 1, "type": "text", "text": "A", "member_id": 10, "published_at": "2023-01-01T00:00:00Z"},
        {"id": 2, "type": "text", "text": "B", "member_id": 10, "published_at": "2023-01-02T00:00:00Z"},
    ]

    async def fake_get_messages(sess, gid, since_ts=None, progress_callback=None):
        if since_ts is None:
            return list(full_history)
        return [m for m in full_history if m["published_at"] >= since_ts]

    sync_manager.client.get_messages.side_effect = fake_get_messages

    await sync_manager.sync_member(session, group, member, media_queue)

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    assert {m["id"] for m in data["messages"]} == {1, 2}


@pytest.mark.asyncio
async def test_sync_member_reraises_session_expired(sync_manager):
    """PY-I4: SessionExpiredError from get_messages must propagate, not be
    swallowed into a '0 new messages' result."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}

    sync_manager.client.get_messages.side_effect = SessionExpiredError("expired")

    with pytest.raises(SessionExpiredError):
        await sync_manager.sync_member(session, group, member, [])


@pytest.mark.asyncio
async def test_sync_member_reraises_refresh_failed(sync_manager):
    """PY-I4: RefreshFailedError from get_messages must propagate."""
    session = AsyncMock()
    group = {"id": 1, "name": "Grp"}
    member = {"id": 10, "name": "Mem"}

    sync_manager.client.get_messages.side_effect = RefreshFailedError("failed")

    with pytest.raises(RefreshFailedError):
        await sync_manager.sync_member(session, group, member, [])
