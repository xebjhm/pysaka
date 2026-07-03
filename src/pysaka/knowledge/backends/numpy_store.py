"""Flat brute-force cosine `VectorStore` backed by an in-memory numpy matrix.

Part of the optional `pysaka[embeddings]` extra: `numpy` is imported here only,
never in `pysaka.knowledge` core, to keep the core pure/UI-agnostic.
"""

from __future__ import annotations

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


class NumpyVectorStore:
    """`VectorStore` doing exhaustive cosine search over an in-memory numpy matrix.

    Vectors are L2-normalized on `add`, so `search` reduces to a single matrix-vector
    dot product against the (also normalized) query. A zero-norm vector cannot be
    normalized and is stored as all-zeros, scoring 0 against every query rather than
    raising.
    """

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._index: dict[str, int] = {}  # id -> row index in `_matrix` (mirror of `_ids`)
        self._matrix: np.ndarray | None = None  # shape (n, dim), float32, L2-normalized rows

    def add(self, ids: list[str], vectors: list[list[float]]) -> None:
        """Add `vectors` under `ids`; an id that already exists REPLACES its row.

        Batched: membership checks go through an id->row dict (O(1) per id, not a
        list scan) and all new rows are appended with a SINGLE `vstack` per call —
        a per-row vstack would copy the whole growing matrix each time, turning a
        large batch load (e.g. rehydrating a persisted store) quadratic.
        """
        new_ids: list[str] = []
        new_rows: list[np.ndarray] = []
        for id_, vector in zip(ids, vectors):
            normalized = _normalize(np.asarray(vector, dtype=np.float32))
            existing = self._index.get(id_)
            if existing is not None:
                self._matrix[existing] = normalized  # type: ignore[index]
            else:
                self._index[id_] = len(self._ids) + len(new_ids)
                new_ids.append(id_)
                new_rows.append(normalized)
        if new_rows:
            block = np.stack(new_rows)
            self._matrix = block if self._matrix is None else np.vstack([self._matrix, block])
            self._ids.extend(new_ids)

    def remove(self, ids: list[str]) -> None:
        """Drop the rows for `ids` (silently ignores unknown ids)."""
        drop = set(ids)
        keep_idx = [i for i, id_ in enumerate(self._ids) if id_ not in drop]
        self._ids = [self._ids[i] for i in keep_idx]
        self._matrix = self._matrix[keep_idx] if keep_idx and self._matrix is not None else None
        self._index = {id_: i for i, id_ in enumerate(self._ids)}

    def search(self, vector: list[float], k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        """Return up to `k` `(id, cosine_score)` pairs, highest score first.

        Ties are broken by ascending id for determinism. Returns `[]` for an empty
        store.
        """
        if self._matrix is None or not self._ids:
            return []
        query = _normalize(np.asarray(vector, dtype=np.float32))
        scores = self._matrix @ query
        candidates = list(zip(self._ids, (float(s) for s in scores)))
        if allowed_ids is not None:
            candidates = [(id_, score) for id_, score in candidates if id_ in allowed_ids]
        candidates.sort(key=lambda pair: (-pair[1], pair[0]))
        return candidates[:k]


def _normalize(vector: np.ndarray) -> np.ndarray:
    """L2-normalize `vector`; a zero-norm vector is returned unchanged (all zeros)."""
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return vector / norm
