from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from pysaka.knowledge.lexical import PureLexicalIndex
from pysaka.knowledge.models import Chunk, Document, Scope, SearchFilters, SourceRef
from pysaka.knowledge.retrieve import HybridRetriever
from pysaka.knowledge.store import DocumentStore

_SERVICE = "hinatazaka46"
_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeEmbedder:
    """Deterministic embedder: looks up a fixed vector for each exact input string.

    ``kind`` is accepted (for protocol compliance) but ignored -- these tests don't
    need query/passage asymmetry, only a fully controllable vector space.
    """

    dim = 2

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        return [self._vectors[text] for text in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeVectorStore:
    """Pure-Python cosine-similarity vector store (no numpy) for tests.

    Mirrors the real backend's contract: only positively-similar vectors are
    considered a match, ranked highest-first, ties broken by id.
    """

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}

    def add(self, ids: list[str], vectors: list[list[float]]) -> None:
        for chunk_id, vector in zip(ids, vectors):
            self._vectors[chunk_id] = vector

    def remove(self, ids: list[str]) -> None:
        for chunk_id in ids:
            self._vectors.pop(chunk_id, None)

    def search(self, vector: list[float], k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        candidate_ids = self._vectors.keys() if allowed_ids is None else self._vectors.keys() & allowed_ids
        scored = [(cid, _cosine(vector, self._vectors[cid])) for cid in candidate_ids]
        scored = [(cid, score) for cid, score in scored if score > 0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


def _doc(
    doc_id: str,
    *,
    author_id: str = "hinatazaka46:12",
    timestamp: datetime = _NOW,
    text: str = "焼肉たべた🍖",
    has_text: bool = True,
) -> Document:
    return Document(
        doc_id=doc_id,
        source_ref=SourceRef(service=_SERVICE, kind="blog", blog_id=doc_id, member_id=12),
        author_id=author_id,
        group=_SERVICE,
        timestamp=timestamp,
        type="blog",
        is_favorite=False,
        text=text,
        has_text=has_text,
    )


def _chunk(doc_id: str, text: str) -> Chunk:
    return Chunk(chunk_id=f"{doc_id}#0", doc_id=doc_id, text=text, context_text=text)


def _filters(**kwargs) -> SearchFilters:
    kwargs.setdefault("scope", Scope(service=_SERVICE))
    return SearchFilters(**kwargs)


# --- search: RRF fusion --------------------------------------------------


def test_search_fuses_disagreeing_lexical_and_vector_rankings_via_rrf():
    # doc_a: lexical loves it (repeats the query term 3x), vector ignores it (orthogonal).
    # doc_b: lexical likes it less (query term once), vector loves it (exact match).
    # doc_c: lexical never sees it (no query term), vector likes it a little (partial match).
    store = DocumentStore()
    doc_a = _doc("blog:hinatazaka46:a", text="ライブ最高でした本当にライブが楽しかったですライブありがとう")
    doc_b = _doc("blog:hinatazaka46:b", text="今日はライブに行きました")
    doc_c = _doc("blog:hinatazaka46:c", text="映画を見て感動しました素晴らしい映画でした")
    store.upsert([doc_a, doc_b, doc_c])

    vectors = {
        doc_a.text: [0.0, 1.0],  # orthogonal to the query vector -> excluded from vector results
        doc_b.text: [1.0, 0.0],  # exact match -> vector rank 1
        doc_c.text: [0.6, 0.8],  # partial match -> vector rank 2
        "ライブ": [1.0, 0.0],  # query vector
    }
    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder(vectors))
    retriever.index(
        [
            _chunk(doc_a.doc_id, doc_a.text),
            _chunk(doc_b.doc_id, doc_b.text),
            _chunk(doc_c.doc_id, doc_c.text),
        ]
    )

    hits = retriever.search(_filters(query="ライブ", sort="relevant", limit=3))

    # Both disagreement docs (a, b) surface in the fused results...
    assert {doc_a.doc_id, doc_b.doc_id} <= {hit.doc_id for hit in hits}
    # ...and doc_b -- ranked #1 by vector, #2 by lexical -- outranks doc_a and doc_c, which
    # are each only ranked by a single arm. This is RRF's whole point: presence near the top
    # of BOTH ranked lists beats being #1 in just one.
    assert [hit.doc_id for hit in hits] == [doc_b.doc_id, doc_a.doc_id, doc_c.doc_id]
    assert hits[0].score > hits[1].score > hits[2].score
    top = hits[0]
    assert top.doc_id == doc_b.doc_id
    assert top.author == doc_b.author_id
    assert top.source_ref == doc_b.source_ref
    assert top.timestamp == doc_b.timestamp
    assert top.snippet == doc_b.text[:200]


def test_search_relevant_restricts_ranking_to_filtered_scope():
    # A doc that would lexically dominate the query but sits outside the requested author
    # scope must never surface, even though it's in the store and indexed.
    store = DocumentStore()
    in_scope = _doc("blog:hinatazaka46:1", author_id="hinatazaka46:12", text="ライブ最高でした")
    out_of_scope = _doc("blog:hinatazaka46:2", author_id="hinatazaka46:99", text="ライブライブライブ最高最高")
    store.upsert([in_scope, out_of_scope])
    retriever = HybridRetriever(
        store,
        PureLexicalIndex(),
        FakeVectorStore(),
        FakeEmbedder({in_scope.text: [1.0, 0.0], out_of_scope.text: [1.0, 0.0], "ライブ": [1.0, 0.0]}),
    )
    retriever.index([_chunk(in_scope.doc_id, in_scope.text), _chunk(out_of_scope.doc_id, out_of_scope.text)])

    hits = retriever.search(_filters(author_id="hinatazaka46:12", query="ライブ", sort="relevant", limit=10))

    assert [hit.doc_id for hit in hits] == [in_scope.doc_id]


# --- search: structured filters (author_id) -------------------------------


def test_author_id_filter_constrains_results_to_that_author():
    store = DocumentStore()
    mine = _doc("blog:hinatazaka46:1", author_id="hinatazaka46:12", text="今日は元気です")
    other = _doc("blog:hinatazaka46:2", author_id="hinatazaka46:99", text="今日は元気です")
    store.upsert([mine, other])
    # No chunks indexed and no query -- exercises the "query is falsy" bypass branch while
    # still proving the author_id filter is applied via store.filter.
    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder({}))

    hits = retriever.search(_filters(author_id="hinatazaka46:12", sort="relevant", query=None, limit=10))

    assert [hit.doc_id for hit in hits] == [mine.doc_id]
    assert hits[0].author == "hinatazaka46:12"


# --- search: sort=recent ----------------------------------------------------


def test_sort_recent_with_limit_one_returns_newest_matching_doc():
    store = DocumentStore()
    oldest = _doc("blog:hinatazaka46:1", timestamp=_NOW - timedelta(days=10), text="ライブ最高")
    newest = _doc("blog:hinatazaka46:2", timestamp=_NOW, text="ライブ最高")
    middle = _doc("blog:hinatazaka46:3", timestamp=_NOW - timedelta(days=1), text="ライブ最高")
    store.upsert([oldest, newest, middle])
    # sort="recent" must bypass ranking entirely -- no index() call happened, so a ranking
    # attempt would KeyError on the FakeEmbedder; this proves recency short-circuits that path.
    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder({}))

    hits = retriever.search(_filters(sort="recent", query="ライブ", limit=1))

    assert len(hits) == 1
    assert hits[0].doc_id == newest.doc_id
    assert hits[0].timestamp == _NOW


def test_sort_recent_orders_all_matches_newest_first():
    store = DocumentStore()
    oldest = _doc("blog:hinatazaka46:1", timestamp=_NOW - timedelta(days=10))
    newest = _doc("blog:hinatazaka46:2", timestamp=_NOW)
    middle = _doc("blog:hinatazaka46:3", timestamp=_NOW - timedelta(days=1))
    store.upsert([oldest, newest, middle])
    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder({}))

    hits = retriever.search(_filters(sort="recent", query=None, limit=10))

    assert [hit.doc_id for hit in hits] == [newest.doc_id, middle.doc_id, oldest.doc_id]
    assert hits[0].score > hits[1].score > hits[2].score


# --- search: caption-less docs ----------------------------------------------


def test_captionless_doc_is_recent_retrievable_but_not_query_matched():
    store = DocumentStore()
    captionless = _doc("blog:hinatazaka46:1", text="", has_text=False, timestamp=_NOW)
    with_text = _doc("blog:hinatazaka46:2", text="ライブ最高でした", timestamp=_NOW - timedelta(days=1))
    store.upsert([captionless, with_text])
    retriever = HybridRetriever(
        store,
        PureLexicalIndex(),
        FakeVectorStore(),
        FakeEmbedder({with_text.text: [1.0, 0.0], "ライブ": [1.0, 0.0]}),
    )
    # Caption-less docs never produce a Chunk (per chunking.py), so only with_text is indexed.
    retriever.index([_chunk(with_text.doc_id, with_text.text)])

    recent_hits = retriever.search(_filters(sort="recent", query=None, limit=10))
    assert {hit.doc_id for hit in recent_hits} == {captionless.doc_id, with_text.doc_id}
    captionless_hit = next(hit for hit in recent_hits if hit.doc_id == captionless.doc_id)
    assert captionless_hit.snippet == ""  # falls back to doc.text[:200], which is empty

    relevant_hits = retriever.search(_filters(sort="relevant", query="ライブ", limit=10))
    assert [hit.doc_id for hit in relevant_hits] == [with_text.doc_id]
