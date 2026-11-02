"""Pure helpers for archiving member profile images with history.

Three images describe a talk room member: the list icon (the room's
``group.thumbnail``), the chat icon (``member.thumbnail``) and the call screen
(``member.phone_image``). Their CDN basenames embed an upload timestamp and change
when the member changes the image, so archiving every distinct basename yields a
history. This module does no I/O; :meth:`pysaka.manager.SyncManager.archive_profile_images`
does the downloads and writes ``index.json``.
"""

from typing import Any, Optional
from urllib.parse import urlparse

from .utils import sanitize_name

KINDS = ("thumbnail", "phone_image", "group_thumbnail")
DIR_NAME = "profile_images"
INDEX_NAME = "index.json"
INDEX_VERSION = 1


def image_urls(group: dict[str, Any], member: dict[str, Any]) -> dict[str, str]:
    """Map each kind to its current CDN URL, omitting kinds without one."""
    raw = {
        "thumbnail": member.get("thumbnail"),
        "phone_image": member.get("phone_image"),
        "group_thumbnail": group.get("thumbnail"),
    }
    return {kind: url for kind, url in raw.items() if url}


def relative_file(kind: str, url: str) -> Optional[str]:
    """Path of the archived file relative to ``profile_images/``, or None.

    Uses the URL's basename (query/fragment dropped) after ``sanitize_name``, so it
    can never contain a path separator or ``..`` and always stays inside the
    kind's directory.
    """
    basename = urlparse(url).path.rsplit("/", 1)[-1]
    safe = sanitize_name(basename)
    if not safe or safe == ".":
        return None
    return f"{kind}/{safe}"


def merge_index(
    current: Optional[Any],
    archived: dict[str, tuple[str, str]],
    current_urls: dict[str, str],
    now_iso: str,
) -> Optional[dict[str, Any]]:
    """Return the updated index, or None if nothing changed.

    Args:
        current: Parsed existing ``index.json`` (None, or invalid — not a dict, no
            ``images`` list, or a non-dict entry in it — → start fresh).
        archived: kind → (relative file, url) for images present on disk now.
        current_urls: kind → URL from the latest API data (recorded even when the
            download has not succeeded yet, so consumers know what is current).
        now_iso: UTC timestamp recorded as ``first_seen`` for new entries.
    """
    images = current.get("images") if isinstance(current, dict) else None
    valid = isinstance(images, list) and all(isinstance(e, dict) for e in images)
    if not valid and not archived and not current_urls:
        return None
    index: dict[str, Any]
    if isinstance(current, dict) and valid:
        index = current
    else:
        index = {"version": INDEX_VERSION, "current": {}, "images": []}
    changed = not valid
    known = {(e.get("kind"), e.get("file")) for e in index["images"]}
    for kind, (rel, url) in archived.items():
        if (kind, rel) not in known:
            index["images"].append({"kind": kind, "file": rel, "url": url, "first_seen": now_iso})
            changed = True
    if index.get("current") != current_urls:
        index["current"] = dict(current_urls)
        changed = True
    return index if changed else None
