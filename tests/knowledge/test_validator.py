"""Tests for the grounding validator (`validate`).

Resolved design (deviates from the Task 15 brief's literal rule 2 — see
task-15-report.md): the agent (Task 14) only produces `AnswerSentence(text,
citation_ids)` — doc_ids, with NO per-citation model-supplied quote. The brief's
"reject unless quoted_snippet matches the doc" needs a model-supplied quote to
verify against; without one, a literal cross-language snippet-match would
withhold every English/Chinese answer (EN prose vs JP doc ~= 0 trigram overlap).
Instead: (1) every citation must resolve to a doc_id the agent actually surfaced
AND that still exists in the store: <=> "no fabricated/stale citations"; (2) for
sentences containing CJK text, the sentence's own text must be trigram-contained
(>=90%) in at least one of its cited docs' text: <=> "the claim's content is
actually present in the source" (same-language grounding, since the LLM was
instructed to quote JP verbatim). Cross-lingual sentences (no CJK) skip step 2
as best-effort, since we cannot verify translated/paraphrased claims without a
model-supplied quote.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pysaka.knowledge.models import Answer, AnswerSentence, Document, SourceRef
from pysaka.knowledge.store import DocumentStore
from pysaka.knowledge.validator import _containment_ratio, _has_cjk, _trigrams, validate

_SERVICE = "hinatazaka46"
_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _doc(
    doc_id: str,
    text: str,
    *,
    author_id: str = "hinatazaka46:12",
    timestamp: datetime = _NOW,
    source_ref: SourceRef | None = None,
) -> Document:
    return Document(
        doc_id=doc_id,
        source_ref=source_ref or SourceRef(service=_SERVICE, kind="blog", blog_id="1", member_id=12),
        author_id=author_id,
        group=_SERVICE,
        timestamp=timestamp,
        type="blog",
        is_favorite=False,
        text=text,
        has_text=True,
    )


_JP_DOC_TEXT = "今日は焼肉を食べました。とても美味しかったです。また行きたいと思います。"
_JP_DOC = _doc("blog:hinatazaka46:1", _JP_DOC_TEXT)
_JP_DOC_2 = _doc(
    "blog:hinatazaka46:9",
    "ラジオ収録に行ってきました。楽しかったです。",
    source_ref=SourceRef(service=_SERVICE, kind="blog", blog_id="9", member_id=12),
)


def _store(*docs: Document) -> DocumentStore:
    store = DocumentStore()
    store.upsert(list(docs))
    return store


# --- _trigrams / _containment_ratio (pure helpers) --------------------------


def test_containment_ratio_identical_strings_is_1():
    assert _containment_ratio("abc", "abc") == 1.0


def test_containment_ratio_disjoint_strings_is_0():
    assert _containment_ratio("abc", "xyz") == 0.0


def test_containment_ratio_empty_a_is_0():
    assert _containment_ratio("", "abc") == 0.0


def test_trigrams_short_text_is_single_element():
    assert _trigrams("ab") == {"ab"}


def test_trigrams_empty_text_is_empty_set():
    assert _trigrams("") == set()


# --- _has_cjk -----------------------------------------------------------


def test_has_cjk_true_for_kanji():
    assert _has_cjk("今日は焼肉を食べました") is True


def test_has_cjk_true_for_kana_only():
    assert _has_cjk("とても") is True


def test_has_cjk_false_for_ascii_prose():
    assert _has_cjk("She ate yakiniku today.") is False


# --- validate: fabricated / unsurfaced / missing doc_id ----------------------


def test_validate_drops_sentence_whose_sole_citation_is_unsurfaced():
    store = _store(_JP_DOC)
    answer = Answer(sentences=[AnswerSentence(text="She ate yakiniku.", citation_ids=["blog:fake:999"])], citations=[])

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id}, store=store)

    assert result.sentences == []
    assert result.no_evidence is True


def test_validate_drops_sentence_whose_sole_citation_is_missing_from_store():
    store = _store(_JP_DOC)  # "blog:hinatazaka46:99" never upserted
    answer = Answer(
        sentences=[AnswerSentence(text="She ate yakiniku.", citation_ids=["blog:hinatazaka46:99"])], citations=[]
    )

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id, "blog:hinatazaka46:99"}, store=store)

    assert result.sentences == []
    assert result.no_evidence is True


def test_validate_drops_only_the_bad_citation_when_a_valid_one_remains():
    store = _store(_JP_DOC)
    answer = Answer(
        sentences=[
            AnswerSentence(text="She ate yakiniku.", citation_ids=[_JP_DOC.doc_id, "blog:fake:999"]),
        ],
        citations=[],
    )

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id}, store=store)

    assert len(result.sentences) == 1
    assert result.sentences[0].citation_ids == [_JP_DOC.doc_id]
    assert result.no_evidence is False


# --- validate: same-language (CJK) grounding gate ----------------------


def test_validate_keeps_cjk_sentence_whose_content_is_in_the_cited_doc():
    store = _store(_JP_DOC)
    answer = Answer(
        sentences=[AnswerSentence(text="今日は焼肉を食べました", citation_ids=[_JP_DOC.doc_id])], citations=[]
    )

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id}, store=store)

    assert len(result.sentences) == 1
    assert result.sentences[0].text == "今日は焼肉を食べました"
    assert result.sentences[0].citation_ids == [_JP_DOC.doc_id]
    assert result.no_evidence is False
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.doc_id == _JP_DOC.doc_id
    assert citation.source_ref == _JP_DOC.source_ref
    assert citation.quoted_snippet == _JP_DOC.text[:240]
    assert citation.member == _JP_DOC.author_id
    assert citation.timestamp == _JP_DOC.timestamp


def test_validate_drops_cjk_sentence_whose_content_is_not_in_the_cited_doc():
    store = _store(_JP_DOC)
    answer = Answer(
        sentences=[AnswerSentence(text="明日はラーメンを作ります。とても楽しみです。", citation_ids=[_JP_DOC.doc_id])],
        citations=[],
    )

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id}, store=store)

    assert result.sentences == []
    assert result.no_evidence is True


def test_validate_cjk_gate_checks_best_match_across_multiple_citations():
    store = _store(_JP_DOC, _JP_DOC_2)
    # Exact phrase lives in _JP_DOC_2, not _JP_DOC -- the "best" of the two must win.
    answer = Answer(
        sentences=[
            AnswerSentence(
                text="ラジオ収録に行ってきました",
                citation_ids=[_JP_DOC.doc_id, _JP_DOC_2.doc_id],
            )
        ],
        citations=[],
    )

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id, _JP_DOC_2.doc_id}, store=store)

    assert len(result.sentences) == 1
    assert result.no_evidence is False


# --- validate: cross-lingual pass-through (no CJK gate) ----------------------


def test_validate_keeps_english_sentence_citing_a_valid_surfaced_jp_doc():
    store = _store(_JP_DOC)
    answer = Answer(
        sentences=[AnswerSentence(text="She ate yakiniku today.", citation_ids=[_JP_DOC.doc_id])], citations=[]
    )

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id}, store=store)

    assert len(result.sentences) == 1
    assert result.sentences[0].text == "She ate yakiniku today."
    assert result.no_evidence is False
    assert len(result.citations) == 1
    assert result.citations[0].doc_id == _JP_DOC.doc_id


# --- validate: no_evidence / dedupe / sort ----------------------------------


def test_validate_all_sentences_dropped_returns_no_evidence_answer():
    store = _store(_JP_DOC)
    answer = Answer(
        sentences=[
            AnswerSentence(text="hallucinated", citation_ids=["blog:fake:1"]),
            AnswerSentence(text="also hallucinated", citation_ids=["blog:fake:2"]),
        ],
        citations=[],
    )

    result = validate(answer, surfaced_doc_ids=set(), store=store)

    assert result == Answer(sentences=[], citations=[], no_evidence=True)


def test_validate_empty_sentences_returns_no_evidence_answer():
    store = _store(_JP_DOC)
    answer = Answer(sentences=[], citations=[])

    result = validate(answer, surfaced_doc_ids=set(), store=store)

    assert result.no_evidence is True
    assert result.sentences == []
    assert result.citations == []


def test_validate_dedupes_citations_by_doc_id_and_sorts_them():
    store = _store(_JP_DOC, _JP_DOC_2)
    answer = Answer(
        sentences=[
            # Cite _JP_DOC_2 (id "...9") before _JP_DOC (id "...1") to prove real sorting.
            AnswerSentence(text="First claim, English.", citation_ids=[_JP_DOC_2.doc_id]),
            AnswerSentence(text="Second claim, English.", citation_ids=[_JP_DOC_2.doc_id, _JP_DOC.doc_id]),
        ],
        citations=[],
    )

    result = validate(answer, surfaced_doc_ids={_JP_DOC.doc_id, _JP_DOC_2.doc_id}, store=store)

    assert len(result.sentences) == 2
    assert [c.doc_id for c in result.citations] == sorted([_JP_DOC.doc_id, _JP_DOC_2.doc_id])
    assert len(result.citations) == 2
