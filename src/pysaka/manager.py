import asyncio
import inspect
import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, TypeVar

import aiofiles
import aiohttp
import structlog

from . import profile_images, roster
from .client import Client
from .exceptions import RefreshFailedError, SessionExpiredError
from .media import get_audio_metadata, get_media_dimensions
from .utils import (
    CANCELED_STATES,
    _normalized_type,
    _short_path,
    get_media_extension,
    media_file_is_present,
    normalize_message,
    sanitize_name,
)

logger = structlog.get_logger()

# Server states where the member withdrew the post, which legitimately strips its
# media (confirmed via the live timeline probe). ANY OTHER non-"published" state is
# treated as a potential real gap and surfaced in `unresolved`, so completeness is
# never overclaimed on an unexpected/transient state (e.g. "processing").
_MEDIA_STRIPPING_STATES = CANCELED_STATES

T = TypeVar("T")

# One lock per messages.json (or any JSON file updated via _locked_update). Thread
# locks, not asyncio locks: SakaDesk's favorite toggle and the sync write both run
# their file work in worker threads (asyncio.to_thread), which an asyncio.Lock
# would not serialise. In-process only — cross-process writers are excluded by the
# consumer (SakaDesk's DataDirLock).
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _error_origin(exc: BaseException) -> dict[str, str]:
    """Text-free origin of ``exc`` for logging; never includes the exception message.

    ``origin`` is the innermost traceback frame as ``<basename>:<lineno>:<function>``,
    ``pysaka_frame`` the innermost frame whose path has a ``pysaka`` component (same
    format), ``cause_type`` the type name of ``__cause__``. Keys that do not apply are
    omitted. The exception text and traceback can quote message content, so neither is used.
    """

    def label(frame: traceback.FrameSummary) -> str:
        return f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"

    out: dict[str, str] = {}
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        out["origin"] = label(frames[-1])
        for frame in reversed(frames):
            if "pysaka" in Path(frame.filename).parts:
                out["pysaka_frame"] = label(frame)
                break
    if exc.__cause__ is not None:
        out["cause_type"] = type(exc.__cause__).__name__
    return out


def _is_skipped_empty(msg: dict[str, Any]) -> bool:
    """True for a raw API message that ``prepare_messages`` skips instead of storing.

    A text message with no text and no media (``file``/``thumbnail``) whose state is
    not canceled: these are often system/metadata entries from the subscription. A
    canceled one is a withdrawn message, not metadata, and is kept so the withdrawal
    is recorded.
    """
    return (
        _normalized_type(msg.get("type")) == "text"
        and not msg.get("text")
        and not (msg.get("file") or msg.get("thumbnail"))
        and msg.get("state") not in CANCELED_STATES
    )


def _lock_for(path: Path) -> threading.Lock:
    """Return the process-wide lock for ``path`` (case-normalised on Windows)."""
    key = os.path.normcase(str(path.absolute().resolve()))
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock


class SyncManager:
    """
    Manages synchronization of messages and media for a specific client.

    Handles state tracking, message fetching, deduplication, and media downloading.
    """

    def __init__(self, client: Client, output_dir: Path):
        """
        Initialize the SyncManager.

        Args:
            client: Authenticated Client instance.
            output_dir: Directory to store synchronized data.
        """
        self.client = client
        self.output_dir = output_dir
        self.state_file = output_dir / "sync_state.json"
        self.sync_state: dict[str, dict[str, Any]] = {}
        # member id -> /members/{id} result for this manager's lifetime (None = unknown)
        self._member_cache: dict[int, Optional[dict[str, Any]]] = {}
        # sync_state key -> deferred completion waiting for finalize_deferred_media:
        # backfill (queued paths, every past message normalized) and letters
        # (missing card paths, newest fetched updated_at). Memory only: a run that
        # never finalizes leaves the flags and cursors unset.
        self._pending_backfill: dict[str, tuple[list[Path], bool]] = {}
        self._pending_letters: dict[str, tuple[list[Path], str]] = {}
        self.load_sync_state()

    def load_sync_state(self) -> None:
        """Load synchronization state from JSON file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    self.sync_state = json.load(f)
            except Exception as e:
                logger.error(
                    "Failed to load sync state", file=_short_path(self.state_file), error_type=type(e).__name__
                )
                self.sync_state = {}

    def save_sync_state(self) -> bool:
        """Save synchronization state to JSON file (atomic write).

        Uses a unique temp filename and retries os.replace to handle
        Windows file locking (antivirus, search indexer).

        Returns:
            True when the file was written, False when the write failed (the
            failure is logged; ``self.sync_state`` in memory is unchanged).
        """
        return self._atomic_write_json(self.state_file, self.sync_state)

    @staticmethod
    def _atomic_write_json(target: Path, data: Any, retries: int = 5) -> bool:
        """Write JSON data atomically with retry for Windows file locking.

        Synchronous + blocking (it retries with ``time.sleep``). Callers writing a
        *local* payload from an async path SHOULD offload via ``asyncio.to_thread``
        (see the messages.json / metadata writes). The shared ``sync_state`` writes
        deliberately do NOT offload: callers gather ``sync_member`` across groups, so
        running the mutate+serialize on the single event-loop thread is what keeps
        ``self.sync_state`` race-free — at the cost of a brief loop stall only on the
        rare Windows-lock retry.

        Returns True on success, False if the write/replace ultimately failed — so
        callers can avoid advancing persisted state past data that never hit disk.
        """
        tmp = target.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            last_err: Optional[Exception] = None
            for attempt in range(retries):
                try:
                    os.replace(tmp, target)
                    return True
                except OSError as e:
                    last_err = e
                    # Brief wait for the file lock to release before retrying.
                    if attempt < retries - 1:
                        time.sleep(0.05 * (attempt + 1))

            logger.error(
                "Failed to write file after retries",
                file=_short_path(target),
                error_type=type(last_err).__name__,
            )
            return False
        except Exception as e:
            logger.error("Failed to write file", file=_short_path(target), error_type=type(e).__name__)
            return False
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _move_corrupt_aside(path: Path) -> Path:
        """Rename ``path`` to ``<name>.corrupt-<UTC timestamp>`` and return the new path.

        Caller must hold the file's lock. Raises ``OSError`` if the rename fails.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = path.with_name(f"{path.name}.corrupt-{stamp}")
        path.replace(aside)
        return aside

    @staticmethod
    def _locked_update(
        path: Path,
        mutate: Callable[[Optional[Any]], tuple[Optional[Any], T]],
        *,
        on_corrupt: Literal["raise", "discard", "quarantine"] = "raise",
    ) -> tuple[bool, T]:
        """Read-modify-write a JSON file under its process-wide lock.

        Synchronous and blocking — call via ``asyncio.to_thread`` from async code.
        The lock is not re-entrant, so ``mutate`` must not call ``_locked_update``
        (or ``update_message_fields*``) on the same path — it would deadlock.
        ``mutate`` receives the parsed file (``None`` if the file is missing, or if
        it is unreadable and ``on_corrupt`` is not ``"raise"``) and returns
        ``(data, result)``. ``data is None`` means "nothing to write". Every writer
        of a given file must go through this method, otherwise a concurrent
        read-modify-write can overwrite the other's changes.

        ``on_corrupt`` decides what happens to a file that is not valid JSON:
        ``"raise"`` raises ``ValueError``; ``"discard"`` passes ``None`` to
        ``mutate`` and logs nothing (for callers that report the corrupt file
        themselves); ``"quarantine"`` renames it to
        ``<name>.corrupt-<UTC timestamp>`` (kept for inspection), logs a warning
        with the file name and error type, and passes ``None`` to ``mutate``.

        Returns:
            ``(write_ok, result)``. ``write_ok`` is False only when a write was
            attempted and ``_atomic_write_json`` failed.

        Raises:
            ValueError: the file is unreadable JSON and ``on_corrupt`` is ``"raise"``.
            OSError: reading the file, or renaming it aside, failed.
        """
        with _lock_for(path):
            current: Optional[Any] = None
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        current = json.load(f)
                except ValueError as e:
                    if on_corrupt == "raise":
                        raise
                    if on_corrupt == "quarantine":
                        aside = SyncManager._move_corrupt_aside(path)
                        logger.warning(
                            "Unreadable JSON file kept aside; rebuilding",
                            file=path.name,
                            kept_as=aside.name,
                            error_type=type(e).__name__,
                        )
                    current = None
            data, result = mutate(current)
            if data is None:
                return True, result
            return SyncManager._atomic_write_json(path, data), result

    @staticmethod
    def update_message_fields_sync(messages_file: Path, message_id: int, fields: dict[str, Any]) -> bool:
        """Set ``fields`` on one message of a ``messages.json``, under the file lock.

        Blocking — for callers already running in a worker thread. From async
        code use :meth:`update_message_fields`. Use one of these instead of reading
        and rewriting the file yourself: the lock keeps a sync running at the same
        moment from losing your change (or you its new messages). It does not
        protect the change from later syncs: a sync that re-fetches this message
        replaces its record with the API's, so a field the API returns (e.g.
        ``is_favorite``) is overwritten and any other field you set is dropped,
        except the ones in ``_LOCAL_DERIVED_FIELDS`` (``width``, ``height``,
        ``media_duration``, ``is_muted``, ``media_file``), which are carried over
        when the fresh record lacks them.

        ``message_id`` is compared as-is with the stored ids, so pass an ``int``
        (a ``str`` never matches).

        Returns:
            True if the message was found and the file was written; False if the
            file is missing, the id is absent, or the write failed.

        Raises:
            ValueError: the file exists but is unreadable JSON.
            OSError: the file exists but could not be read.
        """

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], bool]:
            if not isinstance(current, dict):
                return None, False
            for msg in current.get("messages", []):
                if msg.get("id") == message_id:
                    msg.update(fields)
                    return current, True
            return None, False

        wrote, found = SyncManager._locked_update(messages_file, mutate)
        return found and wrote

    @staticmethod
    async def update_message_fields(messages_file: Path, message_id: int, fields: dict[str, Any]) -> bool:
        """Async form of :meth:`update_message_fields_sync` (runs it in a thread).

        Same return value, ``message_id`` handling and exceptions (``ValueError``
        on unreadable JSON, ``OSError`` on read errors).
        """
        return await asyncio.to_thread(SyncManager.update_message_fields_sync, messages_file, message_id, fields)

    def update_sync_state(
        self, group_id: int, member_id: int, last_msg_id: int, count: int, last_ts: Optional[str] = None
    ) -> None:
        """
        Update state for a specific member after sync.

        Args:
            group_id: ID of the group/artist.
            member_id: ID of the member.
            last_msg_id: The highest message ID synced (diagnostic only).
            count: Total message count after merge.
            last_ts: The newest message's published_at timestamp (primary cursor).
        """
        key = f"{group_id}_{member_id}"
        # Merge, don't replace: other features keep their own keys in the entry
        # (e.g. backfill's past_backfilled_at), which a full replace would wipe on
        # every sync.
        entry = dict(self.sync_state.get(key) or {})
        entry.update(
            {
                "last_message_id": last_msg_id,
                "last_sync_ts": last_ts,
                "total_messages": count,
                "last_sync": datetime.now(timezone.utc).isoformat() + "Z",
            }
        )
        self.sync_state[key] = entry
        self.save_sync_state()

    def update_total_messages(self, group_id: int, member_id: int, count: int) -> None:
        """Update only ``total_messages`` for a member (cursor fields untouched)."""
        key = f"{group_id}_{member_id}"
        entry = dict(self.sync_state.get(key) or {})
        entry["total_messages"] = count
        self.sync_state[key] = entry
        self.save_sync_state()

    def get_last_ts(self, group_id: int, member_id: int) -> Optional[str]:
        """
        Get the last synced timestamp cursor for a member.

        Returns:
            ISO timestamp string or None if never synced.
        """
        key = f"{group_id}_{member_id}"
        state = self.sync_state.get(key)
        if state:
            return state.get("last_sync_ts")
        return None

    def get_last_id(self, group_id: int, member_id: int) -> Optional[int]:
        """Get the last synced message ID (legacy, kept for diagnostics)."""
        key = f"{group_id}_{member_id}"
        state = self.sync_state.get(key)
        if state:
            return state.get("last_message_id")
        return None

    @staticmethod
    def _room_state_key(group_id: int) -> str:
        # Never collides with "<gid>_<mid>" member keys.
        return f"room_{group_id}"

    def history_scanned(self, group_id: int) -> bool:
        """True once the room's one-time full-history scan has completed.

        Stored as ``sync_state["room_<gid>"]["history_scanned_at"]``; deleting
        ``sync_state.json`` (a force resync) clears it, so the next sync scans again.
        """
        return bool((self.sync_state.get(self._room_state_key(group_id)) or {}).get("history_scanned_at"))

    def mark_history_scanned(self, group_id: int) -> bool:
        """Record that the room's full history is archived (call only after
        :meth:`room_archive_complete` returned True for the scan's timeline).

        Returns:
            True when ``sync_state.json`` was saved. False when the save failed:
            a warning ``Room history scan not saved`` is logged, the flag stays
            set in memory (:meth:`history_scanned` is True for this manager), and
            the room is scanned again after a restart.
        """
        key = self._room_state_key(group_id)
        entry = dict(self.sync_state.get(key) or {})
        entry["history_scanned_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.sync_state[key] = entry
        if not self.save_sync_state():
            logger.warning("Room history scan not saved", group_id=group_id)
            return False
        logger.info("Room history scan recorded", group_id=group_id, history_scanned_at=entry["history_scanned_at"])
        return True

    async def room_archive_complete(self, group: dict[str, Any], messages: list[dict[str, Any]]) -> bool:
        """True when every message id in ``messages`` that sync stores is in its author's ``messages.json``.

        Authors are taken with :func:`pysaka.roster.author_id` (member 0 for messages
        without ``member_id``). Messages that sync skips (a text message with no text
        and no media whose state is not canceled), entries without an ``id`` and
        non-dict entries are not expected. A stored ``"messages": null`` counts as
        empty. False when an author's ids are missing, or when a ``messages.json`` is
        unreadable, not a JSON object, or has a ``messages`` value that is not a list
        (a warning with group id, member id and the reason is logged). Catches what a
        raised exception would not: a write that failed (``sync_member`` returns 0)
        or a message that failed to normalize.
        """
        return await asyncio.to_thread(self._room_archive_complete_sync, group, messages)

    def _room_archive_complete_sync(self, group: dict[str, Any], messages: list[dict[str, Any]]) -> bool:
        gid = group["id"]
        wanted: dict[int, set[int]] = {}
        skipped = 0
        for m in messages:
            if not isinstance(m, dict) or m.get("id") is None or _is_skipped_empty(m):
                skipped += 1
                continue
            wanted.setdefault(roster.author_id(m), set()).add(m["id"])
        messages_dir = self.output_dir / "messages"
        group_dirs = list(messages_dir.glob(f"{gid} *")) if messages_dir.exists() else []
        for aid, ids in wanted.items():
            stored: set[Any] = set()
            for group_dir in group_dirs:
                for path in group_dir.glob(f"{aid} */messages.json"):
                    reason: Optional[str] = None
                    error_type: Optional[str] = None
                    try:
                        with open(path, encoding="utf-8") as f:
                            data = json.load(f)
                    except (OSError, ValueError) as e:
                        reason = "unreadable"
                        error_type = type(e).__name__
                    else:
                        if not isinstance(data, dict):
                            reason = "not_an_object"
                        elif not isinstance(data.get("messages") or [], list):
                            reason = "messages_not_a_list"
                    if reason is not None:
                        logger.warning(
                            "Room archive unreadable",
                            group_id=gid,
                            member_id=aid,
                            reason=reason,
                            **({"error_type": error_type} if error_type else {}),
                        )
                        return False
                    stored |= {m.get("id") for m in data.get("messages") or [] if isinstance(m, dict)}
            if not ids <= stored:
                logger.warning("Room archive incomplete", group_id=gid, member_id=aid, missing=len(ids - stored))
                return False
        logger.debug(
            "Room archive complete",
            group_id=gid,
            checked=sum(len(ids) for ids in wanted.values()),
            authors=len(wanted),
            skipped=skipped,
        )
        return True

    def _member_dir_path(self, group: dict[str, Any], member: dict[str, Any]) -> Path:
        """The on-disk directory of a member's archive (same naming as sync_member)."""
        return (
            self.output_dir
            / "messages"
            / f"{group['id']} {sanitize_name(group['name'])}"
            / f"{member['id']} {sanitize_name(member['name'])}"
        )

    def _ensure_group_dir(self, group: dict[str, Any]) -> Path:
        """Return ``messages/<gid> <name>``, renaming a room folder left under an old name.

        If the room was renamed on the server (e.g. "12th Single" -> "16th Single")
        and only a ``messages/<gid> <old name>`` folder exists, it is renamed
        instead of leaving a duplicate, and the ``media_file`` paths under the old
        folder are rewritten in every member folder's ``messages.json`` (see
        :meth:`_rewrite_media_paths`). Call this before creating anything under
        the group directory; it does not create the directory itself.

        Raises:
            OSError: the rename failed (nothing was changed).
        """
        gid = group["id"]
        messages_dir = self.output_dir / "messages"
        group_dir = messages_dir / f"{gid} {sanitize_name(group['name'])}"
        if not group_dir.exists() and messages_dir.exists():
            for existing in messages_dir.iterdir():
                if existing.is_dir() and existing.name.startswith(f"{gid} "):
                    logger.info("Group renamed on server, renaming directory", old=existing.name, new=group_dir.name)
                    existing.rename(group_dir)
                    for member_dir in sorted(group_dir.iterdir()):
                        if (member_dir / "messages.json").is_file():
                            self._rewrite_media_paths(member_dir / "messages.json", existing, group_dir)
                    break
        return group_dir

    def _ensure_member_dir(self, group_dir: Path, member_id: int, name: str) -> Path:
        """Return ``group_dir/<mid> <name>``, renaming a member folder left under an old name.

        A member who changed their display name, or an author first archived under
        a fallback name (their id), keeps one folder instead of a duplicate, and the
        ``media_file`` paths under the old folder are rewritten in its
        ``messages.json`` (see :meth:`_rewrite_media_paths`). Only renames when the
        target does not exist yet; never creates the directory.

        Raises:
            OSError: the rename failed (a warning is logged; nothing was changed).
        """
        member_dir = group_dir / f"{member_id} {name}"
        if not member_dir.exists() and group_dir.exists():
            prefix = f"{member_id} "
            for existing in group_dir.iterdir():
                if existing.is_dir() and existing.name.startswith(prefix):
                    logger.info(
                        "Member renamed, renaming directory",
                        member_id=member_id,
                        old=existing.name,
                        new=member_dir.name,
                    )
                    try:
                        existing.rename(member_dir)
                    except OSError as e:
                        logger.warning(
                            "Member folder rename failed",
                            member_id=member_id,
                            old=existing.name,
                            new=member_dir.name,
                            error_type=type(e).__name__,
                        )
                        raise
                    if (member_dir / "messages.json").is_file():
                        self._rewrite_media_paths(member_dir / "messages.json", existing, member_dir)
                    break
        return member_dir

    def _renamed_member_dir(self, group: dict[str, Any], member_id: int, name: str) -> Optional[Path]:
        """:meth:`_ensure_group_dir` + :meth:`_ensure_member_dir`, or None when a rename failed.

        A failed rename (``OSError``, e.g. a file locked on Windows) is logged as a
        warning with group id, member id and the error type; the caller must then
        skip the member without writing anything or moving a cursor, so the next
        sync retries the rename instead of creating a second folder.
        """
        try:
            return self._ensure_member_dir(self._ensure_group_dir(group), member_id, name)
        except OSError as e:
            logger.warning(
                "Sync skipped: folder rename failed",
                group_id=group["id"],
                member_id=member_id,
                error_type=type(e).__name__,
            )
            return None

    def _rewrite_media_paths(self, messages_file: Path, old_dir: Path, new_dir: Path) -> None:
        """After renaming ``old_dir`` to ``new_dir``, point ``media_file`` values at the new folder.

        ``media_file`` is relative to ``output_dir`` with the separator of the OS
        that wrote it (``\\`` on Windows, ``/`` elsewhere), so the old prefix is
        matched with either separator. Only the prefix is replaced, in the record's
        own separator style; a value that does not start with the old folder, and
        every other field, are left as they are. Runs under the file lock. A failure
        is logged (file and error type) and not raised: the folder is already renamed.
        """
        old_prefix = old_dir.relative_to(self.output_dir).as_posix() + "/"
        new_prefix = new_dir.relative_to(self.output_dir).as_posix()

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], int]:
            messages = current.get("messages") if isinstance(current, dict) else None
            if not isinstance(messages, list):
                return None, 0
            updated = 0
            for msg in messages:
                value = msg.get("media_file") if isinstance(msg, dict) else None
                if not isinstance(value, str) or not value.replace("\\", "/").startswith(old_prefix):
                    continue
                sep = value[len(old_prefix) - 1]
                msg["media_file"] = new_prefix.replace("/", sep) + sep + value[len(old_prefix) :]
                updated += 1
            return (current if updated else None), updated

        rel_file = str(messages_file.relative_to(self.output_dir))
        try:
            wrote, updated = self._locked_update(messages_file, mutate, on_corrupt="discard")
        except OSError as e:
            logger.warning("Media paths not updated after folder rename", file=rel_file, error_type=type(e).__name__)
            return
        if not wrote:
            logger.warning("Media paths not updated after folder rename", file=rel_file, error_type="write_failed")
        elif updated:
            logger.info("Media paths updated after folder rename", file=rel_file, updated=updated)

    @staticmethod
    def _type_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"text": 0, "video": 0, "picture": 0, "voice": 0}
        for msg in messages:
            mtype = msg.get("type", "text")
            if mtype in counts:
                counts[mtype] += 1
        return counts

    @staticmethod
    async def _notify_new_messages(
        callback: Callable[[dict[str, Any], list[dict[str, Any]]], Any],
        member_info: dict[str, Any],
        new_messages: list[dict[str, Any]],
    ) -> None:
        """Invoke a consumer callback; its failures must never break a sync.

        Only the exception type is logged: its text or traceback may quote the
        message text the callback was handling.
        """
        try:
            result = callback(member_info, new_messages)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error(
                "on_new_messages callback failed",
                member_id=member_info.get("member_id"),
                error_type=type(e).__name__,
            )

    async def sync_member(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        media_queue: list[dict[str, Any]],
        progress_callback: Optional[Any] = None,
        prefetched_messages: Optional[list[dict[str, Any]]] = None,
        *,
        on_new_messages: Optional[Callable[[dict[str, Any], list[dict[str, Any]]], Any]] = None,
        ignore_cursor: bool = False,
    ) -> int:
        """
        Syncs messages for a member, prepares the media queue, and archives the
        member's profile images.

        Args:
            session: Active aiohttp session.
            group: Group object dict.
            member: Member object dict.
            media_queue: List to append media download tasks to.
            progress_callback: Optional callback for progress updates.
            prefetched_messages: Pre-fetched group timeline messages. When
                provided, the API call is skipped and messages are filtered
                from this list instead (using this member's own last_id).
            on_new_messages: Optional ``callback(member_info, new_messages)``
                (sync or async) called once after a successful write when at
                least one message was not on disk before. Not called during
                corrupt-file / count-mismatch recovery or when
                ``ignore_cursor`` is True. Exceptions it raises are
                logged (exception type only) and swallowed. The callback is
                awaited inline, so a slow callback delays that member's sync;
                hand heavy work off (e.g. schedule a task). ``member_info`` has
                ``group_id``, ``group_name``, ``member_id``, ``member_name``,
                ``member_dir``, ``phone_image`` and ``initial_sync``. ``initial_sync`` is True
                when ``messages.json`` did not exist before this write, in which
                case ``new_messages`` is the whole fetched history, not fresh
                posts; consumers sending notifications should ignore those calls.
                The callback runs before the profile-image step, so a just-changed
                ``phone_image`` may not be archived yet; use the ``phone_image``
                URL from ``member_info``.
            ignore_cursor: Take all of this member's rows from ``prefetched_messages``
                (or a full fetch) instead of only those at/after the cursor. Used by a
                room's one-time history scan so rows older than the cursor (e.g.
                canceled ones) are merged. Merging is idempotent; the cursor is
                recomputed from the merged file as usual, except that only messages
                published at or after the previous cursor can hold it back: an older
                message whose media is still missing, or that failed to normalize, is
                still queued (or logged) but does not pull the cursor back to its date
                (``room_archive_complete`` and Verify & Fix cover it). Without a
                previous cursor every message can hold it back, as in a normal sync.
                ``on_new_messages`` is not called for any row merged in this call,
                including genuinely new posts that arrived since the last sync, so a
                consumer that notifies should not rely on scan runs.

        Messages without a ``member_id`` belong to member 0, the room author (see
        :mod:`pysaka.roster`). Member 0 has no profile images, so none are
        archived for it. A member folder left under an old name (or a room folder
        left under an old room name) is renamed to the current name instead of
        creating a second folder, and ``media_file`` paths into the old folder are
        rewritten in the affected ``messages.json`` files.

        If a room or member folder left under an old name cannot be renamed
        (``OSError``, e.g. a file locked on Windows), a warning with group id,
        member id and error type is logged and 0 is returned: nothing is written,
        the cursor is untouched and profile images are skipped, so the next sync
        retries the rename.

        Returns:
            Number of messages processed in this run (includes re-fetched
            overlap, so it is not a count of new messages).
        """
        processed = await self._sync_member_messages(
            session,
            group,
            member,
            media_queue,
            progress_callback,
            prefetched_messages,
            on_new_messages=on_new_messages,
            ignore_cursor=ignore_cursor,
        )
        if processed is None:
            # A folder rename failed: archiving profile images now would create the
            # new folder next to the old one and split the archive.
            return 0
        if member["id"] != roster.ROOM_AUTHOR_ID:
            await self._archive_profile_images_safely(session, group, member)
        else:
            logger.debug("Profile images skipped: room author", group_id=group["id"], member_id=member["id"])
        return processed

    _MEMBER_PROFILE_KEYS = ("thumbnail", "phone_image", "portrait")

    async def extra_room_members(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        api_members: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        name_hints: Optional[dict[int, dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        """Members to sync for this room **in addition to** ``api_members``.

        ``/groups/{id}/members`` is the room's current lineup; earlier lineups and
        members posting in official rooms are missing from it, and sync would drop
        their messages. This returns one member dict per author of ``messages``
        not in ``api_members`` (ascending id), then member 0 (the room author,
        ``pysaka.roster.room_author``) when a message has no usable ``member_id``
        (absent, or not an int / decimal string) and 0 is not in ``api_members``.
        Rows sync skips as empty (non-canceled text with no text and no media) are
        ignored, so they never create an author or an empty folder.

        A name is resolved from, in order: ``name_hints`` (id -> member dict, e.g.
        every room's member list fetched in this sync); the member block of a local
        ``messages/* */<id> */messages.json`` (only when its ``name`` is a non-empty
        string other than the fallback ``str(id)``); ``Client.get_member`` (cached per manager); else ``str(id)``. A
        message is never dropped because its author's name is unknown; a later sync
        that learns the name renames the folder.

        Raises:
            ApiError, SessionExpiredError, RefreshFailedError: ``ApiError``
                (including ``RateLimitedError``), ``SessionExpiredError`` or
                ``RefreshFailedError`` from ``Client.get_member``; none is cached.
        """
        hints = name_hints or {}
        gid = group["id"]
        # Rows sync skips as empty (see _is_skipped_empty) are never stored, so their
        # author must not get a folder: an empty "<mid> <name>" or "0 <room>" folder
        # would make a 1:1 room look like a group chat.
        stored = [m for m in messages if not _is_skipped_empty(m)]
        if len(stored) != len(messages):
            logger.debug(
                "Skipped-empty rows ignored for extra members", group_id=gid, ignored=len(messages) - len(stored)
            )
        messages = stored
        # author_id maps a member_id it cannot read to the room author; say so, or a
        # malformed id would silently merge a member's messages into member 0.
        unusable = [
            type(m.get("member_id")).__name__
            for m in messages
            if m.get("member_id") is not None
            and roster.author_id(m) == roster.ROOM_AUTHOR_ID
            and (isinstance(m["member_id"], bool) or m["member_id"] not in (0, "0"))
        ]
        if unusable:
            logger.warning(
                "Messages with unusable member_id attributed to room author",
                group_id=gid,
                count=len(unusable),
                id_types=sorted(set(unusable)),
            )
        extra_ids = roster.extra_author_ids((m["id"] for m in api_members), messages)
        result: list[dict[str, Any]] = []
        for aid in extra_ids:
            if aid == roster.ROOM_AUTHOR_ID:
                result.append(roster.room_author(group))
                logger.debug("Room member resolved", group_id=gid, member_id=aid, source="room")
            else:
                result.append(await self._resolve_member(session, aid, hints, group_id=gid))
        if extra_ids:
            logger.info("Extra room members", group_id=gid, member_ids=extra_ids, count=len(extra_ids))
        return result

    async def _resolve_member(
        self,
        session: aiohttp.ClientSession,
        member_id: int,
        hints: dict[int, dict[str, Any]],
        *,
        group_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Resolve one extra author's member dict; logs the source (hint/local/api/fallback)."""
        hinted = hints.get(member_id)
        if hinted and hinted.get("name"):
            logger.debug("Room member resolved", group_id=group_id, member_id=member_id, source="hint")
            return dict(hinted)
        local = await asyncio.to_thread(self._local_member_block, member_id)
        if local:
            logger.debug("Room member resolved", group_id=group_id, member_id=member_id, source="local")
            return local
        cached_lookup = member_id in self._member_cache
        if not cached_lookup:
            fetched = await self.client.get_member(session, member_id)
            self._member_cache[member_id] = fetched if fetched and fetched.get("name") else None
        cached = self._member_cache[member_id]
        if cached:
            logger.debug(
                "Room member resolved",
                group_id=group_id,
                member_id=member_id,
                source="api",
                cached=cached_lookup,
            )
            return {"id": member_id, "name": cached["name"], **{k: cached.get(k) for k in self._MEMBER_PROFILE_KEYS}}
        logger.warning(
            "Room member name unknown, using id as name",
            group_id=group_id,
            member_id=member_id,
            source="fallback",
            cached=cached_lookup,
        )
        return {"id": member_id, "name": str(member_id)}

    def _local_member_block(self, member_id: int) -> Optional[dict[str, Any]]:
        """Member block of any archived room folder for ``member_id`` with a real name.

        A real name is a non-empty string other than the fallback ``str(member_id)``.
        """
        messages_dir = self.output_dir / "messages"
        if not messages_dir.exists():
            return None
        for path in messages_dir.glob(f"* */{member_id} */messages.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    block = json.load(f).get("member")
            except (OSError, ValueError, AttributeError) as e:
                logger.warning(
                    "Local member block unreadable",
                    member_id=member_id,
                    file=str(path.relative_to(self.output_dir)),
                    error_type=type(e).__name__,
                )
                continue
            name = block.get("name") if isinstance(block, dict) else None
            if isinstance(name, str) and name and name != str(member_id):
                return {"id": member_id, "name": name, **{k: block.get(k) for k in self._MEMBER_PROFILE_KEYS}}
        return None

    async def backfill_past_messages(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        media_queue: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        """Merge the room's pre-subscription messages into the member's archive.

        Runs once per member: once ``past_backfilled_at`` is set in the member's
        sync_state entry, later calls return immediately. Requires a prior
        successful :meth:`sync_member` (no entry or no ``messages.json`` →
        skipped). Fetches its own media (or queues it for the caller, see
        *Deferred media*), because Verify & Fix takes fresh URLs from the
        timeline, which never contains these messages. The flag is set
        only when the request succeeded, no past message failed to normalize and
        every queued file is on disk; otherwise it stays unset and later calls
        retry (merging is idempotent). Cursor fields are never touched and
        ``on_new_messages`` is never fired.

        Returns 0 for member 0 (the room author), which has no own timeline or
        letters.

        Deferred media: with ``media_queue`` (a list), the media items are
        appended to it in the :meth:`sync_member` item format instead of being
        downloaded here, so the caller fetches them with the rest of its queue
        (and merges their dimensions as usual). The flag is then not set by
        this call: :meth:`finalize_deferred_media`, called after the caller's
        downloads, sets it under the same rule. When nothing had to be queued,
        the usual rule applies here at once (the flag is set unless a past
        message failed to normalize). Call it at most once per member before
        finalizing: a second call queues the same files again. With ``None``
        (the default) the media is downloaded here, as before.

        Args:
            session: Active aiohttp session.
            group: Group dict (``id``, ``name``).
            member: Member dict (``id``, ``name``).
            media_queue: Optional caller queue for deferred media downloads.

        Returns:
            Number of messages that were not in the archive before.

        Raises:
            SessionExpiredError: authentication failed (also
                ``RefreshFailedError``); the flag is left unset.
            OSError: reading or writing ``messages.json`` or its media failed
                after the fetch (other unexpected errors after the fetch also
                propagate), so callers should wrap the call. These do not raise
                and leave the flag unset: a failed fetch (a warning is logged and
                0 returned) and, when this member has past messages to merge, a
                ``messages.json`` that is unreadable JSON or not a JSON object
                (left untouched; a warning is logged, with the found type for a
                non-object; 0 returned) or whose write failed (logged by the
                write; 0 returned).
        """
        gid, mid = group["id"], member["id"]
        if mid == roster.ROOM_AUTHOR_ID:
            logger.debug("Backfill skipped: room author", group_id=gid, member_id=mid)
            return 0
        key = f"{gid}_{mid}"
        entry = self.sync_state.get(key)
        if not entry or entry.get("past_backfilled_at"):
            return 0
        member_dir = self._member_dir_path(group, member)
        messages_file = member_dir / "messages.json"
        if not messages_file.exists():
            return 0

        try:
            past = await self.client.get_past_messages(session, gid, strict=True)
        except (SessionExpiredError, RefreshFailedError):
            raise
        except Exception as e:
            logger.warning("Past message fetch failed", group_id=gid, error_type=type(e).__name__)
            return 0
        if past is None:
            logger.warning("Past message fetch failed", group_id=gid)
            return 0

        mine = [m for m in past if roster.author_id(m) == mid]
        queue: list[dict[str, Any]] = []
        processed, earliest_failed_ts, _ = self.prepare_messages(mine, member_dir, queue)

        added = 0
        if processed:

            def mutate(current: Optional[Any]) -> tuple[Optional[Any], tuple[int, int, Optional[str]]]:
                if not isinstance(current, dict):
                    # None: the file vanished after the existence check above.
                    return None, (0, -1, None if current is None else type(current).__name__)
                base = current.get("messages", [])
                base_ids = {m.get("id") for m in base}
                new_count = sum(1 for m in processed if m["id"] not in base_ids)
                merged = self._merge_messages(base, processed, group_id=gid, member_id=mid)
                merged.sort(key=lambda x: x.get("timestamp") or "")
                current["messages"] = merged
                current["total_messages"] = len(merged)
                current["message_type_counts"] = self._type_counts(merged)
                return current, (new_count, len(merged), None)

            try:
                wrote, (added, total, found_type) = await asyncio.to_thread(self._locked_update, messages_file, mutate)
            except ValueError:
                logger.warning("Backfill skipped: messages.json unreadable", group_id=gid, member_id=mid)
                return 0
            if found_type is not None:
                logger.warning(
                    "Backfill skipped: messages.json is not an object",
                    group_id=gid,
                    member_id=mid,
                    found_type=found_type,
                )
                return 0
            if not wrote or total < 0:
                return 0
            self.update_total_messages(gid, mid, total)

        if media_queue is not None and queue:
            # Deferred: the caller downloads these with its own queue, then
            # finalize_deferred_media applies the completion rule below.
            media_queue.extend(queue)
            self._pending_backfill[key] = ([item["path"] for item in queue], earliest_failed_ts is None)
            logger.info(
                "Past messages backfilled",
                group_id=gid,
                member_id=mid,
                added=added,
                complete=False,
                deferred=len(queue),
            )
            return added

        metadata = await self.process_media_queue(session, queue)
        if metadata.get(member_dir):
            await self.update_message_metadata(messages_file, metadata[member_dir])

        complete = earliest_failed_ts is None and all(media_file_is_present(item["path"]) for item in queue)
        if complete:
            self.sync_state[key]["past_backfilled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.save_sync_state()
        logger.info("Past messages backfilled", group_id=gid, member_id=mid, added=added, complete=complete)
        return added

    _LETTER_PAGE = 200
    _LETTER_MAX_PAGES = 50

    @staticmethod
    def _is_letter_id(value: Any) -> bool:
        """A letter id is a true integer (not a bool, str or float)."""
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _timestamp_text(value: Any) -> str:
        """``value`` if it is a string, else ``""`` (a non-string timestamp is missing)."""
        return value if isinstance(value, str) else ""

    @staticmethod
    def _quarantine_corrupt_letters(path: Path) -> bool:
        """Move an unreadable ``letters.json`` aside as ``letters.json.corrupt-<UTC>``.

        Blocking, takes the file's lock. Returns True when the file existed but
        was not valid JSON and was renamed (kept for inspection), else False.
        """
        with _lock_for(path):
            if not path.exists():
                return False
            try:
                with open(path, encoding="utf-8") as f:
                    json.load(f)
            except ValueError:
                SyncManager._move_corrupt_aside(path)
                return True
            return False

    async def sync_letters(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        media_queue: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        """Archive the account's sent letters to this member.

        Stores ``<member_dir>/letters.json`` (``client_token`` is never stored) and
        each letter-card image at ``<member_dir>/letters/<id>.<ext>``. Uses its own
        cursor ``letters_updated_at`` in the member's sync_state entry. The cursor
        moves to the newest ``updated_at`` among all letters fetched for the room
        only when the fetch was complete (it ended on a short page; an empty page
        after a full one, the page cap, or a page with no new letter are treated as
        incomplete) and every image of this member's letters is on disk, so a failed
        image is re-fetched with a fresh signed URL next run. When a complete
        fetch returned letters but none to this member and ``letters.json`` does
        not exist, an empty ``{"version": 1, "letters": []}`` is written so the
        cursor persists (an incomplete fetch writes nothing).
        Pages of 200 are chained by the last letter's ``created_at`` (not its
        ``updated_at``, which a later favourite would push past unseen letters).
        A letter whose ``id`` is not an integer (a string, float, bool or a missing
        id) is skipped and only the id's type name is logged. A non-string
        ``created_at``/``updated_at`` counts as missing (stored as ``None``).
        The cursor is used only while ``letters.json`` exists: if it is missing
        the cursor is dropped and everything is fetched again; if it is not valid
        JSON it is renamed to ``letters.json.corrupt-<UTC timestamp>`` (kept for
        inspection), the cursor is dropped, and the letters are rebuilt from a
        full fetch in the same call.
        Returns 0 for member 0 (the room author), which has no own timeline or
        letters.
        Works for expired subscriptions (the server still returns their letters).
        Never touches the message cursor and never fires ``on_new_messages``.
        Deferred images: with ``media_queue`` (a list), each card image not yet
        on disk is appended to it as ``{"url", "path", "timestamp": None,
        "media_type": "picture", "kind": "letter"}`` (no ``member_dir``, so
        :meth:`process_media_queue` returns no metadata for it and nothing
        reaches ``messages.json``) instead of being downloaded here. After a
        complete fetch whose images are not all on disk yet, the cursor move
        waits for :meth:`finalize_deferred_media`; with every image already
        present it moves at once. Call it at most once per member before
        finalizing. With ``None`` (the default) the images are downloaded here,
        as before. The log line ``Letters synced`` says what happened to the
        cursor: ``advanced``, ``unchanged``, ``held`` (an image is missing),
        ``deferred`` or ``incomplete`` (the fetch was incomplete).
        ``letters.json`` is written only when its content changes or it is
        created. Before that write (and only when there is something to write),
        a room folder left under an old room name and a member folder left under
        an old member name are renamed (with their ``media_file`` paths
        rewritten, as in :meth:`sync_member`), so letters and messages share one
        folder.

        Raises:
            SessionExpiredError, RefreshFailedError: auth failures propagate.
            OSError: creating the member directory, or file I/O after the fetch,
                failed (other unexpected errors after the fetch also propagate),
                so callers should wrap the call. A failed fetch does not raise: it
                logs and returns 0 (a corrupt ``letters.json`` is already set aside
                by then, so the next call rebuilds it). A failed folder rename does
                not raise either: a warning with group id, member id and error type
                is logged and 0 returned, nothing is written and the letters cursor
                is untouched.

        Returns:
            Number of letters that were not in ``letters.json`` before (all of
            them after a corrupt or missing file).
        """
        gid, mid = group["id"], member["id"]
        if mid == roster.ROOM_AUTHOR_ID:
            logger.debug("Letters skipped: room author", group_id=gid, member_id=mid)
            return 0
        key = f"{gid}_{mid}"
        member_dir = self._member_dir_path(group, member)
        letters_file = member_dir / "letters.json"
        if await asyncio.to_thread(self._quarantine_corrupt_letters, letters_file):
            logger.warning("letters.json unreadable; kept aside and rebuilding", group_id=gid, member_id=mid)
        # A cursor is only trustworthy next to the letters.json it describes.
        stored_cursor = (self.sync_state.get(key) or {}).get("letters_updated_at")
        cursor = stored_cursor if isinstance(stored_cursor, str) and stored_cursor and letters_file.exists() else None
        if stored_cursor is not None and cursor is None:
            entry = dict(self.sync_state[key])
            entry.pop("letters_updated_at")
            self.sync_state[key] = entry
            self.save_sync_state()
        fetched: dict[int, dict[str, Any]] = {}
        bad_id_types: set[str] = set()
        updated_from = cursor
        complete = False
        try:
            for page_no in range(self._LETTER_MAX_PAGES):
                page = await self.client.get_letters(session, gid, updated_from=updated_from, count=self._LETTER_PAGE)
                # Only true integers identify a letter ("12", 1.9, True are not ids);
                # the rest is dropped here and reported once below.
                keyed = [(item["id"], item) for item in page if self._is_letter_id(item.get("id"))]
                new_ids = [letter_id for letter_id, _ in keyed if letter_id not in fetched]
                for letter_id, letter in keyed:
                    fetched[letter_id] = letter
                bad_id_types.update(
                    type(item.get("id")).__name__
                    for item in page
                    if item.get("member_id") == mid and not self._is_letter_id(item.get("id"))
                )
                if len(page) < self._LETTER_PAGE:
                    # An empty page after a full one is impossible (updated_from is
                    # inclusive), so it means the request failed and was swallowed.
                    complete = page_no == 0 or bool(page)
                    break
                if not new_ids:
                    break
                # Page by the last letter's creation time, not its updated_at: every
                # letter with a higher id has updated_at >= created_at >= this value,
                # whereas a favourited boundary letter's later updated_at would skip
                # them. Inclusive overlap on purpose: deduped by id above.
                last = page[-1]
                updated_from = (
                    self._timestamp_text(last.get("created_at"))
                    or self._timestamp_text(last.get("updated_at"))
                    or updated_from
                )
        except (SessionExpiredError, RefreshFailedError):
            raise
        except Exception as e:
            logger.warning("Letter fetch failed", group_id=gid, error_type=type(e).__name__)
            return 0

        if bad_id_types:
            logger.warning("Letters skipped: id is not an integer", group_id=gid, id_types=sorted(bad_id_types))
        records: list[dict[str, Any]] = []
        downloads: list[tuple[str, Path]] = []
        for letter_id, letter in fetched.items():
            if letter.get("member_id") != mid:
                continue
            url = letter.get("file")
            image_file = f"letters/{letter_id}.{get_media_extension(url, 'picture')}" if url else None
            if url and image_file:
                downloads.append((url, member_dir / image_file))
            records.append(
                {
                    "id": letter_id,
                    "member_id": mid,
                    "text": letter.get("text") or "",
                    "created_at": self._timestamp_text(letter.get("created_at")) or None,
                    "updated_at": self._timestamp_text(letter.get("updated_at")) or None,
                    "is_favorite": bool(letter.get("is_favorite")),
                    "image_file": image_file,
                    "width": letter.get("thumbnail_width"),
                    "height": letter.get("thumbnail_height"),
                }
            )

        added = 0
        # A member with no letters of its own still needs a letters.json after a
        # complete fetch: the cursor is dropped whenever the file is missing, so
        # without it every call would refetch the whole room.
        if records or (complete and fetched and not letters_file.exists()):

            def mutate(current: Optional[Any]) -> tuple[Optional[Any], int]:
                existing = current.get("letters", []) if isinstance(current, dict) else []
                by_id = {item.get("id"): item for item in existing}
                new_count = sum(1 for r in records if r["id"] not in by_id)
                for r in records:
                    prev = by_id.get(r["id"])
                    if prev and r["image_file"] is None and prev.get("image_file"):
                        r["image_file"] = prev["image_file"]
                    by_id[r["id"]] = r
                letters = sorted(by_id.values(), key=lambda item: self._timestamp_text(item.get("created_at")))
                merged = {"version": 1, "letters": letters}
                return (None if merged == current else merged), new_count

            # Rename a renamed room's / member's folder before creating anything.
            renamed = self._renamed_member_dir(group, mid, sanitize_name(member["name"]))
            if renamed is None:
                return 0
            member_dir = renamed
            member_dir.mkdir(parents=True, exist_ok=True)
            try:
                wrote, added = await asyncio.to_thread(self._locked_update, letters_file, mutate)
            except ValueError:
                # Unreadable letters.json: leave it for inspection and refetch everything next time.
                entry = dict(self.sync_state.get(key) or {})
                entry.pop("letters_updated_at", None)
                self.sync_state[key] = entry
                self.save_sync_state()
                logger.warning("letters.json unreadable; letters cursor reset", group_id=gid, member_id=mid)
                return 0
            if not wrote:
                return 0

            if media_queue is None:
                for url, path in downloads:
                    await self.client.download_file(session, url, path)
            else:
                # Deferred: no member_dir (and no message_id), so process_media_queue
                # returns no metadata for letter cards and nothing reaches messages.json.
                deferred = [
                    {"url": url, "path": path, "timestamp": None, "media_type": "picture", "kind": "letter"}
                    for url, path in downloads
                    if not media_file_is_present(path)
                ]
                media_queue.extend(deferred)

        missing = [path for _, path in downloads if not media_file_is_present(path)]
        cursor = "incomplete"
        if complete:
            # Over everything fetched, not just this member's rows: other members' letters
            # in the same group are not ours, so there is nothing to re-fetch for them.
            newest = max((self._timestamp_text(item.get("updated_at")) for item in fetched.values()), default="")
            if not missing:
                cursor = "advanced" if self._advance_letters_cursor(key, newest) else "unchanged"
            elif media_queue is not None:
                self._pending_letters[key] = (missing, newest)
                cursor = "deferred"
            else:
                cursor = "held"
        logger.info(
            "Letters synced",
            group_id=gid,
            member_id=mid,
            added=added,
            complete=complete,
            cursor=cursor,
            missing=len(missing),
        )
        return added

    def _advance_letters_cursor(self, key: str, newest: str) -> bool:
        """Move ``letters_updated_at`` forward to ``newest`` (never back). True when saved."""
        entry = dict(self.sync_state.get(key) or {})
        if newest and newest > self._timestamp_text(entry.get("letters_updated_at")):
            entry["letters_updated_at"] = newest
            self.sync_state[key] = entry
            self.save_sync_state()
            return True
        return False

    def finalize_deferred_media(self) -> dict[str, Any]:
        """Complete the backfills and letter syncs whose media was deferred.

        Call after downloading the items that :meth:`backfill_past_messages`
        and :meth:`sync_letters` appended to a caller ``media_queue``. For each
        pending backfill, ``past_backfilled_at`` is set when every past message
        normalized, its sync_state entry still exists and every queued file is
        on disk. For each pending letter sync whose card images are all on disk,
        ``letters_updated_at`` moves forward (never back) to the newest fetched
        ``updated_at``. ``sync_state`` is saved once if anything changed, and
        the pending set is cleared either way, so an entry that could not
        complete is retried by the next sync (merging is idempotent); a backfill
        whose sync_state entry is gone is not retried (backfill needs that
        entry). Each incomplete entry is logged (``Deferred media left
        pending`` with its state key, kind, reason and missing-file count).
        Pending entries are kept in memory only: a run that never calls this
        leaves the flags and cursors unset.

        Returns:
            ``{"backfill_marked": n, "letters_marked": n, "still_pending": n,
            "saved": bool}``. ``letters_marked`` counts letter syncs whose
            images are all on disk (the cursor is written only when newer);
            ``still_pending`` counts entries left incomplete; ``saved`` is
            False only when a needed ``sync_state`` write failed (the new
            values then stay in memory for this run only).
        """
        report: dict[str, Any] = {"backfill_marked": 0, "letters_marked": 0, "still_pending": 0}
        changed = False
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def left_pending(key: str, kind: str, reason: str, missing: int) -> None:
            report["still_pending"] += 1
            logger.info("Deferred media left pending", state_key=key, kind=kind, reason=reason, missing=missing)

        for key, (paths, normalized) in self._pending_backfill.items():
            missing = sum(1 for p in paths if not media_file_is_present(p))
            if not normalized:
                left_pending(key, "backfill", "not_normalized", missing)
            elif key not in self.sync_state:
                left_pending(key, "backfill", "no_state", missing)
            elif missing:
                left_pending(key, "backfill", "media_missing", missing)
            else:
                self.sync_state[key]["past_backfilled_at"] = stamp
                report["backfill_marked"] += 1
                changed = True
        for key, (paths, newest) in self._pending_letters.items():
            missing = sum(1 for p in paths if not media_file_is_present(p))
            if missing:
                left_pending(key, "letters", "media_missing", missing)
                continue
            entry = dict(self.sync_state.get(key) or {})
            if newest and newest > self._timestamp_text(entry.get("letters_updated_at")):
                entry["letters_updated_at"] = newest
                self.sync_state[key] = entry
                changed = True
            report["letters_marked"] += 1
        self._pending_backfill.clear()
        self._pending_letters.clear()
        report["saved"] = self.save_sync_state() if changed else True
        if not report["saved"]:
            logger.warning("Deferred media finalize could not save sync_state")
        logger.info("Deferred media finalized", **report)
        return report

    async def archive_profile_images(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        member_dir: Path,
    ) -> dict[str, int]:
        """Download the member's current profile images and record them in
        ``<member_dir>/profile_images/index.json``.

        Idempotent: an image already on disk (non-empty) is skipped. A failed
        download is not indexed and is retried on the next call. An unreadable
        ``index.json`` is renamed to ``index.json.corrupt-<UTC timestamp>`` (kept
        for inspection, a warning is logged) and a fresh index is built; the old
        history is not carried over (the image files stay on disk).

        Returns:
            ``{"downloaded": n, "skipped": n, "failed": n}``.
        """
        urls = profile_images.image_urls(group, member)
        root = member_dir / profile_images.DIR_NAME
        return await self._archive_image_set(session, root, urls)

    async def _archive_image_set(
        self, session: aiohttp.ClientSession, root: Path, urls: dict[str, str]
    ) -> dict[str, int]:
        """Download ``urls`` (kind -> URL) under ``root/<kind>/<basename>`` and
        update ``root/index.json``. Shared by member and account images."""
        report = {"downloaded": 0, "skipped": 0, "failed": 0}
        archived: dict[str, tuple[str, str]] = {}
        for kind, url in urls.items():
            rel = profile_images.relative_file(kind, url)
            if rel is None:
                report["failed"] += 1
                continue
            path = root / rel
            if media_file_is_present(path):
                report["skipped"] += 1
            elif await self.client.download_file(session, url, path):
                report["downloaded"] += 1
            else:
                report["failed"] += 1
                continue
            archived[kind] = (rel, url)

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], None]:
            return profile_images.merge_index(current, archived, urls, now_iso), None

        if urls or (root / profile_images.INDEX_NAME).exists():
            root.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                self._locked_update, root / profile_images.INDEX_NAME, mutate, on_corrupt="quarantine"
            )
        return report

    async def _archive_profile_images_safely(
        self, session: aiohttp.ClientSession, group: dict[str, Any], member: dict[str, Any]
    ) -> None:
        """Run :meth:`archive_profile_images`; any failure is logged, never raised,
        so it cannot change sync_member's result or the cursor."""
        try:
            report = await self.archive_profile_images(session, group, member, self._member_dir_path(group, member))
            if report["downloaded"] or report["failed"]:
                logger.info("Profile images archived", member_id=member.get("id"), **report)
        except Exception as e:
            logger.warning("Profile image archiving failed", member_id=member.get("id"), error_type=type(e).__name__)

    async def archive_account_profile(self, session: aiohttp.ClientSession) -> dict[str, int]:
        """Archive the account's own nickname and avatar for this service.

        Writes ``<output_dir>/account/profile.json`` (``name`` and ``updated_at``
        only — birthday, sex, subdivision and FC accounts are personal data the
        app does not need and are never stored or logged) and archives the avatar
        under ``account/profile_images/`` with the same index/history as member
        images. A missing, non-string or empty ``name``, and a missing or
        non-string ``updated_at``, keep the stored value (``""``/``None`` on first
        write); only a non-empty string ``thumbnail`` is archived. An unreadable ``profile.json`` (or avatar
        ``index.json``) is renamed to ``<name>.corrupt-<UTC timestamp>`` (kept
        for inspection, a warning with the file name is logged) and rebuilt. Auth errors (``SessionExpiredError``,
        ``RefreshFailedError``) propagate; any other error, from the fetch or
        from the file/avatar work after it, yields all-zero counts and this
        method's own error log carries the exception type only (never the
        nickname). A failed avatar download is logged by
        :meth:`Client.download_file` with the URL's scheme, host, port and path
        (the signed query string and any ``user:password@`` are redacted).

        Returns:
            ``{"downloaded": n, "skipped": n, "failed": n}`` for the avatar
            (all zero when nothing was archived or a non-auth error occurred).
        """
        empty = {"downloaded": 0, "skipped": 0, "failed": 0}
        try:
            profile = await self.client.get_profile(session)
        except (SessionExpiredError, RefreshFailedError):
            raise
        except Exception as e:
            logger.warning("Profile fetch failed", error_type=type(e).__name__)
            return empty
        if not isinstance(profile, dict):
            return empty

        name = profile.get("name")
        updated_at = profile.get("updated_at")
        thumbnail = profile.get("thumbnail")
        urls = {"thumbnail": thumbnail} if isinstance(thumbnail, str) and thumbnail else {}

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], None]:
            stored = current if isinstance(current, dict) else {}
            kept_name = stored.get("name")
            kept_updated_at = stored.get("updated_at")
            record = {
                "version": 1,
                "name": name if isinstance(name, str) and name else (kept_name if isinstance(kept_name, str) else ""),
                "updated_at": updated_at
                if isinstance(updated_at, str)
                else (kept_updated_at if isinstance(kept_updated_at, str) else None),
            }
            return (None if current == record else record), None

        try:
            account_dir = self.output_dir / "account"
            account_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._locked_update, account_dir / "profile.json", mutate, on_corrupt="quarantine")
            return await self._archive_image_set(session, account_dir / profile_images.DIR_NAME, urls)
        except (SessionExpiredError, RefreshFailedError):
            raise
        except Exception as e:
            logger.warning("Account profile archiving failed", error_type=type(e).__name__)
            return empty

    async def _sync_member_messages(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        media_queue: list[dict[str, Any]],
        progress_callback: Optional[Any] = None,
        prefetched_messages: Optional[list[dict[str, Any]]] = None,
        *,
        on_new_messages: Optional[Callable[[dict[str, Any], list[dict[str, Any]]], Any]] = None,
        ignore_cursor: bool = False,
    ) -> Optional[int]:
        """
        Message part of :meth:`sync_member` (see there for arguments).

        Returns:
            Number of messages processed in this run (re-fetched overlap
            included), or None when a room/member folder rename failed (nothing
            was written and the cursor is untouched).
        """
        gid = group["id"]
        mid = member["id"]
        mname = sanitize_name(member["name"])

        # output_dir is already service-specific (e.g., output/日向坂46/)
        # so we only need to add messages/ and the group directory
        renamed = self._renamed_member_dir(group, mid, mname)
        if renamed is None:
            return None
        member_dir = renamed
        member_dir.mkdir(parents=True, exist_ok=True)
        for t in ["picture", "video", "voice"]:
            (member_dir / t).mkdir(exist_ok=True)

        last_ts = self.get_last_ts(gid, mid)
        logger.info("Syncing member", member=mname, member_id=mid, last_ts=last_ts)

        state_key = f"{gid}_{mid}"
        existing_file = member_dir / "messages.json"

        async def fetch_for_member(since_ts: Optional[str], *, recovery: bool = False) -> list[dict[str, Any]]:
            """Fetch this member's messages, filtered by the ``since_ts`` cursor.

            Normal path: when a shared ``prefetched_messages`` timeline was supplied,
            filter it by member (and by ``since_ts`` when set; ``since_ts=None`` here
            means a first sync with no cursor, i.e. take all of the member's rows).

            Recovery path (``recovery=True``): fetch the member's TRUE full history
            from the API, bypassing ``prefetched_messages``. The prefetched timeline
            is only the group's incremental window, so it CANNOT supply full history —
            filtering it during recovery truncated the archive (PY-MGR-01). ``since_ts``
            is None here and full history is fetched regardless.
            """
            if prefetched_messages is not None and not recovery:
                # Pre-fetched: filter by member_id AND this member's timestamp cursor
                filtered = [
                    x
                    for x in prefetched_messages
                    if roster.author_id(x) == mid and (since_ts is None or (x.get("published_at") or "") >= since_ts)
                ]
                logger.info("Filtered prefetched messages for member", count=len(filtered), member=mname)
                return filtered

            fetched = await self.client.get_messages(
                session, gid, since_ts=None if recovery else since_ts, progress_callback=progress_callback
            )
            logger.info("Fetched messages", count=len(fetched), group_id=gid)

            # Filter for member
            fetched = [x for x in fetched if roster.author_id(x) == mid]
            logger.info("Filtered messages for member", count=len(fetched), member=mname)
            return fetched

        try:
            messages = await fetch_for_member(None if ignore_cursor else last_ts)
            if ignore_cursor:
                logger.debug(
                    "Cursor ignored for room scan", group_id=gid, member_id=mid, last_ts=last_ts, count=len(messages)
                )

            # Load existing (may be corrupt / truncated — see recovery below)
            existing_msgs: list[dict[str, Any]] = []
            corrupt = False
            if existing_file.exists():
                try:
                    async with aiofiles.open(existing_file, encoding="utf-8") as f:
                        data = json.loads(await f.read())
                        existing_msgs = data.get("messages", [])
                except Exception:
                    # Corrupt file (e.g. force-close during write). The existing
                    # history is unreadable, so a full re-fetch is required.
                    logger.warning(
                        "corrupt_messages_file",
                        member=mname,
                        member_id=mid,
                        group_id=gid,
                    )
                    corrupt = True

            # Integrity check: if the file has fewer messages than sync_state
            # recorded, data was lost (e.g. past force-close overwrote the
            # file with only new messages).
            expected = (self.sync_state.get(state_key) or {}).get("total_messages", 0)
            mismatch = expected > 0 and len(existing_msgs) < expected
            if mismatch:
                logger.warning(
                    "message_count_mismatch",
                    member=mname,
                    member_id=mid,
                    group_id=gid,
                    expected=expected,
                    actual=len(existing_msgs),
                )

            # Recovery: on corruption or count mismatch, the incremental fetch
            # above (bounded by last_ts) only returned NEW messages, which would
            # overwrite the file with a truncated history. Re-fetch the member's
            # TRUE full history from the API (recovery=True bypasses the prefetched
            # incremental window, which cannot contain full history — PY-MGR-01).
            # Keep any readable existing_msgs and merge, so recovery can only ADD:
            # for a corrupt (unreadable) file existing_msgs is already [], but for a
            # count mismatch the file is readable and must not be discarded.
            recovering = corrupt or mismatch
            if recovering:
                logger.info(
                    "recovering_full_history",
                    member=mname,
                    member_id=mid,
                    group_id=gid,
                    preserved_existing=len(existing_msgs),
                )
                messages = await fetch_for_member(None, recovery=True)
                # Fail closed: an empty full-history re-fetch is indistinguishable
                # from a transient API failure (get_messages returns [] when the
                # first page fails after its refresh retry — it cannot tell a
                # genuinely-empty timeline from a hiccup). Writing here would
                # overwrite a corrupt/short file with an EMPTY archive and advance
                # the cursor — the exact truncation recovery exists to prevent. So
                # skip the write and the cursor advance and retry next sync. Any
                # readable existing_msgs stay on disk untouched. (Review follow-up
                # to PY-MGR-01.)
                if not messages:
                    logger.error(
                        "recovery_fetch_empty_skipping_write",
                        member=mname,
                        member_id=mid,
                        group_id=gid,
                        preserved_existing=len(existing_msgs),
                    )
                    return 0

            # No new messages and nothing to recover: nothing to write.
            if not messages and not recovering:
                return 0

            # Process & Prepare (on the recovered full set when recovering)
            # A scan re-reads rows older than the cursor. Their missing media or a
            # failed normalize must not drag the cursor back to an old date for good
            # (every later sync would re-read from there): room_archive_complete and
            # Verify & Fix cover those rows. Rows at/after the old cursor still clamp.
            processed, earliest_failed_ts, earliest_pending_media_ts = self.prepare_messages(
                messages, member_dir, media_queue, cursor_floor=last_ts if ignore_cursor else None
            )

            member_block = {
                "id": mid,
                "name": mname,
                "group_id": gid,
                "portrait": member.get("portrait"),
                "thumbnail": member.get("thumbnail"),
                "phone_image": member.get("phone_image"),
                "group_thumbnail": group.get("thumbnail"),
            }

            def mutate(
                current: Optional[Any],
            ) -> tuple[Optional[Any], tuple[list[dict[str, Any]], list[dict[str, Any]], bool]]:
                # Merge against the file as it is NOW, under the lock — not the copy
                # read before the API call. Another writer (e.g. a favorite toggle)
                # may have rewritten it since; merging the stale copy would drop
                # that change, and the other writer's stale copy would drop ours.
                if not isinstance(current, dict) and not corrupt and existing_file.exists():
                    # Readable at the pre-read, unreadable now (a corrupt pre-read was
                    # already logged above). Falls back to the pre-read copy.
                    logger.warning(
                        "messages.json unreadable at write time; merging the earlier read",
                        member_id=mid,
                        group_id=gid,
                    )
                base = current.get("messages", []) if isinstance(current, dict) else existing_msgs
                base_ids = {m.get("id") for m in base}
                new_msgs = [m for m in processed if m["id"] not in base_ids]
                # Dedupe (Upsert: prefer fresh API data, but preserve locally-derived
                # media metadata the API never returns — see _merge_messages).
                merged = self._merge_messages(base, processed, group_id=gid, member_id=mid)
                merged.sort(key=lambda x: x.get("timestamp") or "")
                export_data = {
                    "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
                    "member": member_block,
                    "total_messages": len(merged),
                    "message_type_counts": self._type_counts(merged),
                    "messages": merged,
                }
                # First write of this member's archive (file absent, not merely
                # unreadable); evaluated under the lock like the merge itself.
                initial = current is None and not existing_file.exists()
                return export_data, (merged, new_msgs, initial)

            wrote, (merged, new_msgs, initial_sync) = await asyncio.to_thread(
                self._locked_update, existing_file, mutate, on_corrupt="discard"
            )
            if not wrote:
                # The messages never hit disk — do NOT advance the cursor, or the next
                # sync would skip them permanently. Abort this member; retry next run.
                logger.error("Skipping cursor advance: messages.json write failed", member=mname)
                return 0

            # Update State. Kept synchronous (NOT offloaded to a thread): callers gather
            # sync_member across groups, all sharing self.sync_state — running the
            # mutate+serialize on the event loop keeps it atomic under cooperative
            # scheduling. (The messages.json write above writes a local dict, so it is
            # safe to offload.)
            if recovering:
                # The rebuilt history comes from the group timeline, which never
                # holds pre-subscription messages, so a corrupt-file rebuild loses
                # the backfilled ones. Re-arm the backfill (merging is idempotent;
                # the server keeps serving them). update_sync_state saves below.
                (self.sync_state.get(state_key) or {}).pop("past_backfilled_at", None)
            max_id = max(x["id"] for x in merged) if merged else 0
            newest_ts = max((x.get("timestamp") or "" for x in merged), default=None)
            # If any message failed to normalize, do NOT advance the cursor to or
            # past it. The next sync fetches published_at >= cursor (inclusive), so
            # an un-clamped cursor would filter the failed message out forever —
            # silent data loss. Clamping to the earliest failure re-fetches it (and
            # its successful siblings, idempotently) next run. A failed message with
            # no timestamp clamps to "" (full re-fetch) — safe over lossy.
            if earliest_failed_ts is not None and newest_ts is not None:
                newest_ts = min(newest_ts, earliest_failed_ts)
            # Also hold the cursor behind any message whose media is still queued
            # (not yet confirmed on disk). If the media phase is interrupted, the
            # next timestamp-filtered sync re-fetches these messages and re-downloads.
            # Note: A message whose media URL is permanently unavailable will keep
            # re-pinning this cursor every sync; a give-up/attempt-cap is a possible
            # future enhancement.
            if earliest_pending_media_ts is not None and newest_ts is not None:
                newest_ts = min(newest_ts, earliest_pending_media_ts)
            self.update_sync_state(gid, mid, max_id, len(merged), last_ts=newest_ts)

            if on_new_messages is not None and new_msgs and not recovering and not ignore_cursor:
                member_info = {
                    "group_id": gid,
                    "group_name": group["name"],
                    "member_id": mid,
                    "member_name": member["name"],
                    "member_dir": member_dir,
                    "phone_image": member.get("phone_image"),
                    "initial_sync": initial_sync,
                }
                await self._notify_new_messages(on_new_messages, member_info, new_msgs)

            return len(processed)

        except (SessionExpiredError, RefreshFailedError):
            # Auth failure is not "no new messages" — propagate so the caller
            # can prompt re-login instead of reporting a successful empty sync.
            raise
        except Exception as e:
            # Only the type: the exception text (or its traceback) may quote message content.
            logger.error(
                "Error syncing member",
                member=mname,
                member_id=mid,
                group_id=gid,
                error_type=type(e).__name__,
                **_error_origin(e),
            )
            return 0

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        member_dir: Path,
        queue: list[dict[str, Any]],
        *,
        cursor_floor: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], Optional[str], Optional[str]]:
        """
        Normalize messages and queue media downloads.

        Args:
            messages: List of raw message dicts.
            member_dir: Directory for member's files.
            queue: Media download waiting queue.
            cursor_floor: When set, only messages whose ``published_at`` is at or
                after it count toward ``earliest_failed_ts`` and
                ``earliest_pending_media_ts`` (older ones are still queued, and
                logged when they fail). None (default): every message counts.

        Returns:
            A tuple ``(processed, earliest_failed_ts, earliest_pending_media_ts)``.
            ``processed`` is the list of normalized message dicts.
            ``earliest_failed_ts`` is the ``published_at`` of the oldest message that
            failed to normalize (``""`` if such a message had no timestamp), or ``None``
            if every message processed. The caller must not advance the sync cursor to or past
            ``earliest_failed_ts`` — doing so would drop the failed message
            permanently on the next timestamp-filtered sync.
            ``earliest_pending_media_ts`` is the ``published_at`` (or ``""`` if none) of
            the oldest message whose media was queued for download; ``None`` if no media
            was queued. The caller must not advance the cursor past it.
        """
        processed = []
        earliest_failed_ts: Optional[str] = None
        earliest_pending_media_ts: Optional[str] = None
        below_floor_pending = 0
        below_floor_failed = 0
        for msg in messages:
            try:
                # Normalize core fields
                p_msg = normalize_message(msg)
                msg_type = p_msg["type"]
                raw_type = p_msg.pop("_raw_type", "text")  # Remove internal helper field

                # Media
                media_url = msg.get("file") or msg.get("thumbnail")
                if media_url:
                    ext = get_media_extension(media_url, raw_type)

                    subdir = "other"
                    if msg_type == "picture":
                        subdir = "picture"
                    elif msg_type == "video":
                        subdir = "video"
                    elif msg_type == "voice":
                        subdir = "voice"

                    filepath = member_dir / subdir / f"{msg['id']}.{ext}"

                    # Compute presence the same way scan_member_media does: absent OR zero-byte = missing.
                    media_present = media_file_is_present(filepath)

                    # Logic: If file doesn't exist or is zero-byte, queue it.
                    if not media_present:
                        queue.append(
                            {
                                "url": media_url,
                                "path": filepath,
                                "timestamp": msg.get("published_at"),
                                "message_id": msg["id"],
                                "media_type": msg_type,
                                "member_dir": member_dir,
                            }
                        )
                        # Hold the cursor behind this message until its media is on
                        # disk, so an interrupted download is re-fetched next run.
                        pending_ts = msg.get("published_at") or ""
                        if cursor_floor is None or pending_ts >= cursor_floor:
                            if earliest_pending_media_ts is None or pending_ts < earliest_pending_media_ts:
                                earliest_pending_media_ts = pending_ts
                        else:
                            below_floor_pending += 1

                    p_msg["media_file"] = str(filepath.relative_to(self.output_dir))

                    # Extract dimensions if file exists (already downloaded or will be processed)
                    if media_present:
                        width, height = get_media_dimensions(filepath, msg_type)
                        if width and height:
                            p_msg["width"] = width
                            p_msg["height"] = height

                # Skip empty text messages (see _is_skipped_empty; room_archive_complete
                # uses the same rule, so a skipped message is never expected on disk).
                if _is_skipped_empty(msg):
                    logger.debug("Skipping empty text message", message_id=msg.get("id"))
                    continue

                processed.append(p_msg)
            except Exception as e:
                mid = msg.get("id")
                # Track the oldest failure's timestamp so the caller can hold the
                # cursor behind it and re-fetch next run instead of losing it.
                failed_ts = msg.get("published_at") or ""
                if cursor_floor is None or failed_ts >= cursor_floor:
                    if earliest_failed_ts is None or failed_ts < earliest_failed_ts:
                        earliest_failed_ts = failed_ts
                else:
                    below_floor_failed += 1
                # Only the type: the exception text may quote the message that failed.
                logger.error("Prepare error", message_id=mid, error_type=type(e).__name__, **_error_origin(e))
        if below_floor_pending or below_floor_failed:
            logger.debug(
                "Old rows excluded from cursor clamp",
                member_dir=member_dir.name,
                cursor_floor=cursor_floor,
                pending_media=below_floor_pending,
                failed=below_floor_failed,
            )
        return processed, earliest_failed_ts, earliest_pending_media_ts

    # Media metadata derived locally from the downloaded file — the message API
    # never returns these fields. Each sync re-fetches an overlapping window and
    # rebuilds messages from the API, so a whole-object upsert would drop them on
    # every re-sync. width/height are also recomputed in prepare_messages for
    # on-disk files, but is_muted/media_duration are computed only once (at
    # download, in process_media_queue), so carrying them forward here is what
    # keeps them alive across re-syncs.
    #
    # media_file is the pointer to the already-downloaded file. It is only set in
    # prepare_messages when the API still returns a media URL, so when a member
    # withdraws a post (state -> canceled, URL stripped) the fresh record omits it.
    # Inheriting it keeps the archived media reachable; dropping it would orphan a
    # file that can no longer be re-downloaded (PY-MGR-02). A genuine media change
    # sets a fresh media_file, which wins (inheritance only fills an omitted field).
    _LOCAL_DERIVED_FIELDS = ("width", "height", "media_duration", "is_muted", "media_file")

    @staticmethod
    def _merge_messages(
        existing_msgs: list[dict[str, Any]],
        processed: list[dict[str, Any]],
        *,
        group_id: Optional[int] = None,
        member_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Upsert ``processed`` over ``existing_msgs`` (prefer fresh API data),
        carrying forward locally-derived media metadata the API cannot supply.

        A field is only inherited from the stored record when the fresh message
        omits it — a freshly re-derived value (e.g. is_muted recomputed on a
        re-download) always wins. A canceled (withdrawn) message arrives without
        text: it keeps the ``content`` captured earlier and the first non-empty
        ``canceled_at`` seen. A fresh row without ``canceled_at`` (the API sent no
        ``updated_at``) inherits the stored one; when none is stored yet (or a
        stored ``None`` from an earlier write), the fresh value is recorded.

        ``group_id`` / ``member_id`` only label the log lines: ``info`` when a stored
        message turns canceled (ids, ``canceled_at`` and whether its content was
        kept, never the text), ``debug`` when a canceled message is first stored.
        """
        merged = {m["id"]: m for m in existing_msgs}
        for pm in processed:
            prev = merged.get(pm["id"])
            if prev:
                for key in SyncManager._LOCAL_DERIVED_FIELDS:
                    if key not in pm and key in prev:
                        pm[key] = prev[key]
                if pm.get("state") in CANCELED_STATES:
                    # A withdrawn message comes back without text: keep what we
                    # captured before the withdrawal, and the first cancel time seen.
                    if not pm.get("content") and prev.get("content"):
                        pm["content"] = prev["content"]
                    if prev.get("canceled_at"):
                        pm["canceled_at"] = prev["canceled_at"]
                    if prev.get("state") not in CANCELED_STATES:
                        logger.info(
                            "Stored message turned canceled",
                            group_id=group_id,
                            member_id=member_id,
                            message_id=pm["id"],
                            canceled_at=pm.get("canceled_at"),
                            content_kept=bool(pm.get("content")),
                        )
            elif pm.get("state") in CANCELED_STATES:
                logger.debug(
                    "Canceled message stored without earlier copy",
                    group_id=group_id,
                    member_id=member_id,
                    message_id=pm["id"],
                    canceled_at=pm.get("canceled_at"),
                )
            merged[pm["id"]] = pm
        return list(merged.values())

    def scan_member_media(self, member_dir: Path) -> dict[str, Any]:
        """
        Offline scan of a member's messages.json for missing media.

        A media file is 'missing' if it does not exist OR has zero bytes.
        Expected paths are resolved as ``self.output_dir / msg["media_file"]``.

        Returns:
            ``{"checked": int, "missing": list[dict], "unresolved": list[dict],
            "error": str | None}`` where each missing descriptor is
            ``{"message_id", "media_type", "path": Path, "timestamp"}`` and each
            ``unresolved`` descriptor is ``{"message_id", "media_type", "timestamp"}``
            (no path). ``unresolved`` lists media-type messages that have no recorded
            ``media_file`` (the media URL was absent at sync time, e.g. a
            source-removed stub): they cannot be located or verified on disk, so they
            are reported separately rather than silently ignored — otherwise the
            result would claim 'all present' while such media is genuinely absent.

            ``error`` is ``None`` for a readable manifest. When messages.json cannot
            be used it is set to a stable id so callers can distinguish it from a
            fully-synced member (which also yields checked=0/missing=[]):
            ``"manifest_missing"`` (absent), ``"manifest_unreadable"`` (present but
            corrupt/unreadable), or ``"manifest_invalid"`` (valid JSON but not an
            object). In every case the zero/empty counts are still returned.
        """
        result: dict[str, Any] = {"checked": 0, "missing": [], "unresolved": [], "error": None}
        messages_file = member_dir / "messages.json"
        if not messages_file.exists():
            # An absent manifest is NOT the same as a fully-synced member (which also
            # yields checked=0/missing=[]). Signal it distinctly so callers can tell
            # "nothing synced yet" apart from "all present".
            result["error"] = "manifest_missing"
            return result
        try:
            with open(messages_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            # A corrupt/unreadable manifest must be surfaced, not collapsed into a
            # clean-looking empty result that reads as "fully synced".
            logger.warning(
                "saka.scan.manifest_unreadable", file=_short_path(messages_file), error_type=type(e).__name__
            )
            result["error"] = "manifest_unreadable"
            return result

        if not isinstance(data, dict):
            logger.warning("saka.scan.manifest_invalid", file=_short_path(messages_file))
            result["error"] = "manifest_invalid"
            return result

        for msg in data.get("messages", []):
            mtype = msg.get("type")
            if mtype not in ("picture", "video", "voice"):
                continue
            media_file = msg.get("media_file")
            if not media_file:
                # A withdrawn post (state in _MEDIA_STRIPPING_STATES, e.g.
                # "canceled" — the member withdrew it, which strips its media)
                # legitimately has no media and is NOT a completeness gap. The state
                # is recorded on disk; skip it silently. Any OTHER non-published
                # state falls through and is surfaced below, so an unexpected or
                # transient state can't silently hide a real gap.
                state = msg.get("state")
                if state in _MEDIA_STRIPPING_STATES:
                    continue
                # Otherwise: a published media-type message with no recorded media
                # path — genuinely unaccounted. Surface it so completeness is never
                # overclaimed.
                result["unresolved"].append(
                    {
                        "message_id": msg.get("id"),
                        "media_type": mtype,
                        "timestamp": msg.get("timestamp") or msg.get("published_at"),
                    }
                )
                continue
            result["checked"] += 1
            path = self.output_dir / media_file
            present = media_file_is_present(path)
            if not present:
                result["missing"].append(
                    {
                        "message_id": msg.get("id"),
                        "media_type": mtype,
                        "path": path,
                        "timestamp": msg.get("timestamp") or msg.get("published_at"),
                    }
                )
        return result

    async def process_media_queue(
        self,
        session: aiohttp.ClientSession,
        queue: list[dict[str, Any]],
        concurrency: int = 5,
        progress_callback: Optional[Any] = None,
        item_callback: Optional[Any] = None,
    ) -> dict[Path, dict[int, dict[str, Any]]]:
        """
        Downloads files in the queue.

        Concurrency is managed by the caller's session wrapper (PooledSession)
        which acquires/releases pool slots per HTTP request. The ``concurrency``
        parameter is kept for backward compatibility but is no longer used
        internally.

        Args:
            session: Active aiohttp session (or PooledSession wrapper).
            queue: List of media items to download.
            concurrency: Deprecated — kept for backward compatibility.
            progress_callback: Optional ``(completed, total)`` callback (sync or
                async), called after each successful download; ``completed``
                counts successes only.
            item_callback: Optional ``(ok)`` callback (sync or async), called
                once per item after its download attempt, success (``True``) or
                failure (``False``), so a caller can count processed items.

        A callback that raises is logged (``Media queue callback failed``, with
        the callback name and error type) and never stops the downloads.

        Returns:
            Dict mapping member_dir to {message_id: metadata_dict} for downloaded media.
            metadata_dict contains: width, height, media_duration, is_muted (as applicable).
        """
        if not queue:
            return {}

        async def _call_safely(callback: Any, name: str, *args: Any) -> None:
            # A caller's callback must never abort the queue (and lose the
            # metadata of files already downloaded).
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(*args)
                else:
                    callback(*args)
            except Exception as e:
                logger.warning("Media queue callback failed", callback=name, error_type=type(e).__name__)

        # Concurrency is managed by the caller's PooledSession / AdaptivePool.
        # No local semaphore needed — each HTTP request in download_file
        # acquires a pool slot via the session wrapper.
        total = len(queue)
        completed = 0
        # Group metadata by member_dir for efficient batch updates
        metadata_by_dir: dict[Path, dict[int, dict[str, Any]]] = {}

        async def worker(item: dict[str, Any]) -> None:
            nonlocal completed
            res = await self.client.download_file(session, item["url"], item["path"], item["timestamp"])
            if res:
                media_type = item.get("media_type", "")
                member_dir = item.get("member_dir")
                if member_dir:
                    metadata: dict[str, Any] = {}

                    # Extract dimensions for pictures and videos
                    if media_type in ("picture", "video"):
                        width, height = get_media_dimensions(item["path"], media_type)
                        if width and height:
                            metadata["width"] = width
                            metadata["height"] = height

                    # Extract audio metadata for videos and voice messages
                    if media_type in ("video", "voice"):
                        audio_meta = get_audio_metadata(item["path"], media_type)
                        if audio_meta.get("duration") is not None:
                            metadata["media_duration"] = audio_meta["duration"]
                        if audio_meta.get("is_muted") is not None:
                            metadata["is_muted"] = audio_meta["is_muted"]

                    if metadata:
                        if member_dir not in metadata_by_dir:
                            metadata_by_dir[member_dir] = {}
                        metadata_by_dir[member_dir][item["message_id"]] = metadata

                completed += 1
                if progress_callback:
                    await _call_safely(progress_callback, "progress", completed, total)
            if item_callback:
                await _call_safely(item_callback, "item", bool(res))

        await asyncio.gather(*[worker(item) for item in queue])
        return metadata_by_dir

    async def update_message_metadata(self, messages_file: Path, metadata: dict[int, dict[str, Any]]) -> None:
        """
        Update messages.json with extracted media metadata.

        Args:
            messages_file: Path to messages.json file.
            metadata: Dict mapping message_id to metadata dict
                      (may contain: width, height, media_duration, is_muted).

        Runs under the file lock (see :meth:`_locked_update`).
        """
        if not metadata or not messages_file.exists():
            return

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], None]:
            if not isinstance(current, dict):
                return None, None
            updated = False
            for msg in current.get("messages", []):
                msg_metadata = metadata.get(msg.get("id"))
                if msg_metadata:
                    for key, value in msg_metadata.items():
                        if value is not None:
                            msg[key] = value
                            updated = True
            return (current if updated else None), None

        try:
            wrote, _ = await asyncio.to_thread(self._locked_update, messages_file, mutate)
            if not wrote:
                logger.error("Failed to write message metadata", file=_short_path(messages_file))
        except Exception as e:
            logger.error(
                "Failed to update message metadata", file=_short_path(messages_file), error_type=type(e).__name__
            )

    async def reconcile_member_media(
        self,
        session: aiohttp.ClientSession,
        member_dir: Path,
        missing: list[dict[str, Any]],
        timeline_messages: list[dict[str, Any]],
        progress_callback: Optional[Any] = None,
    ) -> dict[str, int]:
        """
        Backfill a member's missing media using fresh URLs from a re-fetched
        timeline. Matches each missing message_id to a ``file``/``thumbnail`` URL,
        downloads via ``process_media_queue`` (hardened ``download_file``), then
        re-checks disk truth and writes back dimension metadata.

        Returns ``{"repaired", "failed", "still_missing"}``.
        """
        report = {"repaired": 0, "failed": 0, "still_missing": 0}
        if not missing:
            return report

        url_by_id: dict[Any, str] = {}
        for m in timeline_messages:
            url = m.get("file") or m.get("thumbnail")
            if url:
                url_by_id[m.get("id")] = url

        queue: list[dict[str, Any]] = []
        for d in missing:
            url = url_by_id.get(d["message_id"])
            if url:
                queue.append(
                    {
                        "url": url,
                        "path": d["path"],
                        "timestamp": d.get("timestamp"),
                        "message_id": d["message_id"],
                        "media_type": d["media_type"],
                        "member_dir": member_dir,
                    }
                )

        metadata_by_dir = await self.process_media_queue(session, queue, progress_callback=progress_callback)

        # Re-check disk truth: a queued item counts as repaired only if the file
        # is now present and non-empty.
        for item in queue:
            p = item["path"]
            ok = media_file_is_present(p)
            if ok:
                report["repaired"] += 1
            else:
                report["failed"] += 1
        report["still_missing"] = len(missing) - report["repaired"]

        member_meta = metadata_by_dir.get(member_dir)
        if member_meta:
            await self.update_message_metadata(member_dir / "messages.json", member_meta)
        return report
