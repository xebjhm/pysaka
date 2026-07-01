"""Pure-Python lexical index: character-trigram BM25-lite over kana-normalized text.

No numpy; keeps ``pysaka.knowledge`` core dependency-free per the purity constraint
(§ Global Constraints in shared-context.md). Katakana is folded to hiragana before
tokenization so kana-script variants (e.g. "ラーメン" vs "らーめん") share the same
trigram vocabulary, matching the app's kata→hira search intent.
"""

from __future__ import annotations

import math
from collections import Counter

from .cleaner import normalize_text

_BM25_K1 = 1.5
_BM25_B = 0.75

# Standard katakana block (ァ..ヶ); the halfwidth/fullwidth variants are normalized
# to this range by NFKC inside normalize_text() before folding runs.
_KATAKANA_LO = 0x30A1
_KATAKANA_HI = 0x30F6
_KATA_TO_HIRA_OFFSET = 0x60  # katakana codepoint - 0x60 == hiragana equivalent


def _fold_kana(s: str) -> str:
    """Fold katakana characters to their hiragana equivalents.

    Leaves everything else untouched, including the prolonged sound mark "ー"
    (U+30FC), which lies just outside the folded range.
    """
    return "".join(chr(ord(c) - _KATA_TO_HIRA_OFFSET) if _KATAKANA_LO <= ord(c) <= _KATAKANA_HI else c for c in s)


def _tokenize(text: str) -> list[str]:
    """Normalize, kana-fold, and split text into character trigrams.

    Strings shorter than 3 characters (after normalization) are emitted as a
    single whole-string gram, so short queries can still match short content.
    Grams that are entirely whitespace are dropped.
    """
    s = _fold_kana(normalize_text(text))
    grams = [s[i : i + 3] for i in range(len(s) - 2)] if len(s) >= 3 else [s]
    return [gram for gram in grams if gram.strip()]


class PureLexicalIndex:
    """Pure-Python BM25-lite full-text index over character trigrams.

    Implements the :class:`~pysaka.knowledge.protocols.LexicalIndex` protocol
    without third-party dependencies: per-chunk term frequencies are accumulated
    in ``collections.Counter`` and scored with a standard BM25 formula
    (k1=1.5, b=0.75).
    """

    def __init__(self) -> None:
        self._grams: dict[str, Counter[str]] = {}  # chunk_id -> gram -> tf
        self._lengths: dict[str, int] = {}  # chunk_id -> total gram count (dl)
        self._df: Counter[str] = Counter()  # gram -> number of chunks containing it
        self._total_length = 0  # sum of all chunk lengths, for avgdl

    def add(self, chunk_id: str, text: str) -> None:
        """Index (or re-index) a text chunk under ``chunk_id``.

        If ``chunk_id`` already exists, its previous content is removed first
        (decrementing document frequencies) before the new content is indexed.
        """
        if chunk_id in self._grams:
            self._remove_one(chunk_id)
        counts = Counter(_tokenize(text))
        self._grams[chunk_id] = counts
        length = sum(counts.values())
        self._lengths[chunk_id] = length
        self._total_length += length
        for gram in counts:
            self._df[gram] += 1

    def remove(self, chunk_ids: list[str]) -> None:
        """Remove chunks from the index, decrementing document frequencies."""
        for chunk_id in chunk_ids:
            self._remove_one(chunk_id)

    def _remove_one(self, chunk_id: str) -> None:
        counts = self._grams.pop(chunk_id, None)
        if counts is None:
            return
        self._total_length -= self._lengths.pop(chunk_id, 0)
        for gram in counts:
            self._df[gram] -= 1
            if self._df[gram] <= 0:
                del self._df[gram]

    def search(self, query: str, k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        """Score indexed chunks against ``query`` with BM25 and return the top-k.

        Only chunks in ``allowed_ids`` (when given) are scored. Zero-score
        chunks are excluded; ties are broken deterministically by ``chunk_id``.
        """
        query_grams = set(_tokenize(query))
        n = len(self._grams)
        if not query_grams or not n:
            return []
        avgdl = self._total_length / n
        candidate_ids = self._grams.keys() if allowed_ids is None else self._grams.keys() & allowed_ids

        scored: list[tuple[str, float]] = []
        for chunk_id in candidate_ids:
            counts = self._grams[chunk_id]
            dl = self._lengths[chunk_id]
            score = 0.0
            for gram in query_grams:
                tf = counts.get(gram, 0)
                if not tf:
                    continue
                df = self._df.get(gram, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl) if avgdl else float(tf)
                score += idf * (tf * (_BM25_K1 + 1)) / denom
            if score > 0:
                scored.append((chunk_id, score))

        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]
