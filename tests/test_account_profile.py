"""Own profile archiving (spec §10): nickname + avatar only, no personal data."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    assert _read(account / "profile_images" / "index.json")["current"] == {"thumbnail": PROFILE["thumbnail"]}


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
    manager.client.get_profile = AsyncMock(side_effect=SessionExpiredError("x"))
    with pytest.raises(SessionExpiredError):
        await manager.archive_account_profile(AsyncMock())
    manager.client.get_profile = AsyncMock(side_effect=RuntimeError("boom"))
    assert (await manager.archive_account_profile(AsyncMock()))["failed"] == 0
