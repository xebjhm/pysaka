from __future__ import annotations

from pysaka.knowledge.ingest import ingest_blog, ingest_messages


def RESOLVE(name: str, svc: str) -> str:
    """Stub author resolver."""
    return f"{svc}:12"


def test_ingest_blog_builds_document():
    blog = {
        "meta": {
            "id": "68177",
            "member_name": "金村 美玖",
            "published_at": "2026-03-03T20:11:00+09:00",
            "url": "https://x/68177",
        },
        "content": {"html": "<p>焼肉たべた</p>"},
    }
    doc = ingest_blog(blog, "hinatazaka46", RESOLVE)
    assert doc.doc_id == "blog:hinatazaka46:68177"
    assert doc.text == "焼肉たべた" and doc.has_text
    assert doc.source_ref.kind == "blog" and doc.source_ref.blog_id == "68177"
    # member_id must be the resolved AUTHOR's blog-system id (12), not the post id (68177)
    assert doc.source_ref.member_id == 12
    assert doc.timestamp.tzinfo is not None  # tz-aware UTC


def test_ingest_blog_with_html_raw_format():
    """Blog with html_raw (saka-cli format) → text extracted via html_to_text."""
    blog = {
        "meta": {
            "id": "68177",
            "member_name": "金村 美玖",
            "published_at": "2026-03-03T20:11:00+09:00",
            "url": "https://x/68177",
        },
        "content": {"html_raw": "<p>焼肉たべた</p>"},
    }
    doc = ingest_blog(blog, "hinatazaka46", RESOLVE)
    assert doc.text == "焼肉たべた" and doc.has_text


def test_ingest_blog_with_plain_text_only():
    """Blog with plain_text only (no html/html_raw) → text via normalize_text."""
    blog = {
        "meta": {
            "id": "68178",
            "member_name": "金村 美玖",
            "published_at": "2026-03-03T20:11:00+09:00",
            "url": "https://x/68178",
        },
        "content": {"plain_text": "%%%焼肉たべた"},
    }
    doc = ingest_blog(blog, "hinatazaka46", RESOLVE)
    assert doc.has_text and "%%%" not in doc.text
    assert "焼肉たべた" in doc.text


def test_ingest_blog_with_empty_content():
    """Blog with empty content → text='', has_text=False."""
    blog = {
        "meta": {
            "id": "68179",
            "member_name": "金村 美玖",
            "published_at": "2026-03-03T20:11:00+09:00",
            "url": "https://x/68179",
        },
        "content": {},
    }
    doc = ingest_blog(blog, "hinatazaka46", RESOLVE)
    assert doc.text == "" and not doc.has_text


def test_ingest_messages_text_and_captionless_media():
    mj = {
        "member": {"id": 58, "name": "金村 美玖", "group_id": 34},
        "messages": [
            {
                "id": 1,
                "timestamp": "2025-08-11T17:00:33Z",
                "type": "text",
                "is_favorite": False,
                "content": "%%%元気？",
            },
            {
                "id": 2,
                "timestamp": "2025-08-12T10:20:41Z",
                "type": "picture",
                "is_favorite": False,
                "content": None,
                "media_file": "x.jpg",
            },
        ],
    }
    docs = ingest_messages(mj, "hinatazaka46", RESOLVE)
    assert docs[0].has_text and "%%%" not in docs[0].text
    assert docs[1].has_text is False and docs[1].text == ""
    assert docs[1].source_ref.message_id == 2 and docs[1].type == "picture_msg"
