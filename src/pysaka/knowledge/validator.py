"""Grounding validator: withholds ungrounded/hallucinated content from an `Answer`.

**Resolved design (deviates from the Task 15 brief's literal rule 2).** The brief
asks to reject a citation unless its `quoted_snippet` matches the cited doc at a
token-set ratio >= threshold. But the agent (Task 14) only ever produces
`AnswerSentence(text, citation_ids)` -- doc_ids, with no per-citation
model-supplied quote to check. Without a quote, a literal snippet-match would
have to compare the *whole answer sentence* against the doc; for an English (or
any non-Japanese) sentence answering from Japanese source material, that
cross-language comparison has ~0% trigram overlap and would withhold every
such answer, which defeats the point of the validator. Instead this module:

1. **Surfaced-citation gate** (language-agnostic): a citation only counts if its
   `doc_id` was actually surfaced to the agent this turn (`surfaced_doc_ids`)
   *and* still resolves in the `DocumentStore` -- this alone rules out
   fabricated and stale doc_ids regardless of language.
2. **Same-language containment gate** (best-effort, CJK-only): when the
   sentence itself contains CJK text, the model was instructed to quote
   Japanese verbatim (see `agent.SYSTEM_PROMPT`), so the sentence's own text
   is checked for character-trigram containment (>= `threshold`) in at least
   one cited doc's text. A CJK sentence failing this is very likely a
   paraphrase or hallucination and is dropped. A sentence with no CJK (e.g. an
   English summary of Japanese evidence) cannot be verified this way -- it
   skips this gate and relies solely on gate 1.
"""

from __future__ import annotations

from .cleaner import normalize_text, strip_sentinel
from .models import Answer, AnswerSentence, Citation, Document
from .store import DocumentStore

_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x30FF),  # Hiragana + Katakana
)


def _trigrams(text: str) -> set[str]:
    """Character trigrams of `normalize_text(text)`; short text is one whole-string element."""
    s = normalize_text(text)
    if not s:
        return set()
    if len(s) < 3:
        return {s}
    return {s[i : i + 3] for i in range(len(s) - 2)}


def _containment_ratio(a: str, b: str) -> float:
    """Fraction of `a`'s trigrams that are also present in `b` -- "is a's content in b"."""
    trigrams_a = _trigrams(a)
    if not trigrams_a:
        return 0.0
    return len(trigrams_a & _trigrams(b)) / len(trigrams_a)


def _has_cjk(s: str) -> bool:
    """True if `s` contains any CJK ideograph or kana character."""
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in s)


def validate(answer: Answer, surfaced_doc_ids: set[str], store: DocumentStore, threshold: float = 0.9) -> Answer:
    """Drop ungrounded sentences/citations from `answer`; see module docstring for the rules.

    For each sentence: citations are pruned to those actually surfaced this turn
    and still present in `store` (gate 1). If nothing survives, the sentence is
    dropped. If the sentence text contains CJK, its best trigram-containment
    ratio against its (now-valid) cited docs must reach `threshold`, or it is
    dropped too (gate 2). Surviving sentences keep only their valid citation
    ids; a deduped, doc_id-sorted `Citation` list is built from them. If no
    sentence survives, returns `Answer(sentences=[], citations=[], no_evidence=True)`.
    """
    kept_sentences: list[AnswerSentence] = []
    citations_by_doc_id: dict[str, Citation] = {}

    for sentence in answer.sentences:
        valid_docs: list[tuple[str, Document]] = []
        for cid in sentence.citation_ids:
            if cid not in surfaced_doc_ids:
                continue
            doc = store.get(cid)
            if doc is None:
                continue
            valid_docs.append((cid, doc))
        if not valid_docs:
            continue

        if _has_cjk(sentence.text):
            best = max(_containment_ratio(sentence.text, doc.text) for _cid, doc in valid_docs)
            if best < threshold:
                continue

        valid_ids = [cid for cid, _doc in valid_docs]
        kept_sentences.append(AnswerSentence(text=sentence.text, citation_ids=valid_ids))
        for cid, doc in valid_docs:
            if cid in citations_by_doc_id:
                continue
            citations_by_doc_id[cid] = Citation(
                doc_id=cid,
                source_ref=doc.source_ref,
                # un-mask the `%%%` subscriber sentinel: `quoted_snippet` is user/LLM-facing
                # output; the sentinel stays in `doc.text` (the stored/indexed copy) untouched.
                quoted_snippet=strip_sentinel(doc.text[:240]),
                member=doc.author_id,
                timestamp=doc.timestamp,
            )

    if not kept_sentences:
        return Answer(sentences=[], citations=[], no_evidence=True)

    sorted_citations = [citations_by_doc_id[doc_id] for doc_id in sorted(citations_by_doc_id)]
    return Answer(sentences=kept_sentences, citations=sorted_citations, no_evidence=False)
