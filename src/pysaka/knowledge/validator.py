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
   fabricated and stale doc_ids regardless of language. This is the HARD
   grounding guarantee and applies to every sentence.
2. **Same-language containment gate** (best-effort, kana-only): when the
   sentence contains KANA (hiragana/katakana), it is at least partly Japanese
   -- the corpus language -- so its own text is checked for character-trigram
   containment (>= `threshold`) in at least one cited doc's text. A kana
   sentence failing this is very likely a mischaracterization or hallucination
   and is dropped. A sentence with NO kana -- English prose, but equally a
   *synthesized Chinese (han-only) sentence* -- cannot be verified this way
   (cross-language trigram overlap is ~0, so gating it would delete every
   correct Chinese answer); it skips this gate and relies solely on gate 1.
   Kanji alone must NOT trigger the gate: the shared CJK ideograph range
   cannot distinguish a verbatim Japanese quote from synthesized Chinese
   prose, but kana is Japanese-only.
"""

from __future__ import annotations

from .cleaner import NICKNAME_TOKEN, SUBSCRIBER_SENTINEL, normalize_text, strip_sentinel
from .models import Answer, AnswerSentence, Citation, Document
from .store import DocumentStore

_KANA_RANGES = (
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
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


def _has_kana(s: str) -> bool:
    """True if `s` contains any hiragana or katakana character.

    Kana is the trigger for the same-language containment gate: a kana-bearing
    sentence is (at least partly) Japanese and can be trigram-checked against
    the cited Japanese docs. A han-only sentence may be synthesized Chinese --
    as unverifiable against a Japanese source as English prose -- so it must
    NOT trigger the gate. A MIXED sentence (e.g. Chinese prose embedding a
    short Japanese quoted span such as 「デート」) still triggers the check on
    the whole sentence text; scoping the check to just the quoted 「」 span is
    future work.
    """
    return any(any(lo <= ord(ch) <= hi for lo, hi in _KANA_RANGES) for ch in s)


def validate(
    answer: Answer,
    surfaced_doc_ids: set[str],
    store: DocumentStore,
    threshold: float = 0.15,
    *,
    subscriber_name: str = "you",
) -> Answer:
    """Drop ungrounded sentences/citations from `answer`; see module docstring for the rules.

    For each sentence: citations are pruned to those actually surfaced this turn
    and still present in `store` (gate 1 -- the HARD grounding guarantee: no
    fabricated/unsurfaced citation can reach the user). If nothing survives, the
    sentence is dropped. If the sentence text contains kana (see `_has_kana` --
    kanji alone deliberately does NOT count), its best trigram-containment
    ratio against its (now-valid) cited docs must reach `threshold`
    (gate 2 -- a LENIENT mischaracterization guard). NOTE: the agent emits
    *synthesized/summarized* prose, not verbatim quotes, so a summary is never
    fully trigram-contained in one source; the default `threshold` is therefore
    low (validated on real LLM answers -- 0.9 withheld every real answer). Strict
    verbatim-quote verification would require the agent to emit per-citation
    quotes (a v1.1 change). Surviving sentences keep only their valid citation
    ids; a deduped, doc_id-sorted `Citation` list is built from them. If no
    sentence survives, returns `Answer(sentences=[], citations=[], no_evidence=True)`.

    `subscriber_name` is the real subscriber display name rendered into
    `Citation.quoted_snippet` (a USER-facing boundary) in place of the
    subscriber sentinel. Sentence TEXTS are left untouched: the LLM writes
    `NICKNAME_TOKEN` where evidence carried the sentinel, and the containment
    comparison maps that token back to the sentinel so it is checked in
    token/sentinel space -- the real name is substituted into sentences only
    AFTER validation, by `KnowledgeAgent.answer`.
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

        if _has_kana(sentence.text):
            # Compare in token/sentinel space: the LLM saw (and reproduces) the
            # NICKNAME_TOKEN where the stored doc text carries the sentinel.
            comparable = sentence.text.replace(NICKNAME_TOKEN, SUBSCRIBER_SENTINEL)
            best = max(_containment_ratio(comparable, doc.text) for _cid, doc in valid_docs)
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
                # USER-facing boundary: un-mask the subscriber sentinel to the REAL subscriber
                # name; the sentinel stays in `doc.text` (the stored/indexed copy) untouched.
                quoted_snippet=strip_sentinel(doc.text[:240], subscriber_name),
                member=doc.author_id,
                timestamp=doc.timestamp,
            )

    if not kept_sentences:
        return Answer(sentences=[], citations=[], no_evidence=True)

    sorted_citations = [citations_by_doc_id[doc_id] for doc_id in sorted(citations_by_doc_id)]
    return Answer(sentences=kept_sentences, citations=sorted_citations, no_evidence=False)
