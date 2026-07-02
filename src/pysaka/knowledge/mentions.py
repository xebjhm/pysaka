from __future__ import annotations

import ahocorasick

from .callnames import CallNameTable
from .cleaner import SUBSCRIBER_SENTINEL
from .models import CanonicalId
from .registry import normalize_name

_HIRAGANA_START, _HIRAGANA_END = 0x3040, 0x309F
_KATAKANA_START, _KATAKANA_END = 0x30A0, 0x30FF


def is_kana(char: str) -> bool:
    """True if `char` is a single Hiragana (U+3040-U+309F) or Katakana (U+30A0-U+30FF) codepoint.

    The Katakana range includes the prolonged sound mark "ー" (U+30FC).
    """
    codepoint = ord(char)
    return _HIRAGANA_START <= codepoint <= _HIRAGANA_END or _KATAKANA_START <= codepoint <= _KATAKANA_END


def _is_all_kana(s: str) -> bool:
    return len(s) > 0 and all(is_kana(c) for c in s)


class MentionDetector:
    """Aho-Corasick multi-pattern scan for alias mentions, with a short-kana guard and
    an optional directional/self-aware disambiguation layer.

    Built from a flat list of `(alias, canonical_id)` pairs (e.g. `AliasTable.entries(group)`
    for a single group). Short (<=2 char) all-kana aliases are prone to firing as false-positive
    substrings inside unrelated longer kana words (e.g. "みく" inside "みくびる"), so those hits
    are rejected unless they are word-bounded: kana characters must not sit immediately before
    or after the match.

    An optional `CallNameTable` (curated `call_names.json`: who calls whom what) resolves
    ambiguous aliases directionally for the author, and also fixes a real false-positive class:
    given-name collisions where two members share a reading (e.g. 蔵盛 妃那乃 and 上村 ひなの
    both read "ひなの"). Without directional evidence, a member using her own nickname must not
    be mis-attributed as mentioning her namesake -- see `detect` for the precise resolution order.
    """

    def __init__(
        self,
        alias_entries: list[tuple[str, CanonicalId]],
        call_names: CallNameTable | None = None,
    ) -> None:
        self._automaton: ahocorasick.Automaton = ahocorasick.Automaton()
        by_alias: dict[str, set[CanonicalId]] = {}
        for alias, canonical_id in alias_entries:
            if alias == SUBSCRIBER_SENTINEL:
                continue
            by_alias.setdefault(alias, set()).add(canonical_id)
        for alias, canonical_ids in by_alias.items():
            self._automaton.add_word(alias, (alias, tuple(sorted(canonical_ids))))
        self._automaton.make_automaton()
        self._call_names = call_names

    def detect(self, text: str, author_id: CanonicalId) -> list[CanonicalId]:
        """Distinct, sorted canonical ids mentioned in `text`, for `author_id`.

        Each Aho-Corasick hit (alias, `canonical_ids` = every roster member sharing that
        alias, per the ambiguity-preserving design of spec §6.4) is resolved in this order:

        1. Short-kana guard (unchanged): word-bounds short all-kana aliases to reject
           substring false positives inside unrelated words.
        2. Directional preference (authoritative): if a `CallNameTable` was supplied and
           `author_id` is known to address someone by this alias, that callee (or callees)
           is the mention -- this resolves ambiguity even when the alias also collides
           with the author's own name.
        3. Self-shadowing: otherwise, if `author_id` is itself among the alias's
           candidates (the alias is ALSO the author's own name -- e.g. two members who
           share a given-name reading), the whole hit is treated as a self-reference and
           suppressed, rather than leaking it to an unrelated namesake.
        4. Default: otherwise every candidate but `author_id` is kept (ambiguity preserved).
        """
        found: set[CanonicalId] = set()
        for end_index, (alias, canonical_ids) in self._automaton.iter(text):
            if self._is_guarded_false_positive(text, alias, end_index):
                continue
            directed_ids: set[CanonicalId] = set()
            if self._call_names is not None:
                directed_ids = self._call_names.directional(author_id, normalize_name(alias))
            if directed_ids:
                found.update(cid for cid in directed_ids if cid != author_id)
            elif author_id in canonical_ids:
                continue
            else:
                found.update(cid for cid in canonical_ids if cid != author_id)
        return sorted(found)

    @staticmethod
    def _is_guarded_false_positive(text: str, alias: str, end_index: int) -> bool:
        if len(alias) > 2 or not _is_all_kana(alias):
            return False
        start = end_index - len(alias) + 1
        before_is_kana = start - 1 >= 0 and is_kana(text[start - 1])
        after_is_kana = end_index + 1 < len(text) and is_kana(text[end_index + 1])
        return before_is_kana or after_is_kana
