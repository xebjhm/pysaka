import asyncio
import inspect
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import aiofiles
import aiohttp
import structlog

from . import profile_images
from .client import Client
from .exceptions import RefreshFailedError, SessionExpiredError
from .media import get_audio_metadata, get_media_dimensions
from .utils import get_media_extension, media_file_is_present, normalize_message, sanitize_name

logger = structlog.get_logger()

# Server states where the member withdrew the post, which legitimately strips its
# media (confirmed via the live timeline probe). ANY OTHER non-"published" state is
# treated as a potential real gap and surfaced in `unresolved`, so completeness is
# never overclaimed on an unexpected/transient state (e.g. "processing").
_MEDIA_STRIPPING_STATES = frozenset({"canceled", "cancelled"})

T = TypeVar("T")

# One lock per messages.json (or any JSON file updated via _locked_update). Thread
# locks, not asyncio locks: SakaDesk's favorite toggle and the sync write both run
# their file work in worker threads (asyncio.to_thread), which an asyncio.Lock
# would not serialise. In-process only — cross-process writers are excluded by the
# consumer (SakaDesk's DataDirLock).
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


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
        self.load_sync_state()

    def load_sync_state(self) -> None:
        """Load synchronization state from JSON file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    self.sync_state = json.load(f)
            except Exception as e:
                logger.error("Failed to load sync state", error=str(e))
                self.sync_state = {}

    def save_sync_state(self) -> None:
        """Save synchronization state to JSON file (atomic write).

        Uses a unique temp filename and retries os.replace to handle
        Windows file locking (antivirus, search indexer).
        """
        self._atomic_write_json(self.state_file, self.sync_state)

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

            logger.error("Failed to write file after retries", file=str(target), error=str(last_err))
            return False
        except Exception as e:
            logger.error("Failed to write file", file=str(target), error=str(e))
            return False
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _locked_update(
        path: Path,
        mutate: Callable[[Optional[Any]], tuple[Optional[Any], T]],
        *,
        corrupt_ok: bool = False,
    ) -> tuple[bool, T]:
        """Read-modify-write a JSON file under its process-wide lock.

        Synchronous and blocking — call via ``asyncio.to_thread`` from async code.
        The lock is not re-entrant, so ``mutate`` must not call ``_locked_update``
        (or ``update_message_fields*``) on the same path — it would deadlock.
        ``mutate`` receives the parsed file (``None`` if the file is missing, or if
        it is unreadable and ``corrupt_ok`` is True) and returns ``(data, result)``.
        ``data is None`` means "nothing to write". Every writer of a given file must
        go through this method, otherwise a concurrent read-modify-write can
        overwrite the other's changes.

        Returns:
            ``(write_ok, result)``. ``write_ok`` is False only when a write was
            attempted and ``_atomic_write_json`` failed.

        Raises:
            ValueError: the file is unreadable JSON and ``corrupt_ok`` is False.
        """
        with _lock_for(path):
            current: Optional[Any] = None
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        current = json.load(f)
                except ValueError:
                    if not corrupt_ok:
                        raise
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
        and rewriting the file yourself: a sync running at the same moment would
        otherwise lose either its new messages or your change.

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

    def _member_dir_path(self, group: dict[str, Any], member: dict[str, Any]) -> Path:
        """The on-disk directory of a member's archive (same naming as sync_member)."""
        return (
            self.output_dir
            / "messages"
            / f"{group['id']} {sanitize_name(group['name'])}"
            / f"{member['id']} {sanitize_name(member['name'])}"
        )

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
        """Invoke a consumer callback; its failures must never break a sync."""
        try:
            result = callback(member_info, new_messages)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error("on_new_messages callback failed", error=str(e), exc_info=True)

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
                corrupt-file / count-mismatch recovery. Exceptions it raises are
                logged and swallowed. The callback is awaited inline, so a slow
                callback delays that member's sync; hand heavy work off (e.g.
                schedule a task). ``member_info`` has ``group_id``,
                ``group_name``, ``member_id``, ``member_name``, ``member_dir``,
                ``phone_image`` and ``initial_sync``. ``initial_sync`` is True
                when ``messages.json`` did not exist before this write, in which
                case ``new_messages`` is the whole fetched history, not fresh
                posts; consumers sending notifications should ignore those calls.
                The callback runs before the profile-image step, so a just-changed
                ``phone_image`` may not be archived yet; use the ``phone_image``
                URL from ``member_info``.

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
        )
        await self._archive_profile_images_safely(session, group, member)
        return processed

    async def backfill_past_messages(
        self, session: aiohttp.ClientSession, group: dict[str, Any], member: dict[str, Any]
    ) -> int:
        """Merge the room's pre-subscription messages into the member's archive.

        Runs once per member: after it completes, ``past_backfilled_at`` is set in
        the member's sync_state entry and later calls return immediately. Requires
        a prior successful :meth:`sync_member` (no entry or no ``messages.json`` →
        skipped). Downloads its own media, because Verify & Fix takes fresh URLs
        from the timeline, which never contains these messages. The flag is set
        only when the request succeeded, no past message failed to normalize
        (otherwise the member would be retried on every sync), and every queued
        file is on disk; on any failure the next call retries (merging is
        idempotent). Cursor fields are
        never touched and ``on_new_messages`` is never fired.

        Returns:
            Number of messages that were not in the archive before.

        Raises:
            SessionExpiredError: authentication failed (also
                ``RefreshFailedError``); the flag is left unset.
            OSError: reading or writing ``messages.json`` or its media failed
                after the fetch (other unexpected errors after the fetch also
                propagate), so callers should wrap the call. An unreadable
                ``messages.json`` or a failed fetch does not raise: it logs and
                returns 0.
        """
        gid, mid = group["id"], member["id"]
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

        mine = [m for m in past if m.get("member_id") == mid]
        queue: list[dict[str, Any]] = []
        processed, earliest_failed_ts, _ = self.prepare_messages(mine, member_dir, queue)

        added = 0
        if processed:

            def mutate(current: Optional[Any]) -> tuple[Optional[Any], tuple[int, int]]:
                if not isinstance(current, dict):
                    return None, (0, -1)
                base = current.get("messages", [])
                base_ids = {m.get("id") for m in base}
                new_count = sum(1 for m in processed if m["id"] not in base_ids)
                merged = self._merge_messages(base, processed)
                merged.sort(key=lambda x: x.get("timestamp") or "")
                current["messages"] = merged
                current["total_messages"] = len(merged)
                current["message_type_counts"] = self._type_counts(merged)
                return current, (new_count, len(merged))

            try:
                wrote, (added, total) = await asyncio.to_thread(self._locked_update, messages_file, mutate)
            except ValueError:
                logger.warning("Backfill skipped: messages.json unreadable", group_id=gid, member_id=mid)
                return 0
            if not wrote or total < 0:
                return 0
            self.update_total_messages(gid, mid, total)

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

    async def sync_letters(self, session: aiohttp.ClientSession, group: dict[str, Any], member: dict[str, Any]) -> int:
        """Archive the account's sent letters to this member.

        Stores ``<member_dir>/letters.json`` (``client_token`` is never stored) and
        each letter-card image at ``<member_dir>/letters/<id>.<ext>``. Uses its own
        cursor ``letters_updated_at`` in the member's sync_state entry, which is
        advanced only when every stored letter's image is on disk, so a failed
        image is re-fetched with a fresh signed URL next run. Works for expired
        subscriptions (the server still returns their letters). Never touches the
        message cursor and never fires ``on_new_messages``.

        Raises:
            SessionExpiredError, RefreshFailedError: auth failures propagate.

        Returns:
            Number of letters that were not in ``letters.json`` before.
        """
        gid, mid = group["id"], member["id"]
        key = f"{gid}_{mid}"
        cursor = (self.sync_state.get(key) or {}).get("letters_updated_at")
        fetched: dict[Any, dict[str, Any]] = {}
        updated_from = cursor
        try:
            for _ in range(self._LETTER_MAX_PAGES):
                page = await self.client.get_letters(session, gid, updated_from=updated_from, count=self._LETTER_PAGE)
                new_ids = [item.get("id") for item in page if item.get("id") not in fetched]
                for letter in page:
                    fetched[letter.get("id")] = letter
                if len(page) < self._LETTER_PAGE or not new_ids:
                    break
                # Inclusive overlap on purpose: deduped by id above.
                updated_from = page[-1].get("updated_at") or updated_from
        except (SessionExpiredError, RefreshFailedError):
            raise
        except Exception as e:
            logger.warning("Letter fetch failed", group_id=gid, error_type=type(e).__name__)
            return 0

        mine = [item for item in fetched.values() if item.get("member_id") == mid]
        if not mine:
            return 0

        member_dir = self._member_dir_path(group, member)
        records: list[dict[str, Any]] = []
        downloads: list[tuple[str, Path]] = []
        for letter in mine:
            url = letter.get("file")
            image_file = f"letters/{letter['id']}.{get_media_extension(url, 'picture')}" if url else None
            if url and image_file:
                downloads.append((url, member_dir / image_file))
            records.append(
                {
                    "id": letter["id"],
                    "member_id": mid,
                    "text": letter.get("text") or "",
                    "created_at": letter.get("created_at"),
                    "updated_at": letter.get("updated_at"),
                    "is_favorite": bool(letter.get("is_favorite")),
                    "image_file": image_file,
                    "width": letter.get("thumbnail_width"),
                    "height": letter.get("thumbnail_height"),
                }
            )

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], int]:
            existing = current.get("letters", []) if isinstance(current, dict) else []
            by_id = {item.get("id"): item for item in existing}
            added = sum(1 for r in records if r["id"] not in by_id)
            for r in records:
                prev = by_id.get(r["id"])
                if prev and r["image_file"] is None and prev.get("image_file"):
                    r["image_file"] = prev["image_file"]
                by_id[r["id"]] = r
            letters = sorted(by_id.values(), key=lambda item: item.get("created_at") or "")
            return {"version": 1, "letters": letters}, added

        member_dir.mkdir(parents=True, exist_ok=True)
        try:
            wrote, added = await asyncio.to_thread(self._locked_update, member_dir / "letters.json", mutate)
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

        for url, path in downloads:
            await self.client.download_file(session, url, path)
        if all(media_file_is_present(path) for _, path in downloads):
            # Over everything fetched, not just this member's rows: other members' letters
            # in the same group are not ours, so there is nothing to re-fetch for them.
            newest = max((item.get("updated_at") or "" for item in fetched.values()), default="")
            entry = dict(self.sync_state.get(key) or {})
            if newest and newest > (entry.get("letters_updated_at") or ""):
                entry["letters_updated_at"] = newest
                self.sync_state[key] = entry
                self.save_sync_state()
        logger.info("Letters synced", group_id=gid, member_id=mid, added=added)
        return added

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
        download is not indexed and is retried on the next call.

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
            await asyncio.to_thread(self._locked_update, root / profile_images.INDEX_NAME, mutate, corrupt_ok=True)
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
        images. Auth errors propagate; anything else is logged by type.

        Returns:
            ``{"downloaded": n, "skipped": n, "failed": n}`` for the avatar.
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

        account_dir = self.output_dir / "account"
        account_dir.mkdir(parents=True, exist_ok=True)
        record = {"version": 1, "name": profile.get("name") or "", "updated_at": profile.get("updated_at")}

        def mutate(current: Optional[Any]) -> tuple[Optional[Any], None]:
            return (None if current == record else record), None

        await asyncio.to_thread(self._locked_update, account_dir / "profile.json", mutate, corrupt_ok=True)
        urls = {"thumbnail": profile["thumbnail"]} if profile.get("thumbnail") else {}
        return await self._archive_image_set(session, account_dir / profile_images.DIR_NAME, urls)

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
    ) -> int:
        """
        Message part of :meth:`sync_member` (see there for arguments).

        Returns:
            Number of messages processed in this run (re-fetched overlap
            included).
        """
        gid = group["id"]
        mid = member["id"]
        gname = sanitize_name(group["name"])
        mname = sanitize_name(member["name"])

        # output_dir is already service-specific (e.g., output/日向坂46/)
        # so we only need to add messages/ and the group directory
        messages_dir = self.output_dir / "messages"
        group_dir = messages_dir / f"{gid} {gname}"

        # If group was renamed on server (e.g., "12th Single" → "16th Single"),
        # rename the existing directory instead of creating a duplicate.
        if not group_dir.exists() and messages_dir.exists():
            for existing in messages_dir.iterdir():
                if existing.is_dir() and existing.name.startswith(f"{gid} "):
                    logger.info("Group renamed on server, renaming directory", old=existing.name, new=group_dir.name)
                    existing.rename(group_dir)
                    break

        member_dir = group_dir / f"{mid} {mname}"
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
                    if x.get("member_id") == mid and (since_ts is None or (x.get("published_at") or "") >= since_ts)
                ]
                logger.info("Filtered prefetched messages for member", count=len(filtered), member=mname)
                return filtered

            fetched = await self.client.get_messages(
                session, gid, since_ts=None if recovery else since_ts, progress_callback=progress_callback
            )
            logger.info("Fetched messages", count=len(fetched), group_id=gid)

            # Filter for member
            fetched = [x for x in fetched if x.get("member_id") == mid]
            logger.info("Filtered messages for member", count=len(fetched), member=mname)
            return fetched

        try:
            messages = await fetch_for_member(last_ts)

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
            processed, earliest_failed_ts, earliest_pending_media_ts = self.prepare_messages(
                messages, member_dir, media_queue
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
                base = current.get("messages", []) if isinstance(current, dict) else existing_msgs
                base_ids = {m.get("id") for m in base}
                new_msgs = [m for m in processed if m["id"] not in base_ids]
                # Dedupe (Upsert: prefer fresh API data, but preserve locally-derived
                # media metadata the API never returns — see _merge_messages).
                merged = self._merge_messages(base, processed)
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
                self._locked_update, existing_file, mutate, corrupt_ok=True
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

            if on_new_messages is not None and new_msgs and not recovering:
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
            logger.error("Error syncing member", member=mname, error=str(e), exc_info=True)
            return 0

    def prepare_messages(
        self, messages: list[dict[str, Any]], member_dir: Path, queue: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], Optional[str], Optional[str]]:
        """
        Normalize messages and queue media downloads.

        Args:
            messages: List of raw message dicts.
            member_dir: Directory for member's files.
            queue: Media download waiting queue.

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
                        if earliest_pending_media_ts is None or pending_ts < earliest_pending_media_ts:
                            earliest_pending_media_ts = pending_ts

                    p_msg["media_file"] = str(filepath.relative_to(self.output_dir))

                    # Extract dimensions if file exists (already downloaded or will be processed)
                    if media_present:
                        width, height = get_media_dimensions(filepath, msg_type)
                        if width and height:
                            p_msg["width"] = width
                            p_msg["height"] = height

                # Skip empty text messages (no content and no media)
                # These are often system/metadata entries from subscription
                if msg_type == "text" and not p_msg.get("content") and not media_url:
                    logger.debug("Skipping empty text message", message_id=msg.get("id"))
                    continue

                processed.append(p_msg)
            except Exception as e:
                mid = msg.get("id")
                # Track the oldest failure's timestamp so the caller can hold the
                # cursor behind it and re-fetch next run instead of losing it.
                failed_ts = msg.get("published_at") or ""
                if earliest_failed_ts is None or failed_ts < earliest_failed_ts:
                    earliest_failed_ts = failed_ts
                logger.error("Prepare error", message_id=mid, error=str(e))
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
    def _merge_messages(existing_msgs: list[dict[str, Any]], processed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Upsert ``processed`` over ``existing_msgs`` (prefer fresh API data),
        carrying forward locally-derived media metadata the API cannot supply.

        A field is only inherited from the stored record when the fresh message
        omits it — a freshly re-derived value (e.g. is_muted recomputed on a
        re-download) always wins.
        """
        merged = {m["id"]: m for m in existing_msgs}
        for pm in processed:
            prev = merged.get(pm["id"])
            if prev:
                for key in SyncManager._LOCAL_DERIVED_FIELDS:
                    if key not in pm and key in prev:
                        pm[key] = prev[key]
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
            logger.warning("saka.scan.manifest_unreadable", file=str(messages_file), error=str(e))
            result["error"] = "manifest_unreadable"
            return result

        if not isinstance(data, dict):
            logger.warning("saka.scan.manifest_invalid", file=str(messages_file))
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
            progress_callback: Optional callback.

        Returns:
            Dict mapping member_dir to {message_id: metadata_dict} for downloaded media.
            metadata_dict contains: width, height, media_duration, is_muted (as applicable).
        """
        if not queue:
            return {}

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
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(completed, total)
                    else:
                        progress_callback(completed, total)

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
                logger.error("Failed to write message metadata", file=str(messages_file))
        except Exception as e:
            logger.error("Failed to update message metadata", file=str(messages_file), error=str(e))

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
