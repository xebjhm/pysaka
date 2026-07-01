from __future__ import annotations

import pytest

from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.models import Scope
from pysaka.knowledge.registry import MemberRegistry

MEMBERS = {
    "meta": {"group": "hinatazaka46"},
    "members": [
        {
            "blogId": "12",
            "nameKanji": "金村 美玖",
            "nameHiragana": "かねむら みく",
            "nameRomaji": "Kanemura Miku",
            "generation": 2,
            "status": "active",
        },
        {
            "blogId": "20",
            "nameKanji": "加藤 史帆",
            "nameHiragana": "かとう しほ",
            "nameRomaji": "Kato Shiho",
            "generation": 1,
            "status": "active",
        },
    ],
}


def _registry() -> MemberRegistry:
    return MemberRegistry.from_members_json(MEMBERS, "hinatazaka46")


def test_seed_from_registry_derives_given_name_romaji_and_no_space_kanji():
    table = AliasTable.seed_from_registry(_registry())
    aliases = table.aliases_for("hinatazaka46:12")
    assert "美玖" in aliases  # given-name kanji
    assert "Kanemura Miku" in aliases  # full romaji
    assert "金村美玖" in aliases  # whitespace-removed kanji


def test_seed_from_registry_derives_hiragana_and_given_name_romaji():
    table = AliasTable.seed_from_registry(_registry())
    aliases = table.aliases_for("hinatazaka46:12")
    assert "かねむら みく" in aliases  # full hiragana
    assert "かねむらみく" in aliases  # whitespace-removed hiragana
    assert "みく" in aliases  # given-name hiragana
    assert "Miku" in aliases  # given-name romaji
    assert "金村 美玖" in aliases  # full kanji, unchanged


def test_seed_does_not_add_no_space_romaji_variant():
    table = AliasTable.seed_from_registry(_registry())
    aliases = table.aliases_for("hinatazaka46:12")
    assert "KanemuraMiku" not in aliases


def test_seed_skips_empty_hiragana_and_romaji_for_autoprovisioned_members():
    reg = _registry()
    reg.resolve_author("新加入 太郎", "hinatazaka46")  # auto-provisioned: empty hiragana/romaji
    table = AliasTable.seed_from_registry(reg)
    auto_id = reg.unaliased()[0].canonical_id
    aliases = table.aliases_for(auto_id)
    assert "太郎" in aliases  # kanji given-name still derived
    assert "" not in aliases  # no empty aliases from missing hiragana/romaji


def test_load_curated_skips_blank_alias_entries():
    table = AliasTable.seed_from_registry(_registry())
    table.load_curated({"members": {"hinatazaka46:12": {"aliases": ["   ", "みくちゃん"]}}})
    aliases = table.aliases_for("hinatazaka46:12")
    assert "みくちゃん" in aliases
    assert "" not in aliases
    assert "   " not in aliases


def test_load_curated_merges_on_top_of_seed():
    table = AliasTable.seed_from_registry(_registry())
    table.load_curated({"members": {"hinatazaka46:12": {"aliases": ["みくちゃん"]}}})
    aliases = table.aliases_for("hinatazaka46:12")
    assert "みくちゃん" in aliases
    assert "美玖" in aliases  # seed aliases untouched


def test_resolve_curated_alias_to_canonical_id():
    table = AliasTable.seed_from_registry(_registry())
    table.load_curated({"members": {"hinatazaka46:12": {"aliases": ["みくちゃん"]}}})
    assert table.resolve("みくちゃん", Scope(service="hinatazaka46")) == ["hinatazaka46:12"]


def test_resolve_is_case_insensitive_for_romaji():
    table = AliasTable.seed_from_registry(_registry())
    assert table.resolve("miku", Scope(service="hinatazaka46")) == ["hinatazaka46:12"]


def test_resolve_no_match_returns_empty_list():
    table = AliasTable.seed_from_registry(_registry())
    assert table.resolve("誰でもない", Scope(service="hinatazaka46")) == []


def test_resolve_ambiguous_alias_returns_all_members_sorted():
    table = AliasTable.seed_from_registry(_registry())
    table.load_curated(
        {
            "members": {
                "hinatazaka46:12": {"aliases": ["みっくん"]},
                "hinatazaka46:20": {"aliases": ["みっくん"]},
            }
        }
    )
    assert table.resolve("みっくん", Scope(service="hinatazaka46")) == [
        "hinatazaka46:12",
        "hinatazaka46:20",
    ]


def test_entries_returns_alias_surface_and_canonical_id_pairs():
    table = AliasTable.seed_from_registry(_registry())
    entries = table.entries("hinatazaka46")
    assert ("Kanemura Miku", "hinatazaka46:12") in entries
    assert ("Kato Shiho", "hinatazaka46:20") in entries


def test_entries_raises_on_group_mismatch():
    table = AliasTable.seed_from_registry(_registry())
    with pytest.raises(ValueError, match="AliasTable is scoped to group"):
        table.entries("nogizaka46")


def test_resolve_raises_on_scope_group_mismatch():
    table = AliasTable.seed_from_registry(_registry())
    with pytest.raises(ValueError, match="AliasTable is scoped to group"):
        table.resolve("美玖", Scope(service="nogizaka46"))
