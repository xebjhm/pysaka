"""Canceled (withdrawn) messages are archived, keeping captured content (spec §6)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka.client import Client, Group
from pysaka.manager import SyncManager
from pysaka.utils import CANCELED_STATES, normalize_message

GROUP = {"id": 86, "name": "蔵盛 妃那乃"}
MEMBER = {"id": 138, "name": "蔵盛 妃那乃"}


def _raw(i, state="published", updated="2026-07-03T11:15:42Z", **kw):
    msg = {
        "id": i,
        "member_id": 138,
        "type": "text",
        "published_at": "2026-07-03T11:15:42Z",
        "updated_at": updated,
        "state": state,
        **kw,
    }
    if state == "published":
        msg.setdefault("text", "おはよう☀️")
    return msg


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.HINATAZAKA46
    client.download_file = AsyncMock(return_value=True)
    return SyncManager(client, tmp_path)


async def _sync(manager, rows, **kw):
    await manager.sync_member(AsyncMock(), GROUP, MEMBER, [], prefetched_messages=rows, **kw)
    with open(manager._member_dir_path(GROUP, MEMBER) / "messages.json", encoding="utf-8") as f:
        return {m["id"]: m for m in json.load(f)["messages"]}


def test_canceled_states_constant():
    assert CANCELED_STATES == frozenset({"canceled", "cancelled"})


def test_normalize_records_canceled_at():
    n = normalize_message(_raw(1, state="canceled", updated="2026-07-03T11:16:36Z"))
    assert n["state"] == "canceled" and n["canceled_at"] == "2026-07-03T11:16:36Z"
    assert "canceled_at" not in normalize_message(_raw(2))


async def test_captured_content_survives_cancel(manager):
    await _sync(manager, [_raw(1)])
    stored = await _sync(manager, [_raw(1, state="canceled", updated="2026-07-03T11:16:36Z")], ignore_cursor=True)
    assert stored[1]["content"] == "おはよう☀️"
    assert stored[1]["state"] == "canceled"
    assert stored[1]["canceled_at"] == "2026-07-03T11:16:36Z"


async def test_canceled_at_kept_from_first_sight(manager):
    await _sync(manager, [_raw(1, state="canceled", updated="2026-07-03T11:16:36Z")])
    stored = await _sync(manager, [_raw(1, state="canceled", updated="2026-09-01T00:00:00Z")], ignore_cursor=True)
    assert stored[1]["canceled_at"] == "2026-07-03T11:16:36Z"


async def test_uncaptured_cancel_stored_as_skeleton(manager):
    stored = await _sync(manager, [_raw(1, state="canceled")])
    assert stored[1]["content"] is None and stored[1]["state"] == "canceled"


async def test_published_update_still_wins(manager):
    await _sync(manager, [_raw(1)])
    stored = await _sync(manager, [_raw(1, text="edited")], ignore_cursor=True)
    assert stored[1]["content"] == "edited" and "state" not in stored[1]


async def test_media_file_still_inherited_on_cancel(manager):
    await _sync(manager, [_raw(1, type="picture", file="https://cdn/p.jpg")])
    stored = await _sync(manager, [_raw(1, state="canceled", type="picture")], ignore_cursor=True)
    assert stored[1].get("media_file")
