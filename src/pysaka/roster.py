"""Who authored the messages of one room.

``GET /groups/{id}/members`` returns a room's *current* lineup, not everyone who
ever posted there: rotating-lineup rooms (one room reused for many live events)
and official rooms (only the official account is listed) hold messages from
members missing from that list. These helpers derive the authors from the
messages themselves. No I/O.
"""

from collections.abc import Iterable
from typing import Any

ROOM_AUTHOR_ID = 0
"""Member id for messages the API returns without ``member_id``. The official app
shows those as the room itself (room name and logo)."""


def author_id(message: dict[str, Any]) -> int:
    """The author's member id, or ``ROOM_AUTHOR_ID`` when the message has none."""
    mid = message.get("member_id")
    if isinstance(mid, int) and not isinstance(mid, bool):
        return mid
    if isinstance(mid, str) and mid.isdecimal():
        return int(mid)
    return ROOM_AUTHOR_ID


def extra_author_ids(api_member_ids: Iterable[int], messages: Iterable[dict[str, Any]]) -> list[int]:
    """Authors of ``messages`` missing from ``api_member_ids``.

    Real member ids come first in ascending order; ``ROOM_AUTHOR_ID`` comes last
    when any message has no author.
    """
    known = set(api_member_ids)
    authors = {author_id(m) for m in messages}
    real = sorted(a for a in authors if a != ROOM_AUTHOR_ID and a not in known)
    room = [ROOM_AUTHOR_ID] if ROOM_AUTHOR_ID in authors and ROOM_AUTHOR_ID not in known else []
    return real + room


def room_author(group: dict[str, Any]) -> dict[str, Any]:
    """The member dict that stands for the room itself (member 0)."""
    return {"id": ROOM_AUTHOR_ID, "name": group.get("name") or str(group["id"]), "thumbnail": group.get("thumbnail")}
