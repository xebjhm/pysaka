"""Server ids and media URLs never lead outside the archive (security audit 2026-11-13)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from pysaka.client import Client, Group
from pysaka.manager import SyncManager


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


GROUP = {"id": 34, "name": "g"}
MEMBER = {"id": 58, "name": "m"}


@pytest.fixture
def mock_client():
    client = MagicMock(spec=Client)
    client.get_messages = AsyncMock(return_value=[])
    client.get_past_messages = AsyncMock(return_value=[])
    client.download_file = AsyncMock(return_value=True)
    client.group = Group.NOGIZAKA46
    return client


@pytest.fixture
def manager(mock_client, tmp_path):
    return SyncManager(mock_client, tmp_path / "out")


@pytest.mark.parametrize("bad_id", ["../../../x", "/abs", True, None, -3, "3 "])
@pytest.mark.asyncio
async def test_sync_member_refuses_a_bad_group_id_before_touching_disk(manager, mock_client, tmp_path, bad_id):
    queue: list = []
    with capture_logs() as logs:
        n = await manager.sync_member(MagicMock(), {"id": bad_id, "name": "g"}, MEMBER, queue, prefetched_messages=[])
    assert n == 0 and queue == []
    assert not (tmp_path / "out" / "messages").exists()
    assert {p.name for p in tmp_path.iterdir()} <= {"out"}
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["group"]
    mock_client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_sync_member_refuses_a_bad_member_id(manager, mock_client, tmp_path):
    with capture_logs() as logs:
        n = await manager.sync_member(MagicMock(), GROUP, {"id": "../m", "name": "m"}, [], prefetched_messages=[])
    assert n == 0
    assert not (tmp_path / "out" / "messages").exists()
    assert {p.name for p in tmp_path.iterdir()} <= {"out"}
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["member"]
    mock_client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_numeric_string_ids_name_the_same_folders_as_ints(manager):
    with patch.object(manager, "_archive_profile_images_safely", AsyncMock()):
        await manager.sync_member(
            MagicMock(), {"id": "34", "name": "g"}, {"id": "58", "name": "m"}, [], prefetched_messages=[]
        )
    assert (manager.output_dir / "messages" / "34 g" / "58 m" / "picture").is_dir()


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.asyncio
async def test_backfill_refuses_a_bad_member_id_even_with_a_matching_state_entry(manager, mock_client, tmp_path):
    # Seed what the unguarded code would need to get past its early returns.
    manager.sync_state["34_../m"] = {"last_id": 1}
    (manager.output_dir / "messages" / "34 g").mkdir(parents=True)
    # "34 g" / "../m m" collapses to messages/"m m": where the unguarded code looked.
    (manager.output_dir / "messages" / "m m").mkdir()
    (manager.output_dir / "messages" / "m m" / "messages.json").write_text("{}", encoding="utf-8")
    before = _tree(tmp_path)
    with capture_logs() as logs:
        n = await manager.backfill_past_messages(MagicMock(), GROUP, {"id": "../m", "name": "m"})
    assert n == 0
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["member"]
    mock_client.get_past_messages.assert_not_called()
    assert _tree(tmp_path) == before


@pytest.mark.asyncio
async def test_letters_refuse_a_bad_member_id_before_fetching_or_touching_disk(manager, mock_client, tmp_path):
    mock_client.get_letters = AsyncMock(return_value=[])
    with capture_logs() as logs:
        n = await manager.sync_letters(MagicMock(), GROUP, {"id": "../m", "name": "m"})
    assert n == 0
    assert [e["kind"] for e in logs if e["event"] == "Room ids refused"] == ["member"]
    mock_client.get_letters.assert_not_called()
    assert not (tmp_path / "out" / "messages").exists()
    assert {p.name for p in tmp_path.iterdir()} <= {"out"}


@pytest.mark.asyncio
async def test_room_archive_complete_is_false_for_a_bad_group_id(manager):
    assert await manager.room_archive_complete({"id": "../..", "name": "g"}, [{"id": 1}]) is False


def test_path_builders_refuse_anything_but_int_ids(manager):
    with pytest.raises(ValueError):
        manager._member_dir_path({"id": "../x", "name": "g"}, MEMBER)
    with pytest.raises(ValueError):
        manager._member_dir_path(GROUP, {"id": "58", "name": "m"})
    with pytest.raises(ValueError):
        manager._ensure_group_dir({"id": "34", "name": "g"})
    with pytest.raises(ValueError):
        manager._ensure_member_dir(manager.output_dir, "58", "m")


def _member_dir(manager) -> Path:
    member_dir = manager.output_dir / "messages" / "34 g" / "58 m"
    member_dir.mkdir(parents=True)
    return member_dir


def _row(mid, ts, url="https://cdn.example.com/a.jpg"):
    return {"id": mid, "type": "picture", "file": url, "published_at": ts, "text": None}


def test_rows_with_unsafe_ids_are_dropped_and_do_not_hold_the_cursor(manager):
    member_dir = _member_dir(manager)
    queue: list = []
    rows = [
        _row("../../../../escape", "2026-01-01T00:00:00Z"),
        _row("/abs/escape", "2026-01-01T00:00:01Z"),
        _row(7, "2026-01-02T00:00:00Z", "https://cdn.example.com/7.jpg"),
    ]
    with capture_logs() as logs, patch("pysaka.manager.media_file_is_present", return_value=False) as present:
        processed, failed_ts, pending_ts = manager.prepare_messages(rows, member_dir, queue)
    assert [p["id"] for p in processed] == [7]
    assert failed_ts is None
    assert pending_ts == "2026-01-02T00:00:00Z"
    assert [item["message_id"] for item in queue] == [7]
    assert queue[0]["path"] == member_dir / "picture" / "7.jpg"
    assert all(call.args[0].parent == member_dir / "picture" for call in present.call_args_list)
    (refused,) = [e for e in logs if e["event"] == "Messages refused"]
    assert (refused["reason"], refused["count"]) == ("bad_id", 2)
    assert "escape" not in repr(logs)


def test_numeric_string_message_ids_are_stored_as_ints(manager):
    member_dir = _member_dir(manager)
    queue: list = []
    with patch("pysaka.manager.media_file_is_present", return_value=False):
        processed, _, _ = manager.prepare_messages([_row("8", "2026-01-03T00:00:00Z")], member_dir, queue)
    assert processed[0]["id"] == 8 and queue[0]["message_id"] == 8
    assert processed[0]["media_file"] == str((member_dir / "picture" / "8.jpg").relative_to(manager.output_dir))


class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def read(self):
        return self._body


class _Ctx:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *exc):
        return False


def _session(*responses):
    session = MagicMock()
    session.get = MagicMock(side_effect=[_Ctx(r) for r in responses])
    return session


@pytest.mark.asyncio
async def test_download_refuses_a_local_address_without_a_request(tmp_path):
    session = _session()
    with capture_logs() as logs:
        ok = await _client().download_file(session, "https://192.168.1.1/a.jpg", tmp_path / "a.jpg")
    assert ok is False
    session.get.assert_not_called()
    assert [e["reason"] for e in logs if e["event"] == "Download refused"] == ["ip_literal"]
    assert not (tmp_path / "a.jpg").exists()


@pytest.mark.asyncio
async def test_download_upgrades_http_to_https(tmp_path):
    session = _session(_Resp(200, b"JPEG"))
    ok = await _client().download_file(session, "http://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is True
    session.get.assert_called_once_with("https://cdn.example.com/a.jpg", allow_redirects=False)


@pytest.mark.asyncio
async def test_a_redirect_into_the_local_network_is_refused_and_not_retried(tmp_path):
    session = _session(_Resp(302, headers={"Location": "http://nas/secret"}))
    with capture_logs() as logs:
        ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is False
    assert session.get.call_count == 1
    assert [e["reason"] for e in logs if e["event"] == "Download refused"] == ["redirect_local_name"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_redirect_to_another_public_host_is_followed(tmp_path):
    session = _session(
        _Resp(302, headers={"Location": "https://cdn2.example.com/a.jpg"}),
        _Resp(200, b"JPEG"),
    )
    ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is True
    assert (tmp_path / "a.jpg").read_bytes() == b"JPEG"
    assert session.get.call_args_list[1].args[0] == "https://cdn2.example.com/a.jpg"


@pytest.mark.asyncio
async def test_too_many_redirects_are_refused(tmp_path):
    hop = {"Location": "https://cdn.example.com/next.jpg"}
    session = _session(*[_Resp(302, headers=hop) for _ in range(4)])
    with capture_logs() as logs:
        ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is False
    assert [e["reason"] for e in logs if e["event"] == "Download refused"] == ["redirect_limit"]


@pytest.mark.asyncio
async def test_queue_items_outside_the_output_folder_are_refused(manager, mock_client, tmp_path):
    outside = tmp_path / "elsewhere" / "x.jpg"
    seen: list = []
    queue = [
        {
            "url": "https://cdn.example.com/x.jpg",
            "path": outside,
            "timestamp": None,
            "message_id": 1,
            "media_type": "picture",
        }
    ]
    with capture_logs() as logs:
        await manager.process_media_queue(MagicMock(), queue, item_callback=lambda ok: seen.append(ok))
    mock_client.download_file.assert_not_called()
    assert seen == [False]
    (refused,) = [e for e in logs if e["event"] == "Media items refused"]
    assert (refused["reason"], refused["count"]) == ("outside_output_dir", 1)


def test_scan_reports_an_escaping_media_file_as_unresolved(manager):
    import json

    member_dir = _member_dir(manager)
    (member_dir / "messages.json").write_text(
        json.dumps({"messages": [{"id": 1, "type": "picture", "media_file": "../../../../escape.jpg"}]}),
        encoding="utf-8",
    )
    with capture_logs() as logs:
        result = manager.scan_member_media(member_dir)
    assert result["missing"] == [] and result["checked"] == 0
    assert [u["message_id"] for u in result["unresolved"]] == [1]
    assert [e["count"] for e in logs if e["event"] == "saka.scan.media_file_refused"] == [1]


# --- sweep: review findings (coverage and edge cases) ---


@pytest.mark.asyncio
async def test_get_messages_survives_a_non_dict_last_row_and_uses_the_last_accepted_row():
    client = _client()
    seen = []
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 3, "published_at": "2026-01-03T00:00:00Z"},
                    {"id": 2, "published_at": "2026-01-02T00:00:00Z"},
                    "garbage",
                ],
                "continuation": None,
            }
        ]
    )
    msgs = await client.get_messages(
        MagicMock(), group_id=1, since_ts="2026-01-02T00:00:00Z", progress_callback=lambda ts, n: seen.append(ts)
    )
    assert [m["id"] for m in msgs] == [2, 3]
    assert seen == ["2026-01-02T00:00:00Z"]  # the last accepted row, not the refused one


@pytest.mark.asyncio
async def test_get_messages_stops_at_the_cursor_using_the_last_accepted_row():
    client = _client()
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 3, "published_at": "2026-01-03T00:00:00Z"},
                    {"id": 2, "published_at": "2026-01-01T00:00:00Z"},
                    {"id": "../x", "published_at": "2027-01-01T00:00:00Z"},
                ],
                "continuation": "more",
            }
        ]
    )
    msgs = await client.get_messages(MagicMock(), group_id=1, since_ts="2026-01-02T00:00:00Z")
    assert [m["id"] for m in msgs] == [3]
    assert client.fetch_json.call_count == 1


@pytest.mark.asyncio
async def test_get_messages_legacy_since_id_accepts_a_string_id():
    client = _client()
    client.fetch_json = AsyncMock(
        side_effect=[
            {
                "messages": [
                    {"id": 5, "published_at": "2026-01-05T00:00:00Z"},
                    {"id": "4", "published_at": "2026-01-04T00:00:00Z"},
                ],
                "continuation": None,
            }
        ]
    )
    msgs = await client.get_messages(MagicMock(), group_id=1, since_id=4)
    assert [m["id"] for m in msgs] == [4, 5]


@pytest.mark.asyncio
async def test_get_messages_fails_closed_when_every_row_of_a_page_is_refused():
    from pysaka.exceptions import ApiError

    client = _client()
    client.fetch_json = AsyncMock(
        side_effect=[{"messages": [{"id": "../x"}, "junk", {"id": None}], "continuation": None}]
    )
    with capture_logs() as logs, pytest.raises(ApiError):
        await client.get_messages(MagicMock(), group_id=1)
    assert [e["count"] for e in logs if e["event"] == "Timeline ids refused"] == [3]
    assert "../x" not in repr(logs)


@pytest.mark.asyncio
async def test_a_refused_redirect_removes_a_stale_part_file(tmp_path):
    part = tmp_path / "a.jpg.part"
    part.write_bytes(b"half")
    session = _session(_Resp(302, headers={"Location": "http://nas/secret"}))
    ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_redirect_without_location_is_refused(tmp_path):
    session = _session(_Resp(302, headers={}))
    with capture_logs() as logs:
        ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is False
    assert session.get.call_count == 1
    assert [e["reason"] for e in logs if e["event"] == "Download refused"] == ["redirect_without_location"]


@pytest.mark.asyncio
async def test_the_redirect_limit_is_four_requests(tmp_path):
    hop = {"Location": "https://cdn.example.com/next.jpg"}
    session = _session(*[_Resp(302, headers=hop) for _ in range(4)], _Resp(200, b"JPEG"))
    ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg", retries=1)
    assert ok is False
    assert session.get.call_count == 4


@pytest.mark.asyncio
async def test_three_redirect_hops_then_200_succeeds(tmp_path):
    hop = {"Location": "https://cdn.example.com/next.jpg"}
    session = _session(*[_Resp(302, headers=hop) for _ in range(3)], _Resp(200, b"JPEG"))
    ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is True
    assert session.get.call_count == 4
    assert all(c.kwargs == {"allow_redirects": False} for c in session.get.call_args_list)


@pytest.mark.asyncio
async def test_relative_and_scheme_relative_locations_resolve_and_http_hops_upgrade(tmp_path):
    session = _session(
        _Resp(301, headers={"Location": "/other/a.jpg"}),
        _Resp(302, headers={"Location": "//cdn3.example.com/b.jpg"}),
        _Resp(307, headers={"Location": "http://cdn4.example.com/c.jpg"}),
        _Resp(200, b"JPEG"),
    )
    ok = await _client().download_file(session, "https://cdn.example.com/a.jpg", tmp_path / "a.jpg")
    assert ok is True
    assert [c.args[0] for c in session.get.call_args_list] == [
        "https://cdn.example.com/a.jpg",
        "https://cdn.example.com/other/a.jpg",
        "https://cdn3.example.com/b.jpg",
        "https://cdn4.example.com/c.jpg",
    ]


def test_prepare_messages_drops_media_paths_outside_the_member_dir(manager):
    member_dir = _member_dir(manager)
    queue: list = []
    with capture_logs() as logs, patch("pysaka.manager.is_contained", return_value=False):
        processed, failed_ts, pending_ts = manager.prepare_messages(
            [_row(7, "2026-01-02T00:00:00Z")], member_dir, queue
        )
    assert processed == [] and queue == []
    assert failed_ts is None and pending_ts is None
    (refused,) = [e for e in logs if e["event"] == "Messages refused"]
    assert (refused["reason"], refused["count"]) == ("outside_member_dir", 1)


def test_scan_lists_a_non_string_media_file_as_unresolved(manager):
    import json

    member_dir = _member_dir(manager)
    (member_dir / "messages.json").write_text(
        json.dumps({"messages": [{"id": 1, "type": "picture", "media_file": 5}]}), encoding="utf-8"
    )
    result = manager.scan_member_media(member_dir)
    assert result["checked"] == 0 and [u["message_id"] for u in result["unresolved"]] == [1]


def test_scan_refuses_a_media_file_that_is_the_output_dir_itself(manager):
    import json

    member_dir = _member_dir(manager)
    (member_dir / "messages.json").write_text(
        json.dumps({"messages": [{"id": 1, "type": "picture", "media_file": "."}]}), encoding="utf-8"
    )
    result = manager.scan_member_media(member_dir)
    assert result["checked"] == 0 and [u["message_id"] for u in result["unresolved"]] == [1]


@pytest.mark.parametrize("bad_path", [None, 5, ["x"]])
@pytest.mark.asyncio
async def test_queue_items_without_a_usable_path_get_their_own_reason(manager, mock_client, bad_path):
    seen: list = []
    queue = [{"url": "https://cdn.example.com/x.jpg", "path": bad_path, "timestamp": None, "media_type": "picture"}]
    with capture_logs() as logs:
        await manager.process_media_queue(MagicMock(), queue, item_callback=lambda ok: seen.append(ok))
    mock_client.download_file.assert_not_called()
    assert seen == [False]
    (refused,) = [e for e in logs if e["event"] == "Media items refused"]
    assert (refused["reason"], refused["count"]) == ("bad_path", 1)


@pytest.mark.asyncio
async def test_queue_item_for_the_output_dir_itself_is_refused(manager, mock_client):
    queue = [{"url": "https://cdn.example.com/x.jpg", "path": manager.output_dir, "timestamp": None}]
    await manager.process_media_queue(MagicMock(), queue)
    mock_client.download_file.assert_not_called()


@pytest.mark.asyncio
async def test_queue_string_path_inside_the_output_dir_is_downloaded(manager, mock_client):
    path = manager.output_dir / "messages" / "x.jpg"
    queue = [{"url": "https://cdn.example.com/x.jpg", "path": str(path), "timestamp": None, "media_type": "text"}]
    await manager.process_media_queue(MagicMock(), queue)
    assert mock_client.download_file.call_args.args[2] == path


@pytest.mark.asyncio
async def test_queue_video_inside_the_output_dir_keeps_its_audio_metadata(manager, mock_client):
    member_dir = _member_dir(manager)
    path = member_dir / "video" / "9.mp4"
    queue = [
        {
            "url": "https://cdn.example.com/9.mp4",
            "path": path,
            "timestamp": None,
            "message_id": 9,
            "media_type": "video",
            "member_dir": member_dir,
        }
    ]
    with (
        patch("pysaka.manager.get_media_dimensions", return_value=(None, None)),
        patch("pysaka.manager.get_audio_metadata", return_value={"duration": 3.5, "is_muted": False}),
    ):
        result = await manager.process_media_queue(MagicMock(), queue)
    assert result == {member_dir: {9: {"media_duration": 3.5, "is_muted": False}}}


@pytest.mark.asyncio
async def test_refused_count_is_logged_when_a_later_page_fails():
    from pysaka.exceptions import ApiError

    client = _client()
    page = {
        "messages": [{"id": 9, "published_at": "2026-01-09T00:00:00Z"}, {"id": "../x"}, {"id": None}],
        "continuation": "c1",
    }
    client.fetch_json = AsyncMock(side_effect=[page, None])
    with capture_logs() as logs, pytest.raises(ApiError):
        await client.get_messages(MagicMock(), group_id=1, since_ts="2026-01-01T00:00:00Z", page_delay=0)
    assert [(e["group_id"], e["count"]) for e in logs if e["event"] == "Timeline ids refused"] == [(1, 2)]
    assert "../x" not in repr(logs)
