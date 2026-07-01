"""Golden-eval quality gate for `pysaka.knowledge`: drives the FULL engine over
synthetic fixtures (`tests/knowledge/golden/`) and gates on citation precision/recall.

This is the real quality gate for the knowledge engine, not a mock -- it wires every
production piece together (`MemberRegistry`, `AliasTable`, `MentionDetector`,
`ingest_blog`/`ingest_messages`, `chunk_documents`, `DocumentStore`, `PureLexicalIndex`,
`HybridRetriever`, `ToolRunner`, `KnowledgeAgent`, `validate`) over a small synthetic
corpus with KNOWN correct answers, then measures how well retrieval actually surfaces
those answers. If this test fails, the fixtures or the engine wiring are wrong --
thresholds must not be relaxed to force a pass.

Two deterministic fakes keep the run fully offline and reproducible:

- `ScriptedLLMClient` stands in for the LLM. Given one case's ordered tool-call `plan`,
  it requests every step of the plan on its first turn, then -- on its second turn,
  after the agent has appended the tool results to the conversation -- faithfully cites
  exactly the `doc_id`s those tool calls actually surfaced (no guessing). This means the
  gate measures RETRIEVAL quality, not LLM answer-writing quality.
- `FakeEmbedder` (+ the pure-Python cosine `FakeVectorStore`, the same shape as the one
  reused across `test_tools.py`/`test_agent.py`) tags text with keyword-triggered topic
  dimensions (JP + EN synonyms per topic), so an English query can retrieve a Japanese
  document about the same topic without a real embedding model.

Fixture design (`golden/*.json`): 3 synthetic hinatazaka46 members, 14 synthetic
documents (6 blogs + 8 messages, mixing CJK text and emoji) with deliberate distractors
(wrong member, wrong topic, wrong date) so precision is genuinely exercised, and 7 cases
covering "last time A mentioned B" (old + new mention, expecting only the newer one),
"food eaten this past month" (exercising the `date_from` filter against an older,
excluded doc), a genuine no-evidence case, an English cross-lingual query against
Japanese source material, and a `search` + `get_document` combination.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from pysaka.knowledge.agent import KnowledgeAgent
from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.chunking import chunk_documents
from pysaka.knowledge.ingest import ingest_blog, ingest_messages
from pysaka.knowledge.lexical import PureLexicalIndex
from pysaka.knowledge.llm import LLMResponse, ToolCall
from pysaka.knowledge.mentions import MentionDetector
from pysaka.knowledge.models import Document, Scope
from pysaka.knowledge.registry import MemberRegistry
from pysaka.knowledge.retrieve import HybridRetriever
from pysaka.knowledge.store import DocumentStore
from pysaka.knowledge.tools import ToolRunner
from pysaka.knowledge.validator import validate

_GOLDEN_DIR = Path(__file__).parent / "golden"

_PRECISION_GATE = 0.9
_RECALL_GATE = 0.7

# --- FakeEmbedder: keyword-triggered topic vectors (pure Python, no numpy) ----------

_TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("yakiniku", ("焼肉", "yakiniku")),
    ("ramen", ("ラーメン", "らーめん", "ramen")),
    ("dance", ("ダンス", "dance")),
    ("live", ("ライブ", "live")),
]


def _topic_vector(text: str) -> list[float]:
    folded = text.casefold()
    return [1.0 if any(trigger in folded for trigger in triggers) else 0.0 for _name, triggers in _TOPICS]


class FakeEmbedder:
    """Deterministic, dependency-free "embedder": keyword-triggered topic vectors.

    Each output dimension is one topic in `_TOPICS`, triggered by any of its JP/EN
    keyword substrings; a matching topic scores 1.0, everything else 0.0. This is
    enough for `FakeVectorStore`'s cosine search to pull an English query toward a
    Japanese document about the same topic (the cross-lingual golden case) without a
    real embedding model -- and to correctly score 0 similarity for unrelated text, so
    it doesn't manufacture false-positive matches.
    """

    dim = len(_TOPICS)

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        return [_topic_vector(text) for text in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeVectorStore:
    """Pure-Python cosine-similarity vector store (no numpy); mirrors test_tools.py."""

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


# --- ScriptedLLMClient: deterministic oracle over one case's tool-call plan ---------


class ScriptedLLMClient:
    """Deterministic oracle LLM: turns one case's `plan` into scripted tool calls, then
    faithfully cites exactly what those tool calls surfaced -- no guessing.

    First `chat()` call: requests every step of `plan` as tool calls in a single turn
    (mirroring the brief's "emit all the plan's tool calls at once"). Second `chat()`
    call (the agent has by then appended the assistant tool-call turn and every tool
    result turn to `messages`): collects every `doc_id` any tool result surfaced and
    cites all of them in one sentence; if none were surfaced, reports `no_evidence`.
    This makes the golden gate measure retrieval quality (does the engine surface the
    right docs for these filters?), never LLM answer-writing quality.
    """

    def __init__(self, plan: list[dict]) -> None:
        self._plan = plan
        self._step = 0

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        self._step += 1
        if self._step == 1:
            return LLMResponse(
                tool_calls=[
                    ToolCall(name=step["name"], arguments=step["arguments"], id=f"call_{i}")
                    for i, step in enumerate(self._plan)
                ]
            )
        doc_ids = _collect_surfaced_doc_ids(messages)
        if not doc_ids:
            return LLMResponse(text=json.dumps({"no_evidence": True}))
        sentence = {"text": "Evidence found.", "citation_ids": sorted(doc_ids)}
        return LLMResponse(text=json.dumps({"sentences": [sentence]}))


def _collect_surfaced_doc_ids(messages: list[dict]) -> set[str]:
    """Every `doc_id` surfaced by a `search`/`get_document` tool result in `messages`.

    Mirrors `agent._surfaced_doc_ids`: every hit in a `"hits"` array, plus a top-level
    `"doc_id"` as long as the result isn't an `"error"` (e.g. a `get_document` miss).
    """
    doc_ids: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        result = json.loads(message["content"])
        for hit in result.get("hits") or []:
            doc_ids.add(hit["doc_id"])
        doc_id = result.get("doc_id")
        if doc_id and "error" not in result:
            doc_ids.add(doc_id)
    return doc_ids


# --- precision/recall scoring --------------------------------------------------------


def _precision_recall(cited: set[str], expected: set[str]) -> tuple[float, float]:
    """Citation precision/recall of `cited` doc_ids against a case's `expected` set.

    Precision = `|cited & expected| / |cited|`, or 1.0 if nothing was cited (vacuously,
    no false positives to be penalized for). Recall = `|cited & expected| / |expected|`,
    or 1.0 if nothing was expected (vacuously, nothing to miss) -- so a correctly
    answered `no_evidence` case (both sets empty) scores a perfect `(1.0, 1.0)`.
    """
    overlap = len(cited & expected)
    precision = 1.0 if not cited else overlap / len(cited)
    recall = 1.0 if not expected else overlap / len(expected)
    return precision, recall


# --- engine wiring ---------------------------------------------------------------


def _load_json(name: str) -> Any:
    return json.loads((_GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _ingest_corpus(corpus: dict, service: str, resolve: Any) -> list[Document]:
    docs = [ingest_blog(blog, service, resolve) for blog in corpus.get("blogs", [])]
    for thread in corpus.get("message_threads", []):
        docs.extend(ingest_messages(thread, service, resolve))
    return docs


async def run_golden() -> dict:
    """Build the knowledge engine ONCE over the synthetic golden fixtures, then run
    every case in `golden/cases.json` end-to-end and score its citations against the
    case's known-correct `expected_doc_ids`.

    Returns `{"precision": <macro-avg over cases>, "recall": <macro-avg over cases>,
    "cases": [<per-case detail dict>, ...]}`.
    """
    members_data = _load_json("members.json")
    aliases_data = _load_json("aliases.json")
    corpus_data = _load_json("corpus.json")
    cases_data = _load_json("cases.json")

    service = members_data["meta"]["group"]
    registry = MemberRegistry.from_members_json(members_data, service)
    aliases = AliasTable.seed_from_registry(registry)
    aliases.load_curated(aliases_data)
    detector = MentionDetector(aliases.entries(service))

    docs = _ingest_corpus(corpus_data, service, registry.resolve_author)
    for doc in docs:
        doc.mentions = detector.detect(doc.text, doc.author_id)

    store = DocumentStore()
    store.upsert(docs)

    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder())
    retriever.index(chunk_documents(docs))

    per_case: list[dict] = []
    for case in cases_data:
        scope = Scope(**case["scope"])
        tools = ToolRunner(aliases, registry, retriever, store)
        agent = KnowledgeAgent(ScriptedLLMClient(case["plan"]), tools)
        answer, surfaced = await agent.ask(case["question"], scope)
        validated = validate(answer, surfaced, store)
        cited = {c.doc_id for c in validated.citations}
        expected = set(case["expected_doc_ids"])
        precision, recall = _precision_recall(cited, expected)
        expect_no_evidence = bool(case.get("expect_no_evidence"))
        no_evidence_ok = (validated.no_evidence and not cited) if expect_no_evidence else True
        per_case.append(
            {
                "id": case["id"],
                "precision": precision,
                "recall": recall,
                "cited": sorted(cited),
                "expected": sorted(expected),
                "no_evidence": validated.no_evidence,
                "expect_no_evidence": expect_no_evidence,
                "no_evidence_ok": no_evidence_ok,
            }
        )

    precisions = [c["precision"] for c in per_case]
    recalls = [c["recall"] for c in per_case]
    return {
        "precision": sum(precisions) / len(precisions) if precisions else 1.0,
        "recall": sum(recalls) / len(recalls) if recalls else 1.0,
        "cases": per_case,
    }


# --- the gate ----------------------------------------------------------------------


async def test_golden_gate():
    result = await run_golden()

    assert result["precision"] >= _PRECISION_GATE, result
    assert result["recall"] >= _RECALL_GATE, result
    assert all(case["no_evidence_ok"] for case in result["cases"]), result


def test_precision_recall_math_handmade_sets():
    assert _precision_recall(set(), set()) == (1.0, 1.0)
    assert _precision_recall(set(), {"a"}) == (1.0, 0.0)
    assert _precision_recall({"a"}, set()) == (0.0, 1.0)
    assert _precision_recall({"a", "b"}, {"a", "c"}) == (0.5, 0.5)
    assert _precision_recall({"a", "b"}, {"a", "b"}) == (1.0, 1.0)
    assert _precision_recall({"a", "b", "c"}, {"a"}) == (1 / 3, 1.0)
