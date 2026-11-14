"""Server ids and media URLs never lead outside the archive (security audit 2026-11-13)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from pysaka.client import Client, Group


def _client() -> Client:
    return Client(group=Group.HINATAZAKA46, access_token="test_token")


@pytest.mark.asyncio
async def test_get_messages_drops_rows_whose_id_is_not_an_integer():
    client = _client()
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 12},
                    {"id": "11"},
                    {"id": "../x"},
                    {"id": True},
                    {"id": None},
                    {"id": 10},
                ],
                "continuation": None,
            }
        ]
    )
    with capture_logs() as logs:
        msgs = await client.get_messages(MagicMock(), group_id=1)
    assert [m["id"] for m in msgs] == [10, 11, 12]
    assert all(type(m["id"]) is int for m in msgs)
    (refused,) = [e for e in logs if e["event"] == "Timeline ids refused"]
    assert (refused["group_id"], refused["count"]) == (1, 3)
    assert "../x" not in repr(logs)
