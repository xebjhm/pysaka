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

from .llm import LLMClient
from .models import Answer, AnswerSentence, Scope
from .tools import TOOL_SCHEMAS, ToolRunner

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


class KnowledgeAgent:
    """Bounded tool-calling planner loop that answers a question via `LLMClient` + `ToolRunner`."""

    def __init__(self, llm: LLMClient, tools: ToolRunner, max_steps: int = 6) -> None:
        self._llm = llm
        self._tools = tools
        self._max_steps = max_steps

    async def ask(self, question: str, scope: Scope, history: list[dict] | None = None) -> tuple[Answer, set[str]]:
        """Run the bounded planner loop for `question` and return `(answer, surfaced_doc_ids)`.

        `surfaced_doc_ids` accumulates every `doc_id` any tool call surfaced this
        conversation (from `search` hits and successful `get_document` calls), for
        Task 15's grounding validator to check citations against.
        """
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": question})

        surfaced: set[str] = set()

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
                    result = self._tools.run(call, scope)
                    messages.append(
                        {
                            "role": "tool",
                            "name": call.name,
                            "id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    surfaced |= _surfaced_doc_ids(result)
                continue

            return _parse_answer(resp.text), surfaced

        return Answer(sentences=[], citations=[], no_evidence=True), surfaced


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

    if data.get("no_evidence") or not data.get("sentences"):
        return Answer(sentences=[], citations=[], no_evidence=True)

    try:
        sentences = [
            AnswerSentence(text=s["text"], citation_ids=list(s.get("citation_ids", []))) for s in data["sentences"]
        ]
    except (KeyError, TypeError):
        return _fallback_answer(text)

    return Answer(sentences=sentences, citations=[], no_evidence=False)


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
