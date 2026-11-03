from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka import ApiError, Client, Group, RefreshFailedError


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
    # PY-CORE-06: no refresh_token/cookies/auth_dir → raise an auth error rather
    # than silently returning False (which would surface as empty data upstream).
    client.refresh_token = None
    with pytest.raises(RefreshFailedError):
        await client.refresh_access_token(mock_session)
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
    assert call_kwargs["cookies"] == {"session": "valid_cookie"}
    assert call_kwargs["json"] == {"refresh_token": None}


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
        {"id": 2, "name": "Group2", "subscription": {"state": "expired"}},
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
        {"messages": [{"id": 10}, {"id": 9}], "continuation": "next_cursor"},
        {"messages": [{"id": 8}], "continuation": None},
    ]

    msgs = await client.get_messages(mock_session, group_id=1)
    assert len(msgs) == 3
    assert msgs[0]["id"] == 8
    assert msgs[2]["id"] == 10  # Sorted ascending


@pytest.mark.asyncio
async def test_get_messages_raises_on_incomplete_pagination(client, mock_session):
    """A continuation-page fetch that fails mid-pagination (fetch_json returns None
    before the cursor/end is reached) must RAISE, not return the partial newest-only
    set. Returning the partial set would let the caller advance its timestamp cursor
    past the un-fetched older-but-still-new messages, losing them silently."""
    from pysaka.exceptions import ApiError

    # Page 0: newest messages + a continuation (cursor not yet reached).
    # Page 1: the continuation fetch fails (fetch_json returns None on a non-network
    # error path — e.g. an unexpected status or a still-401 after refresh).
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 100, "published_at": "2026-01-10T00:00:00Z"},
                    {"id": 99, "published_at": "2026-01-09T00:00:00Z"},
                ],
                "continuation": "next",
            },
            None,
        ]
    )

    with pytest.raises(ApiError):
        await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")


@pytest.mark.asyncio
async def test_get_messages_raises_on_empty_page_with_continuation(client, mock_session):
    """An EMPTY page mid-pagination that STILL carries a continuation (the server
    claims more data but returned nothing) must RAISE, not break and return the
    partial newest set — same silent-gap risk as a failed continuation fetch."""
    from pysaka.exceptions import ApiError

    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 100, "published_at": "2026-01-10T00:00:00Z"},
                    {"id": 99, "published_at": "2026-01-09T00:00:00Z"},
                ],
                "continuation": "next",
            },
            {"messages": [], "continuation": "still_more"},
        ]
    )

    with pytest.raises(ApiError):
        await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")


@pytest.mark.asyncio
async def test_get_messages_empty_page_without_continuation_is_clean_end(client, mock_session):
    """An empty page with NO continuation is a legitimate end-of-timeline: return
    what was collected so far, do not raise."""
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 100, "published_at": "2026-01-10T00:00:00Z"},
                    {"id": 99, "published_at": "2026-01-09T00:00:00Z"},
                ],
                "continuation": "next",
            },
            {"messages": [], "continuation": None},
        ]
    )

    msgs = await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")
    assert [m["id"] for m in msgs] == [99, 100]


@pytest.mark.asyncio
async def test_get_messages_missing_continuation_before_cursor_is_timeline_start(client, mock_session):
    """A NON-empty page with NO continuation whose oldest message is still newer than
    the cursor is the START of the timeline, not a gap: the server says nothing older
    exists. This is routine — a caller that subtracts an overlap window from a cursor
    pinned at the room's oldest message lands just before it. Raising here deadlocked
    such rooms permanently (every sync failed, so the cursor never moved). Return the
    messages, mirroring the empty-page-without-continuation clean end."""
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 100, "published_at": "2026-01-10T00:00:00Z"},
                    {"id": 99, "published_at": "2026-01-09T00:00:00Z"},
                ],
                # No continuation, oldest (01-09) newer than the cursor (01-01):
                # the timeline simply begins at 01-09.
                "continuation": None,
            },
        ]
    )

    msgs = await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")
    assert [m["id"] for m in msgs] == [99, 100]


@pytest.mark.asyncio
async def test_get_messages_cursor_overlap_before_oldest_message_does_not_deadlock(client, mock_session):
    """Regression (field report, pysaka 0.4.3): a member's cursor was pinned at the
    oldest message in the room (2026-04-11T10:30:14Z); the caller subtracted a 5-minute
    overlap, so the effective cursor (10:25:14Z) predated every message. Pagination
    reached the true end of the timeline and get_messages raised on EVERY sync, so the
    room never received new messages again. It must return the full set instead, and
    leave a warning breadcrumb."""
    from structlog.testing import capture_logs

    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 503, "published_at": "2026-07-20T09:00:00Z"},
                    {"id": 502, "published_at": "2026-07-08T09:00:00Z"},
                ],
                "continuation": "page2",
            },
            {
                "messages": [
                    {"id": 501, "published_at": "2026-05-01T09:00:00Z"},
                    {"id": 500, "published_at": "2026-04-11T10:30:14Z"},
                ],
                "continuation": None,
            },
        ]
    )

    with capture_logs() as logs:
        msgs = await client.get_messages(mock_session, group_id=72, since_ts="2026-04-11T10:25:14Z")
    assert [m["id"] for m in msgs] == [500, 501, 502, 503]
    assert any(
        e["event"] == "Timeline starts after cursor" and e["log_level"] == "warning" and e["group_id"] == 72
        for e in logs
    )


def _page(start_id: int, n: int, day: int) -> list[dict]:
    """``n`` messages, newest first: ids descend from ``start_id`` and timestamps
    descend one minute apart from 2026-01-``day``T23:59 (``n`` <= 1440)."""
    return [
        {"id": start_id - i, "published_at": f"2026-01-{day:02d}T{(1439 - i) // 60:02d}:{(1439 - i) % 60:02d}:00Z"}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_get_messages_raises_on_duplicate_continuation_before_cursor(client, mock_session):
    """A FULL page that hands back the SAME continuation it was given means
    pagination is stuck mid-timeline: more pages should exist but the server will not
    advance. Breaking while the oldest fetched message is still newer than the cursor
    skips a gap — fail closed."""
    from pysaka.exceptions import ApiError

    client.fetch_json = AsyncMock(
        side_effect=[
            {"messages": _page(1000, 200, 10), "continuation": "tok1"},
            # Full page of new ids, same token we just sent, oldest (01-09) still
            # newer than the cursor (01-01).
            {"messages": _page(800, 200, 9), "continuation": "tok1"},
        ]
    )

    with pytest.raises(ApiError):
        await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")


@pytest.mark.asyncio
async def test_get_messages_raises_on_full_page_without_continuation_before_cursor(client, mock_session):
    """A FULL page (``count`` messages) with NO continuation while the cursor is
    unreached is ambiguous: more data should follow a full page, so the token may
    have been dropped. Treating it as the timeline start would let the caller advance
    its cursor past everything older — fail closed. The ``order=asc`` probe returns
    an uncollected (older) first message, so the page is not confirmed as the
    timeline start. (A genuine exact-``count`` last page is accepted immediately via
    the probe; see ``test_full_last_page_at_timeline_start_is_not_a_gap``.)"""
    from pysaka.exceptions import ApiError

    async def fetch(session, endpoint, params=None):
        if params.get("order") == "asc":
            return {"messages": [{"id": 1, "published_at": "2025-12-01T00:00:00Z"}]}
        return {"messages": _page(1000, 200, 10), "continuation": None}

    client.fetch_json = AsyncMock(side_effect=fetch)

    with pytest.raises(ApiError):
        await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")


@pytest.mark.asyncio
async def test_get_messages_full_page_without_continuation_no_cursor_is_clean_end(client, mock_session):
    """With no cursor (full fetch) there is no gap to protect: a full last page with
    no continuation simply ends pagination, without any ``order=asc`` probe request."""
    client.fetch_json = AsyncMock(return_value={"messages": _page(1000, 200, 10), "continuation": None})

    msgs = await client.get_messages(mock_session, group_id=1)
    assert len(msgs) == 200
    assert client.fetch_json.await_count == 1
    assert all(call.args[2].get("order") != "asc" for call in client.fetch_json.await_args_list)


@pytest.mark.asyncio
async def test_get_messages_duplicate_continuation_on_partial_page_is_timeline_start(client, mock_session):
    """A server may mark the LAST page by echoing the continuation instead of
    omitting it. A partial page (fewer than ``count`` messages) of NEW messages cannot
    be followed by more data, so even before the cursor this is the start of the
    timeline — raising would deadlock the room exactly like the 0.4.3 bug."""
    client.fetch_json = AsyncMock(
        side_effect=[
            {"messages": _page(1000, 200, 10), "continuation": "tok1"},
            {"messages": _page(800, 50, 9), "continuation": "tok1"},
        ]
    )

    msgs = await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")
    assert len(msgs) == 250
    assert client.fetch_json.await_count == 2


@pytest.mark.asyncio
async def test_get_messages_raises_when_server_replays_full_page_before_cursor(client, mock_session):
    """A server that ignores the continuation re-serves the newest page with the same
    token. That is NOT the end of the timeline — older messages exist but are
    unreachable this run. Treating it as the end would return only the newest page and
    let the caller advance its cursor past everything older, so fail closed."""
    from pysaka.exceptions import ApiError

    first = _page(1000, 200, 10)
    client.fetch_json = AsyncMock(
        side_effect=[
            {"messages": first, "continuation": "tok1"},
            {"messages": list(first), "continuation": "tok1"},
        ]
    )

    with pytest.raises(ApiError):
        await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")


@pytest.mark.asyncio
async def test_get_messages_raises_when_server_replays_partial_page_before_cursor(client, mock_session):
    """A replay is a replay regardless of page size: a short page that only re-serves
    already-collected messages with the same token is the server ignoring the token,
    not a last page — fail closed while the cursor is unreached."""
    from pysaka.exceptions import ApiError

    first = _page(1000, 150, 10)
    client.fetch_json = AsyncMock(
        side_effect=[
            {"messages": first, "continuation": "tok1"},
            {"messages": list(first), "continuation": "tok1"},
        ]
    )

    with pytest.raises(ApiError):
        await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")


@pytest.mark.asyncio
async def test_get_messages_duplicate_continuation_without_cursor_is_clean_end(client, mock_session):
    """A repeated continuation on a full page with NO cursor (full fetch) has no gap
    to protect: stop paginating (avoiding an infinite loop), return what was
    collected, and warn that pagination looked stuck."""
    from structlog.testing import capture_logs

    client.fetch_json = AsyncMock(
        side_effect=[
            {"messages": _page(1000, 200, 10), "continuation": "tok1"},
            {"messages": _page(800, 200, 9), "continuation": "tok1"},
        ]
    )

    with capture_logs() as logs:
        msgs = await client.get_messages(mock_session, group_id=1)
    assert len(msgs) == 400
    assert client.fetch_json.await_count == 2
    assert any(e["event"].startswith("Timeline pagination stuck") and e["log_level"] == "warning" for e in logs)


@pytest.mark.asyncio
async def test_get_messages_legacy_since_id_raises_on_stuck_full_page_before_id_found(client, mock_session):
    """Legacy ``since_id`` path: while the since_id message has not been seen, the
    cursor is unreached even though no timestamp is known yet. A stuck full page must
    fail closed rather than return only the newest pages (the caller would advance
    ``last_id`` past the unfetched messages)."""
    from pysaka.exceptions import ApiError

    client.fetch_json = AsyncMock(
        side_effect=[
            {"messages": _page(1000, 200, 10), "continuation": "tok1"},
            {"messages": _page(800, 200, 9), "continuation": "tok1"},
        ]
    )

    with pytest.raises(ApiError):
        await client.get_messages(mock_session, group_id=1, since_id=5)


@pytest.mark.asyncio
async def test_get_messages_legacy_since_id_partial_last_page_is_clean_end(client, mock_session):
    """Legacy ``since_id`` path: a partial last page with no continuation is the
    timeline start even if the since_id message was never found (e.g. deleted);
    return everything newer than since_id."""
    client.fetch_json = AsyncMock(return_value={"messages": _page(1000, 10, 10), "continuation": None})

    # since_id 5 is not on the page (never found): falls back to the id filter.
    msgs = await client.get_messages(mock_session, group_id=1, since_id=5)
    assert [m["id"] for m in msgs] == list(range(991, 1001))


@pytest.mark.asyncio
async def test_get_messages_missing_continuation_after_cursor_is_clean_end(client, mock_session):
    """A page with no continuation whose oldest message has REACHED the cursor is a
    legitimate end-of-timeline: return the newer-than-cursor messages, do not raise.
    Guards against the gap check false-positiving once the cursor is crossed."""
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 100, "published_at": "2026-01-10T00:00:00Z"},
                    {"id": 5, "published_at": "2025-12-31T00:00:00Z"},
                ],
                "continuation": None,
            },
        ]
    )

    msgs = await client.get_messages(mock_session, group_id=1, since_ts="2026-01-01T00:00:00Z")
    assert [m["id"] for m in msgs] == [100]


@pytest.mark.asyncio
async def test_get_messages_does_not_clear_unread_by_default(client, mock_session):
    """Syncing must NOT clear the user's unread badge on the official mobile app.

    The timeline GET must carry clear_unread=false; otherwise the server's
    absent-default zeroes the official app's unread count/red-dot on every sync.
    """
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = {"messages": [{"id": 1}], "continuation": None}

    await client.get_messages(mock_session, group_id=1)

    _, kwargs = mock_session.get.call_args
    assert kwargs["params"]["clear_unread"] == "false"


@pytest.mark.asyncio
async def test_mark_group_read_clears_unread(client, mock_session):
    """mark_group_read sends a minimal timeline fetch with clear_unread=true
    (what the official app does on room-open) and reports success."""
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = {"messages": [], "continuation": None}

    ok = await client.mark_group_read(mock_session, group_id=42)

    assert ok is True
    url, kwargs = mock_session.get.call_args
    assert url[0].endswith("/groups/42/timeline")
    assert kwargs["params"]["clear_unread"] == "true"
    assert kwargs["params"]["count"] == 1


@pytest.mark.asyncio
async def test_get_messages_can_opt_in_to_clear_unread(client, mock_session):
    """Callers may explicitly clear the badge (e.g. an opt-in setting)."""
    mock_resp = mock_session.get.return_value.__aenter__.return_value
    mock_resp.status = 200
    mock_resp.json.return_value = {"messages": [{"id": 1}], "continuation": None}

    await client.get_messages(mock_session, group_id=1, clear_unread=True)

    _, kwargs = mock_session.get.call_args
    assert kwargs["params"]["clear_unread"] == "true"


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

    # When the 401 refresh exhausts all plans, fetch_json must propagate
    # RefreshFailedError (not swallow it into None) so the caller can prompt
    # re-login instead of silently rendering empty data.
    with pytest.raises(RefreshFailedError):
        await client.fetch_json(mock_session, "/test")
    mock_session.get.assert_called_once()
    mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_download_file_zero_byte_stub_is_redownloaded(tmp_path, client, mock_session):
    # A pre-existing 0-byte file must NOT count as "already downloaded".
    dest = tmp_path / "picture" / "1.jpg"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"")
    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 200
    resp.read = AsyncMock(return_value=b"REALBYTES")
    resp.headers = {}
    ok = await client.download_file(mock_session, "https://cdn/1.jpg", dest)
    assert ok is True
    assert dest.read_bytes() == b"REALBYTES"


@pytest.mark.asyncio
async def test_download_file_content_length_mismatch_leaves_no_stub(tmp_path, client, mock_session):
    dest = tmp_path / "picture" / "2.jpg"
    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 200
    resp.read = AsyncMock(return_value=b"AB")
    resp.headers = {"Content-Length": "999"}
    ok = await client.download_file(mock_session, "https://cdn/2.jpg", dest, retries=1)
    assert ok is False
    assert not dest.exists()  # no truncated stub
    assert not (dest.parent / "2.jpg.part").exists()


@pytest.mark.asyncio
async def test_download_file_skips_length_check_when_encoded(tmp_path, client, mock_session):
    # aiohttp auto-decompresses the body, but Content-Length reflects the
    # compressed wire size. When Content-Encoding is present, the declared
    # length must not be compared against the decompressed body size.
    dest = tmp_path / "picture" / "4.jpg"
    resp = mock_session.get.return_value.__aenter__.return_value
    resp.status = 200
    resp.read = AsyncMock(return_value=b"DECOMPRESSED_BYTES")
    resp.headers = {"Content-Encoding": "gzip", "Content-Length": "999"}
    ok = await client.download_file(mock_session, "https://cdn/4.jpg", dest)
    assert ok is True
    assert dest.read_bytes() == b"DECOMPRESSED_BYTES"


@pytest.mark.asyncio
async def test_download_file_retries_then_succeeds(tmp_path, client, mock_session):
    dest = tmp_path / "picture" / "3.jpg"
    resp_500 = AsyncMock()
    resp_500.status = 500
    resp_ok = AsyncMock()
    resp_ok.status = 200
    resp_ok.read = AsyncMock(return_value=b"OK")
    resp_ok.headers = {}
    mock_session.get.return_value.__aenter__.side_effect = [resp_500, resp_ok]
    ok = await client.download_file(mock_session, "https://cdn/3.jpg", dest, retries=3)
    assert ok is True
    assert dest.read_bytes() == b"OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "exception"])
async def test_download_file_logs_never_contain_signed_query(tmp_path, client, mock_session, failure):
    from structlog.testing import capture_logs

    url = "https://cdn/x.jpg?sig=SECRET#frag"
    if failure == "status":
        resp = mock_session.get.return_value.__aenter__.return_value
        resp.status = 403
    else:
        # aiohttp error messages embed the request URL, query included.
        mock_session.get.return_value.__aenter__.side_effect = RuntimeError(f"Cannot connect to {url}")
    with capture_logs() as logs:
        ok = await client.download_file(mock_session, url, tmp_path / "x.jpg", retries=1)
    assert ok is False
    dumped = str(logs)
    assert "SECRET" not in dumped and "frag" not in dumped
    assert "/x.jpg" in dumped


def test_redact_url_tolerates_unparseable_urls():
    from pysaka.client import _redact_url

    assert _redact_url("http://[abc/x.jpg?sig=SECRET") == "<unparseable url>"


@pytest.mark.asyncio
async def test_download_file_with_unparseable_url_fails_cleanly(tmp_path, client, mock_session):
    from structlog.testing import capture_logs

    mock_session.get.return_value.__aenter__.return_value.status = 403
    with capture_logs() as logs:
        ok = await client.download_file(mock_session, "http://[abc/x.jpg?sig=SECRET", tmp_path / "x.jpg", retries=1)
    assert ok is False
    dumped = str(logs)
    assert "SECRET" not in dumped and "<unparseable url>" in dumped


def _desc_page(start_id, n, ts_fn):
    # Newest first (order=desc), like the API.
    return [{"id": i, "published_at": ts_fn(i)} for i in range(start_id + n - 1, start_id - 1, -1)]


def _sec_ts(i):
    return f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"


@pytest.mark.asyncio
async def test_full_last_page_at_timeline_start_is_not_a_gap(monkeypatch):
    """History is exactly 400 messages: the second page is full (200) and has no
    continuation. When the room's first message (order=asc probe) is in the
    collected set, this is the start of the timeline — return, don't raise."""
    monkeypatch.setattr("pysaka.client.asyncio.sleep", AsyncMock())
    client = Client(group=Group.HINATAZAKA46, access_token="t")
    pages = [
        {"messages": _desc_page(201, 200, _sec_ts), "continuation": "c1"},
        {"messages": _desc_page(1, 200, _sec_ts)},  # full, no continuation
    ]
    calls = []

    async def fetch(session, endpoint, params=None):
        calls.append(params)
        if params.get("order") == "asc":
            return {"messages": [{"id": 1, "published_at": _sec_ts(1)}]}
        return pages.pop(0)

    client.fetch_json = AsyncMock(side_effect=fetch)
    msgs = await client.get_messages(MagicMock(), 5, since_ts="2025-12-31T00:00:00Z")
    assert len(msgs) == 400
    probes = [p for p in calls if p.get("order") == "asc"]
    assert len(probes) == 1
    assert probes[0]["clear_unread"] == "false"
    assert probes[0]["count"] == 1


@pytest.mark.asyncio
async def test_full_last_page_still_raises_when_first_message_not_collected(monkeypatch):
    """A full last page whose room-start message (order=asc probe) was never collected
    is still a possible gap, so it raises."""
    monkeypatch.setattr("pysaka.client.asyncio.sleep", AsyncMock())
    client = Client(group=Group.HINATAZAKA46, access_token="t")
    pages = [{"messages": _desc_page(201, 200, _sec_ts)}]  # full, no continuation, but id 1 never seen

    async def fetch(session, endpoint, params=None):
        if params.get("order") == "asc":
            return {"messages": [{"id": 1, "published_at": _sec_ts(1)}]}
        return pages.pop(0)

    client.fetch_json = AsyncMock(side_effect=fetch)
    with pytest.raises(ApiError):
        await client.get_messages(MagicMock(), 5, since_ts="2025-12-31T00:00:00Z")


@pytest.mark.asyncio
async def test_full_last_page_raises_when_probe_fails(monkeypatch):
    """If the order=asc probe fails, the full last page cannot be confirmed as the
    timeline start, so it fails closed."""
    monkeypatch.setattr("pysaka.client.asyncio.sleep", AsyncMock())
    client = Client(group=Group.HINATAZAKA46, access_token="t")
    pages = [{"messages": _desc_page(1, 200, _sec_ts)}]

    async def fetch(session, endpoint, params=None):
        if params.get("order") == "asc":
            return None
        return pages.pop(0)

    client.fetch_json = AsyncMock(side_effect=fetch)
    with pytest.raises(ApiError):
        await client.get_messages(MagicMock(), 5, since_ts="2025-12-31T00:00:00Z")


@pytest.mark.asyncio
async def test_full_last_page_raises_when_probe_ignores_order(monkeypatch):
    """A server that ignores ``order=asc`` answers the probe with the NEWEST message,
    which is always in the collected set. That must not pass for the room's first
    message: ids 1..200 were never fetched, so the dropped continuation is a gap."""
    monkeypatch.setattr("pysaka.client.asyncio.sleep", AsyncMock())
    client = Client(group=Group.HINATAZAKA46, access_token="t")
    pages = [{"messages": _desc_page(201, 200, _sec_ts)}]  # full page of ids 400..201, token dropped

    async def fetch(session, endpoint, params=None):
        if params.get("order") == "asc":
            return {"messages": [{"id": 400, "published_at": _sec_ts(400)}]}
        return pages.pop(0)

    client.fetch_json = AsyncMock(side_effect=fetch)
    with pytest.raises(ApiError):
        await client.get_messages(MagicMock(), 5, since_ts="2025-12-31T00:00:00Z")
