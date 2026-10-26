from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agents import Agent, RunConfig, RunHooks, Runner, UserError, function_tool
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from acdp_hooks import AcdpPublisher, PublishError
from acdp_hooks.openai_agents import AcdpRunHooks, CompositeRunHooks, compose_run_hooks

from .conftest import FakeRegistry

CITED = "acdp://registry.example.com/12345678-1234-4321-8123-123456781234"
FETCHED = "acdp://localhost/00000000-0000-4000-a000-000000000000"


class ScriptedModel(Model):
    """Turn 1: call ``lookup`` citing a ctx_id. Turn 2: answer."""

    def __init__(self) -> None:
        self.turn = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.turn += 1
        if self.turn == 1:
            item: Any = ResponseFunctionToolCall(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="lookup",
                arguments=json.dumps({"ref": CITED}),
            )
        else:
            item = ResponseOutputMessage(
                type="message",
                id="msg_1",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text="the answer", annotations=[])],
            )
        return ModelResponse(output=[item], usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


@function_tool
def lookup(ref: str) -> str:
    """Look a context up."""
    return f"{ref} says see {FETCHED}"


def _agent() -> Agent[Any]:
    return Agent(name="Researcher", model=ScriptedModel(), tools=[lookup])


class Recorder(RunHooks[Any]):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: object) -> None:
        self.calls.append("tool_end")

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        self.calls.append("agent_end")


async def _run(hooks: RunHooks[Any]) -> Any:
    return await Runner.run(_agent(), "go", hooks=hooks, run_config=RunConfig(tracing_disabled=True))


async def test_real_run_publishes_tool_result_and_output(
    publisher: AcdpPublisher, registry: FakeRegistry
) -> None:
    acdp_hooks = AcdpRunHooks(publisher)
    recorder = Recorder()
    result = await _run(compose_run_hooks(recorder, None, acdp_hooks))

    assert result.final_output == "the answer"
    assert recorder.calls == ["tool_end", "agent_end"]  # the other hooks still ran

    tool_body, out_body = registry.bodies
    assert tool_body["type"] == "agent_hooks:tool-result"
    assert tool_body["metadata"]["framework"] == "openai-agents"
    assert tool_body["metadata"]["tool"] == "lookup"
    assert "started_at" in tool_body["metadata"]
    assert tool_body["derived_from"] == [CITED]

    tool_link, out_link = acdp_hooks.links
    assert out_body["type"] == "agent_hooks:agent-output"
    assert out_body["metadata"]["agent"] == "Researcher"
    # The tool echoed the id it was given and named another: both were consumed.
    assert out_body["derived_from"] == [CITED, FETCHED, tool_link.ctx_id]
    assert acdp_hooks.ledger.output_link == out_link


async def test_fail_open_run_completes(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    registry.script = [400, 400]
    acdp_hooks = AcdpRunHooks(publisher)
    result = await _run(acdp_hooks)
    assert result.final_output == "the answer"
    assert [f.code for f in acdp_hooks.ledger.failures] == ["code_400", "code_400"]
    assert acdp_hooks.links == []


async def test_fail_closed_aborts_the_run(closed_publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    registry.script = [400]
    # The SDK wraps a raising tool hook in its own UserError; the run aborts
    # before the model sees the result.
    with pytest.raises(UserError) as e:
        await _run(AcdpRunHooks(closed_publisher))
    assert isinstance(e.value.__cause__, PublishError)


async def test_fail_closed_wraps_unexpected_errors(
    closed_publisher: AcdpPublisher, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(closed_publisher, "publish_output", broken)
    with pytest.raises(PublishError) as e:
        await AcdpRunHooks(closed_publisher).on_agent_end(None, None, "x")
    assert e.value.code == "transport"


async def test_hosted_tool_without_tool_context(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    class Hosted:
        name = "web_search"

    hooks = AcdpRunHooks(publisher)
    await hooks.on_tool_start(object(), None, Hosted())
    await hooks.on_tool_end(object(), None, Hosted(), "results")
    record = registry.bodies[0]["metadata"]
    assert record["tool"] == "web_search"
    assert "started_at" not in record


def test_composite_forwards_every_callback() -> None:
    names = [n for n in dir(RunHooks) if n.startswith("on_")]
    assert names
    for name in names:
        assert getattr(CompositeRunHooks, name) is not getattr(RunHooks, name)
    assert compose_run_hooks(None).members == ()
