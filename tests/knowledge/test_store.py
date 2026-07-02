from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import time_machine

from pysaka.knowledge.models import Document, Scope, SearchFilters, SourceRef
from pysaka.knowledge.store import DocumentStore

_SERVICE = "hinatazaka46"
_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _blog_doc(
    doc_id: str,
    *,
    service: str = _SERVICE,
    author_id: str = "hinatazaka46:12",
    blog_id: str = "1",
    member_id: int = 12,
    timestamp: datetime = _NOW,
    text: str = "焼肉たべた🍖",
    has_text: bool = True,
    mentions: list[str] | None = None,
    is_favorite: bool = False,
) -> Document:
    return Document(
        doc_id=doc_id,
        source_ref=SourceRef(service=service, kind="blog", blog_id=blog_id, member_id=member_id),
        author_id=author_id,
        group=service,
        timestamp=timestamp,
        type="blog",
        is_favorite=is_favorite,
        text=text,
        has_text=has_text,
        mentions=mentions or [],
    )


def _msg_doc(
    doc_id: str,
    *,
    service: str = _SERVICE,
    author_id: str = "hinatazaka46:12",
    group_id: int = 34,
    message_id: int,
    timestamp: datetime = _NOW,
    text: str = "やっほー🎤",
    has_text: bool = True,
    mentions: list[str] | None = None,
    type_: str = "text_msg",
    is_favorite: bool = False,
) -> Document:
    return Document(
        doc_id=doc_id,
        source_ref=SourceRef(
            service=service,
            kind="message",
            member_id=58,
            group_id=group_id,
            group_name="Group A",
            member_name="金村 美玖",
            message_id=message_id,
            is_group_chat=True,
        ),
        author_id=author_id,
        group=service,
        timestamp=timestamp,
        type=type_,
        is_favorite=is_favorite,
        text=text,
        has_text=has_text,
        mentions=mentions or [],
    )


def _scope(**kwargs) -> Scope:
    kwargs.setdefault("service", _SERVICE)
    return Scope(**kwargs)


def _filters(**kwargs) -> SearchFilters:
    kwargs.setdefault("scope", _scope())
    return SearchFilters(**kwargs)


# --- upsert / get / all -------------------------------------------------


def test_upsert_replaces_existing_doc_by_doc_id():
    store = DocumentStore()
    original = _blog_doc("blog:hinatazaka46:1", text="v1")
    updated = _blog_doc("blog:hinatazaka46:1", text="v2")

    store.upsert([original])
    store.upsert([updated])

    assert store.get("blog:hinatazaka46:1").text == "v2"
    assert len(store.all()) == 1


def test_get_returns_none_for_missing_doc_id():
    store = DocumentStore()

    assert store.get("does-not-exist") is None


def test_all_returns_every_stored_document():
    store = DocumentStore()
    docs = [_blog_doc(f"blog:hinatazaka46:{i}") for i in range(2)]

    store.upsert(docs)

    assert store.all() == docs


# --- filter: structured fields -------------------------------------------


def test_filter_excludes_docs_from_a_different_service():
    store = DocumentStore()
    same_service = _blog_doc("blog:hinatazaka46:1")
    other_service = _blog_doc("blog:sakurazaka46:1", service="sakurazaka46")
    store.upsert([same_service, other_service])

    result = store.filter(_filters())

    assert result == [same_service]


def test_filter_by_scope_member_id_returns_only_that_members_docs():
    store = DocumentStore()
    mine = _blog_doc("blog:hinatazaka46:1", author_id="hinatazaka46:12")
    other = _blog_doc("blog:hinatazaka46:2", author_id="hinatazaka46:99")
    store.upsert([mine, other])

    result = store.filter(_filters(scope=_scope(member_id="hinatazaka46:12")))

    assert result == [mine]


def test_filter_by_scope_group_ids_applies_only_to_message_docs():
    store = DocumentStore()
    msg_in_group = _msg_doc("msg:hinatazaka46:g:1", message_id=1, group_id=34)
    msg_other_group = _msg_doc("msg:hinatazaka46:g:2", message_id=2, group_id=99)
    blog_doc = _blog_doc("blog:hinatazaka46:1")  # blogs aren't group-scoped
    store.upsert([msg_in_group, msg_other_group, blog_doc])

    result = store.filter(_filters(scope=_scope(group_ids=[34])))

    assert {d.doc_id for d in result} == {msg_in_group.doc_id, blog_doc.doc_id}


def test_filter_by_author_id_returns_only_that_authors_docs():
    store = DocumentStore()
    doc_a = _blog_doc("blog:hinatazaka46:1", author_id="hinatazaka46:12")
    doc_b = _blog_doc("blog:hinatazaka46:2", author_id="hinatazaka46:99")
    store.upsert([doc_a, doc_b])

    result = store.filter(_filters(author_id="hinatazaka46:12"))

    assert result == [doc_a]


def test_filter_by_mentions_id_returns_only_docs_mentioning_it():
    store = DocumentStore()
    mentions_doc = _blog_doc("blog:hinatazaka46:1", mentions=["hinatazaka46:7"])
    other_doc = _blog_doc("blog:hinatazaka46:2", mentions=["hinatazaka46:8"])
    store.upsert([mentions_doc, other_doc])

    result = store.filter(_filters(mentions_id="hinatazaka46:7"))

    assert result == [mentions_doc]


@time_machine.travel(_NOW, tick=False)
def test_filter_by_date_from_and_date_to_returns_only_docs_in_range():
    now = datetime.now(timezone.utc)
    store = DocumentStore()
    too_old = _blog_doc("blog:hinatazaka46:1", timestamp=now - timedelta(days=10))
    in_range = _blog_doc("blog:hinatazaka46:2", timestamp=now - timedelta(days=1))
    too_new = _blog_doc("blog:hinatazaka46:3", timestamp=now + timedelta(days=1))
    store.upsert([too_old, in_range, too_new])

    result = store.filter(_filters(date_from=now - timedelta(days=5), date_to=now))

    assert result == [in_range]


def test_filter_by_type_returns_only_matching_type():
    store = DocumentStore()
    blog_doc = _blog_doc("blog:hinatazaka46:1")
    msg_doc = _msg_doc("msg:hinatazaka46:g:1", message_id=1)
    store.upsert([blog_doc, msg_doc])

    result = store.filter(_filters(type="blog"))

    assert result == [blog_doc]


def test_filter_with_has_text_true_excludes_metadata_only_docs():
    store = DocumentStore()
    with_text = _msg_doc("msg:hinatazaka46:g:1", message_id=1, has_text=True, text="げんき？")
    captionless = _msg_doc("msg:hinatazaka46:g:2", message_id=2, has_text=False, text="", type_="picture_msg")
    store.upsert([with_text, captionless])

    result = store.filter(_filters(has_text=True))

    assert result == [with_text]


def test_filter_with_has_text_false_returns_only_metadata_only_docs():
    store = DocumentStore()
    with_text = _msg_doc("msg:hinatazaka46:g:1", message_id=1, has_text=True, text="げんき？")
    captionless = _msg_doc("msg:hinatazaka46:g:2", message_id=2, has_text=False, text="", type_="picture_msg")
    store.upsert([with_text, captionless])

    result = store.filter(_filters(has_text=False))

    assert result == [captionless]


def test_filter_ignores_query_and_limit():
    store = DocumentStore()
    docs = [_blog_doc(f"blog:hinatazaka46:{i}") for i in range(3)]
    store.upsert(docs)

    result = store.filter(_filters(query="ご無沙汰しております", limit=1))

    assert len(result) == 3


# --- filter: sort ----------------------------------------------------------


def test_filter_with_sort_recent_returns_newest_first():
    store = DocumentStore()
    oldest = _blog_doc("blog:hinatazaka46:1", timestamp=_NOW - timedelta(days=2))
    newest = _blog_doc("blog:hinatazaka46:2", timestamp=_NOW)
    middle = _blog_doc("blog:hinatazaka46:3", timestamp=_NOW - timedelta(days=1))
    store.upsert([oldest, newest, middle])

    result = store.filter(_filters(sort="recent"))

    assert result == [newest, middle, oldest]


def test_filter_with_sort_relevant_preserves_insertion_order():
    store = DocumentStore()
    first = _blog_doc("blog:hinatazaka46:1", timestamp=_NOW)
    second = _blog_doc("blog:hinatazaka46:2", timestamp=_NOW - timedelta(days=5))
    third = _blog_doc("blog:hinatazaka46:3", timestamp=_NOW - timedelta(days=1))
    store.upsert([first, second, third])

    result = store.filter(_filters())  # default sort="relevant"

    assert result == [first, second, third]


# --- content_hash ------------------------------------------------------


def test_content_hash_is_stable_for_identical_doc_id_and_text():
    doc1 = _blog_doc("blog:hinatazaka46:1", text="焼肉たべた")
    doc2 = _blog_doc("blog:hinatazaka46:1", text="焼肉たべた")

    assert DocumentStore.content_hash(doc1) == DocumentStore.content_hash(doc2)


def test_content_hash_differs_when_text_changes():
    doc1 = _blog_doc("blog:hinatazaka46:1", text="焼肉たべた")
    doc2 = _blog_doc("blog:hinatazaka46:1", text="ラーメンたべた")

    assert DocumentStore.content_hash(doc1) != DocumentStore.content_hash(doc2)


def test_content_hash_differs_when_doc_id_changes():
    doc1 = _blog_doc("blog:hinatazaka46:1", text="焼肉たべた")
    doc2 = _blog_doc("blog:hinatazaka46:2", text="焼肉たべた")

    assert DocumentStore.content_hash(doc1) != DocumentStore.content_hash(doc2)


# --- save_json / load_json --------------------------------------------


def test_save_json_load_json_round_trips_store(tmp_path: Path):
    store = DocumentStore()
    blog = _blog_doc(
        "blog:hinatazaka46:1",
        mentions=["hinatazaka46:7"],
        timestamp=_NOW,
        text="焼肉たべた🍖 #ひなあい",
    )
    msg = _msg_doc(
        "msg:hinatazaka46:g:1",
        message_id=1,
        has_text=False,
        text="",
        type_="picture_msg",
        mentions=[],
        timestamp=_NOW - timedelta(hours=3),
    )
    store.upsert([blog, msg])
    path = tmp_path / "store.json"

    store.save_json(path)
    loaded = DocumentStore.load_json(path)

    loaded_blog = loaded.get(blog.doc_id)
    loaded_msg = loaded.get(msg.doc_id)
    assert loaded_blog == blog
    assert loaded_msg == msg
    assert loaded_blog.timestamp.tzinfo is not None
    assert loaded_blog.timestamp == blog.timestamp
    assert loaded_blog.source_ref == blog.source_ref
    assert loaded_blog.mentions == ["hinatazaka46:7"]
    assert len(loaded.all()) == 2


def test_save_json_writes_utf8_with_non_ascii_preserved(tmp_path: Path):
    store = DocumentStore()
    doc = _blog_doc("blog:hinatazaka46:1", text="絵文字テスト🍖✨")
    store.upsert([doc])
    path = tmp_path / "store.json"

    store.save_json(path)

    raw = path.read_text(encoding="utf-8")
    assert "絵文字テスト🍖✨" in raw
    assert DocumentStore.load_json(path).get(doc.doc_id).text == "絵文字テスト🍖✨"
