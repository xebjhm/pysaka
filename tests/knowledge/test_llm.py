from __future__ import annotations

import pytest

from pysaka.knowledge.llm import FakeLLMClient, LLMClient, LLMResponse, ToolCall


async def test_fake_llm_client_returns_queued_responses_in_order():
    """FakeLLMClient returns responses from script in order."""
    script = [
        LLMResponse(text="hello"),
        LLMResponse(text="world"),
    ]
    fake = FakeLLMClient(script)

    resp1 = await fake.chat([{"role": "user", "content": "hi"}])
    assert resp1.text == "hello"
    assert resp1.tool_calls == []

    resp2 = await fake.chat([{"role": "user", "content": "hi again"}])
    assert resp2.text == "world"
    assert resp2.tool_calls == []


async def test_fake_llm_client_records_calls():
    """FakeLLMClient records all chat calls with messages and tools."""
    script = [
        LLMResponse(text="response1"),
        LLMResponse(tool_calls=[ToolCall(name="search", arguments={"q": "test"})]),
    ]
    fake = FakeLLMClient(script)

    msgs1 = [{"role": "user", "content": "query1"}]
    tools1 = None
    await fake.chat(msgs1, tools1)

    msgs2 = [{"role": "user", "content": "query2"}, {"role": "assistant", "content": "thought"}]
    tools2 = [{"name": "search", "description": "search docs"}]
    await fake.chat(msgs2, tools2)

    assert len(fake.calls) == 2
    assert fake.calls[0] == (msgs1, tools1)
    assert fake.calls[1] == (msgs2, tools2)


async def test_fake_llm_client_tool_calls_response():
    """FakeLLMClient returns tool_calls correctly."""
    tool_call = ToolCall(name="search", arguments={"q": "hinatazaka46"}, id="call_123")
    script = [LLMResponse(tool_calls=[tool_call])]
    fake = FakeLLMClient(script)

    resp = await fake.chat([{"role": "user", "content": "search"}])
    assert resp.text is None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "search"
    assert resp.tool_calls[0].arguments == {"q": "hinatazaka46"}
    assert resp.tool_calls[0].id == "call_123"


def test_fake_llm_client_is_llm_client_protocol():
    """FakeLLMClient implements LLMClient protocol (runtime_checkable)."""
    script = [LLMResponse(text="test")]
    fake = FakeLLMClient(script)
    assert isinstance(fake, LLMClient)


async def test_fake_llm_client_script_exhaustion():
    """FakeLLMClient raises IndexError when script is exhausted."""
    script = [LLMResponse(text="one")]
    fake = FakeLLMClient(script)

    # First call succeeds
    await fake.chat([{"role": "user", "content": "q1"}])

    # Second call raises IndexError
    with pytest.raises(IndexError, match="FakeLLMClient script exhausted"):
        await fake.chat([{"role": "user", "content": "q2"}])
