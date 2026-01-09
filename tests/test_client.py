from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhako import ApiError, Client, Group


@pytest.mark.asyncio
async def test_client_init():
    c = Client(group=Group.HINATAZAKA46)
    assert c.api_base == "https://api.message.hinatazaka46.com/v2"

    c2 = Client(group="nogizaka46")
    assert c2.group == Group.NOGIZAKA46
    assert "nogizaka" in c2.api_base

    with pytest.raises(ValueError):
        Client(group="invalid_group")

@pytest.mark.asyncio
async def test_fetch_json_success(client, mock_session):
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = {"foo": "bar"}

    data = await client.fetch_json(mock_session, "/test")
    assert data == {"foo": "bar"}
    mock_session.get.assert_called_with(
        "https://api.message.hinatazaka46.com/v2/test",
        headers=client.headers,
        params=None,
        ssl=False
    )

@pytest.mark.asyncio
async def test_fetch_json_server_error(client, mock_session):
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 500

    with pytest.raises(ApiError):
        await client.fetch_json(mock_session, "/error")

@pytest.mark.asyncio
async def test_refresh_token(client, mock_session):
    client.refresh_token = "valid_rt"

    mock_resp = mock_session.post.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = {"access_token": "new_token"}

    success = await client.refresh_access_token(mock_session)
    assert success is True
    assert client.access_token == "new_token"
    assert client.headers["Authorization"] == "Bearer new_token"

@pytest.mark.asyncio
async def test_refresh_missing_token(client, mock_session):
    client.refresh_token = None
    success = await client.refresh_access_token(mock_session)
    assert success is False
    mock_session.post.assert_not_called()

@pytest.mark.asyncio
async def test_refresh_cookie_success(client, mock_session):
    client.refresh_token = None
    client.cookies = {"session": "valid_cookie"}

    mock_resp = mock_session.post.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = {"access_token": "cookie_token"}
    # Mock cookies mapping for iteration
    mock_resp.cookies = MagicMock()
    mock_resp.cookies.items.return_value = []

    success = await client.refresh_access_token(mock_session)
    assert success is True
    assert client.access_token == "cookie_token"
    # Verify post called with cookies
    call_kwargs = mock_session.post.call_args[1]
    assert call_kwargs['cookies'] == {"session": "valid_cookie"}
    assert call_kwargs['json'] == {"refresh_token": None}

@pytest.mark.asyncio
async def test_get_profile(client, mock_session):
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = {"nickname": "TestUser"}

    profile = await client.get_profile(mock_session)
    assert profile["nickname"] == "TestUser"

@pytest.mark.asyncio
async def test_get_groups(client, mock_session):
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = [
        {"id": 1, "name": "Group1", "subscription": {"state": "active"}},
        {"id": 2, "name": "Group2", "subscription": {"state": "expired"}}
    ]

    # Active only
    groups = await client.get_groups(mock_session, include_inactive=False)
    assert len(groups) == 1
    assert groups[0]["id"] == 1

    # All
    groups = await client.get_groups(mock_session, include_inactive=True)
    assert len(groups) == 2

@pytest.mark.asyncio
async def test_get_messages_pagination(client, mock_session):
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200

    # Page 1
    mock_resp.json.side_effect = [
        {
            "messages": [{"id": 10}, {"id": 9}],
            "continuation": "next_cursor"
        },
        {
            "messages": [{"id": 8}],
            "continuation": None
        }
    ]

    msgs = await client.get_messages(mock_session, group_id=1)
    assert len(msgs) == 3
    assert msgs[0]["id"] == 8
    assert msgs[2]["id"] == 10  # Sorted ascending

@pytest.mark.asyncio
async def test_get_additional_endpoints(client, mock_session):
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200

    # Tags
    mock_resp.json.return_value = {"tags": [{"id": 1}]}
    assert len(await client.get_tags(mock_session)) == 1

    # FC
    mock_resp.json.return_value = {"contents": [{"id": 1}]}
    assert len(await client.get_fc_contents(mock_session)) == 1

    # Orgs
    mock_resp.json.return_value = {"organizations": [{"id": 1}]}
    assert len(await client.get_organizations(mock_session)) == 1

    # Products
    mock_resp.json.return_value = {"products": [{"id": 1}]}
    assert len(await client.get_products(mock_session)) == 1

@pytest.mark.asyncio
async def test_fetch_json_auto_refresh_success(client, mock_session):
    resp_401 = AsyncMock()
    resp_401.status = 401

    resp_200 = AsyncMock()
    resp_200.status = 200
    resp_200.json.return_value = {"success": True}

    resp_refresh = AsyncMock()
    resp_refresh.status = 200
    resp_refresh.json.return_value = {"access_token": "new_refreshed_token"}

    # Configure side effects
    mock_session.get.return_value.__aenter__.side_effect = [resp_401, resp_200]
    mock_session.post.return_value.__aenter__.return_value = resp_refresh

    # Needs refresh_token for refresh to work
    client.refresh_token = "valid_rt"

    result = await client.fetch_json(mock_session, "/test")

    assert result == {"success": True}
    assert client.access_token == "new_refreshed_token"
    mock_session.post.assert_called_once()
    assert mock_session.get.call_count == 2

@pytest.mark.asyncio
async def test_fetch_json_auto_refresh_fail(client, mock_session):
    resp_401 = AsyncMock()
    resp_401.status = 401

    resp_refresh_fail = AsyncMock()
    resp_refresh_fail.status = 401

    mock_session.get.return_value.__aenter__.return_value = resp_401
    mock_session.post.return_value.__aenter__.return_value = resp_refresh_fail

    client.refresh_token = "valid_rt"

    result = await client.fetch_json(mock_session, "/test")

    assert result is None
    mock_session.get.assert_called_once()
    mock_session.post.assert_called_once()
