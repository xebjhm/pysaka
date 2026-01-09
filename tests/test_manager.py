
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhako.client import Client, Group
from pyhako.manager import SyncManager


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
    # Updated logic: Service/messages/GID GName/MID MName
    # Group.NOGIZAKA46 display_name is "乃木坂46" (assuming config is standard)
    member_dir = sync_manager.output_dir / "乃木坂46" / "messages" / "1 Grp" / "10 Mem"
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
