"""Own profile archiving (spec §10): nickname + avatar only, no personal data."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from pysaka.client import Client, Group
from pysaka.exceptions import SessionExpiredError
from pysaka.manager import SyncManager

PROFILE = {
    "name": "ハオ🐱",
    "birthday": "1990-01-01",
    "sex": "male",
    "subdivision": "TW-TPE",
    "thumbnail": "https://cdn/users/thumbnails/u-20260101-000000.jpeg?sig=abc",
    "fc_accounts": [{"organization_id": 1, "account": "secret"}],
    "updated_at": "2026-10-01T00:00:00Z",
}


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.HINATAZAKA46
    client.get_profile = AsyncMock(return_value=dict(PROFILE))

    async def fake_download(session, url, path, timestamp=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"me")
        return True

    client.download_file = AsyncMock(side_effect=fake_download)
    return SyncManager(client, tmp_path)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


async def test_stores_name_and_avatar_only(manager):
    report = await manager.archive_account_profile(AsyncMock())
    assert report == {"downloaded": 1, "skipped": 0, "failed": 0}
    account = manager.output_dir / "account"
    assert _read(account / "profile.json") == {"version": 1, "name": "ハオ🐱", "updated_at": "2026-10-01T00:00:00Z"}
    blob = (account / "profile.json").read_text(encoding="utf-8")
    for secret in ("1990-01-01", "TW-TPE", "male", "secret"):
        assert secret not in blob
    assert (account / "profile_images" / "thumbnail" / "u-20260101-000000.jpeg").read_bytes() == b"me"
    index_text = (account / "profile_images" / "index.json").read_text(encoding="utf-8")
    assert json.loads(index_text)["current"] == {"thumbnail": "https://cdn/users/thumbnails/u-20260101-000000.jpeg"}
    assert "sig=abc" not in index_text


async def test_second_run_skips_and_avatar_change_adds_history(manager):
    await manager.archive_account_profile(AsyncMock())
    assert (await manager.archive_account_profile(AsyncMock()))["skipped"] == 1
    manager.client.get_profile.return_value = {
        **PROFILE,
        "thumbnail": "https://cdn/users/thumbnails/u-20261101-000000.jpeg",
    }
    await manager.archive_account_profile(AsyncMock())
    idx = _read(manager.output_dir / "account" / "profile_images" / "index.json")
    assert [e["file"] for e in idx["images"]] == [
        "thumbnail/u-20260101-000000.jpeg",
        "thumbnail/u-20261101-000000.jpeg",
    ]


async def test_missing_profile_writes_nothing(manager):
    manager.client.get_profile.return_value = None
    assert await manager.archive_account_profile(AsyncMock()) == {"downloaded": 0, "skipped": 0, "failed": 0}
    assert not (manager.output_dir / "account").exists()


async def test_auth_error_propagates_other_errors_swallowed(manager):
    empty = {"downloaded": 0, "skipped": 0, "failed": 0}
    manager.client.get_profile = AsyncMock(side_effect=SessionExpiredError("x"))
    with pytest.raises(SessionExpiredError):
        await manager.archive_account_profile(AsyncMock())
    manager.client.get_profile = AsyncMock(side_effect=RuntimeError("boom"))
    assert await manager.archive_account_profile(AsyncMock()) == empty
    assert not (manager.output_dir / "account").exists()


async def test_profile_without_thumbnail_archives_no_avatar(manager):
    manager.client.get_profile.return_value = {k: v for k, v in PROFILE.items() if k != "thumbnail"}
    assert await manager.archive_account_profile(AsyncMock()) == {"downloaded": 0, "skipped": 0, "failed": 0}
    account = manager.output_dir / "account"
    assert _read(account / "profile.json")["name"] == "ハオ🐱"
    assert not (account / "profile_images").exists()
    manager.client.download_file.assert_not_called()


@pytest.mark.parametrize("thumbnail", [None, "", 123, ["x"], {"a": "b"}])
async def test_non_string_thumbnail_is_ignored(manager, thumbnail):
    manager.client.get_profile.return_value = {**PROFILE, "thumbnail": thumbnail}
    assert await manager.archive_account_profile(AsyncMock()) == {"downloaded": 0, "skipped": 0, "failed": 0}
    assert not (manager.output_dir / "account" / "profile_images").exists()
    manager.client.download_file.assert_not_called()


@pytest.mark.parametrize("name", [None, "", 42])
async def test_missing_or_invalid_name_keeps_stored_name(manager, name):
    await manager.archive_account_profile(AsyncMock())
    manager.client.get_profile.return_value = {**PROFILE, "name": name}
    if name is None:
        del manager.client.get_profile.return_value["name"]
    await manager.archive_account_profile(AsyncMock())
    assert _read(manager.output_dir / "account" / "profile.json")["name"] == "ハオ🐱"


async def test_invalid_updated_at_keeps_stored_or_none(manager):
    manager.client.get_profile.return_value = {**PROFILE, "updated_at": 5}
    await manager.archive_account_profile(AsyncMock())
    path = manager.output_dir / "account" / "profile.json"
    assert _read(path)["updated_at"] is None  # first write: nothing stored yet
    manager.client.get_profile.return_value = dict(PROFILE)
    await manager.archive_account_profile(AsyncMock())
    assert _read(path)["updated_at"] == "2026-10-01T00:00:00Z"
    manager.client.get_profile.return_value = {k: v for k, v in PROFILE.items() if k != "updated_at"}
    await manager.archive_account_profile(AsyncMock())
    assert _read(path)["updated_at"] == "2026-10-01T00:00:00Z"


async def test_missing_name_on_first_write_is_empty(manager):
    manager.client.get_profile.return_value = {k: v for k, v in PROFILE.items() if k != "name"}
    await manager.archive_account_profile(AsyncMock())
    assert _read(manager.output_dir / "account" / "profile.json")["name"] == ""


async def test_post_fetch_oserror_is_swallowed(manager, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(manager.output_dir), "mkdir", boom)
    assert await manager.archive_account_profile(AsyncMock()) == {"downloaded": 0, "skipped": 0, "failed": 0}


async def test_archive_failure_is_swallowed_and_auth_still_propagates(manager):
    manager._archive_image_set = AsyncMock(side_effect=RuntimeError("boom"))
    assert await manager.archive_account_profile(AsyncMock()) == {"downloaded": 0, "skipped": 0, "failed": 0}
    manager._archive_image_set = AsyncMock(side_effect=SessionExpiredError("x"))
    with pytest.raises(SessionExpiredError):
        await manager.archive_account_profile(AsyncMock())


async def test_logs_never_contain_nickname_or_avatar_url(manager):
    manager._archive_image_set = AsyncMock(side_effect=RuntimeError(f"{PROFILE['name']} {PROFILE['thumbnail']}"))
    with capture_logs() as logs:
        await manager.archive_account_profile(AsyncMock())
    assert logs, "failure path should log something"
    blob = json.dumps(logs, ensure_ascii=False, default=str)
    assert PROFILE["name"] not in blob
    assert PROFILE["thumbnail"] not in blob
    assert "abc" not in blob
    assert any(e.get("error_type") == "RuntimeError" for e in logs)


async def test_unreadable_profile_json_is_kept_aside_and_rebuilt(manager, time_machine):
    time_machine.move_to("2026-11-03T12:34:56Z", tick=False)
    account = manager.output_dir / "account"
    account.mkdir(parents=True)
    (account / "profile.json").write_text("{broken ハオ", encoding="utf-8")

    with capture_logs() as logs:
        await manager.archive_account_profile(AsyncMock())

    assert (account / "profile.json.corrupt-20261103T123456Z").read_text(encoding="utf-8") == "{broken ハオ"
    assert _read(account / "profile.json")["name"] == "ハオ🐱"
    (entry,) = [e for e in logs if e.get("file") == "profile.json"]
    assert entry["log_level"] == "warning"
    assert "ハオ" not in repr(logs)  # neither the nickname nor the corrupt content is logged
