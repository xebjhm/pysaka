from __future__ import annotations

from datetime import datetime, timezone

from pysaka.knowledge.models import Document, Member, SourceRef


def test_document_defaults_and_fields():
    ref = SourceRef(service="hinatazaka46", kind="blog", blog_id="68177", member_id=12)
    doc = Document(
        doc_id="blog:hinatazaka46:68177",
        source_ref=ref,
        author_id="hinatazaka46:12",
        group="hinatazaka46",
        timestamp=datetime(2026, 3, 3, tzinfo=timezone.utc),
        type="blog",
        is_favorite=False,
        text="今日は焼肉",
        has_text=True,
    )
    assert doc.mentions == []  # default_factory
    assert doc.source_ref.kind == "blog"


def test_member_canonical_id_shape():
    m = Member(
        canonical_id="hinatazaka46:12",
        group="hinatazaka46",
        name="金村 美玖",
        name_hiragana="かねむら みく",
        name_romaji="Kanemura Miku",
        generation=2,
        status="active",
        blog_id="12",
    )
    assert m.canonical_id == f"{m.group}:{m.blog_id}"
    assert m.aliases == []
