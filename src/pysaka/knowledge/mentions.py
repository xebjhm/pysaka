from __future__ import annotations

import ahocorasick

from .cleaner import SUBSCRIBER_SENTINEL
from .models import CanonicalId

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
    """Aho-Corasick multi-pattern scan for alias mentions, with a short-kana guard.

    Built from a flat list of `(alias, canonical_id)` pairs (e.g. `AliasTable.entries(group)`
    for a single group). Short (<=2 char) all-kana aliases are prone to firing as false-positive
    substrings inside unrelated longer kana words (e.g. "みく" inside "みくびる"), so those hits
    are rejected unless they are word-bounded: kana characters must not sit immediately before
    or after the match.
    """

    def __init__(self, alias_entries: list[tuple[str, CanonicalId]]) -> None:
        self._automaton: ahocorasick.Automaton = ahocorasick.Automaton()
        by_alias: dict[str, set[CanonicalId]] = {}
        for alias, canonical_id in alias_entries:
            if alias == SUBSCRIBER_SENTINEL:
                continue
            by_alias.setdefault(alias, set()).add(canonical_id)
        for alias, canonical_ids in by_alias.items():
            self._automaton.add_word(alias, (alias, tuple(sorted(canonical_ids))))
        self._automaton.make_automaton()

    def detect(self, text: str, author_id: CanonicalId) -> list[CanonicalId]:
        """Distinct, sorted canonical ids mentioned in `text`, excluding `author_id` (self-mentions).

        An alias may be ambiguous (map to multiple canonical ids, e.g. two members sharing a
        nickname); all non-self candidates for a matched alias are kept (spec §6.4).
        """
        found: set[CanonicalId] = set()
        for end_index, (alias, canonical_ids) in self._automaton.iter(text):
            if self._is_guarded_false_positive(text, alias, end_index):
                continue
            for canonical_id in canonical_ids:
                if canonical_id == author_id:
                    continue
                found.add(canonical_id)
        return sorted(found)

    @staticmethod
    def _is_guarded_false_positive(text: str, alias: str, end_index: int) -> bool:
        if len(alias) > 2 or not _is_all_kana(alias):
            return False
        start = end_index - len(alias) + 1
        before_is_kana = start - 1 >= 0 and is_kana(text[start - 1])
        after_is_kana = end_index + 1 < len(text) and is_kana(text[end_index + 1])
        return before_is_kana or after_is_kana
