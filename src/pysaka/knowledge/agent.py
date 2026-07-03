"""Bounded agent planner loop: turns a natural-language question into a grounded `Answer`.

`KnowledgeAgent` drives an `LLMClient` through a tool-calling loop against a
`ToolRunner`: it hands the model the question plus `TOOL_SCHEMAS`, executes any
tool calls the model requests, feeds the results back, and repeats -- up to
`max_steps` times -- until the model returns a final structured-answer JSON
payload instead of tool calls. It never validates citations itself (Task 15's
validator does that from the `doc_id`s this loop surfaces); it only parses the
model's final JSON into an `Answer` and tracks which `doc_id`s were surfaced.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, tzinfo
from typing import Callable

from .llm import LLMClient, ToolCall
from .models import Answer, AnswerSentence, Scope
from .tools import TOOL_SCHEMAS, ToolRunner

# How many invalid (unparseable-arguments) tool calls one `ask()` tolerates
# before giving up on the model entirely, rather than burning the rest of
# `max_steps` on a model that keeps emitting malformed JSON.
_MAX_INVALID_TOOL_CALLS = 3

SYSTEM_PROMPT = """\
You are a grounded research assistant over a group member's blog posts and messages.

Answer ONLY from facts returned by the tools below -- never rely on outside knowledge
or guesses. If a question refers to a member by nickname or partial name, call
`resolve_member` first to find the canonical member(s) it refers to. Then use `search`,
`get_document`, and `aggregate` to gather evidence before answering. Call as many tools,
in as many rounds, as you need to find the evidence -- but only what you need.

When you have enough evidence (or have determined there is none), respond with ONLY a
JSON object and nothing else -- no prose, no markdown fences. The JSON must have this
shape:

    {"sentences": [{"text": "...", "citation_ids": ["<doc_id>", ...]}, ...]}

Every sentence must cite the `doc_id`(s) of the document(s) that support it in
`citation_ids`. Quote Japanese snippets verbatim from the source text -- do not
paraphrase or translate quoted material. If, after using the tools, you find no
evidence to answer the question, respond with exactly:

    {"no_evidence": true}
"""


class ToolCallingUnreliableError(RuntimeError):
    """Raised when the model can't reliably drive the knowledge tools.

    `KnowledgeAgent.ask` aborts with this after `_MAX_INVALID_TOOL_CALLS`
    invalid (unparseable-arguments) tool calls in one ask -- rather than
    burning the rest of `max_steps` on a model that keeps emitting malformed
    JSON. Pure/UI-agnostic like the rest of `pysaka.knowledge`: callers (e.g.
    SakaDesk's `KnowledgeService`) are expected to catch this and translate it
    into whatever typed, actionable error their own UI layer uses.
    """


class KnowledgeAgent:
    """Bounded tool-calling planner loop that answers a question via `LLMClient` + `ToolRunner`.

    `clock`/`tz` are the agent's "what time is it right now, and in what
    timezone" seam: `ask()` injects a `Current date/time: <local iso> (<zone>)`
    line into the system prompt (see `_now_prompt_line`) so relative-date
    questions ("last month", "先月") resolve against the real clock in the
    user's zone instead of the model's training-era guess. `clock` defaults to
    `datetime.now(timezone.utc)` -- callers that use `time-machine` to freeze
    time need no special wiring, since that patches `datetime.now()` itself;
    `clock` is still injectable for tests that want a fixed instant without
    freezing the whole process clock. `tz` defaults to UTC.
    """

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRunner,
        max_steps: int = 6,
        *,
        clock: Callable[[], datetime] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._max_steps = max_steps
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._tz = tz if tz is not None else timezone.utc

    async def ask(self, question: str, scope: Scope, history: list[dict] | None = None) -> tuple[Answer, set[str]]:
        """Run the bounded planner loop for `question` and return `(answer, surfaced_doc_ids)`.

        `surfaced_doc_ids` accumulates every `doc_id` any tool call surfaced this
        conversation (from `search` hits and successful `get_document` calls), for
        Task 15's grounding validator to check citations against.

        Raises `ToolCallingUnreliableError` if `_MAX_INVALID_TOOL_CALLS` tool
        calls in this ask come back with `invalid_reason` set (see
        `_dispatch_tool_call`) -- a model that keeps failing to emit valid tool
        arguments won't reliably produce a usable answer either.
        """
        messages: list[dict] = [{"role": "system", "content": self._system_prompt()}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": question})

        surfaced: set[str] = set()
        invalid_call_count = 0

        for _step in range(self._max_steps):
            resp = await self._llm.chat(messages, tools=TOOL_SCHEMAS)

            if resp.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"name": call.name, "arguments": call.arguments, "id": call.id} for call in resp.tool_calls
                        ],
                    }
                )
                for call in resp.tool_calls:
                    result = self._dispatch_tool_call(call, scope)
                    messages.append(
                        {
                            "role": "tool",
                            "name": call.name,
                            "id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    surfaced |= _surfaced_doc_ids(result)
                    if call.invalid_reason is not None:
                        invalid_call_count += 1
                        if invalid_call_count >= _MAX_INVALID_TOOL_CALLS:
                            raise ToolCallingUnreliableError(
                                "model cannot drive the knowledge tools reliably "
                                f"({invalid_call_count} invalid tool call arguments in one ask)"
                            )
                continue

            return _parse_answer(resp.text), surfaced

        return Answer(sentences=[], citations=[], no_evidence=True), surfaced

    def _system_prompt(self) -> str:
        """`SYSTEM_PROMPT` plus the injected "current date/time" anchor line."""
        return f"{SYSTEM_PROMPT}\n{self._now_prompt_line()}\n"

    def _now_prompt_line(self) -> str:
        """`Current date/time: <local ISO> (<zone name>). Resolve relative dates ...`

        `self._clock()` may return a naive `datetime` (a test-supplied `clock`
        with no tzinfo of its own) -- treated as UTC before converting to
        `self._tz`, mirroring `_parse_datetime`'s naive-input handling in
        `tools.py`.
        """
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local_now = now.astimezone(self._tz)
        zone_name = getattr(self._tz, "key", None) or str(self._tz)
        return (
            f"Current date/time: {local_now.isoformat()} ({zone_name}). "
            "Resolve relative dates like 'last month' or 'last week' against this."
        )

    def _dispatch_tool_call(self, call: ToolCall, scope: Scope) -> dict:
        """Run `call` against `ToolRunner`, UNLESS it's flagged `invalid_reason`.

        An invalid call's `arguments` are just a placeholder (the `LLMClient`
        couldn't parse what the model actually sent) -- dispatching it to
        `ToolRunner` would either run a real tool with bogus/empty arguments
        (silently wrong, for tools with no required args) or raise. Neither
        gives the model an actionable error to self-correct from, so it's
        short-circuited into an explicit `{"error": invalid_reason}` instead.
        """
        if call.invalid_reason is not None:
            return {"error": call.invalid_reason}
        return self._tools.run(call, scope)

    async def answer(self, question: str, scope: Scope, history: list[dict] | None = None) -> Answer:
        """Ask `question` and return a grounding-VALIDATED `Answer` -- the recommended entry point.

        Runs `ask()` and then feeds its `(answer, surfaced_doc_ids)` straight into
        Task 15's `validate()` against the store `self._tools` was built with, so
        every citation returned here is guaranteed to resolve to a doc_id this
        turn actually surfaced. `ask()` remains available as the advanced,
        unvalidated entry point (e.g. for callers who want to validate against a
        different store or inspect `surfaced_doc_ids` themselves); prefer
        `answer()` unless you have a specific reason not to.
        """
        from .validator import validate

        raw, surfaced = await self.ask(question, scope, history)
        return validate(raw, surfaced, self._tools.store)


def _surfaced_doc_ids(result: dict) -> set[str]:
    """Extract every `doc_id` a tool result surfaced (`search` hits, or a found `get_document`)."""
    doc_ids: set[str] = set()
    hits = result.get("hits")
    if hits:
        doc_ids.update(hit["doc_id"] for hit in hits)
    doc_id = result.get("doc_id")
    if doc_id and "error" not in result:
        doc_ids.add(doc_id)
    return doc_ids


def _parse_answer(text: str | None) -> Answer:
    """Parse the model's final response `text` into an `Answer`.

    Expects `{"sentences": [{"text": ..., "citation_ids": [...]}, ...]}` or
    `{"no_evidence": true}`. Tolerant of surrounding whitespace/markdown fences
    (extracts the first `{...}` span). On parse failure or an unexpected shape,
    falls back to a single uncited sentence containing the raw text so a caller
    always gets an `Answer` back; Task 15's validator withholds uncited content.
    """
    data = _extract_json(text)
    if not isinstance(data, dict):
        return _fallback_answer(text)

    # If no_evidence key is explicitly truthy, return no evidence
    if data.get("no_evidence"):
        return Answer(sentences=[], citations=[], no_evidence=True)

    # If sentences key is present, check its value
    if "sentences" in data:
        if not data["sentences"]:
            # Empty sentences list means no evidence
            return Answer(sentences=[], citations=[], no_evidence=True)
        # Non-empty sentences list: parse and return
        try:
            sentences = [
                AnswerSentence(text=s["text"], citation_ids=list(s.get("citation_ids", []))) for s in data["sentences"]
            ]
        except (KeyError, TypeError):
            return _fallback_answer(text)
        return Answer(sentences=sentences, citations=[], no_evidence=False)

    # Dict present but missing both no_evidence and sentences: fallback to uncited sentence
    return _fallback_answer(text)


def _extract_json(text: str | None) -> object | None:
    """Lenient JSON extraction: strip whitespace/markdown fences, take the first `{...}` span."""
    if text is None:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        newline = stripped.find("\n")
        if newline != -1 and not stripped[:newline].strip().startswith("{"):
            stripped = stripped[newline + 1 :]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None


def _fallback_answer(text: str | None) -> Answer:
    return Answer(sentences=[AnswerSentence(text=text or "", citation_ids=[])], citations=[], no_evidence=False)
