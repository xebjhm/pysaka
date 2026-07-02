
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
    group = {'id': 1, 'name': 'G'}
    member = {'id': 100, 'name': 'M'}
    media_queue = []

    sync_manager.client.get_messages.return_value = []

    count = await sync_manager.sync_member(session, group, member, media_queue)
    assert count == 0
    assert len(media_queue) == 0

@pytest.mark.asyncio
async def test_sync_member_flow(sync_manager):
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp', 'subscription': {'state': 'active'}}
    member = {'id': 10, 'name': 'Mem', 'portrait': 'url'}
    media_queue = []

    # Mock API response
    sync_manager.client.get_messages.return_value = [
        {'id': 101, 'type': 'text', 'text': 'Hello', 'member_id': 10, 'published_at': '2023-01-01T10:00:00Z'},
        {'id': 102, 'type': 'image', 'file': 'http://img.jpg', 'member_id': 10, 'published_at': '2023-01-01T11:00:00Z'}
    ]

    count = await sync_manager.sync_member(session, group, member, media_queue)

    assert count == 2

    # Check media queue
    assert len(media_queue) == 1
    assert media_queue[0]['url'] == 'http://img.jpg'
    assert str(media_queue[0]['path']).endswith('.jpg')

    # Check messages.json content
    # output_dir is already service-specific, so path is: messages/GID GName/MID MName
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    json_path = member_dir / "messages.json"
    assert json_path.exists()

    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)
        assert len(data['messages']) == 2
        assert data['messages'][0]['content'] == 'Hello'
        assert data['messages'][1]['type'] == 'picture'

@pytest.mark.asyncio
async def test_process_media_queue(sync_manager):
    session = AsyncMock()
    queue = [
        {'url': 'u1', 'path': Path('p1'), 'timestamp': 't1'},
        {'url': 'u2', 'path': Path('p2'), 'timestamp': 't2'}
    ]
    sync_manager.client.download_file.return_value = True

    callback = MagicMock()
    await sync_manager.process_media_queue(session, queue, concurrency=2, progress_callback=callback)

    assert sync_manager.client.download_file.call_count == 2
    assert callback.call_count == 2


@pytest.mark.asyncio
async def test_sync_member_prefetched_skips_api(sync_manager):
    """When prefetched_messages is provided, no API call is made."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem', 'portrait': 'url'}
    media_queue = []

    prefetched = [
        {'id': 201, 'type': 'text', 'text': 'Hi', 'member_id': 10, 'published_at': '2023-06-01T10:00:00Z'},
        {'id': 202, 'type': 'text', 'text': 'Bye', 'member_id': 20, 'published_at': '2023-06-01T11:00:00Z'},
    ]

    count = await sync_manager.sync_member(
        session, group, member, media_queue, prefetched_messages=prefetched
    )

    assert count == 1  # Only member_id=10
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_prefetched_respects_last_id(sync_manager):
    """Prefetched path filters by this member's own last_id."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem'}
    media_queue = []

    # Pre-existing on-disk history matching the recorded count, so the
    # integrity check does not treat this as data loss (PY-I3 recovery).
    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True, exist_ok=True)
    (member_dir / "messages.json").write_text(json.dumps({
        "messages": [
            {'id': i, 'type': 'text', 'content': f'old{i}',
             'timestamp': f'2023-01-01T0{i}:00:00Z'}
            for i in range(1, 6)
        ]
    }), encoding='utf-8')

    # Set last_ts cursor — messages with published_at < last_ts should be skipped
    sync_manager.update_sync_state(1, 10, 300, 5, last_ts='2023-01-01T03:00:00Z')

    prefetched = [
        {'id': 299, 'type': 'text', 'text': 'Old', 'member_id': 10, 'published_at': '2023-01-01T01:00:00Z'},
        {'id': 300, 'type': 'text', 'text': 'Boundary', 'member_id': 10, 'published_at': '2023-01-01T02:00:00Z'},
        {'id': 301, 'type': 'text', 'text': 'New', 'member_id': 10, 'published_at': '2023-01-01T03:00:00Z'},
        {'id': 302, 'type': 'text', 'text': 'Also new', 'member_id': 10, 'published_at': '2023-01-01T04:00:00Z'},
    ]

    count = await sync_manager.sync_member(
        session, group, member, media_queue, prefetched_messages=prefetched
    )

    assert count == 2  # Only id=301 (>= last_ts) and id=302
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_prefetched_no_last_id_takes_all(sync_manager):
    """When last_id is None (first sync), all prefetched messages for the member are used."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem'}
    media_queue = []

    prefetched = [
        {'id': 1, 'type': 'text', 'text': 'First', 'member_id': 10, 'published_at': '2023-01-01T01:00:00Z'},
        {'id': 2, 'type': 'text', 'text': 'Second', 'member_id': 10, 'published_at': '2023-01-01T02:00:00Z'},
        {'id': 3, 'type': 'text', 'text': 'Other', 'member_id': 99, 'published_at': '2023-01-01T03:00:00Z'},
    ]

    count = await sync_manager.sync_member(
        session, group, member, media_queue, prefetched_messages=prefetched
    )

    assert count == 2  # member_id=10 only, but all of them
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_prefetched_empty_returns_zero(sync_manager):
    """Prefetched with no matching messages returns 0."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem'}
    media_queue = []

    prefetched = [
        {'id': 100, 'type': 'text', 'text': 'Other member', 'member_id': 20, 'published_at': '2023-01-01T01:00:00Z'},
    ]

    count = await sync_manager.sync_member(
        session, group, member, media_queue, prefetched_messages=prefetched
    )

    assert count == 0
    sync_manager.client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_recovers_full_history_on_count_mismatch(sync_manager):
    """PY-I3: a count mismatch forces a full re-fetch (since_ts=None) so the
    complete history is recovered, not truncated to only the new messages."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem'}
    media_queue = []

    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True, exist_ok=True)
    json_path = member_dir / "messages.json"

    # On-disk file is truncated: only 1 message, but state expects 3.
    json_path.write_text(json.dumps({
        "messages": [
            {'id': 3, 'type': 'text', 'content': 'C', 'timestamp': '2023-01-03T00:00:00Z'},
        ]
    }), encoding='utf-8')
    sync_manager.update_sync_state(1, 10, 3, 3, last_ts='2023-01-03T00:00:00Z')

    full_history = [
        {'id': 1, 'type': 'text', 'text': 'A', 'member_id': 10, 'published_at': '2023-01-01T00:00:00Z'},
        {'id': 2, 'type': 'text', 'text': 'B', 'member_id': 10, 'published_at': '2023-01-02T00:00:00Z'},
        {'id': 3, 'type': 'text', 'text': 'C', 'member_id': 10, 'published_at': '2023-01-03T00:00:00Z'},
    ]

    async def fake_get_messages(sess, gid, since_ts=None, progress_callback=None):
        # Incremental fetch (bounded by cursor) returns nothing new; the full
        # re-fetch (since_ts=None) returns the entire history.
        if since_ts is None:
            return list(full_history)
        return [m for m in full_history if m['published_at'] >= since_ts]

    sync_manager.client.get_messages.side_effect = fake_get_messages

    await sync_manager.sync_member(session, group, member, media_queue)

    # File must contain the COMPLETE history, not just the truncated single msg.
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)
    assert len(data['messages']) == 3
    assert {m['id'] for m in data['messages']} == {1, 2, 3}
    # A full re-fetch (since_ts=None) must have occurred.
    assert any(
        call.kwargs.get('since_ts') is None
        for call in sync_manager.client.get_messages.call_args_list
    )
    # Recovered state reflects the full count.
    assert sync_manager.sync_state["1_10"]["total_messages"] == 3


@pytest.mark.asyncio
async def test_sync_member_recovers_on_corrupt_file(sync_manager):
    """PY-I3: a corrupt messages.json forces a full re-fetch to recover."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem'}
    media_queue = []

    member_dir = sync_manager.output_dir / "messages" / "1 Grp" / "10 Mem"
    member_dir.mkdir(parents=True, exist_ok=True)
    json_path = member_dir / "messages.json"
    json_path.write_text("{ this is not valid json", encoding='utf-8')

    # Prior cursor so the incremental fetch would otherwise return nothing.
    sync_manager.update_sync_state(1, 10, 2, 2, last_ts='2023-01-02T00:00:00Z')

    full_history = [
        {'id': 1, 'type': 'text', 'text': 'A', 'member_id': 10, 'published_at': '2023-01-01T00:00:00Z'},
        {'id': 2, 'type': 'text', 'text': 'B', 'member_id': 10, 'published_at': '2023-01-02T00:00:00Z'},
    ]

    async def fake_get_messages(sess, gid, since_ts=None, progress_callback=None):
        if since_ts is None:
            return list(full_history)
        return [m for m in full_history if m['published_at'] >= since_ts]

    sync_manager.client.get_messages.side_effect = fake_get_messages

    await sync_manager.sync_member(session, group, member, media_queue)

    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)
    assert {m['id'] for m in data['messages']} == {1, 2}


@pytest.mark.asyncio
async def test_sync_member_reraises_session_expired(sync_manager):
    """PY-I4: SessionExpiredError from get_messages must propagate, not be
    swallowed into a '0 new messages' result."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem'}

    sync_manager.client.get_messages.side_effect = SessionExpiredError("expired")

    with pytest.raises(SessionExpiredError):
        await sync_manager.sync_member(session, group, member, [])


@pytest.mark.asyncio
async def test_sync_member_reraises_refresh_failed(sync_manager):
    """PY-I4: RefreshFailedError from get_messages must propagate."""
    session = AsyncMock()
    group = {'id': 1, 'name': 'Grp'}
    member = {'id': 10, 'name': 'Mem'}

    sync_manager.client.get_messages.side_effect = RefreshFailedError("failed")

    with pytest.raises(RefreshFailedError):
        await sync_manager.sync_member(session, group, member, [])
