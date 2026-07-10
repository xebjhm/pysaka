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
from zoneinfo import ZoneInfo

import pytest

from pysaka.knowledge.agent import SYSTEM_PROMPT, AskCancelled, KnowledgeAgent, ToolCallingUnreliableError
from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.cleaner import SUBSCRIBER_SENTINEL, normalize_text
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


def _build_tools(*, text: str = "ライブ最高でした", subscriber_name: str = "you") -> tuple[ToolRunner, Document]:
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
        text=text,
        has_text=True,
    )
    store.upsert([doc])

    vectors = {doc.text: [1.0, 0.0]}
    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder(vectors))
    retriever.index([Chunk(chunk_id=f"{doc.doc_id}#0", doc_id=doc.doc_id, text=doc.text, context_text=doc.text)])

    runner = ToolRunner(aliases, reg, retriever, store, subscriber_name=subscriber_name)
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


async def test_ask_injects_current_datetime_and_zone_into_system_prompt():
    """Fix 2 (pwave-2): the agent must anchor relative-date questions ("last
    month") against a real clock in the user's zone, not the model's
    training-era guess -- via a `Current date/time: ... (<zone>)` line injected
    into the system message. Uses an injected `clock` (not time-machine) to
    keep this a pure/deterministic unit test of the seam itself."""
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"no_evidence": True}))]
    fake = FakeLLMClient(script)
    fixed_now = datetime(2026, 7, 3, 21, 15, tzinfo=timezone.utc)
    agent = KnowledgeAgent(fake, tools, clock=lambda: fixed_now, tz=ZoneInfo("Asia/Taipei"))

    await agent.ask("what did she do last month", _SCOPE)

    messages, _tools_schema = fake.calls[0]
    system_content = messages[0]["content"]
    # 21:15 UTC + 8h (Asia/Taipei, no DST) = 2026-07-04T05:15:00+08:00.
    assert "2026-07-04T05:15:00+08:00" in system_content
    assert "Asia/Taipei" in system_content
    assert "Current date/time:" in system_content


async def test_ask_injects_utc_zone_name_when_no_tz_given():
    """Default `tz` (none injected) is UTC -- the zone name in the prompt line
    must say "UTC", not some other stringified tzinfo repr."""
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"no_evidence": True}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools, clock=lambda: datetime(2026, 7, 3, 21, 15, tzinfo=timezone.utc))

    await agent.ask("what did she do last month", _SCOPE)

    system_content = fake.calls[0][0][0]["content"]
    assert "2026-07-03T21:15:00+00:00" in system_content
    assert "UTC" in system_content


async def test_ask_treats_naive_clock_result_as_utc_in_system_prompt():
    """A test-supplied `clock` returning a NAIVE `datetime` (no tzinfo) must be
    treated as UTC before converting to `tz`, mirroring `_parse_datetime`'s
    naive-input handling in `tools.py` -- not raise when `.astimezone()` is
    called on it."""
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"no_evidence": True}))]
    fake = FakeLLMClient(script)
    naive_now = datetime(2026, 7, 3, 21, 15)  # no tzinfo
    agent = KnowledgeAgent(fake, tools, clock=lambda: naive_now, tz=ZoneInfo("Asia/Taipei"))

    await agent.ask("what did she do last month", _SCOPE)

    system_content = fake.calls[0][0][0]["content"]
    # Treated as 21:15 UTC, then converted to +8h Asia/Taipei.
    assert "2026-07-04T05:15:00+08:00" in system_content


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


async def test_ask_falls_back_to_uncited_sentence_when_dict_missing_both_keys():
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"answer": "some text"}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("unexpected shape", _SCOPE)

    assert answer.no_evidence is False
    assert len(answer.sentences) == 1
    assert answer.sentences[0].text == json.dumps({"answer": "some text"})
    assert answer.sentences[0].citation_ids == []


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


# --- system prompt content (corpus language, answer language, style) ------------


def test_system_prompt_instructs_japanese_queries_and_question_language_answers():
    """The prompt must tell the model (i) the corpus is Japanese and `query`
    values are written in Japanese, (ii) to answer in the user's question
    language, (iii) to synthesize rather than dump dated quotes -- and the old
    verbatim-quote command (which manufactured the quote-dump style) is gone."""
    assert "The corpus is JAPANESE" in SYSTEM_PROMPT
    assert "`query` in Japanese" in SYSTEM_PROMPT
    assert "kana/synonym variants" in SYSTEM_PROMPT
    assert "ALWAYS answer in the language of the user's question" in SYSTEM_PROMPT
    assert "do NOT enumerate" in SYSTEM_PROMPT
    assert "{{NICKNAME}}" in SYSTEM_PROMPT
    # The verbatim-quote-dump command must be gone.
    assert "Quote Japanese snippets verbatim" not in SYSTEM_PROMPT
    assert "paraphrase or translate" not in SYSTEM_PROMPT


# --- max_steps bound: forced final synthesis turn -------------------------------


async def test_ask_forces_final_no_tools_synthesis_turn_after_max_steps():
    """Exhausting the tool-call budget must NOT silently return no_evidence:
    the agent gets ONE final no-tools turn to synthesize from the evidence
    gathered so far, and a grounded final answer from that turn is returned."""
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(tool_calls=[ToolCall("aggregate", {}, id="call_2")]),
        # The forced-synthesis response: grounded in the step-1 evidence.
        LLMResponse(text=json.dumps({"sentences": [{"text": "synthesized", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools, max_steps=2)

    answer, surfaced = await agent.ask("question that runs out of budget", _SCOPE)

    assert len(fake.calls) == 3  # 2 budgeted steps + 1 forced synthesis turn
    final_messages, final_tools = fake.calls[2]
    assert final_tools is None  # the synthesis turn offers NO tools
    # The synthesis instruction was appended for that final turn.
    assert any(m.get("role") == "user" and "final answer" in (m.get("content") or "") for m in final_messages)
    assert answer.no_evidence is False
    assert answer.sentences[0].text == "synthesized"
    assert doc.doc_id in surfaced


async def test_ask_forced_synthesis_turn_with_no_text_returns_no_evidence():
    """If even the forced no-tools synthesis turn produces nothing (no text --
    e.g. the model still tries to emit tool calls), THEN the ask resolves to
    no_evidence."""
    tools, _doc = _build_tools()
    # More scripted tool-call-only responses than max_steps allows; the 4th
    # (the synthesis turn) also carries no text.
    script = [LLMResponse(tool_calls=[ToolCall("aggregate", {}, id=f"call_{i}")]) for i in range(10)]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools, max_steps=3)

    answer, surfaced = await agent.ask("runaway tool caller", _SCOPE)

    assert len(fake.calls) == 4  # 3 budgeted steps + 1 forced synthesis turn
    assert fake.calls[3][1] is None  # synthesis turn offers no tools
    assert answer.no_evidence is True
    assert answer.sentences == []
    assert isinstance(surfaced, set)


async def test_ask_default_max_steps_allows_eight_llm_rounds():
    """The default budget is 8 steps (raised from 6): seven tool-call rounds
    followed by a final in-loop answer must complete WITHOUT triggering the
    forced-synthesis turn."""
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id=f"call_{i}")]) for i in range(7)
    ]
    script.append(LLMResponse(text=json.dumps({"sentences": [{"text": "found", "citation_ids": [doc.doc_id]}]})))
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("question needing many rounds", _SCOPE)

    assert len(fake.calls) == 8
    assert fake.calls[7][1] is not None  # still an in-budget step, tools offered
    assert answer.sentences[0].text == "found"


# --- invalid tool calls (malformed LLM tool-call arguments) ---------------------


async def test_ask_feeds_invalid_tool_call_back_as_error_without_dispatching_to_runner():
    """A `ToolCall` flagged `invalid_reason` (the LLM client's arguments couldn't be
    parsed) must never reach `ToolRunner.run` -- it should be turned directly into
    an `{"error": ...}` tool result carrying that reason, giving the model a
    self-correction round instead of crashing or running a bogus tool call."""
    tools, doc = _build_tools()
    script = [
        LLMResponse(
            tool_calls=[
                ToolCall("search", {}, id="call_1", invalid_reason="invalid arguments for tool 'search': bad json")
            ]
        ),
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_2")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "corrected", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, surfaced = await agent.ask("question that gets a malformed tool call first", _SCOPE)

    # The model got a 3rd chance (self-corrected on step 2) rather than the ask dying.
    assert len(fake.calls) == 3
    assert answer.no_evidence is False
    assert doc.doc_id in surfaced

    # The tool message fed back for the invalid call is the error, not a real search result.
    second_messages, _tools_schema = fake.calls[1]
    tool_turn = next(m for m in second_messages if m.get("role") == "tool" and m.get("id") == "call_1")
    assert json.loads(tool_turn["content"]) == {"error": "invalid arguments for tool 'search': bad json"}


async def test_ask_aborts_with_tool_calling_unreliable_after_three_invalid_calls():
    tools, _doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("search", {}, id=f"call_{i}", invalid_reason="bad json")]) for i in range(5)
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    with pytest.raises(ToolCallingUnreliableError, match="3"):
        await agent.ask("model that keeps emitting malformed tool calls", _SCOPE)

    # Aborted as soon as the 3rd invalid call was seen -- not all 5 scripted steps ran.
    assert len(fake.calls) == 3


async def test_ask_does_not_abort_on_two_invalid_calls_followed_by_success():
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("search", {}, id="call_1", invalid_reason="bad json")]),
        LLMResponse(tool_calls=[ToolCall("search", {}, id="call_2", invalid_reason="bad json")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "ok", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("two strikes then a valid final answer", _SCOPE)

    assert answer.no_evidence is False
    assert answer.sentences[0].text == "ok"


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


# --- should_abort: cooperative-cancel seam --------------------------------------


async def test_ask_raises_ask_cancelled_when_should_abort_flips_after_tool_batch():
    """`should_abort` is checked right after step 1's tool-call batch -- so when
    it flips there, step 2's LLM call must never happen."""
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "never reached", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)
    checks: list[int] = []

    def should_abort() -> bool:
        checks.append(1)
        return len(checks) >= 2  # False on the pre-step-1 check, True on the post-tool-batch check

    with pytest.raises(AskCancelled, match="after tool-call batch"):
        await agent.ask("question", _SCOPE, should_abort=should_abort)

    assert len(fake.calls) == 1  # step 2's LLM call never fired


async def test_ask_raises_ask_cancelled_when_should_abort_flips_before_next_llm_call():
    """`should_abort` also gets a fresh check before EVERY step's LLM call -- not
    just right after a tool batch. Flip it only on the 3rd check (step 2's
    pre-call check, having passed both of step 1's checks) and confirm exactly
    one LLM call happened before the loop unwound."""
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_2")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "never reached", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)
    checks: list[int] = []

    def should_abort() -> bool:
        checks.append(1)
        return len(checks) >= 3  # False on step 1's two checks, True on step 2's pre-call check

    with pytest.raises(AskCancelled, match="before LLM call"):
        await agent.ask("question", _SCOPE, should_abort=should_abort)

    assert len(fake.calls) == 1


async def test_ask_does_not_abort_when_should_abort_stays_false() -> None:
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "reached", "citation_ids": [doc.doc_id]}]})),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    answer, _surfaced = await agent.ask("question", _SCOPE, should_abort=lambda: False)

    assert answer.sentences[0].text == "reached"
    assert len(fake.calls) == 2


async def test_answer_propagates_ask_cancelled_from_should_abort():
    """`answer()` threads `should_abort` straight through to `ask()`; a raised
    `AskCancelled` propagates uncaught -- there is no partial answer to
    validate when the ask itself never completed."""
    tools, _doc = _build_tools()
    script = [LLMResponse(text=json.dumps({"no_evidence": True}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    with pytest.raises(AskCancelled):
        await agent.answer("question", _SCOPE, should_abort=lambda: True)

    assert len(fake.calls) == 0  # cancelled before the first LLM call


# --- answer(): grounded facade (ask() + validate() atomically) -----------------


async def test_answer_drops_a_citation_to_a_doc_id_ask_never_surfaced():
    """`ask()` blindly returns whatever the model cites; `answer()` must not.

    The model cites a doc_id no tool call ever surfaced this turn (fabricated /
    stale). `ask()` -- which never validates -- returns it as-is. `answer()`
    must run the same script through `validate()` and drop it, proving it is
    the grounded, by-construction entry point that `ask()` is not.
    """
    tools, _doc = _build_tools()
    fabricated_script = [
        LLMResponse(text=json.dumps({"sentences": [{"text": "fabricated claim", "citation_ids": ["blog:fake:999"]}]}))
    ]

    ask_agent = KnowledgeAgent(FakeLLMClient(fabricated_script), tools)
    raw_answer, surfaced = await ask_agent.ask("question with a fabricated citation", _SCOPE)
    assert raw_answer.no_evidence is False
    assert raw_answer.sentences[0].citation_ids == ["blog:fake:999"]  # unvalidated: leaks the bad citation
    assert "blog:fake:999" not in surfaced

    answer_agent = KnowledgeAgent(FakeLLMClient(fabricated_script), tools)
    validated = await answer_agent.answer("question with a fabricated citation", _SCOPE)

    assert validated.no_evidence is True
    assert validated.sentences == []
    assert validated.citations == []


async def test_answer_keeps_a_citation_to_a_doc_id_ask_did_surface():
    tools, doc = _build_tools()
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(text=json.dumps({"sentences": [{"text": "genuine claim", "citation_ids": [doc.doc_id]}]})),
    ]
    agent = KnowledgeAgent(FakeLLMClient(script), tools)

    validated = await agent.answer("question with a real citation", _SCOPE)

    assert validated.no_evidence is False
    assert len(validated.sentences) == 1
    assert validated.sentences[0].citation_ids == [doc.doc_id]
    assert len(validated.citations) == 1
    assert validated.citations[0].doc_id == doc.doc_id


# --- subscriber-name privacy: {{NICKNAME}} to the LLM, real name to the user ---

_REAL_NAME = "浩(ハオ)@台湾"


async def test_answer_threads_custom_subscriber_name_end_to_end():
    """Full privacy round-trip: evidence contains the subscriber sentinel; the
    LLM only ever sees the `{{NICKNAME}}` token (never the real name); the
    validated answer and its citation snippets render the REAL name."""
    doc_text = normalize_text("%%%さん、今日は焼肉を食べました")
    tools, doc = _build_tools(text=doc_text, subscriber_name=_REAL_NAME)
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(
            text=json.dumps(
                {"sentences": [{"text": "{{NICKNAME}}さん、今日は焼肉を食べました", "citation_ids": [doc.doc_id]}]}
            )
        ),
    ]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)

    validated = await agent.answer("她說了什麼？", _SCOPE)

    # LLM-facing: no message content ever carries the real name; the tool
    # result carries the token instead of the sentinel.
    for messages, _tools_schema in fake.calls:
        for message in messages:
            content = message.get("content") or ""
            assert _REAL_NAME not in content
    tool_turn = next(m for m in fake.calls[1][0] if m.get("role") == "tool")
    assert "{{NICKNAME}}" in tool_turn["content"]
    assert SUBSCRIBER_SENTINEL not in tool_turn["content"]

    # USER-facing: token substituted with the real name AFTER validation.
    assert validated.no_evidence is False
    assert validated.sentences[0].text == f"{_REAL_NAME}さん、今日は焼肉を食べました"
    assert "{{NICKNAME}}" not in validated.sentences[0].text
    assert _REAL_NAME in validated.citations[0].quoted_snippet
    assert SUBSCRIBER_SENTINEL not in validated.citations[0].quoted_snippet


async def test_answer_replaces_nickname_token_with_default_you():
    """Default behavior unchanged for API consumers: with no subscriber_name
    configured, user-facing output says "you" (the pre-existing rendering)."""
    doc_text = normalize_text("%%%さん、今日は焼肉を食べました")
    tools, doc = _build_tools(text=doc_text)
    script = [
        LLMResponse(tool_calls=[ToolCall("get_document", {"doc_id": doc.doc_id}, id="call_1")]),
        LLMResponse(
            text=json.dumps(
                {"sentences": [{"text": "{{NICKNAME}}さん、今日は焼肉を食べました", "citation_ids": [doc.doc_id]}]}
            )
        ),
    ]
    agent = KnowledgeAgent(FakeLLMClient(script), tools)

    validated = await agent.answer("what did she say", _SCOPE)

    assert validated.sentences[0].text == "youさん、今日は焼肉を食べました"
    assert "you" in validated.citations[0].quoted_snippet


async def test_ask_retokenizes_subscriber_name_in_history_before_llm():
    """History hygiene: prior turns shown to the user carry the REAL name;
    feeding them back verbatim would leak it to the LLM, so it is replaced
    with the `{{NICKNAME}}` token before the messages are sent."""
    tools, _doc = _build_tools(subscriber_name=_REAL_NAME)
    script = [LLMResponse(text=json.dumps({"no_evidence": True}))]
    fake = FakeLLMClient(script)
    agent = KnowledgeAgent(fake, tools)
    history = [
        {"role": "user", "content": "previous question"},
        {"role": "assistant", "content": f"{_REAL_NAME}さん、こんにちは"},
    ]

    await agent.ask("follow up", _SCOPE, history=history)

    messages, _tools_schema = fake.calls[0]
    assert messages[1] == {"role": "user", "content": "previous question"}
    assert messages[2] == {"role": "assistant", "content": "{{NICKNAME}}さん、こんにちは"}
    # The caller's own history list must not be mutated.
    assert history[1]["content"] == f"{_REAL_NAME}さん、こんにちは"


async def test_ask_does_not_retokenize_history_for_default_or_short_names():
    """Guard against mangling ordinary prose: no replacement for the default
    name ("you" appears in normal English) or a name shorter than 2 chars."""
    default_tools, _doc = _build_tools()
    fake_default = FakeLLMClient([LLMResponse(text=json.dumps({"no_evidence": True}))])
    history_you = [{"role": "assistant", "content": "you said hello"}]
    await KnowledgeAgent(fake_default, default_tools).ask("q", _SCOPE, history=history_you)
    assert fake_default.calls[0][0][1] == {"role": "assistant", "content": "you said hello"}

    short_tools, _doc = _build_tools(subscriber_name="浩")
    fake_short = FakeLLMClient([LLMResponse(text=json.dumps({"no_evidence": True}))])
    history_short = [{"role": "assistant", "content": "浩さん、こんにちは"}]
    await KnowledgeAgent(fake_short, short_tools).ask("q", _SCOPE, history=history_short)
    assert fake_short.calls[0][0][1] == {"role": "assistant", "content": "浩さん、こんにちは"}
