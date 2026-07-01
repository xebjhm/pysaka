from __future__ import annotations

import unicodedata

from .models import CanonicalId, Member

_WHITESPACE = (" ", "\t", "\n", "\r", "　")  # ASCII space/tab/newlines + ideographic space


def _normkey(name: str) -> str:
    """Normalize a member name into a lookup key.

    Applies NFKC normalization then strips all whitespace (ASCII space/tab and the
    full-width ideographic space `　`) so that e.g. "金村 美玖" and "金村　美玖"
    (full-width space) resolve to the same key.
    """
    normalized = unicodedata.normalize("NFKC", name)
    for ch in _WHITESPACE:
        normalized = normalized.replace(ch, "")
    return normalized


class MemberRegistry:
    """Roster of known Members for a group, with name-based reconciliation.

    Holds members loaded from `members.json` (keyed by normalized kanji name) plus
    any auto-provisioned Members created when an unrecognized author name is
    resolved. Auto-provisioned members are tracked separately so `.unaliased()`
    can report the set that still needs curated aliases (Task 5).
    """

    def __init__(self) -> None:
        self._by_id: dict[CanonicalId, Member] = {}
        self._by_normname: dict[str, CanonicalId] = {}
        self._provisional_ids: set[CanonicalId] = set()
        self._group: str = ""

    @classmethod
    def from_members_json(cls, data: dict, group: str) -> MemberRegistry:
        """Build a registry from a `members.json` payload for `group`.

        `data` shape: `{"meta": {...}, "members": [{"blogId": ..., "nameKanji": ...,
        "nameHiragana": ..., "nameRomaji": ..., "generation": ..., "status": ...}, ...]}`.
        """
        registry = cls()
        registry._group = group
        for entry in data.get("members", []):
            blog_id = str(entry["blogId"])
            name = entry["nameKanji"]
            member = Member(
                canonical_id=f"{group}:{blog_id}",
                group=group,
                name=name,
                name_hiragana=entry.get("nameHiragana", ""),
                name_romaji=entry.get("nameRomaji", ""),
                generation=entry.get("generation", 0),
                status=entry.get("status", "active"),
                blog_id=blog_id,
                aliases=[],
            )
            registry._add(member, _normkey(name))
        return registry

    def _add(self, member: Member, normname: str) -> None:
        self._by_id[member.canonical_id] = member
        self._by_normname[normname] = member.canonical_id

    def _provision(self, name: str, group: str, normname: str) -> Member:
        member = Member(
            canonical_id=f"{group}:auto:{normname}",
            group=group,
            name=name,
            name_hiragana="",
            name_romaji="",
            generation=0,
            status="active",
            blog_id="",
            aliases=[],
        )
        self._add(member, normname)
        self._provisional_ids.add(member.canonical_id)
        return member

    def resolve_author(self, name: str, group: str) -> CanonicalId:
        """Resolve an author's display name to a stable canonical id.

        Looks up `name` (NFKC + whitespace-normalized) against the roster. If no
        roster member matches, auto-provisions a synthetic Member keyed by the
        normalized name and returns its canonical id. Re-resolving the same
        unknown name is idempotent (returns the same id, no re-provisioning).
        """
        normname = _normkey(name)
        canonical_id = self._by_normname.get(normname)
        if canonical_id is not None:
            return canonical_id
        return self._provision(name, group, normname).canonical_id

    def link_message_ids(self, group_id: int, member_id: int, name: str) -> None:
        """Attach message-service ids to the Member resolved from `name`.

        A registry is scoped to a single roster group (set by `from_members_json`);
        resolves `name` within that group (auto-provisioning if unknown, same as
        `resolve_author`), then sets `message_group_id`/`message_member_id` on the
        resolved Member.
        """
        canonical_id = self.resolve_author(name, self._group)
        member = self._by_id[canonical_id]
        member.message_group_id = group_id
        member.message_member_id = member_id

    def get(self, canonical_id: CanonicalId) -> Member | None:
        return self._by_id.get(canonical_id)

    def all(self) -> list[Member]:
        return list(self._by_id.values())

    def unaliased(self) -> list[Member]:
        """Return auto-provisioned Members (authors not found in members.json)."""
        return [self._by_id[cid] for cid in self._provisional_ids]
