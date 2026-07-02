from __future__ import annotations

from pysaka.knowledge.callnames import CallNameTable
from pysaka.knowledge.cleaner import SUBSCRIBER_SENTINEL
from pysaka.knowledge.mentions import MentionDetector

ENTRIES = [("みくちゃん", "g:12"), ("かとし", "g:20"), ("みく", "g:12")]


def test_detects_alias_mention_excluding_self():
    d = MentionDetector(ENTRIES)
    assert d.detect("今日はかとしと会った", author_id="g:12") == ["g:20"]


def test_self_mention_excluded():
    d = MentionDetector(ENTRIES)
    assert d.detect("みくちゃんです", author_id="g:12") == []


def test_short_kana_guard_avoids_substring_false_positive():
    d = MentionDetector(ENTRIES)
    # "みく" must NOT fire inside an unrelated word like "みくびる"
    assert "g:12" not in d.detect("みくびるのは良くない", author_id="g:99")


def test_detects_multiple_distinct_members_in_one_text():
    d = MentionDetector(ENTRIES)
    assert d.detect("みくちゃんとかとしが話した", author_id="g:99") == ["g:12", "g:20"]


def test_short_kana_guard_allows_word_bounded_match():
    d = MentionDetector(ENTRIES)
    # "みく" surrounded by punctuation/boundaries (non-kana) IS a valid mention.
    assert d.detect("みく、元気？", author_id="g:99") == ["g:12"]


def test_sentinel_alias_is_skipped():
    entries = [(SUBSCRIBER_SENTINEL, "g:12"), ("かとし", "g:20")]
    d = MentionDetector(entries)
    assert d.detect(f"{SUBSCRIBER_SENTINEL}とかとし", author_id="g:99") == ["g:20"]


def test_ambiguous_alias_preserves_both_members():
    # Same alias string maps to two distinct members (spec §6.4: "an alias that maps to
    # two members stores both") — must not be lost to pyahocorasick's last-write-wins.
    entries = [("さくちゃん", "g:46"), ("さくちゃん", "g:99")]
    d = MentionDetector(entries)
    # Third party (doesn't share the alias): both candidates are genuinely ambiguous mentions.
    assert d.detect("さくちゃんだね", author_id="g:1") == ["g:46", "g:99"]
    # Author shares the alias with a namesake: precision fix — this is self-shadowing, not
    # a mention of the namesake. Without directional call-name evidence pointing elsewhere,
    # the whole hit is treated as a self-reference and suppressed (not leaked to g:99).
    assert d.detect("さくちゃんだね", author_id="g:46") == []


def test_self_shadowing_collision_is_suppressed_not_leaked_to_namesake():
    # Real false-positive class: given-name collisions where two members read the same
    # (e.g. 蔵盛 妃那乃 and 上村 ひなの both "ひなの"). Without directional evidence, an
    # author saying her own nickname must NOT be mis-attributed as mentioning the namesake.
    entries = [("ひなの", "g:H"), ("ひなの", "g:U")]
    d = MentionDetector(entries)
    assert d.detect("今日はひなのだよ", author_id="g:H") == []
    # A third party using the same alias still gets both ambiguous candidates.
    result = d.detect("今日はひなのだよ", author_id="g:other")
    assert result == ["g:H", "g:U"]


def test_directional_call_name_resolves_ambiguity_for_the_caller():
    # Curated call_names.json data (who calls whom what) is authoritative: if the author is
    # known to address a specific person by this alias, that resolves the ambiguity even
    # though the alias also maps to an unrelated third member.
    entries = [("まなみん", "g:B"), ("まなみん", "g:C")]
    edge = {"caller_id": "g:A", "caller_name": "A", "callee_id": "g:B", "callee_name": "B", "names": ["まなみん"]}
    call_names = CallNameTable.from_json({"edges": [edge]})
    d = MentionDetector(entries, call_names)
    assert d.detect("まなみんと会った", author_id="g:A") == ["g:B"]
