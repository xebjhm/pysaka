from __future__ import annotations

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
    assert d.detect("さくちゃんだね", author_id="g:1") == ["g:46", "g:99"]
    # Self excluded, ambiguous partner still kept.
    assert d.detect("さくちゃんだね", author_id="g:46") == ["g:99"]
