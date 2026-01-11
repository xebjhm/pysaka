import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiofiles
import aiohttp
import structlog

from .client import GROUP_CONFIG, Client
from .media import get_media_dimensions
from .utils import get_media_extension, normalize_message, sanitize_name

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
                with open(self.state_file, encoding='utf-8') as f:
                    self.sync_state = json.load(f)
            except Exception as e:
                logger.error("Failed to load sync state", error=str(e))
                self.sync_state = {}

    def save_sync_state(self) -> None:
        """Save synchronization state to JSON file."""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.sync_state, f, indent=2)
        except Exception as e:
            logger.error("Failed to save sync state", error=str(e))

    def update_sync_state(self, group_id: int, member_id: int, last_msg_id: int, count: int) -> None:
        """
        Update state for a specific member after sync.

        Args:
            group_id: ID of the group/artist.
            member_id: ID of the member.
            last_msg_id: The highest message ID synced.
            count: Number of messages synced in this batch.
        """
        key = f"{group_id}_{member_id}"
        self.sync_state[key] = {
            "last_message_id": last_msg_id,
            "total_messages": count,
            "last_sync": datetime.now(timezone.utc).isoformat() + "Z"
        }
        self.save_sync_state()

    def get_last_id(self, group_id: int, member_id: int) -> Optional[int]:
        """
        Get the last synced message ID for a member.

        Args:
            group_id: ID of the group/artist.
            member_id: ID of the member.

        Returns:
            The message ID or None if never synced.
        """
        key = f"{group_id}_{member_id}"
        state = self.sync_state.get(key)
        if state:
            return state.get('last_message_id')
        return None

    async def sync_member(
        self,
        session: aiohttp.ClientSession,
        group: dict[str, Any],
        member: dict[str, Any],
        media_queue: list[dict[str, Any]],
        progress_callback: Optional[Any] = None
    ) -> int:
        """
        Syncs messages for a member and prepares media queue.

        Args:
            session: Active aiohttp session.
            group: Group object dict.
            member: Member object dict.
            media_queue: List to append media download tasks to.
            progress_callback: Optional callback for progress updates.

        Returns:
            Number of new messages processed.
        """
        gid = group['id']
        mid = member['id']
        gname = sanitize_name(group['name'])
        mname = sanitize_name(member['name'])

        service_name = GROUP_CONFIG[self.client.group].get("display_name", self.client.group.value)
        group_dir = self.output_dir / service_name / "messages" / f"{gid} {gname}"
        member_dir = group_dir / f"{mid} {mname}"
        member_dir.mkdir(parents=True, exist_ok=True)
        for t in ['picture', 'video', 'voice']:
            (member_dir / t).mkdir(exist_ok=True)

        last_id = self.get_last_id(gid, mid)
        logger.info("Syncing member", member=mname, member_id=mid, last_id=last_id)

        try:
            messages = await self.client.get_messages(
                session, gid, since_id=last_id, progress_callback=progress_callback
            )
            logger.info("Fetched messages", count=len(messages), group_id=gid)

            # Filter for member
            messages = [x for x in messages if x.get('member_id') == mid]
            logger.info("Filtered messages for member", count=len(messages), member=mname)

            if not messages:
                return 0

            # Process & Prepare
            processed = self.prepare_messages(messages, member_dir, media_queue)

            # Load existing
            existing_file = member_dir / "messages.json"
            existing_msgs: list[dict[str, Any]] = []
            if existing_file.exists():
                try:
                    async with aiofiles.open(existing_file, encoding='utf-8') as f:
                        data = json.loads(await f.read())
                        existing_msgs = data.get('messages', [])
                except Exception:
                    pass

            # Dedupe (Upsert: Prefer new data)
            merged_dict = {x['id']: x for x in existing_msgs}
            for pm in processed:
                merged_dict[pm['id']] = pm

            merged = list(merged_dict.values())
            merged.sort(key=lambda x: x.get('timestamp') or '')

            # Stats
            type_counts = {"text": 0, "video": 0, "picture": 0, "voice": 0}
            for msg in merged:
                mtype = msg.get('type', 'text')
                if mtype in type_counts:
                    type_counts[mtype] += 1

            # Save
            export_data = {
                "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
                "member": {
                    "id": mid,
                    "name": mname,
                    "group_id": gid,
                    "portrait": member.get('portrait'),
                    "thumbnail": member.get('thumbnail'),
                    "phone_image": member.get('phone_image'),
                    "group_thumbnail": group.get('thumbnail'),
                    "is_active": group.get('subscription', {}).get('state') == 'active'
                },
                "total_messages": len(merged),
                "message_type_counts": type_counts,
                "messages": merged
            }

            async with aiofiles.open(existing_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(export_data, ensure_ascii=False, indent=2))

            # Update State
            max_id = max(x['id'] for x in merged) if merged else (last_id or 0)
            if max_id is not None:
                self.update_sync_state(gid, mid, max_id, len(merged))

            return len(processed)

        except Exception as e:
            logger.error("Error syncing member", member=mname, error=str(e), exc_info=True)
            return 0

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        member_dir: Path,
        queue: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Normalize messages and queue media downloads.

        Args:
            messages: List of raw message dicts.
            member_dir: Directory for member's files.
            queue: Media download waiting queue.

        Returns:
            List of processed message dicts.
        """
        processed = []
        for msg in messages:
            try:
                # Normalize core fields
                p_msg = normalize_message(msg)
                msg_type = p_msg['type']
                raw_type = p_msg.pop('_raw_type', 'text') # Remove internal helper field

                # Media
                media_url = msg.get('file') or msg.get('thumbnail')
                if media_url:
                    ext = get_media_extension(media_url, raw_type)

                    subdir = 'other'
                    if msg_type == 'picture':
                        subdir = 'picture'
                    elif msg_type == 'video':
                        subdir = 'video'
                    elif msg_type == 'voice':
                        subdir = 'voice'

                    filepath = member_dir / subdir / f"{msg['id']}.{ext}"

                    # Logic: If file doesn't exist, queue it.
                    if not filepath.exists():
                        queue.append({
                            'url': media_url,
                            'path': filepath,
                            'timestamp': msg.get('published_at'),
                            'message_id': msg['id'],
                            'media_type': msg_type,
                            'member_dir': member_dir,
                        })

                    p_msg['media_file'] = str(filepath.relative_to(self.output_dir))

                    # Extract dimensions if file exists (already downloaded or will be processed)
                    if filepath.exists():
                        width, height = get_media_dimensions(filepath, msg_type)
                        if width and height:
                            p_msg['width'] = width
                            p_msg['height'] = height

                processed.append(p_msg)
            except Exception as e:
                mid = msg.get('id')
                logger.error("Prepare error", message_id=mid, error=str(e))
        return processed

    async def process_media_queue(
        self,
        session: aiohttp.ClientSession,
        queue: list[dict[str, Any]],
        concurrency: int = 5,
        progress_callback: Optional[Any] = None
    ) -> dict[Path, dict[int, tuple[Optional[int], Optional[int]]]]:
        """
        Downloads files in the queue using a semaphore for concurrency.

        Args:
            session: Active aiohttp session.
            queue: List of media items to download.
            concurrency: Max concurrent downloads.
            progress_callback: Optional callback.

        Returns:
            Dict mapping member_dir to {message_id: (width, height)} for downloaded media.
        """
        if not queue:
            return {}

        sem = asyncio.Semaphore(concurrency)
        total = len(queue)
        completed = 0
        # Group dimensions by member_dir for efficient batch updates
        dimensions_by_dir: dict[Path, dict[int, tuple[Optional[int], Optional[int]]]] = {}

        async def worker(item: dict[str, Any]) -> None:
            nonlocal completed
            async with sem:
                res = await self.client.download_file(
                    session,
                    item['url'],
                    item['path'],
                    item['timestamp']
                )
                if res:
                    # Extract dimensions after successful download
                    media_type = item.get('media_type', '')
                    member_dir = item.get('member_dir')
                    if media_type in ('picture', 'video') and member_dir:
                        width, height = get_media_dimensions(item['path'], media_type)
                        if width and height:
                            if member_dir not in dimensions_by_dir:
                                dimensions_by_dir[member_dir] = {}
                            dimensions_by_dir[member_dir][item['message_id']] = (width, height)

                    completed += 1
                    if progress_callback:
                        if asyncio.iscoroutinefunction(progress_callback):
                            await progress_callback(completed, total)
                        else:
                            progress_callback(completed, total)

        await asyncio.gather(*[worker(item) for item in queue])
        return dimensions_by_dir

    async def update_message_dimensions(
        self,
        messages_file: Path,
        dimensions: dict[int, tuple[Optional[int], Optional[int]]]
    ) -> None:
        """
        Update messages.json with extracted media dimensions.

        Args:
            messages_file: Path to messages.json file.
            dimensions: Dict mapping message_id to (width, height).
        """
        if not dimensions or not messages_file.exists():
            return

        try:
            async with aiofiles.open(messages_file, encoding='utf-8') as f:
                data = json.loads(await f.read())

            updated = False
            for msg in data.get('messages', []):
                msg_id = msg.get('id')
                if msg_id in dimensions:
                    width, height = dimensions[msg_id]
                    if width and height:
                        msg['width'] = width
                        msg['height'] = height
                        updated = True

            if updated:
                async with aiofiles.open(messages_file, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(data, ensure_ascii=False, indent=2))

        except Exception as e:
            logger.error("Failed to update message dimensions", file=str(messages_file), error=str(e))
