"""Sent-letter archiving (spec §9)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pysaka.client import Client, Group
from pysaka.exceptions import SessionExpiredError
from pysaka.manager import SyncManager

GROUP = {"id": 90, "name": "高井 俐香"}
MEMBER = {"id": 90, "name": "高井 俐香"}


def _letter(i, upd, member_id=90, url=True):
    return {
        "id": i,
        "group_id": 90,
        "member_id": member_id,
        "text": f"りかたん 手紙 {i} 🎉",
        "file": f"https://cdn/letters/{i}.jpg?sig=x" if url else None,
        "thumbnail": f"https://cdn/letters/t{i}.jpg",
        "thumbnail_width": 300,
        "thumbnail_height": 480,
        "is_favorite": False,
        "client_token": "SECRET-TOKEN",
        "created_at": upd,
        "updated_at": upd,
    }


class FakeLetterApi:
    """Mimics the server: ascending by id, inclusive updated_from, count from the oldest."""

    def __init__(self, letters):
        self.letters = letters
        self.calls = []

    async def __call__(self, session, group_id, updated_from=None, count=200):
        self.calls.append(updated_from)
        ordered = sorted(self.letters, key=lambda row: row["id"])
        rows = [row for row in ordered if updated_from is None or row["updated_at"] >= updated_from]
        return rows[:count]

    def as_mock(self):
        # AsyncMock only awaits a side_effect it recognises as a coroutine function.
        return AsyncMock(side_effect=self.__call__)


@pytest.fixture
def manager(tmp_path):
    client = MagicMock(spec=Client)
    client.group = Group.HINATAZAKA46

    async def fake_download(session, url, path, timestamp=None):
        if "fail" in url:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"card")
        return True

    client.download_file = AsyncMock(side_effect=fake_download)
    return SyncManager(client, tmp_path)


def _stored(manager):
    with open(manager._member_dir_path(GROUP, MEMBER) / "letters.json", encoding="utf-8") as f:
        return json.load(f)


def _ts(i):
    return f"2026-01-{1 + i // 1440:02d}T{(i // 60) % 24:02d}:{i % 60:02d}:00Z"


async def test_first_sync_stores_letters_images_and_cursor(manager):
    api = FakeLetterApi([_letter(1, _ts(1)), _letter(2, _ts(2)), _letter(3, _ts(3), member_id=7)])
    manager.client.get_letters = api.as_mock()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 2

    data = _stored(manager)
    assert data["version"] == 1
    assert [row["id"] for row in data["letters"]] == [1, 2]
    first = data["letters"][0]
    assert first["text"] == "りかたん 手紙 1 🎉" and first["image_file"] == "letters/1.jpg"
    assert first["width"] == 300 and first["height"] == 480
    assert "client_token" not in first and "file" not in first
    assert (manager._member_dir_path(GROUP, MEMBER) / "letters" / "1.jpg").read_bytes() == b"card"
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(3)
    assert "last_sync_ts" not in manager.sync_state["90_90"]  # message cursor untouched


async def test_incremental_sync_uses_cursor_and_upserts(manager):
    api = FakeLetterApi([_letter(1, _ts(1)), _letter(2, _ts(2))])
    manager.client.get_letters = api.as_mock()
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)

    api.letters.append(_letter(4, _ts(9)))
    api.letters[1] = {**_letter(2, _ts(8)), "is_favorite": True}  # letter 2 updated later
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 1

    assert api.calls[-1] == _ts(2)  # resumed from the stored cursor
    data = _stored(manager)
    assert [row["id"] for row in data["letters"]] == [1, 2, 4]
    assert data["letters"][1]["is_favorite"] is True
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(9)


async def test_pages_beyond_200(manager):
    api = FakeLetterApi([_letter(i, _ts(i)) for i in range(1, 451)])
    manager.client.get_letters = api.as_mock()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 450
    assert len(_stored(manager)["letters"]) == 450
    assert len(api.calls) == 3


async def test_boundary_letter_with_later_updated_at_does_not_lose_letters(manager):
    """Paging by the boundary's updated_at skipped higher ids with an older updated_at."""
    letters = [_letter(i, _ts(i)) for i in range(1, 451)]
    letters[199] = {**letters[199], "updated_at": _ts(1000)}  # letter 200 favorited later
    api = FakeLetterApi(letters)
    manager.client.get_letters = api.as_mock()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 450

    assert [row["id"] for row in _stored(manager)["letters"]] == list(range(1, 451))
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(1000)
    assert api.calls[1:] == [_ts(200), _ts(399)]  # paged by created_at, not updated_at


async def test_failed_image_holds_cursor_and_retries(manager):
    api = FakeLetterApi([_letter(1, _ts(1))])
    api.letters[0]["file"] = "https://cdn/fail/1.jpg"
    manager.client.get_letters = api.as_mock()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 1
    assert "letters_updated_at" not in (manager.sync_state.get("90_90") or {})

    api.letters[0]["file"] = "https://cdn/ok/1.jpg"
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert api.calls[-1] is None  # no cursor yet: full refetch gives a fresh signed URL
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(1)


async def test_letter_without_image(manager):
    manager.client.get_letters = FakeLetterApi([_letter(1, _ts(1), url=False)]).as_mock()
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 1
    assert _stored(manager)["letters"][0]["image_file"] is None
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(1)


async def test_corrupt_letters_json_is_quarantined_and_rebuilt_in_the_same_call(manager):
    import time_machine

    api = FakeLetterApi([_letter(1, _ts(1))])
    manager.client.get_letters = api.as_mock()
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    member_dir = manager._member_dir_path(GROUP, MEMBER)
    path = member_dir / "letters.json"
    path.write_text("{bad", encoding="utf-8")
    api.letters.append(_letter(2, _ts(2)))

    with time_machine.travel("2026-11-03T12:34:56Z", tick=False):
        assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 2

    kept = member_dir / "letters.json.corrupt-20261103T123456Z"
    assert kept.read_text(encoding="utf-8") == "{bad"
    assert [row["id"] for row in _stored(manager)["letters"]] == [1, 2]
    assert api.calls[-1] is None  # cursor dropped: full refetch
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(2)


async def test_corrupt_file_racing_in_after_the_check_resets_cursor_and_returns_zero(manager, monkeypatch):
    api = FakeLetterApi([_letter(1, _ts(1))])
    manager.client.get_letters = api.as_mock()
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    path = manager._member_dir_path(GROUP, MEMBER) / "letters.json"
    path.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(SyncManager, "_quarantine_corrupt_letters", staticmethod(lambda p: False))

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert path.read_text(encoding="utf-8") == "{bad"
    assert "letters_updated_at" not in manager.sync_state["90_90"]


async def test_cursor_without_letters_json_forces_a_full_refetch(manager):
    api = FakeLetterApi([_letter(1, _ts(1)), _letter(2, _ts(2))])
    manager.client.get_letters = api.as_mock()
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    (manager._member_dir_path(GROUP, MEMBER) / "letters.json").unlink()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 2
    assert api.calls[-1] is None
    assert [row["id"] for row in _stored(manager)["letters"]] == [1, 2]


async def test_stale_cursor_is_dropped_even_if_the_refetch_is_incomplete(manager):
    manager.sync_state["90_90"] = {"letters_updated_at": _ts(50)}  # but no letters.json
    manager.client.get_letters = AsyncMock(side_effect=RuntimeError("boom"))

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert "letters_updated_at" not in manager.sync_state["90_90"]


async def test_non_string_timestamps_are_treated_as_missing(manager):
    odd_updated = {**_letter(2, _ts(2)), "updated_at": 12345}
    odd_created = {**_letter(3, _ts(3)), "created_at": None}
    odd_both = {**_letter(4, _ts(4)), "created_at": 7, "updated_at": ["x"]}
    manager.client.get_letters = AsyncMock(return_value=[_letter(1, _ts(1)), odd_updated, odd_created, odd_both])

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 4

    rows = {row["id"]: row for row in _stored(manager)["letters"]}
    assert rows[2]["updated_at"] is None and rows[4]["created_at"] is None and rows[4]["updated_at"] is None
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(3)  # newest string updated_at


async def test_non_string_created_at_pages_by_updated_at(manager):
    page = [_letter(i, _ts(i)) for i in range(1, 201)]
    page[-1] = {**page[-1], "created_at": 123}
    manager.client.get_letters = AsyncMock(side_effect=[page, [page[-1]]])

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 200
    assert manager.client.get_letters.await_args_list[1].kwargs["updated_from"] == _ts(200)


async def test_non_string_stored_cursor_is_ignored(manager):
    manager.sync_state["90_90"] = {"letters_updated_at": 99}
    manager.client.get_letters = FakeLetterApi([_letter(1, _ts(1))]).as_mock()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 1
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(1)


async def test_auth_error_propagates_and_other_errors_are_swallowed(manager):
    manager.client.get_letters = AsyncMock(side_effect=SessionExpiredError("expired"))
    with pytest.raises(SessionExpiredError):
        await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    manager.client.get_letters = AsyncMock(side_effect=RuntimeError("boom"))
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0


async def test_nothing_fetched_writes_nothing(manager):
    manager.client.get_letters = FakeLetterApi([]).as_mock()
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert not (manager._member_dir_path(GROUP, MEMBER) / "letters.json").exists()


async def test_write_failure_returns_zero_and_downloads_nothing(manager, monkeypatch):
    manager.client.get_letters = FakeLetterApi([_letter(1, _ts(1))]).as_mock()
    monkeypatch.setattr(SyncManager, "_atomic_write_json", staticmethod(lambda path, data: False))

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    manager.client.download_file.assert_not_called()
    assert "letters_updated_at" not in (manager.sync_state.get("90_90") or {})


async def test_refetched_letter_without_url_keeps_stored_image(manager):
    api = FakeLetterApi([_letter(1, _ts(1))])
    manager.client.get_letters = api.as_mock()
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)

    api.letters[0] = {**_letter(1, _ts(5), url=False), "is_favorite": True}
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    stored = _stored(manager)["letters"][0]
    assert stored["image_file"] == "letters/1.jpg" and stored["is_favorite"] is True


async def test_repeated_page_stops_pagination(manager):
    page = [_letter(i, _ts(1)) for i in range(1, 201)]
    manager.client.get_letters = AsyncMock(return_value=page)

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 200
    assert manager.client.get_letters.await_count == 2  # second page added no new ids


async def test_logs_never_contain_letter_text_or_token(manager):
    from structlog.testing import capture_logs

    manager.client.get_letters = FakeLetterApi([_letter(1, _ts(1))]).as_mock()
    with capture_logs() as logs:
        await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    dumped = json.dumps(logs, ensure_ascii=False)
    assert "SECRET-TOKEN" not in dumped and "手紙" not in dumped and "cdn/" not in dumped


async def test_empty_later_page_stores_letters_but_holds_cursor(manager):
    """An empty page after a full one means a swallowed fetch error, not the end."""
    page = [_letter(i, _ts(i)) for i in range(1, 201)]
    manager.client.get_letters = AsyncMock(side_effect=[page, []])

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 200
    assert len(_stored(manager)["letters"]) == 200
    assert manager.client.download_file.await_count == 200
    assert "letters_updated_at" not in (manager.sync_state.get("90_90") or {})


async def test_page_cap_holds_cursor(manager, monkeypatch):
    monkeypatch.setattr(SyncManager, "_LETTER_PAGE", 2)
    monkeypatch.setattr(SyncManager, "_LETTER_MAX_PAGES", 2)
    manager.client.get_letters = FakeLetterApi([_letter(i, _ts(i)) for i in range(1, 8)]).as_mock()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 3  # page 2 overlaps letter 2
    assert len(_stored(manager)["letters"]) == 3
    assert "letters_updated_at" not in (manager.sync_state.get("90_90") or {})


async def test_repeated_full_page_holds_cursor(manager):
    manager.client.get_letters = AsyncMock(return_value=[_letter(i, _ts(1)) for i in range(1, 201)])

    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    assert "letters_updated_at" not in (manager.sync_state.get("90_90") or {})


async def test_non_integer_letter_id_is_skipped(manager, tmp_path):
    evil = {**_letter(2, _ts(2)), "id": "../../evil"}
    missing = {k: v for k, v in _letter(3, _ts(3)).items() if k != "id"}
    manager.client.get_letters = AsyncMock(return_value=[_letter(1, _ts(1)), evil, missing])

    from structlog.testing import capture_logs

    with capture_logs() as logs:
        assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 1

    assert [row["id"] for row in _stored(manager)["letters"]] == [1]
    written = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    assert not any("evil" in str(p) for p in written)
    assert not list(tmp_path.parent.glob("evil*"))
    downloaded = [call.args[2] for call in manager.client.download_file.await_args_list]
    member_dir = manager._member_dir_path(GROUP, MEMBER)
    assert downloaded == [member_dir / "letters" / "1.jpg"]
    assert "evil" not in json.dumps(logs, ensure_ascii=False)
    assert any(e.get("id_types") for e in logs)


@pytest.mark.parametrize("bad_id", ["12", 1.9, True, None], ids=["str", "float", "bool", "none"])
async def test_only_true_integer_ids_are_accepted(manager, bad_id):
    bad = {**_letter(2, _ts(2)), "id": bad_id}
    missing = {k: v for k, v in _letter(3, _ts(3)).items() if k != "id"}
    manager.client.get_letters = AsyncMock(return_value=[_letter(1, _ts(1)), bad, missing])

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 1

    assert [row["id"] for row in _stored(manager)["letters"]] == [1]
    downloaded = [call.args[2].name for call in manager.client.download_file.await_args_list]
    assert downloaded == ["1.jpg"]


async def test_mixed_int_and_string_ids_do_not_double_count(manager):
    """The same letter returned as 12 and "12" (overlap) is stored once."""
    twin = {**_letter(12, _ts(12)), "id": "12"}
    manager.client.get_letters = AsyncMock(return_value=[_letter(12, _ts(12)), twin])

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 1
    assert [row["id"] for row in _stored(manager)["letters"]] == [12]


async def test_quiet_member_keeps_cursor_with_an_empty_letters_json(manager):
    """A member without letters of its own in a complete fetch gets an empty
    letters.json, so the cursor survives and the next call does not refetch the
    whole room."""
    api = FakeLetterApi([_letter(1, _ts(1), member_id=7), _letter(2, _ts(2), member_id=7)])
    manager.client.get_letters = api.as_mock()

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(2)
    assert _stored(manager) == {"version": 1, "letters": []}

    api.letters.append(_letter(3, _ts(3), member_id=7))
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert api.calls[-1] == _ts(2)  # cursor kept: no full refetch
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(3)
    assert _stored(manager) == {"version": 1, "letters": []}


async def test_quiet_member_incomplete_fetch_writes_nothing(manager):
    page = [_letter(i, _ts(i), member_id=7) for i in range(1, 201)]
    manager.client.get_letters = AsyncMock(side_effect=[page, []])  # empty later page: incomplete

    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert not (manager._member_dir_path(GROUP, MEMBER) / "letters.json").exists()
    assert "letters_updated_at" not in (manager.sync_state.get("90_90") or {})


async def test_quiet_member_does_not_rewrite_existing_letters_json(manager):
    api = FakeLetterApi([_letter(1, _ts(1))])
    manager.client.get_letters = api.as_mock()
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    path = manager._member_dir_path(GROUP, MEMBER) / "letters.json"
    before = (path.stat().st_mtime_ns, _stored(manager))

    api.letters.append(_letter(2, _ts(2), member_id=7))  # only another member's letter is new
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert (path.stat().st_mtime_ns, _stored(manager)) == before
    assert manager.sync_state["90_90"]["letters_updated_at"] == _ts(2)


async def test_identical_second_sync_does_not_rewrite_letters_json(manager):
    manager.client.get_letters = FakeLetterApi([_letter(1, _ts(1))]).as_mock()
    await manager.sync_letters(AsyncMock(), GROUP, MEMBER)
    path = manager._member_dir_path(GROUP, MEMBER) / "letters.json"
    before = path.stat().st_mtime_ns

    manager.sync_state["90_90"].pop("letters_updated_at")  # force a full refetch
    assert await manager.sync_letters(AsyncMock(), GROUP, MEMBER) == 0
    assert path.stat().st_mtime_ns == before


async def test_letters_after_room_rename_share_the_renamed_folder(manager, tmp_path):
    """sync_letters must not create the new-name folder before the rename logic ran."""
    old_member_dir = tmp_path / "messages" / "90 Old Name" / "90 高井 俐香"
    old_member_dir.mkdir(parents=True)
    (old_member_dir / "messages.json").write_text('{"messages": []}', encoding="utf-8")
    manager.client.get_letters = FakeLetterApi([_letter(1, _ts(1))]).as_mock()

    renamed = {"id": 90, "name": "New Name"}
    assert await manager.sync_letters(AsyncMock(), renamed, MEMBER) == 1

    assert [p.name for p in (tmp_path / "messages").iterdir()] == ["90 New Name"]
    member_dir = tmp_path / "messages" / "90 New Name" / "90 高井 俐香"
    assert (member_dir / "messages.json").exists()
    assert (member_dir / "letters.json").exists()
    assert (member_dir / "letters" / "1.jpg").exists()
