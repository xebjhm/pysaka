from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pysaka.knowledge.chunking import chunk_documents, default_count_tokens
from pysaka.knowledge.models import Document, SourceRef

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _msg(doc_id: str, author_id: str, text: str, *, minute: int, has_text: bool = True, type_="text_msg") -> Document:
    return Document(
        doc_id=doc_id,
        source_ref=SourceRef(service="hinatazaka46", kind="message", message_id=int(doc_id.rsplit(":", 1)[-1])),
        author_id=author_id,
        group="hinatazaka46",
        timestamp=_T0 + timedelta(minutes=minute),
        type=type_,
        is_favorite=False,
        text=text,
        has_text=has_text,
    )


def _blog(doc_id: str, text: str) -> Document:
    return Document(
        doc_id=doc_id,
        source_ref=SourceRef(service="hinatazaka46", kind="blog", blog_id="1"),
        author_id="hinatazaka46:12",
        group="hinatazaka46",
        timestamp=_T0,
        type="blog",
        is_favorite=False,
        text=text,
        has_text=bool(text),
    )


# Five short paragraphs, five Latin "words" each -> 5 tokens/paragraph under the default heuristic.
_P1 = "alpha bravo charlie delta echo"
_P2 = "foxtrot golf hotel india juliet"
_P3 = "kilo lima mike november oscar"
_P4 = "papa quebec romeo sierra tango"
_P5 = "uniform victor whiskey xray yankee"


def test_long_blog_yields_multiple_chunks_with_overlap():
    text = "\n\n".join([_P1, _P2, _P3, _P4, _P5])
    doc = _blog("blog:hinatazaka46:1", text)

    chunks = chunk_documents([doc], max_tokens=20, overlap=0.15)

    assert len(chunks) > 1
    # Consecutive chunks share overlapping content: the tail paragraph of chunk N
    # reappears at the head of chunk N+1.
    for i in range(len(chunks) - 1):
        tail_paragraph = chunks[i].text.strip().split("\n")[-1]
        assert chunks[i + 1].text.startswith(tail_paragraph)
    for n, chunk in enumerate(chunks):
        assert chunk.chunk_id == f"{doc.doc_id}#{n}"
        assert chunk.doc_id == doc.doc_id
        assert chunk.context_text == chunk.text  # blogs don't use neighbor context


def test_short_blog_yields_exactly_one_chunk():
    doc = _blog("blog:hinatazaka46:2", "短い日記です。\n\n今日は焼肉を食べました。")

    chunks = chunk_documents([doc], max_tokens=400)

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "blog:hinatazaka46:2#0"
    assert chunks[0].text == chunks[0].context_text


def test_two_messages_same_author_each_yield_one_chunk_with_neighbor_context():
    doc1 = _msg("msg:hinatazaka46:g:1", "hinatazaka46:12", "やっほー", minute=0)
    doc2 = _msg("msg:hinatazaka46:g:2", "hinatazaka46:12", "げんき？", minute=5)

    chunks = chunk_documents([doc1, doc2])

    assert len(chunks) == 2
    c1 = next(c for c in chunks if c.doc_id == doc1.doc_id)
    c2 = next(c for c in chunks if c.doc_id == doc2.doc_id)

    assert c1.chunk_id == f"{doc1.doc_id}#0"
    assert c1.text == doc1.text  # indexing unit is the message's own text
    assert doc2.text in c1.context_text  # neighbor context includes the next message
    assert c1.context_text != c1.text

    assert c2.text == doc2.text
    assert doc1.text in c2.context_text  # neighbor context includes the previous message


def test_message_with_no_neighbors_context_text_equals_text():
    doc = _msg("msg:hinatazaka46:g:9", "hinatazaka46:99", "ひとりごと", minute=0)

    chunks = chunk_documents([doc])

    assert len(chunks) == 1
    assert chunks[0].context_text == chunks[0].text


def test_captionless_doc_yields_no_chunk():
    doc = _msg("msg:hinatazaka46:g:3", "hinatazaka46:12", "", minute=0, has_text=False, type_="picture_msg")

    assert chunk_documents([doc]) == []


def test_captionless_doc_with_empty_text_but_has_text_true_still_yields_no_chunk():
    # Defensive: has_text=True but text=="" should also be skipped (per spec "or doc.text is empty").
    doc = _msg("msg:hinatazaka46:g:4", "hinatazaka46:12", "", minute=0, has_text=True, type_="voice_msg")

    assert chunk_documents([doc]) == []


def test_mixed_docs_only_produce_chunks_for_text_bearing_ones():
    blog = _blog("blog:hinatazaka46:5", "短いブログです。")
    captionless = _msg("msg:hinatazaka46:g:5", "hinatazaka46:12", "", minute=0, has_text=False, type_="video_msg")
    msg = _msg("msg:hinatazaka46:g:6", "hinatazaka46:12", "こんにちは", minute=1)

    chunks = chunk_documents([blog, captionless, msg])

    doc_ids = {c.doc_id for c in chunks}
    assert doc_ids == {blog.doc_id, msg.doc_id}


def test_default_count_tokens_counts_latin_word_runs_and_cjk_chars_separately():
    # 2 Latin word runs + 3 individual CJK chars ("焼肉食") = 5 tokens.
    assert default_count_tokens("hello world 焼肉食") == 5


def test_custom_count_tokens_callable_is_used_when_provided():
    text = "\n\n".join([_P1, _P2, _P3])
    doc = _blog("blog:hinatazaka46:6", text)

    # A counter that always reports 0 tokens should never trigger a window split.
    chunks = chunk_documents([doc], max_tokens=1, count_tokens=lambda _t: 0)

    assert len(chunks) == 1


def test_zero_overlap_budget_yields_non_overlapping_windows():
    text = "\n\n".join([_P1, _P2, _P3, _P4, _P5])
    doc = _blog("blog:hinatazaka46:7", text)

    chunks = chunk_documents([doc], max_tokens=20, overlap=0.0)

    assert len(chunks) > 1
    tail_paragraph = chunks[0].text.strip().split("\n")[-1]
    assert not chunks[1].text.startswith(tail_paragraph)


def test_blog_with_only_whitespace_text_yields_no_chunk():
    # has_text=True but the text is entirely newlines/whitespace -> no paragraphs -> no chunk.
    doc = _blog("blog:hinatazaka46:8", "\n\n   \n")

    assert chunk_documents([doc]) == []
