from __future__ import annotations

import pytest

from pysaka.knowledge.registry import MemberRegistry

MEMBERS = {
    "meta": {"group": "hinatazaka"},
    "members": [
        {
            "blogId": "12",
            "nameKanji": "金村 美玖",
            "nameHiragana": "かねむら みく",
            "nameRomaji": "Kanemura Miku",
            "generation": 2,
            "status": "active",
        }
    ],
}


def test_resolve_author_by_normalized_name():
    reg = MemberRegistry.from_members_json(MEMBERS, "hinatazaka46")
    assert reg.resolve_author("金村　美玖", "hinatazaka46") == "hinatazaka46:12"  # full-width space
    assert reg.get("hinatazaka46:12").generation == 2


def test_unknown_author_autoprovisions():
    reg = MemberRegistry.from_members_json(MEMBERS, "hinatazaka46")
    cid = reg.resolve_author("新加入 太郎", "hinatazaka46")
    assert reg.get(cid).name == "新加入 太郎"
    assert reg.get(cid) in reg.unaliased()


def test_resolve_author_unknown_is_idempotent():
    reg = MemberRegistry.from_members_json(MEMBERS, "hinatazaka46")
    first = reg.resolve_author("新加入 太郎", "hinatazaka46")
    second = reg.resolve_author("新加入 太郎", "hinatazaka46")
    assert first == second
    assert len(reg.unaliased()) == 1
    assert len(reg.all()) == 2  # roster member + the one auto-provisioned member


def test_link_message_ids_sets_ids_and_reuses_resolution():
    reg = MemberRegistry.from_members_json(MEMBERS, "hinatazaka46")
    reg.link_message_ids(group_id=34, member_id=58, name="金村 美玖")
    member = reg.get("hinatazaka46:12")
    assert member.message_group_id == 34
    assert member.message_member_id == 58
    assert member not in reg.unaliased()  # roster member, not auto-provisioned


def test_resolve_author_fails_on_cross_group():
    reg = MemberRegistry.from_members_json(MEMBERS, "hinatazaka46")
    with pytest.raises(ValueError, match="MemberRegistry is scoped to group"):
        reg.resolve_author("金村 美玖", "other_group")
