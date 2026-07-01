from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@runtime_checkable
class LLMClient(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse: ...


class FakeLLMClient:
    """Scriptable LLMClient for tests: returns queued responses in order and records calls."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self._i = 0
        self.calls: list[tuple[list[dict], list[dict] | None]] = []

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        self.calls.append((messages, tools))
        if self._i >= len(self._script):
            raise IndexError("FakeLLMClient script exhausted")
        resp = self._script[self._i]
        self._i += 1
        return resp
