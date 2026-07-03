from __future__ import annotations

from pysaka.knowledge.protocols import Embedder, LexicalIndex, VectorStore


class FakeEmbedder:
    """Minimal embedder returning deterministic vectors for testing."""

    dim: int = 768

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        """Return deterministic vectors (one per text, all values = index + 1.0)."""
        return [[float(i + 1) for _ in range(self.dim)] for i in range(len(texts))]


class FakeVectorStore:
    """Minimal vector store for testing."""

    def __init__(self) -> None:
        self.store: dict[str, list[float]] = {}

    def add(self, ids: list[str], vectors: list[list[float]]) -> None:
        """Add vectors to store."""
        for id_, vector in zip(ids, vectors):
            self.store[id_] = vector

    def remove(self, ids: list[str]) -> None:
        """Remove vectors from store."""
        for id_ in ids:
            self.store.pop(id_, None)

    def search(self, vector: list[float], k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        """Return top-k results (mock: just return first k stored items with dummy scores)."""
        candidates = list(self.store.items())
        if allowed_ids:
            candidates = [(id_, vec) for id_, vec in candidates if id_ in allowed_ids]
        return [(id_, 0.95) for id_, _ in candidates[:k]]


class FakeLexicalIndex:
    """Minimal lexical index for testing."""

    def __init__(self) -> None:
        self.index: dict[str, str] = {}

    def add(self, chunk_id: str, text: str) -> None:
        """Add text to index."""
        self.index[chunk_id] = text

    def remove(self, chunk_ids: list[str]) -> None:
        """Remove chunks from index."""
        for chunk_id in chunk_ids:
            self.index.pop(chunk_id, None)

    def search(self, query: str, k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        """Return top-k results (mock: just return first k stored items with dummy scores)."""
        candidates = list(self.index.keys())
        if allowed_ids:
            candidates = [id_ for id_ in candidates if id_ in allowed_ids]
        return [(id_, 0.85) for id_ in candidates[:k]]


def test_embedder_is_protocol_instance():
    """FakeEmbedder should pass isinstance check for Embedder protocol."""
    fake = FakeEmbedder()
    assert isinstance(fake, Embedder)
    assert fake.dim == 768


def test_embedder_embed_method():
    """Test embedder's embed method signature and deterministic output."""
    fake = FakeEmbedder()
    texts = ["text1", "text2"]
    vectors = fake.embed(texts, kind="passage")
    assert len(vectors) == 2
    assert all(len(v) == 768 for v in vectors)
    # Deterministic: first text should have all 1.0 values, second all 2.0
    assert all(v == 1.0 for v in vectors[0])
    assert all(v == 2.0 for v in vectors[1])


def test_vector_store_is_protocol_instance():
    """FakeVectorStore should pass isinstance check for VectorStore protocol."""
    fake = FakeVectorStore()
    assert isinstance(fake, VectorStore)


def test_vector_store_add_and_search():
    """Test VectorStore add and search operations."""
    fake = FakeVectorStore()
    fake.add(["id1", "id2"], [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    results = fake.search([1.0, 2.0, 3.0], k=2)
    assert len(results) == 2
    assert all(isinstance(score, float) for _, score in results)


def test_vector_store_remove():
    """Test VectorStore remove operation."""
    fake = FakeVectorStore()
    fake.add(["id1", "id2"], [[1.0, 2.0], [3.0, 4.0]])
    fake.remove(["id1"])
    results = fake.search([1.0, 2.0], k=10)
    assert len(results) == 1
    assert results[0][0] == "id2"


def test_vector_store_search_with_allowed_ids():
    """Test VectorStore search with filtering by allowed_ids."""
    fake = FakeVectorStore()
    fake.add(["id1", "id2", "id3"], [[1.0], [2.0], [3.0]])
    results = fake.search([1.0], k=10, allowed_ids={"id2", "id3"})
    assert len(results) == 2
    assert all(id_ in {"id2", "id3"} for id_, _ in results)


def test_lexical_index_is_protocol_instance():
    """FakeLexicalIndex should pass isinstance check for LexicalIndex protocol."""
    fake = FakeLexicalIndex()
    assert isinstance(fake, LexicalIndex)


def test_lexical_index_add_and_search():
    """Test LexicalIndex add and search operations."""
    fake = FakeLexicalIndex()
    fake.add("chunk1", "これはテストです")
    fake.add("chunk2", "別のテストです")
    results = fake.search("テスト", k=2)
    assert len(results) == 2
    assert all(isinstance(score, float) for _, score in results)


def test_lexical_index_remove():
    """Test LexicalIndex remove operation."""
    fake = FakeLexicalIndex()
    fake.add("chunk1", "text1")
    fake.add("chunk2", "text2")
    fake.remove(["chunk1"])
    results = fake.search("text", k=10)
    assert len(results) == 1
    assert results[0][0] == "chunk2"


def test_lexical_index_search_with_allowed_ids():
    """Test LexicalIndex search with filtering by allowed_ids."""
    fake = FakeLexicalIndex()
    fake.add("chunk1", "text1")
    fake.add("chunk2", "text2")
    fake.add("chunk3", "text3")
    results = fake.search("text", k=10, allowed_ids={"chunk2", "chunk3"})
    assert len(results) == 2
    assert all(id_ in {"chunk2", "chunk3"} for id_, _ in results)
