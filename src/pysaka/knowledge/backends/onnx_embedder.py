"""ONNX-based `Embedder` (Granite / e5 families) for the `pysaka[embeddings]` extra.

Loads a local ONNX model + `tokenizers` tokenizer from a directory, runs inference
via `onnxruntime`, and reduces per-token hidden states to a single L2-normalized
embedding per text via masked mean pooling.

`numpy` / `onnxruntime` / `tokenizers` are imported only in this module (never in
`pysaka.knowledge` core) to keep the core pure/UI-agnostic; downloading/verifying
model weights is out of scope (a helper script, not core).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
import structlog
from tokenizers import Tokenizer

logger = structlog.get_logger(__name__)


class OnnxEmbedder:
    """`Embedder` backed by a local ONNX model + `tokenizers` tokenizer.

    `model_dir` must contain `model.onnx` and `tokenizer.json`. `prefix_scheme`
    selects model-specific input formatting:
    - `"granite"` (default): no prefix — Granite embedding models are prefix-free.
    - `"e5"`: prepend `"query: "` for `kind="query"`, `"passage: "` otherwise, per
      the e5 family's documented usage convention.
    """

    def __init__(self, model_dir: Path, prefix_scheme: str = "granite") -> None:
        self._prefix_scheme = prefix_scheme
        self._session = ort.InferenceSession(str(model_dir / "model.onnx"))
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self.dim = _infer_dim(self._session)

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        """Embed `texts`; `kind` ("query" | "passage") selects the `e5` prefix."""
        if not texts:
            return []
        prefixed = [_apply_prefix(self._prefix_scheme, kind, text) for text in texts]
        encodings = self._tokenizer.encode_batch(prefixed)
        input_ids = np.asarray([encoding.ids for encoding in encodings], dtype=np.int64)
        attention_mask = np.asarray([encoding.attention_mask for encoding in encodings], dtype=np.int64)
        input_names = {node.name for node in self._session.get_inputs()}
        feed = _build_feed(input_names, input_ids, attention_mask)
        outputs = self._session.run(None, feed)
        hidden_state = np.asarray(outputs[0])
        pooled = _masked_mean_pool(hidden_state, attention_mask)
        normalized = _l2_normalize_rows(pooled)
        return normalized.tolist()


def _apply_prefix(prefix_scheme: str, kind: str, text: str) -> str:
    """Prepend the model-specific prefix for `prefix_scheme`/`kind` (`"granite"` adds none)."""
    if prefix_scheme == "e5":
        return f"{'query' if kind == 'query' else 'passage'}: {text}"
    return text


def _build_feed(input_names: set[str], input_ids: np.ndarray, attention_mask: np.ndarray) -> dict[str, np.ndarray]:
    """Build the onnxruntime input feed, including only names the model actually declares."""
    feed: dict[str, np.ndarray] = {}
    if "input_ids" in input_names:
        feed["input_ids"] = input_ids
    if "attention_mask" in input_names:
        feed["attention_mask"] = attention_mask
    if "token_type_ids" in input_names:
        feed["token_type_ids"] = np.zeros_like(input_ids)
    return feed


def _masked_mean_pool(hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Mean-pool `hidden_state` (batch, seq, dim) over tokens where `attention_mask == 1`."""
    mask = attention_mask.astype(np.float32)[:, :, None]
    summed = (hidden_state * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-9, None)
    return summed / counts


def _l2_normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize each row of `matrix`; a zero-norm row is left unchanged (all zeros)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe_norms = np.where(norms == 0, 1.0, norms)
    return matrix / safe_norms


def _infer_dim(session: Any) -> int:
    """Infer embedding dimensionality from the last axis of the model's last output."""
    shape = session.get_outputs()[-1].shape
    return int(shape[-1])
