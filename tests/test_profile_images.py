"""Profile image archiving (spec §2)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka import profile_images as pi
from pysaka.client import Client, Group
from pysaka.manager import SyncManager

NOW = "2026-11-03T04:12:00Z"
GROUP = {"id": 79, "name": "中川 智尋", "thumbnail": "https://cdn/public/groups/thumbnails/79-20261001-010023.jpg"}
MEMBER = {
    "id": 134,
    "name": "中川 智尋",
    "thumbnail": "https://cdn/public/members/thumbnails/134-20260705-113257.jpeg",
    "phone_image": "https://cdn/public/members/phone-images/134-20260223-125348.jpeg?sig=abc",
}


def test_image_urls_skips_empty():
    urls = pi.image_urls({"thumbnail": ""}, {"thumbnail": "u1", "phone_image": None})
    assert urls == {"thumbnail": "u1"}


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cdn/a/134-20260705-113257.jpeg", "thumbnail/134-20260705-113257.jpeg"),
        ("https://cdn/a/x.jpeg?sig=1#f", "thumbnail/x.jpeg"),
        ("https://cdn/a/..", "thumbnail/_"),
        ("https://cdn/a/", None),
    ],
)
def test_relative_file(url, expected):
    assert pi.relative_file("thumbnail", url) == expected


def test_merge_index_creates_and_appends_history():
    first = pi.merge_index(None, {"thumbnail": ("thumbnail/a.jpg", "u/a.jpg")}, {"thumbnail": "u/a.jpg"}, NOW)
    assert first == {
        "version": 1,
        "current": {"thumbnail": "u/a.jpg"},
        "images": [{"kind": "thumbnail", "file": "thumbnail/a.jpg", "url": "u/a.jpg", "first_seen": NOW}],
    }
    assert pi.merge_index(first, {"thumbnail": ("thumbnail/a.jpg", "u/a.jpg")}, {"thumbnail": "u/a.jpg"}, NOW) is None

    later = "2026-12-01T00:00:00Z"
    second = pi.merge_index(first, {"thumbnail": ("thumbnail/b.jpg", "u/b.jpg")}, {"thumbnail": "u/b.jpg"}, later)
    assert [e["file"] for e in second["images"]] == ["thumbnail/a.jpg", "thumbnail/b.jpg"]
    assert second["images"][1]["first_seen"] == later
    assert second["current"] == {"thumbnail": "u/b.jpg"}


def test_merge_index_current_tracks_api_even_before_download():
    idx = pi.merge_index(None, {}, {"phone_image": "u/p.jpg"}, NOW)
    assert idx == {"version": 1, "current": {"phone_image": "u/p.jpg"}, "images": []}


def test_merge_index_nothing_to_record_returns_none():
    assert pi.merge_index(None, {}, {}, NOW) is None


def test_merge_index_replaces_invalid_existing():
    idx = pi.merge_index(["bad"], {}, {"thumbnail": "u"}, NOW)
    assert idx["current"] == {"thumbnail": "u"} and idx["images"] == []


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.SAKURAZAKA46
    client.get_messages = AsyncMock(return_value=[])

    async def fake_download(session, url, path, timestamp=None):
        if "fail" in url:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")
        return True

    client.download_file = AsyncMock(side_effect=fake_download)
    return SyncManager(client, tmp_path)


def _index(member_dir):
    with open(member_dir / "profile_images" / "index.json", encoding="utf-8") as f:
        return json.load(f)


async def test_archive_downloads_then_skips(manager, tmp_path, time_machine):
    time_machine.move_to(NOW)
    member_dir = tmp_path / "m"
    report = await manager.archive_profile_images(AsyncMock(), GROUP, MEMBER, member_dir)
    assert report == {"downloaded": 3, "skipped": 0, "failed": 0}
    assert (member_dir / "profile_images" / "phone_image" / "134-20260223-125348.jpeg").read_bytes() == b"img"
    idx = _index(member_dir)
    assert idx["current"]["phone_image"] == MEMBER["phone_image"]
    assert {e["first_seen"] for e in idx["images"]} == {NOW}

    report = await manager.archive_profile_images(AsyncMock(), GROUP, MEMBER, member_dir)
    assert report == {"downloaded": 0, "skipped": 3, "failed": 0}
    assert manager.client.download_file.await_count == 3


async def test_archive_failed_download_is_not_indexed_and_retried(manager, tmp_path):
    member_dir = tmp_path / "m"
    member = {**MEMBER, "thumbnail": "https://cdn/fail/t.jpg"}
    report = await manager.archive_profile_images(AsyncMock(), GROUP, member, member_dir)
    assert report["failed"] == 1
    idx = _index(member_dir)
    assert "thumbnail/t.jpg" not in {e["file"] for e in idx["images"]}
    assert idx["current"]["thumbnail"] == "https://cdn/fail/t.jpg"


async def test_archive_no_urls_writes_nothing(manager, tmp_path):
    member_dir = tmp_path / "m"
    report = await manager.archive_profile_images(
        AsyncMock(), {"id": 1, "name": "G"}, {"id": 2, "name": "M"}, member_dir
    )
    assert report == {"downloaded": 0, "skipped": 0, "failed": 0}
    assert not (member_dir / "profile_images").exists()


async def test_sync_member_archives_even_without_new_messages(manager):
    count = await manager.sync_member(AsyncMock(), GROUP, MEMBER, [])
    assert count == 0
    member_dir = manager._member_dir_path(GROUP, MEMBER)
    assert len(_index(member_dir)["images"]) == 3


async def test_sync_member_survives_archiving_crash(manager, monkeypatch):
    monkeypatch.setattr(manager, "archive_profile_images", AsyncMock(side_effect=RuntimeError("disk full")))
    manager.client.get_messages.return_value = [
        {"id": 1, "type": "text", "text": "hi", "member_id": 134, "published_at": "2026-01-01T00:00:00Z"}
    ]
    assert await manager.sync_member(AsyncMock(), GROUP, MEMBER, []) == 1
    assert manager.get_last_ts(79, 134) == "2026-01-01T00:00:00Z"


async def test_archive_url_without_basename_counts_as_failed(manager, tmp_path):
    member_dir = tmp_path / "m"
    member = {**MEMBER, "thumbnail": "https://cdn/dir/"}
    report = await manager.archive_profile_images(AsyncMock(), {"id": 1, "name": "G"}, member, member_dir)
    assert report["failed"] == 1
    assert manager.client.download_file.await_count == 1  # only phone_image was attempted
    assert "thumbnail" not in _index(member_dir)["images"][0]["file"]


async def test_archive_clears_current_when_urls_disappear(manager, tmp_path):
    member_dir = tmp_path / "m"
    await manager.archive_profile_images(AsyncMock(), GROUP, MEMBER, member_dir)
    report = await manager.archive_profile_images(
        AsyncMock(), {"id": 1, "name": "G"}, {"id": 2, "name": "M"}, member_dir
    )
    assert report == {"downloaded": 0, "skipped": 0, "failed": 0}
    idx = _index(member_dir)
    assert idx["current"] == {}
    assert len(idx["images"]) == 3  # history is never dropped
