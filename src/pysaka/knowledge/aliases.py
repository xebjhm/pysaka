from __future__ import annotations

from .models import CanonicalId, Member, Scope
from .registry import MemberRegistry, normalize_name


class AliasTable:
    """Alias -> canonical id lookup, scoped to a single roster group.

    Two sources feed the table:
    - `seed_from_registry`: mechanically-derived aliases (full kanji/hiragana/romaji,
      whitespace-removed kanji/hiragana, given-name-only for each script) for every
      `Member` in a `MemberRegistry`.
    - `load_curated`: hand-curated nicknames from `data/knowledge/<service>/aliases.json`,
      merged on top of the seed (never replaces it).

    Lookup keys are normalized with `registry.normalize_name` (NFKC + whitespace-strip)
    plus `casefold()` so romaji aliases match case-insensitively; the original surface
    form is preserved for `aliases_for`/`entries`. An alias mapped to multiple members
    is ambiguous by design — `resolve` returns all of them.
    """

    def __init__(self, group: str = "") -> None:
        self._group = group
        self._by_key: dict[str, set[CanonicalId]] = {}
        self._surfaces: dict[CanonicalId, set[str]] = {}

    @staticmethod
    def _key(alias: str) -> str:
        return normalize_name(alias).casefold()

    def _add(self, alias: str, canonical_id: CanonicalId) -> None:
        alias = alias.strip()
        if not alias:
            return
        key = self._key(alias)
        self._by_key.setdefault(key, set()).add(canonical_id)
        self._surfaces.setdefault(canonical_id, set()).add(alias)

    @classmethod
    def seed_from_registry(cls, reg: MemberRegistry) -> AliasTable:
        """Derive mechanical aliases for every `Member` in `reg`.

        Seeds, per member: full kanji name (`Member.name`) and its whitespace-removed
        form, full hiragana (`Member.name_hiragana`) and its whitespace-removed form,
        full romaji (`Member.name_romaji`, no whitespace-removed variant), and the
        given-name-only part (substring after the first space) of each of
        kanji/hiragana/romaji. Empty parts are skipped (auto-provisioned members have
        empty hiragana/romaji). The table's group is taken from the members themselves
        (a registry is always single-group), so an empty registry yields group `""`.
        """
        members = reg.all()
        group = members[0].group if members else ""
        table = cls(group=group)
        for member in members:
            table._seed_member(member)
        return table

    def _seed_member(self, member: Member) -> None:
        cid = member.canonical_id
        self._seed_full_and_given(member.name, cid, no_space_variant=True)
        self._seed_full_and_given(member.name_hiragana, cid, no_space_variant=True)
        self._seed_full_and_given(member.name_romaji, cid, no_space_variant=False)

    def _seed_full_and_given(self, full: str, canonical_id: CanonicalId, *, no_space_variant: bool) -> None:
        if not full:
            return
        self._add(full, canonical_id)
        if no_space_variant:
            self._add(normalize_name(full), canonical_id)
        given = self._given_name(full)
        if given:
            self._add(given, canonical_id)

    @staticmethod
    def _given_name(full: str) -> str:
        """The part after the first whitespace run, or "" if there's no space."""
        parts = full.split(None, 1)
        return parts[1] if len(parts) > 1 else ""

    def load_curated(self, data: dict) -> None:
        """Merge curated aliases on top of the seed.

        `data` shape: `{"members": {"<canonical_id>": {"aliases": [...], ...}, ...}}`
        (the `data/knowledge/<service>/aliases.json` format). Any extra keys per member
        entry besides `"aliases"` are ignored by this loader.
        """
        for canonical_id, entry in data.get("members", {}).items():
            for alias in entry.get("aliases", []):
                self._add(alias, canonical_id)

    def aliases_for(self, canonical_id: CanonicalId) -> list[str]:
        """Distinct aliases mapped to `canonical_id`, sorted, in original surface form."""
        return sorted(self._surfaces.get(canonical_id, ()))

    def entries(self, group: str) -> list[tuple[str, CanonicalId]]:
        """Every `(alias_surface, canonical_id)` pair for `group` (feeds Task 6's scan)."""
        self._check_group(group)
        return [(alias, cid) for cid, aliases in self._surfaces.items() for alias in aliases]

    def resolve(self, text: str, scope: Scope) -> list[CanonicalId]:
        """Resolve `text` (normalized the same way alias keys are) to canonical ids.

        Scoped to `scope.service` / the table's group. An alias mapping to multiple
        members returns all of them (ambiguity), sorted. No match -> empty list.
        """
        self._check_group(scope.service)
        return sorted(self._by_key.get(self._key(text), ()))

    def _check_group(self, group: str) -> None:
        if group != self._group:
            raise ValueError(f"AliasTable is scoped to group {self._group!r}, got {group!r}")
