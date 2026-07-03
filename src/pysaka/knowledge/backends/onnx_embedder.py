"""ONNX-based `Embedder` (Granite / e5 families) for the `pysaka[embeddings]` extra.

Loads a local ONNX model + `tokenizers` tokenizer from a directory, runs inference
via `onnxruntime`, and reduces per-token hidden states to a single L2-normalized
embedding per text via masked mean pooling.

`numpy` / `onnxruntime` / `tokenizers` are imported only in this module (never in
`pysaka.knowledge` core) to keep the core pure/UI-agnostic; downloading/verifying
model weights is out of scope (a helper script, not core).

**Execution providers (Product-wave Task 4).** `onnxruntime` picks a provider (CPU,
CUDA, DirectML, CoreML, ...) per session from an ORDERED list -- the first provider
in the list that's both AVAILABLE (installed) and able to successfully initialize a
session wins. `select_providers()` builds that preference list from whatever the
installed `onnxruntime` build reports as available (`ort.get_available_providers()`,
which differs between the plain `onnxruntime` wheel and `onnxruntime-gpu`/
`onnxruntime-directml`); `_create_session()` then guards against a provider that's
*listed* as available but fails to actually initialize (a stale/partial CUDA
install is the common case) by retrying CPU-only. This module never imports
anything SakaDesk-specific -- `providers` is a plain `list[str] | None`, so pysaka
stays UI-agnostic; the caller (e.g. SakaDesk's `knowledge_service.py`) is free to
plumb a settings-driven override through it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
import structlog
from tokenizers import Tokenizer

logger = structlog.get_logger(__name__)

# Preference order when the caller doesn't force a specific provider (`providers=None`):
# CUDA (NVIDIA) > DirectML (Windows, any GPU vendor) > CoreML (Apple Silicon/macOS) >
# whatever's left (CPU always ends up last via `select_providers`'s CPU-only fallback).
_PROVIDER_PREFERENCE = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
)
_CPU_PROVIDER = "CPUExecutionProvider"


class OnnxEmbedder:
    """`Embedder` backed by a local ONNX model + `tokenizers` tokenizer.

    `model_dir` must contain `model.onnx` and `tokenizer.json`. `prefix_scheme`
    selects model-specific input formatting:
    - `"granite"` (default): no prefix — Granite embedding models are prefix-free.
    - `"e5"`: prepend `"query: "` for `kind="query"`, `"passage: "` otherwise, per
      the e5 family's documented usage convention.

    `providers`: an ordered `onnxruntime` execution-provider list, e.g.
    `["CUDAExecutionProvider", "CPUExecutionProvider"]`. `None` (default)
    auto-selects via `select_providers()` -- preferring a GPU provider when the
    installed `onnxruntime` build reports one available, CPU otherwise. Either
    way, session creation falls back to CPU-only if the preferred provider list
    fails to initialize (see `_create_session`). `self.active_provider` records
    whichever provider `onnxruntime` actually reports using, for callers that
    want to surface it (e.g. a "Embedding: CUDA — RTX 3090" status line).
    """

    def __init__(
        self,
        model_dir: Path,
        providers: list[str] | None = None,
        prefix_scheme: str = "granite",
        max_length: int = 512,
    ) -> None:
        self._prefix_scheme = prefix_scheme
        resolved_providers = providers if providers is not None else select_providers()
        self._session, self.active_provider = _create_session(str(model_dir / "model.onnx"), resolved_providers)
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        # Truncate to the model's max sequence length BEFORE padding: encoder models
        # (Granite/e5, max 512) overflow their position embeddings on longer inputs.
        self._tokenizer.enable_truncation(max_length=max_length)
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


def select_providers(available: list[str] | None = None) -> list[str]:
    """The `onnxruntime` provider preference list to try, most-preferred first.

    `available` defaults to `ort.get_available_providers()` (the providers this
    `onnxruntime` install actually ships, not necessarily all usable at runtime --
    see `_create_session`). Returns `[<preferred-gpu-provider>, "CPUExecutionProvider"]`
    for the first entry of `_PROVIDER_PREFERENCE` found in `available` (CPU appended
    as `onnxruntime`'s own runtime fallback within one session), or `["CPUExecutionProvider"]`
    alone when no preferred GPU provider is available.
    """
    if available is None:
        available = ort.get_available_providers()
    available_set = set(available)
    for name in _PROVIDER_PREFERENCE:
        if name in available_set:
            return [name, _CPU_PROVIDER]
    return [_CPU_PROVIDER]


def _create_session(model_path: str, providers: list[str], session_cls: Any = None) -> tuple[Any, str]:
    """Create an `onnxruntime.InferenceSession`, falling back to CPU-only if
    `providers` fails to initialize (e.g. `CUDAExecutionProvider` is *listed* as
    available but the CUDA/cuDNN runtime itself is missing or broken -- a common
    real-world gap between "the onnxruntime-gpu wheel is installed" and "the
    system actually has a working CUDA runtime"). Returns `(session, active_provider)`
    where `active_provider` is `session.get_providers()[0]` -- whichever provider
    `onnxruntime` reports actually using, logged so it's visible which one "won".
    `session_cls` is test-injectable; defaults to the real `ort.InferenceSession`.
    """
    session_cls = session_cls if session_cls is not None else ort.InferenceSession
    try:
        session = session_cls(model_path, providers=providers)
    except Exception as exc:  # noqa: BLE001 - any provider-init failure must fall back, not crash embedding
        if providers == [_CPU_PROVIDER]:
            raise
        logger.warning(
            "onnx_embedder.provider_init_failed",
            providers=providers,
            error=str(exc),
            fallback=_CPU_PROVIDER,
        )
        session = session_cls(model_path, providers=[_CPU_PROVIDER])
    active_providers = session.get_providers()
    active = active_providers[0] if active_providers else _CPU_PROVIDER
    logger.info("onnx_embedder.provider_selected", provider=active, requested=providers)
    return session, active


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
