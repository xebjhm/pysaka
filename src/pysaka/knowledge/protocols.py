"""Protocol definitions for pluggable knowledge engine backends.

These @runtime_checkable protocols allow dependency injection of embeddings,
vector storage, and lexical indexing backends without coupling the core
knowledge engine to specific implementations.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Protocol for text embedding backends.

    Implementations should convert text sequences into dense vector representations.
    """

    dim: int
    """Dimensionality of the embedding vectors."""

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        """Embed a list of texts.

        Args:
            texts: List of text strings to embed.
            kind: Embedding purpose ("passage" or "query"); may affect normalization.

        Returns:
            List of embedding vectors, one per input text.
        """
        ...


@runtime_checkable
class VectorStore(Protocol):
    """Protocol for vector similarity search backends.

    Implementations manage a collection of dense vectors indexed by string ID.
    """

    def add(self, ids: list[str], vectors: list[list[float]]) -> None:
        """Add or update vectors in the store.

        Args:
            ids: String identifiers for the vectors.
            vectors: Corresponding dense vectors.
        """
        ...

    def remove(self, ids: list[str]) -> None:
        """Remove vectors from the store.

        Args:
            ids: String identifiers to remove.
        """
        ...

    def search(self, vector: list[float], k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        """Search for the k nearest neighbors.

        Args:
            vector: Query vector.
            k: Number of results to return.
            allowed_ids: Optional set of IDs to restrict results.

        Returns:
            List of (id, similarity_score) tuples, highest scoring first.
        """
        ...


@runtime_checkable
class LexicalIndex(Protocol):
    """Protocol for full-text search backends.

    Implementations manage a text index for keyword/phrase matching.
    """

    def add(self, chunk_id: str, text: str) -> None:
        """Index a text chunk.

        Args:
            chunk_id: Unique identifier for the chunk.
            text: Text to index.
        """
        ...

    def remove(self, chunk_ids: list[str]) -> None:
        """Remove chunks from the index.

        Args:
            chunk_ids: Identifiers to remove.
        """
        ...

    def search(self, query: str, k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        """Search for documents matching the query.

        Args:
            query: Search query (keyword or phrase).
            k: Number of results to return.
            allowed_ids: Optional set of IDs to restrict results.

        Returns:
            List of (id, relevance_score) tuples, highest scoring first.
        """
        ...
