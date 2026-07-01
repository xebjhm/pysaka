"""Tests for the bounded agent planner loop (`KnowledgeAgent`).

Builds a small end-to-end fixture (MemberRegistry + AliasTable + DocumentStore +
HybridRetriever over indexed chunks), reusing the same pure-Python fakes as
test_tools.py/test_retrieve.py, so a `search` tool call returns a KNOWN `doc_id`.
Drives `KnowledgeAgent.ask` with a scripted `FakeLLMClient`.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from pysaka.knowledge.agent import KnowledgeAgent
from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.lexical import PureLexicalIndex
from pysaka.knowledge.llm import FakeLLMClient, LLMResponse, ToolCall
from pysaka.knowledge.models import Answer, Chunk, Document, Scope, SourceRef
from pysaka.knowledge.registry import MemberRegistry
from pysaka.knowledge.retrieve import HybridRetriever
from pysaka.knowledge.store import DocumentStore
from pysaka.knowledge.tools import ToolRunner

_SERVICE = "hinatazaka46"
_SCOPE = Scope(service=_SERVICE)
_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_MEMBERS = {
    "meta": {"group": _SERVICE},
    "members": [
        {
            "blogId": "12",
            "nameKanji": "金村 美玖",
            "nameHiragana": "かねむら みく",
            "nameRomaji": "Kanemura Miku",
            "generation": 2,
            "status": "active",
        },
    ],
}


# --- fakes (copied from test_tools.py/test_retrieve.py: pure-Python, no numpy) --


class FakeEmbedder:
    """Deterministic embedder: looks up a fixed vector for each exact input string."""

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
    """Pure-Python cosine-similarity vector store (no numpy) for tests."""

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


# --- fixture builder ----------------------------------------------------------


def _build_tools() -> tuple[ToolRunner, Document]:
    reg = MemberRegistry.from_members_json(_MEMBERS, _SERVICE)
    aliases = AliasTable.seed_from_registry(reg)
    aliases.load_curated({"members": {"hinatazaka46:12": {"aliases": ["みくちゃん"]}}})

    store = DocumentStore()
    doc = Document(
        doc_id="blog:hinatazaka46:2",
        source_ref=SourceRef(service=_SERVICE, kind="blog", blog_id="2", member_id=12),
        author_id="hinatazaka46:12",
        group=_SERVICE,
        timestamp=_NOW,
        type="blog",
        is_favorite=False,
        text="ライブ最高でした",
        has_text=True,
    )
    store.upsert([doc])

    vectors = {doc.text: [1.0, 0.0]}
    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder(vectors))
    retriever.index([Chunk(chunk_id=f"{doc.doc_id}#0", doc_id=doc.doc_id, text=doc.text, context_text=doc.text)])

    runner = ToolRunner(aliases, reg, retriever, store)
    return runner, doc


# --- ask: happy path -----------------------------------------------------------


async def test_ask_runs_scripted_resolve_then_search_then_returns_cited_answer():
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("resolve_member", {"text": "みくちゃん"}, id="call_1")]),
        LLMResponse(
            tool_calls=[ToolCall("search", {"author": "hinatazaka46:12", "sort": "recent", "limit": 1}, id="call_2")]
        ),
        LLMResponse(
            text=json.dumps(
                {"sentences": [{"text": "みくちゃんはライブ最高でしたと投稿しました", "citation_ids": [doc.doc_id]}]}
            )
        ),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, surfaced = await agent.ask("when did みくちゃん mention live", _SCOPE)

    assert len(fake.calls) == 3
    assert isinstance(answer, Answer)
    assert answer.no_evidence is False
    assert len(answer.sentences) == 1
    assert doc.doc_id in answer.sentences[0].citation_ids
    assert doc.doc_id in surfaced


async def test_ask_builds_system_then_history_then_user_messages():
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"no_evidence": True}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)
    history = [{"role": "user", "content": "previous question"}, {"role": "assistant", "content": "previous answer"}]

    await agent.ask("follow up question", _SCOPE, history=history)

    messages, tools_schema = fake.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[1:3] == history
    assert messages[3] == {"role": "user", "content": "follow up question"}
    assert tools_schema is not None
    assert {schema["name"] for schema in tools_schema} == {"resolve_member", "search", "get_document", "aggregate"}


# --- ask: no_evidence ------------------------------------------------------


async def test_ask_returns_no_evidence_when_final_answer_says_so():
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"no_evidence": True}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, surfaced = await agent.ask("who mentioned nobody", _SCOPE)

    assert answer.no_evidence is True
    assert answer.sentences == []
    assert answer.citations == []
    assert surfaced == set()


async def test_ask_returns_no_evidence_when_sentences_list_is_empty():
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"sentences": []}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("question with no evidence", _SCOPE)

    assert answer.no_evidence is True
    assert answer.sentences == []


async def test_ask_falls_back_to_uncited_sentence_on_unparseable_json():
    tools, _doc = _build_tools()
    script = [LLMResponse(text="this is not json")]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("garbled response", _SCOPE)

    assert answer.no_evidence is False
    assert len(answer.sentences) == 1
    assert answer.sentences[0].text == "this is not json"
    assert answer.sentences[0].citation_ids == []


async def test_ask_falls_back_to_uncited_sentence_on_malformed_json_inside_braces():
    tools, _doc = _build_tools()
    script = [LLMResponse(text="{not: valid, json}")]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("malformed braces", _SCOPE)

    assert answer.no_evidence is False
    assert len(answer.sentences) == 1
    assert answer.sentences[0].text == "{not: valid, json}"


async def test_ask_falls_back_to_uncited_sentence_when_final_text_is_none():
    tools, _doc = _build_tools()
    script = [LLMResponse(text=None)]  # no tool_calls, no text: degenerate but must not crash
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("empty final response", _SCOPE)

    assert answer.no_evidence is False
    assert len(answer.sentences) == 1
    assert answer.sentences[0].text == ""
    assert answer.sentences[0].citation_ids == []


async def test_ask_parses_answer_from_markdown_fenced_json():
    tools, doc = _build_tools()
    fenced = "```json\n" + json.dumps({"sentences": [{"text": "fenced", "citation_ids": [doc.doc_id]}]}) + "\n```"
    script = [LLMResponse(text=fenced)]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("fenced response", _SCOPE)

    assert answer.no_evidence is False
    assert answer.sentences[0].text == "fenced"
    assert answer.sentences[0].citation_ids == [doc.doc_id]


async def test_ask_falls_back_to_uncited_sentence_when_sentence_missing_text_key():
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"sentences": ["not-a-dict"]}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("malformed sentence shape", _SCOPE)

    assert answer.no_evidence is False
    assert len(answer.sentences) == 1
    assert answer.sentences[0].citation_ids == []


# --- max_steps bound -----------------------------------------------------------


async def test_ask_stops_after_max_steps_without_exhausting_script():
    tools, _doc = _build_tools()
    # More scripted tool-call-only responses than max_steps allows.
    script = [LLMResponse(tool_calls=[ToolCall("aggregate", {}, id=f"call_{i}")]) for i in range(10)]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools, max_steps=3)

    answer, surfaced = await agent.ask("runaway tool caller", _SCOPE)

    assert len(fake.calls) == 3
    assert answer.no_evidence is True
    assert answer.sentences == []
    assert isinstance(surfaced, set)


# --- surfaced doc_id extraction -------------------------------------------------


async def test_ask_surfaces_doc_id_from_get_document_result():
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "quoted text", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    _answer, surfaced = await agent.ask("quote the document", _SCOPE)

    assert surfaced == {doc.doc_id}


async def test_ask_does_not_surface_doc_id_on_get_document_error():
    tools, _doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": "does-not-exist"}, id="call_1")]),
        LLMResponse(text=json.dumps({"no_evidence": True})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    _answer, surfaced = await agent.ask("quote a missing document", _SCOPE)

    assert surfaced == set()


# --- tool-call message shape recorded in the transcript -------------------------


async def test_ask_appends_assistant_tool_calls_and_tool_result_messages():
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("search", {"author": "hinatazaka46:12", "limit": 1}, id="call_1")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "text", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    await agent.ask("question", _SCOPE)

    # second call's messages include the assistant tool_calls turn + tool result turn appended
    second_messages, _tools_schema = fake.calls[1]
    assistant_turn = next(m for m in second_messages if m.get("role") == "assistant" and "tool_calls" in m)
    assert assistant_turn["tool_calls"] == [
        {"name": "search", "arguments": {"author": "hinatazaka46:12", "limit": 1}, "id": "call_1"}
    ]

    tool_turn = next(m for m in second_messages if m.get("role") == "tool")
    assert tool_turn["name"] == "search"
    assert tool_turn["id"] == "call_1"
    parsed = json.loads(tool_turn["content"])
    assert parsed["hits"][0]["doc_id"] == doc.doc_id
