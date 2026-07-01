"""Tests for the `[embeddings]` extra: `NumpyVectorStore` and `OnnxEmbedder`.

`NumpyVectorStore` tests are pure numpy and always run. `OnnxEmbedder` pure-logic
helpers (prefix selection, masked mean-pool, L2-normalize, input-feed building,
dim inference) are unit-tested with hand-made arrays and a fake tokenizer/session
— no real model weights required. The full integration test is
`@pytest.mark.integration` and is skipped unless `SAKA_TEST_MODEL_DIR` is set
(the default pytest addopts deselect `-m "not integration"`, so it never runs in
CI without an explicit opt-in).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from pysaka.knowledge.backends.numpy_store import NumpyVectorStore
from pysaka.knowledge.backends.onnx_embedder import (
    OnnxEmbedder,
    _apply_prefix,
    _build_feed,
    _infer_dim,
    _l2_normalize_rows,
    _masked_mean_pool,
)

# --------------------------------------------------------------------------
# NumpyVectorStore
# --------------------------------------------------------------------------


def test_search_empty_store_returns_empty_list() -> None:
    store = NumpyVectorStore()
    assert store.search([1.0, 0.0, 0.0], k=5) == []


def test_search_returns_nearest_by_cosine_first() -> None:
    store = NumpyVectorStore()
    store.add(
        ["a", "b", "c"],
        [
            [1.0, 0.0, 0.0],  # identical direction to query
            [0.0, 1.0, 0.0],  # orthogonal to query
            [0.9, 0.1, 0.0],  # close to query, but not identical
        ],
    )
    results = store.search([1.0, 0.0, 0.0], k=3)
    ids = [id_ for id_, _ in results]
    assert ids == ["a", "c", "b"]
    # scores sorted descending
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)
    # "a" is an exact direction match -> cosine == 1.0
    assert results[0][1] == pytest.approx(1.0)


def test_search_respects_k() -> None:
    store = NumpyVectorStore()
    store.add(
        ["a", "b", "c"],
        [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]],
    )
    results = store.search([1.0, 0.0], k=1)
    assert len(results) == 1
    assert results[0][0] == "a"


def test_search_allowed_ids_restricts_candidates() -> None:
    store = NumpyVectorStore()
    store.add(
        ["a", "b", "c"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.9, 0.1, 0.0]],
    )
    results = store.search([1.0, 0.0, 0.0], k=3, allowed_ids={"b", "c"})
    ids = [id_ for id_, _ in results]
    assert ids == ["c", "b"]
    assert "a" not in ids


def test_remove_drops_id() -> None:
    store = NumpyVectorStore()
    store.add(["a", "b"], [[1.0, 0.0], [0.0, 1.0]])
    store.remove(["a"])
    results = store.search([1.0, 0.0], k=5)
    ids = [id_ for id_, _ in results]
    assert ids == ["b"]


def test_remove_all_then_search_returns_empty_list() -> None:
    store = NumpyVectorStore()
    store.add(["a"], [[1.0, 0.0]])
    store.remove(["a"])
    assert store.search([1.0, 0.0], k=5) == []


def test_add_existing_id_replaces_row() -> None:
    store = NumpyVectorStore()
    store.add(["a", "b"], [[1.0, 0.0], [0.0, 1.0]])
    store.add(["a"], [[0.0, 1.0]])  # replace "a" so it now points where "b" does
    results = store.search([0.0, 1.0], k=2)
    scores = dict(results)
    assert scores["a"] == pytest.approx(1.0)
    assert scores["b"] == pytest.approx(1.0)
    # still only one row for "a" -- no duplicate entries
    assert len(results) == 2


def test_zero_norm_vector_does_not_crash_and_scores_zero() -> None:
    store = NumpyVectorStore()
    store.add(["z"], [[0.0, 0.0, 0.0]])
    results = store.search([1.0, 0.0, 0.0], k=5)
    assert results == [("z", pytest.approx(0.0))]


def test_search_tie_break_deterministic_by_id() -> None:
    store = NumpyVectorStore()
    store.add(["b", "a"], [[1.0, 0.0], [1.0, 0.0]])
    results = store.search([1.0, 0.0], k=2)
    # equal cosine scores -> ascending id order
    assert [id_ for id_, _ in results] == ["a", "b"]


# --------------------------------------------------------------------------
# OnnxEmbedder -- pure helpers (no model required)
# --------------------------------------------------------------------------


def test_apply_prefix_granite_adds_no_prefix() -> None:
    assert _apply_prefix("granite", "passage", "焼肉") == "焼肉"
    assert _apply_prefix("granite", "query", "焼肉") == "焼肉"


def test_apply_prefix_e5_adds_query_and_passage_prefixes() -> None:
    assert _apply_prefix("e5", "query", "焼肉") == "query: 焼肉"
    assert _apply_prefix("e5", "passage", "焼肉") == "passage: 焼肉"


def test_masked_mean_pool_averages_only_unmasked_tokens() -> None:
    # batch of 1, 3 tokens, dim 2; last token is padding (masked out)
    hidden_state = np.array(
        [[[1.0, 1.0], [3.0, 3.0], [99.0, 99.0]]],
        dtype=np.float32,
    )
    attention_mask = np.array([[1, 1, 0]], dtype=np.int64)
    pooled = _masked_mean_pool(hidden_state, attention_mask)
    np.testing.assert_allclose(pooled, [[2.0, 2.0]])


def test_masked_mean_pool_handles_batch() -> None:
    hidden_state = np.array(
        [
            [[1.0, 0.0], [1.0, 0.0]],
            [[0.0, 2.0], [0.0, 6.0]],
        ],
        dtype=np.float32,
    )
    attention_mask = np.array([[1, 1], [1, 1]], dtype=np.int64)
    pooled = _masked_mean_pool(hidden_state, attention_mask)
    np.testing.assert_allclose(pooled, [[1.0, 0.0], [0.0, 4.0]])


def test_l2_normalize_rows_unit_length() -> None:
    matrix = np.array([[3.0, 4.0], [1.0, 0.0]], dtype=np.float32)
    normalized = _l2_normalize_rows(matrix)
    norms = np.linalg.norm(normalized, axis=1)
    np.testing.assert_allclose(norms, [1.0, 1.0])
    np.testing.assert_allclose(normalized[0], [0.6, 0.8])


def test_l2_normalize_rows_zero_row_left_unchanged() -> None:
    matrix = np.array([[0.0, 0.0], [2.0, 0.0]], dtype=np.float32)
    normalized = _l2_normalize_rows(matrix)
    np.testing.assert_allclose(normalized[0], [0.0, 0.0])
    np.testing.assert_allclose(normalized[1], [1.0, 0.0])


def test_build_feed_includes_only_declared_input_names() -> None:
    input_ids = np.array([[1, 2]], dtype=np.int64)
    attention_mask = np.array([[1, 1]], dtype=np.int64)
    feed = _build_feed({"input_ids", "attention_mask"}, input_ids, attention_mask)
    assert set(feed.keys()) == {"input_ids", "attention_mask"}
    np.testing.assert_array_equal(feed["input_ids"], input_ids)
    np.testing.assert_array_equal(feed["attention_mask"], attention_mask)


def test_build_feed_adds_token_type_ids_when_declared() -> None:
    input_ids = np.array([[1, 2]], dtype=np.int64)
    attention_mask = np.array([[1, 1]], dtype=np.int64)
    feed = _build_feed({"input_ids", "attention_mask", "token_type_ids"}, input_ids, attention_mask)
    assert set(feed.keys()) == {"input_ids", "attention_mask", "token_type_ids"}
    np.testing.assert_array_equal(feed["token_type_ids"], np.zeros_like(input_ids))


class _FakeOutputInfo:
    def __init__(self, shape: list[object]) -> None:
        self.shape = shape


class _FakeInputInfo:
    def __init__(self, name: str) -> None:
        self.name = name


def test_infer_dim_reads_last_dim_of_last_output() -> None:
    class _FakeSession:
        def get_outputs(self) -> list[_FakeOutputInfo]:
            return [_FakeOutputInfo(["batch", "sequence", 768])]

    assert _infer_dim(_FakeSession()) == 768


# --------------------------------------------------------------------------
# OnnxEmbedder -- full `embed()` pipeline wired to a fake tokenizer + session
# --------------------------------------------------------------------------


class _FakeEncoding:
    def __init__(self, ids: list[int], attention_mask: list[int]) -> None:
        self.ids = ids
        self.attention_mask = attention_mask


class _FakeTokenizer:
    def __init__(self) -> None:
        self.seen_texts: list[str] = []
        self.padding_enabled = False

    def enable_padding(self, pad_id: int = 0, pad_token: str = "[PAD]") -> None:
        self.padding_enabled = True

    def encode_batch(self, texts: list[str]) -> list[_FakeEncoding]:
        self.seen_texts = list(texts)
        # 3 tokens per text, last one padding for the (shorter) second text
        return [
            _FakeEncoding([10, 20, 30], [1, 1, 1]),
            _FakeEncoding([11, 21, 0], [1, 1, 0]),
        ][: len(texts)]


class _FakeSession:
    def __init__(self, input_names: list[str], hidden_state: np.ndarray) -> None:
        self._input_names = input_names
        self._hidden_state = hidden_state
        self.last_feed: dict[str, np.ndarray] | None = None

    def get_inputs(self) -> list[_FakeInputInfo]:
        return [_FakeInputInfo(name) for name in self._input_names]

    def run(self, output_names: object, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.last_feed = feed
        return [self._hidden_state]


def _make_embedder(prefix_scheme: str, session: _FakeSession, tokenizer: _FakeTokenizer) -> OnnxEmbedder:
    embedder = OnnxEmbedder.__new__(OnnxEmbedder)
    embedder._prefix_scheme = prefix_scheme
    embedder._session = session
    embedder._tokenizer = tokenizer
    embedder.dim = 2
    return embedder


def test_embed_applies_prefix_pools_and_normalizes() -> None:
    hidden_state = np.array(
        [
            [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],  # all unmasked -> mean [1, 0]
            [[0.0, 3.0], [0.0, 5.0], [99.0, 99.0]],  # last masked -> mean [0, 4]
        ],
        dtype=np.float32,
    )
    session = _FakeSession(["input_ids", "attention_mask"], hidden_state)
    tokenizer = _FakeTokenizer()
    embedder = _make_embedder("e5", session, tokenizer)

    result = embedder.embed(["焼肉", "寿司"], kind="query")

    assert tokenizer.seen_texts == ["query: 焼肉", "query: 寿司"]
    assert session.last_feed is not None
    assert set(session.last_feed.keys()) == {"input_ids", "attention_mask"}
    np.testing.assert_allclose(result, [[1.0, 0.0], [0.0, 1.0]])
    assert all(len(vec) == 2 for vec in result)


def test_embed_granite_scheme_sends_text_unprefixed() -> None:
    hidden_state = np.zeros((2, 3, 2), dtype=np.float32)
    session = _FakeSession(["input_ids", "attention_mask"], hidden_state)
    tokenizer = _FakeTokenizer()
    embedder = _make_embedder("granite", session, tokenizer)

    embedder.embed(["焼肉", "寿司"], kind="passage")

    assert tokenizer.seen_texts == ["焼肉", "寿司"]


def test_embed_empty_texts_returns_empty_list() -> None:
    session = _FakeSession(["input_ids", "attention_mask"], np.zeros((0, 0, 2), dtype=np.float32))
    tokenizer = _FakeTokenizer()
    embedder = _make_embedder("granite", session, tokenizer)
    assert embedder.embed([]) == []


# --------------------------------------------------------------------------
# OnnxEmbedder -- full integration (real model), skipped by default
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_onnx_embedder_embeds_real_text() -> None:
    model_dir_env = os.environ.get("SAKA_TEST_MODEL_DIR")
    if not model_dir_env:
        pytest.skip("SAKA_TEST_MODEL_DIR not set; skipping real-model integration test")
    embedder = OnnxEmbedder(Path(model_dir_env))
    vectors = embedder.embed(["焼肉"])
    assert len(vectors) == 1
    assert len(vectors[0]) == embedder.dim
    norm = sum(v * v for v in vectors[0]) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-3)
