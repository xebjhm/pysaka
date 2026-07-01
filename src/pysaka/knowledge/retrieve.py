"""HybridRetriever: RRF fusion of lexical + vector search over structured filters.

Integration layer over :class:`~pysaka.knowledge.store.DocumentStore` and the
pluggable :class:`~pysaka.knowledge.protocols.LexicalIndex` /
:class:`~pysaka.knowledge.protocols.VectorStore` / :class:`~pysaka.knowledge.protocols.Embedder`
backends. `search()` applies every structured `SearchFilters` field via
`DocumentStore.filter`, then either short-circuits to recency order or fuses lexical
and vector top-k rankings with Reciprocal Rank Fusion (k=60, equal weight) before
mapping the winning chunks back to their parent documents.
"""

from __future__ import annotations

import structlog

from .models import Chunk, Document, Hit, SearchFilters
from .protocols import Embedder, LexicalIndex, VectorStore
from .store import DocumentStore

logger = structlog.get_logger(__name__)

_RRF_K = 60  # standard Reciprocal Rank Fusion constant
_SNIPPET_LEN = 200


class HybridRetriever:
    """Fuses lexical (`LexicalIndex`) and semantic (`VectorStore`) search behind one API.

    Maintains the chunk<->doc bookkeeping (`chunk_id -> doc_id`, `doc_id -> [chunk_id]`,
    `chunk_id -> chunk.text`) needed to turn chunk-level ranking into document-level `Hit`s,
    since lexical/vector backends only know about chunks.
    """

    def __init__(self, store: DocumentStore, lexical: LexicalIndex, vectors: VectorStore, embedder: Embedder) -> None:
        self._store = store
        self._lexical = lexical
        self._vectors = vectors
        self._embedder = embedder
        self._chunk_to_doc: dict[str, str] = {}
        self._doc_to_chunks: dict[str, list[str]] = {}
        self._chunk_text: dict[str, str] = {}

    def index(self, chunks: list[Chunk]) -> None:
        """Add `chunks` to both the lexical and vector backends, recording doc/chunk maps."""
        for chunk in chunks:
            self._lexical.add(chunk.chunk_id, chunk.text)
            vector = self._embedder.embed([chunk.context_text], kind="passage")[0]
            self._vectors.add([chunk.chunk_id], [vector])
            self._chunk_to_doc[chunk.chunk_id] = chunk.doc_id
            self._doc_to_chunks.setdefault(chunk.doc_id, []).append(chunk.chunk_id)
            self._chunk_text[chunk.chunk_id] = chunk.text
        logger.debug("hybrid_retriever.indexed", chunk_count=len(chunks))

    def search(self, filters: SearchFilters) -> list[Hit]:
        """Structured-filter, then rank: RRF fusion for `sort="relevant"` + a query, else recency."""
        candidate_docs = self._store.filter(filters)

        if filters.sort == "recent" or not filters.query:
            taken = candidate_docs[: filters.limit]
            total = len(taken)
            return [self._build_hit(doc, float(total - i), None) for i, doc in enumerate(taken)]

        allowed_chunk_ids = {chunk_id for doc in candidate_docs for chunk_id in self._doc_to_chunks.get(doc.doc_id, [])}
        pool = max(filters.limit * 5, 50)
        lex_results = self._lexical.search(filters.query, k=pool, allowed_ids=allowed_chunk_ids)
        query_vector = self._embedder.embed([filters.query], kind="query")[0]
        vec_results = self._vectors.search(query_vector, k=pool, allowed_ids=allowed_chunk_ids)

        rrf_scores = _reciprocal_rank_fusion(lex_results, vec_results)
        doc_score, doc_best_chunk = _aggregate_to_docs(rrf_scores, self._chunk_to_doc)

        docs_by_id = {doc.doc_id: doc for doc in candidate_docs}
        ranked_doc_ids = sorted(doc_score, key=lambda doc_id: (-doc_score[doc_id], doc_id))[: filters.limit]
        return [
            self._build_hit(docs_by_id[doc_id], doc_score[doc_id], doc_best_chunk[doc_id]) for doc_id in ranked_doc_ids
        ]

    def _build_hit(self, doc: Document, score: float, best_chunk_id: str | None) -> Hit:
        snippet_source = self._chunk_text[best_chunk_id] if best_chunk_id is not None else doc.text
        return Hit(
            doc_id=doc.doc_id,
            source_ref=doc.source_ref,
            author=doc.author_id,  # canonical id; display-name resolution belongs to the presentation layer
            timestamp=doc.timestamp,
            snippet=snippet_source[:_SNIPPET_LEN],
            score=score,
        )


def _reciprocal_rank_fusion(*ranked_lists: list[tuple[str, float]]) -> dict[str, float]:
    """RRF-fuse any number of ranked `(id, score)` lists: `sum(1 / (k + rank))` per id."""
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (item_id, _score) in enumerate(ranked, start=1):
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (_RRF_K + rank)
    return fused


def _aggregate_to_docs(
    rrf_scores: dict[str, float], chunk_to_doc: dict[str, str]
) -> tuple[dict[str, float], dict[str, str]]:
    """Roll chunk-level RRF scores up to their parent docs (max chunk score wins per doc)."""
    doc_score: dict[str, float] = {}
    doc_best_chunk: dict[str, str] = {}
    for chunk_id, score in rrf_scores.items():
        # `chunk_id` always came from lexical/vector search results restricted to
        # `allowed_chunk_ids`, which is itself derived from `chunk_to_doc`'s keys -- so the
        # lookup can never miss; a KeyError here would mean that invariant broke upstream.
        doc_id = chunk_to_doc[chunk_id]
        if doc_id not in doc_score or score > doc_score[doc_id]:
            doc_score[doc_id] = score
            doc_best_chunk[doc_id] = chunk_id
    return doc_score, doc_best_chunk
