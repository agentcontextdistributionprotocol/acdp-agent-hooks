from __future__ import annotations

from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from acdp_hooks import AcdpPublisher, PublishError
from acdp_hooks.claude_agent import AcdpClaudeHooks, merge_hooks

from .conftest import FakeRegistry

INPUT = "acdp://registry.example.com/12345678-1234-4321-8123-123456781234"
SESSION = "sess-1"


def _post(**kw: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/etc/hosts"},
        "tool_response": {"content": f"cites {INPUT}"},
        "tool_use_id": "tu-1",
        "duration_ms": 250,
    }
    payload.update(kw)
    return payload


async def _seam_pre(input: Any, tool_use_id: Any, ctx: Any) -> dict[str, Any]:
    return {}


async def _seam_post(input: Any, tool_use_id: Any, ctx: Any) -> dict[str, Any]:
    return {}


def _seam_like() -> dict[str, list[HookMatcher]]:
    """The shape seam's SeamHooks.matchers() returns."""
    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[_seam_pre], timeout=35.0)],  # type: ignore[list-item]
        "PostToolUse": [HookMatcher(matcher=None, hooks=[_seam_post], timeout=35.0)],  # type: ignore[list-item]
        "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[_seam_post], timeout=35.0)],  # type: ignore[list-item]
    }


def test_attach_appends_and_never_replaces(publisher: AcdpPublisher) -> None:
    hooks = AcdpClaudeHooks(publisher)
    existing = _seam_like()
    snapshot = {k: list(v) for k, v in existing.items()}

    merged = hooks.attach(existing)

    assert existing == snapshot  # input untouched
    assert merged["PreToolUse"] == existing["PreToolUse"]
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert merged[event][0] is existing[event][0]  # seam's matcher first
        assert len(merged[event]) == 2
    assert [m.hooks for m in merged["Stop"]] == [[hooks.stop]]
    assert merged["PostToolUse"][1].hooks == [hooks.post_tool_use]
    assert merged["PostToolUse"][1].matcher is None
    assert merged["PostToolUse"][1].timeout == hooks.hook_timeout_s > publisher.config.deadline_s


def test_attach_to_nothing_and_merge_order(publisher: AcdpPublisher) -> None:
    hooks = AcdpClaudeHooks(publisher, publish_output_on_stop=False)
    assert set(hooks.attach()) == {"PostToolUse", "PostToolUseFailure"}
    a, b = _seam_like(), _seam_like()
    merged = merge_hooks(a, None, b)
    assert merged["PreToolUse"] == a["PreToolUse"] + b["PreToolUse"]


def test_install_on_options(publisher: AcdpPublisher) -> None:
    options = ClaudeAgentOptions(hooks=_seam_like())
    hooks = AcdpClaudeHooks(publisher)
    assert hooks.install(options) is options
    assert options.hooks is not None
    assert len(options.hooks["PostToolUse"]) == 2
    assert options.hooks["PostToolUse"][0].hooks == [_seam_post]


async def test_post_tool_use_publishes_and_returns_link(
    publisher: AcdpPublisher, registry: FakeRegistry
) -> None:
    hooks = AcdpClaudeHooks(publisher)
    out = await hooks.post_tool_use(_post(), "tu-1", {"signal": None})

    link = hooks.ledger(SESSION).tool_links[0]
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"ACDP provenance for this Read result: {link.ctx_id}",
        }
    }
    record = registry.bodies[0]["metadata"]
    assert record["framework"] == "claude-agent-sdk"
    assert record["tool"] == "Read"
    assert record["is_error"] is False
    assert "started_at" in record
    assert hooks.ledger(SESSION).consumed == [INPUT]


async def test_inject_link_off_and_mcp_error_flag(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    hooks = AcdpClaudeHooks(publisher, inject_link=False)
    out = await hooks.post_tool_use(
        _post(tool_name="mcp__db__q", tool_response={"isError": True}, duration_ms=None), None, None
    )
    assert out == {}
    record = registry.bodies[0]["metadata"]
    assert record["is_error"] is True
    assert "started_at" not in record


async def test_tool_failure_is_published_as_error(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    hooks = AcdpClaudeHooks(publisher)
    out = await hooks.post_tool_use_failure(
        _post(hook_event_name="PostToolUseFailure", error="boom", tool_response=None), "tu-1", None
    )
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert registry.bodies[0]["metadata"]["is_error"] is True


async def test_fail_open_lets_the_result_through(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    registry.script = [400]
    hooks = AcdpClaudeHooks(publisher)
    assert await hooks.post_tool_use(_post(), "tu-1", None) == {}
    assert hooks.ledger(SESSION).failures[0].code == "code_400"


@pytest.mark.parametrize(
    ("tool", "key"), [("Read", "updatedToolOutput"), ("mcp__db__q", "updatedMCPToolOutput")]
)
async def test_fail_closed_withholds_the_result(
    closed_publisher: AcdpPublisher, registry: FakeRegistry, tool: str, key: str
) -> None:
    registry.script = [500]
    hooks = AcdpClaudeHooks(closed_publisher)
    out = await hooks.post_tool_use(_post(tool_name=tool), "tu-1", None)
    assert out["decision"] == "block"
    assert "withheld" in out["reason"]
    assert "code_500" in out["hookSpecificOutput"][key]


async def test_fail_closed_does_not_rewrite_failure_events(
    closed_publisher: AcdpPublisher, registry: FakeRegistry
) -> None:
    registry.script = [500]
    hooks = AcdpClaudeHooks(closed_publisher)
    out = await hooks.post_tool_use_failure(
        _post(hook_event_name="PostToolUseFailure", error="x"), None, None
    )
    assert out == {}


async def test_malformed_input_never_raises(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    hooks = AcdpClaudeHooks(publisher)
    assert await hooks.post_tool_use({"session_id": SESSION}, None, None) == {}
    assert registry.requests == []


async def test_stop_publishes_output_deriving_from_tool_results(
    publisher: AcdpPublisher, registry: FakeRegistry
) -> None:
    hooks = AcdpClaudeHooks(publisher)
    await hooks.post_tool_use(_post(), "tu-1", None)
    stop = {"session_id": SESSION, "hook_event_name": "Stop", "stop_hook_active": False}
    assert await hooks.stop({**stop, "last_assistant_message": "done"}, None, None) == {}

    ledger = hooks.ledger(SESSION)
    out_body = registry.bodies[-1]
    assert out_body["type"] == "agent_hooks:agent-output"
    assert out_body["derived_from"] == [INPUT, ledger.tool_links[0].ctx_id]
    assert ledger.output_link is not None

    # No message, no body.
    assert await hooks.stop(stop, None, None) == {}
    assert len(registry.bodies) == 2
    assert hooks.forget(SESSION) is ledger
    assert hooks.forget(SESSION) is None


async def test_stop_fail_open_and_closed(
    publisher: AcdpPublisher, closed_publisher: AcdpPublisher, registry: FakeRegistry
) -> None:
    stop = {"session_id": SESSION, "hook_event_name": "Stop", "last_assistant_message": "done"}
    registry.script = [400]
    assert await AcdpClaudeHooks(publisher).stop(stop, None, None) == {}

    registry.script = [400]
    out = await AcdpClaudeHooks(closed_publisher).stop(stop, None, None)
    assert out["continue_"] is False
    assert "provenance" in out["stopReason"]


async def test_publish_final_output_policy(
    publisher: AcdpPublisher,
    closed_publisher: AcdpPublisher,
    registry: FakeRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.script = [400]
    assert await AcdpClaudeHooks(publisher).publish_final_output("x", SESSION) is None

    registry.script = [400]
    with pytest.raises(PublishError):
        await AcdpClaudeHooks(closed_publisher).publish_final_output("x", SESSION)

    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(closed_publisher, "publish_output", broken)
    with pytest.raises(PublishError) as e:
        await AcdpClaudeHooks(closed_publisher).publish_final_output("x", SESSION)
    assert e.value.code == "transport"
