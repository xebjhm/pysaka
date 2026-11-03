"""HTTP 429 must surface as RateLimitedError, never as "no data" (spec §5)."""

from unittest.mock import AsyncMock

import pytest

from pysaka import ApiError, RateLimitedError


async def test_fetch_json_429_raises_rate_limited(client, mock_session):
    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 429
    resp.headers = {"Retry-After": "30"}

    with pytest.raises(RateLimitedError) as exc:
        await client.fetch_json(mock_session, "/groups/1/timeline")

    assert isinstance(exc.value, ApiError)
    assert exc.value.status_code == 429
    assert exc.value.retry_after == 30.0


async def test_fetch_json_429_without_retry_after(client, mock_session):
    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 429
    resp.headers = {}

    with pytest.raises(RateLimitedError) as exc:
        await client.fetch_json(mock_session, "/groups")
    assert exc.value.retry_after is None


async def test_fetch_json_429_after_401_refresh_raises(client, mock_session):
    first = AsyncMock()
    first.status = 401
    second = AsyncMock()
    second.status = 429
    second.headers = {"Retry-After": "not-a-number"}
    mock_session.get.return_value.__aenter__.side_effect = [first, second]
    client.refresh_access_token = AsyncMock(return_value=True)

    with pytest.raises(RateLimitedError) as exc:
        await client.fetch_json(mock_session, "/groups")
    assert exc.value.retry_after is None


@pytest.mark.parametrize("method,args", [("get_groups", ()), ("get_members", (1,)), ("get_messages", (1,))])
async def test_list_calls_propagate_rate_limit(client, mock_session, method, args):
    client.fetch_json = AsyncMock(side_effect=RateLimitedError("Rate limited at /x"))
    with pytest.raises(RateLimitedError):
        await getattr(client, method)(mock_session, *args)
