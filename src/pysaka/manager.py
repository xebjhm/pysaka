import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiofiles
import aiohttp
import structlog

from .client import Client
from .media import get_audio_metadata, get_media_dimensions
from .utils import get_media_extension, media_file_is_present, normalize_message, sanitize_name

logger = structlog.get_logger()


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
        self.sync_state[key] = {
            "last_message_id": last_msg_id,
            "last_sync_ts": last_ts,
            "total_messages": count,
            "last_sync": datetime.now(timezone.utc).isoformat() + "Z",
        }
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

    async def sync_member(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        media_queue: list[dict[str, Any]],
        progress_callback: Optional[Any] = None,
        prefetched_messages: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        """
        Syncs messages for a member and prepares media queue.

        Args:
            session: Active aiohttp session.
            group: Group object dict.
            member: Member object dict.
            media_queue: List to append media download tasks to.
            progress_callback: Optional callback for progress updates.
            prefetched_messages: Pre-fetched group timeline messages. When
                provided, the API call is skipped and messages are filtered
                from this list instead (using this member's own last_id).

        Returns:
            Number of new messages processed.
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

        try:
            if prefetched_messages is not None:
                # Pre-fetched: filter by member_id AND this member's timestamp cursor
                messages = [
                    x
                    for x in prefetched_messages
                    if x.get("member_id") == mid and (last_ts is None or (x.get("published_at") or "") >= last_ts)
                ]
                logger.info("Filtered prefetched messages for member", count=len(messages), member=mname)
            else:
                messages = await self.client.get_messages(
                    session, gid, since_ts=last_ts, progress_callback=progress_callback
                )
                logger.info("Fetched messages", count=len(messages), group_id=gid)

                # Filter for member
                messages = [x for x in messages if x.get("member_id") == mid]
                logger.info("Filtered messages for member", count=len(messages), member=mname)

            if not messages:
                return 0

            # Process & Prepare
            processed, earliest_failed_ts, earliest_pending_media_ts = self.prepare_messages(
                messages, member_dir, media_queue
            )

            # Load existing
            existing_file = member_dir / "messages.json"
            existing_msgs: list[dict[str, Any]] = []
            if existing_file.exists():
                try:
                    async with aiofiles.open(existing_file, encoding="utf-8") as f:
                        data = json.loads(await f.read())
                        existing_msgs = data.get("messages", [])
                except Exception:
                    # Corrupt file (e.g. force-close during write).
                    # Reset this member's last_id so the next sync
                    # re-fetches from the beginning to recover.
                    logger.warning(
                        "corrupt_messages_file",
                        member=mname,
                        member_id=mid,
                        group_id=gid,
                    )
                    self.sync_state.pop(f"{gid}_{mid}", None)
                    self.save_sync_state()

            # Integrity check: if the file has fewer messages than sync_state
            # recorded, data was lost (e.g. past force-close overwrote the
            # file with only new messages).  Reset last_id so the next sync
            # does a full re-fetch to recover.
            state_key = f"{gid}_{mid}"
            expected = (self.sync_state.get(state_key) or {}).get("total_messages", 0)
            if expected > 0 and len(existing_msgs) < expected:
                logger.warning(
                    "message_count_mismatch",
                    member=mname,
                    member_id=mid,
                    group_id=gid,
                    expected=expected,
                    actual=len(existing_msgs),
                )
                self.sync_state.pop(state_key, None)
                self.save_sync_state()

            # Dedupe (Upsert: Prefer new data)
            merged_dict = {x["id"]: x for x in existing_msgs}
            for pm in processed:
                merged_dict[pm["id"]] = pm

            merged = list(merged_dict.values())
            merged.sort(key=lambda x: x.get("timestamp") or "")

            # Stats
            type_counts = {"text": 0, "video": 0, "picture": 0, "voice": 0}
            for msg in merged:
                mtype = msg.get("type", "text")
                if mtype in type_counts:
                    type_counts[mtype] += 1

            # Save
            export_data = {
                "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
                "member": {
                    "id": mid,
                    "name": mname,
                    "group_id": gid,
                    "portrait": member.get("portrait"),
                    "thumbnail": member.get("thumbnail"),
                    "phone_image": member.get("phone_image"),
                    "group_thumbnail": group.get("thumbnail"),
                },
                "total_messages": len(merged),
                "message_type_counts": type_counts,
                "messages": merged,
            }

            wrote = await asyncio.to_thread(self._atomic_write_json, existing_file, export_data)
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

            return len(processed)

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

    def scan_member_media(self, member_dir: Path) -> dict[str, Any]:
        """
        Offline scan of a member's messages.json for missing media.

        A media file is 'missing' if it does not exist OR has zero bytes.
        Expected paths are resolved as ``self.output_dir / msg["media_file"]``.

        Returns:
            ``{"checked": int, "missing": list[dict], "unresolved": list[dict]}``
            where each missing descriptor is ``{"message_id", "media_type",
            "path": Path, "timestamp"}`` and each ``unresolved`` descriptor is
            ``{"message_id", "media_type", "timestamp"}`` (no path). ``unresolved``
            lists media-type messages that have no recorded ``media_file`` (the
            media URL was absent at sync time, e.g. a source-removed stub): they
            cannot be located or verified on disk, so they are reported separately
            rather than silently ignored — otherwise the result would claim 'all
            present' while such media is genuinely absent. Returns zero/empty if
            messages.json is absent or unreadable.
        """
        result: dict[str, Any] = {"checked": 0, "missing": [], "unresolved": []}
        messages_file = member_dir / "messages.json"
        if not messages_file.exists():
            return result
        try:
            with open(messages_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("scan: unreadable messages.json", file=str(messages_file), error=str(e))
            return result

        if not isinstance(data, dict):
            logger.warning("scan: messages.json is not a JSON object", file=str(messages_file))
            return result

        for msg in data.get("messages", []):
            mtype = msg.get("type")
            if mtype not in ("picture", "video", "voice"):
                continue
            media_file = msg.get("media_file")
            if not media_file:
                # A non-"published" message (e.g. state="canceled" — the member
                # withdrew the post, which strips its media) legitimately has no
                # media and is NOT a completeness gap. The state is recorded on
                # disk; skip it silently.
                state = msg.get("state")
                if state and state != "published":
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
        """
        if not metadata or not messages_file.exists():
            return

        try:
            async with aiofiles.open(messages_file, encoding="utf-8") as f:
                data = json.loads(await f.read())

            updated = False
            for msg in data.get("messages", []):
                msg_id = msg.get("id")
                if msg_id in metadata:
                    msg_metadata = metadata[msg_id]
                    for key, value in msg_metadata.items():
                        if value is not None:
                            msg[key] = value
                            updated = True

            if updated:
                await asyncio.to_thread(self._atomic_write_json, messages_file, data)

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
