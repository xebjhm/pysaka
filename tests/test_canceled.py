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


async def test_empty_text_on_cancel_keeps_captured_content(manager):
    await _sync(manager, [_raw(1)])
    stored = await _sync(manager, [_raw(1, state="canceled", text="")], ignore_cursor=True)
    assert stored[1]["content"] == "おはよう☀️"
    assert stored[1]["state"] == "canceled"


async def test_published_empty_text_message_still_skipped(manager):
    stored = await _sync(manager, [_raw(1, text=""), _raw(2)])
    assert 1 not in stored and 2 in stored


async def test_cancelled_spelling_treated_like_canceled(manager):
    await _sync(manager, [_raw(1)])
    stored = await _sync(manager, [_raw(1, state="cancelled", updated="2026-07-03T11:16:36Z")], ignore_cursor=True)
    assert stored[1]["content"] == "おはよう☀️"
    assert stored[1]["state"] == "cancelled"
    assert stored[1]["canceled_at"] == "2026-07-03T11:16:36Z"
    skeleton = await _sync(manager, [_raw(3, state="cancelled")])
    assert skeleton[3]["content"] is None and skeleton[3]["state"] == "cancelled"


def test_normalize_omits_canceled_at_without_updated_at():
    raw = _raw(1, state="canceled")
    del raw["updated_at"]
    assert "canceled_at" not in normalize_message(raw)
    assert "canceled_at" not in normalize_message(_raw(2, state="canceled", updated=None))


async def test_canceled_at_recorded_once_the_api_supplies_it(manager):
    first = _raw(1, state="canceled")
    del first["updated_at"]
    stored = await _sync(manager, [first])
    assert "canceled_at" not in stored[1]

    stored = await _sync(manager, [_raw(1, state="canceled", updated="2026-07-03T11:16:36Z")], ignore_cursor=True)
    assert stored[1]["canceled_at"] == "2026-07-03T11:16:36Z"

    stored = await _sync(manager, [first], ignore_cursor=True)  # a later row without updated_at keeps it
    assert stored[1]["canceled_at"] == "2026-07-03T11:16:36Z"
    stored = await _sync(manager, [_raw(1, state="canceled", updated="2026-09-01T00:00:00Z")], ignore_cursor=True)
    assert stored[1]["canceled_at"] == "2026-07-03T11:16:36Z"


async def test_stored_null_canceled_at_is_replaced_by_a_real_value(manager):
    d = manager._member_dir_path(GROUP, MEMBER)
    d.mkdir(parents=True)
    legacy = {"id": 1, "type": "text", "timestamp": "2026-07-03T11:15:42Z", "state": "canceled", "canceled_at": None}
    (d / "messages.json").write_text(json.dumps({"messages": [legacy]}), encoding="utf-8")
    stored = await _sync(manager, [_raw(1, state="canceled", updated="2026-07-03T11:16:36Z")], ignore_cursor=True)
    assert stored[1]["canceled_at"] == "2026-07-03T11:16:36Z"
