"""Claude Agent SDK hooks: one signed body per tool result, one per final output.

Wiring, alongside any hooks you already have (for example seam's SeamHooks)::

    from acdp_hooks import AcdpPublisher
    from acdp_hooks.claude_agent import AcdpClaudeHooks

    acdp_hooks = AcdpClaudeHooks(AcdpPublisher.from_env())
    options = ClaudeAgentOptions(hooks=acdp_hooks.attach(seam_hooks.matchers()))

``attach`` appends this package's matchers to the existing lists and never
replaces or reorders an existing matcher.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from claude_agent_sdk import HookMatcher

from .config import FailPolicy
from .publisher import AcdpPublisher, Link, PublishError, RunLedger, now_ms

log = logging.getLogger("acdp_hooks")

FRAMEWORK = "claude-agent-sdk"
EVENTS = ("PostToolUse", "PostToolUseFailure", "Stop")


def _is_error(response: Any) -> bool:
    if isinstance(response, Mapping):
        return bool(response.get("is_error") or response.get("isError"))
    return False


def merge_hooks(*hook_maps: Mapping[str, Sequence[HookMatcher]] | None) -> dict[str, list[HookMatcher]]:
    """Concatenate ``ClaudeAgentOptions.hooks`` maps, event by event, in the
    order given. Nothing is dropped, deduplicated or reordered."""
    merged: dict[str, list[HookMatcher]] = {}
    for hook_map in hook_maps:
        for event, matchers in (hook_map or {}).items():
            merged.setdefault(event, []).extend(matchers)
    return merged


class AcdpClaudeHooks:
    """Publishes an ACDP body for every tool result and every final output.

    - ``PostToolUse`` / ``PostToolUseFailure``: a tool-result body. Its
      ``ctx_id`` is handed back to the model as ``additionalContext`` (turn
      off with ``inject_link=False``) and recorded in the session's ledger.
    - ``Stop``: an output body over ``last_assistant_message``.

    Every hook body is sentineled: nothing it does raises into the SDK's
    control protocol. A publish failure is resolved by the publisher's
    :class:`~acdp_hooks.FailPolicy`.
    """

    def __init__(
        self,
        publisher: AcdpPublisher,
        *,
        inject_link: bool = True,
        publish_output_on_stop: bool = True,
    ) -> None:
        self.publisher = publisher
        self.inject_link = inject_link
        self.publish_output_on_stop = publish_output_on_stop
        self._ledgers: dict[str, RunLedger] = {}

    # ── ledgers ──────────────────────────────────────────────────────────

    def ledger(self, session_id: str) -> RunLedger:
        """The ledger for one session (created on first use)."""
        return self._ledgers.setdefault(session_id, RunLedger())

    def forget(self, session_id: str) -> RunLedger | None:
        """Drop a finished session's ledger and return it."""
        return self._ledgers.pop(session_id, None)

    # ── wiring ───────────────────────────────────────────────────────────

    @property
    def hook_timeout_s(self) -> float:
        """Strictly longer than one publish with all its retries, so the fail
        policy, not the SDK's hook timeout, decides a slow registry."""
        return self.publisher.config.deadline_s + 5.0

    def matchers(self) -> dict[str, list[HookMatcher]]:
        t = self.hook_timeout_s
        hooks: dict[str, list[HookMatcher]] = {
            "PostToolUse": [HookMatcher(matcher=None, hooks=[self.post_tool_use], timeout=t)],  # type: ignore[list-item]
            "PostToolUseFailure": [
                HookMatcher(matcher=None, hooks=[self.post_tool_use_failure], timeout=t)  # type: ignore[list-item]
            ],
        }
        if self.publish_output_on_stop:
            hooks["Stop"] = [HookMatcher(matcher=None, hooks=[self.stop], timeout=t)]  # type: ignore[list-item]
        return hooks

    def attach(
        self, existing: Mapping[str, Sequence[HookMatcher]] | None = None
    ) -> dict[str, list[HookMatcher]]:
        """``existing`` plus this package's matchers, appended per event. The
        input is not mutated."""
        return merge_hooks(existing, self.matchers())

    def install(self, options: Any) -> Any:
        """Append to ``options.hooks`` in place (a ``ClaudeAgentOptions``)."""
        options.hooks = self.attach(options.hooks)
        return options

    # ── hook bodies ──────────────────────────────────────────────────────

    async def post_tool_use(self, input: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        response = input.get("tool_response")
        return await self._tool(input, response, _is_error(response))

    async def post_tool_use_failure(
        self, input: dict[str, Any], tool_use_id: str | None, ctx: Any
    ) -> dict[str, Any]:
        return await self._tool(input, {"error": input.get("error")}, True)

    async def _tool(self, input: dict[str, Any], result: Any, is_error: bool) -> dict[str, Any]:
        tool = "unknown"
        event = str(input.get("hook_event_name") or "PostToolUse")
        try:
            tool = str(input["tool_name"])
            completed = now_ms()
            duration = input.get("duration_ms")
            started = completed - timedelta(milliseconds=duration) if isinstance(duration, int) else None
            link = await self.publisher.publish_tool_result(
                self.ledger(str(input.get("session_id", ""))),
                framework=FRAMEWORK,
                tool=tool,
                arguments=input.get("tool_input"),
                result=result,
                is_error=is_error,
                completed_at=completed,
                started_at=started,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # sentinel: never raise into the SDK
            return self._tool_failed(event, tool, e)
        if not self.inject_link:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": f"ACDP provenance for this {tool} result: {link.ctx_id}",
            }
        }

    def _tool_failed(self, event: str, tool: str, error: Exception) -> dict[str, Any]:
        code = error.code if isinstance(error, PublishError) else "transport"
        log.warning("acdp: publishing the %s result failed (%s): %s", tool, code, error)
        if self.publisher.fail_policy is FailPolicy.OPEN or event != "PostToolUse":
            return {}
        # Fail-closed: the model must not act on a result that has no
        # provenance, so the result is replaced and the turn is told why.
        message = f"Result of {tool} withheld: ACDP provenance publishing failed ({code})."
        output_key = "updatedMCPToolOutput" if tool.startswith("mcp__") else "updatedToolOutput"
        return {
            "decision": "block",
            "reason": message,
            "hookSpecificOutput": {"hookEventName": event, output_key: message},
        }

    async def stop(self, input: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        output = input.get("last_assistant_message")
        if not isinstance(output, str) or not output:
            return {}
        try:
            await self.publish_final_output(output, str(input.get("session_id", "")))
        except PublishError:  # only raised under fail-closed
            return {
                "continue_": False,
                "stopReason": "ACDP provenance publishing failed for the final output.",
            }
        return {}

    async def publish_final_output(self, output: Any, session_id: str) -> Link | None:
        """Publish ``output`` (for example ``ResultMessage.result``) as the
        session's final output. Fail-open returns ``None`` on failure;
        fail-closed raises :class:`~acdp_hooks.PublishError`."""
        try:
            return await self.publisher.publish_output(
                self.ledger(session_id), framework=FRAMEWORK, output=output
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("acdp: publishing the final output failed: %s", e)
            if self.publisher.fail_policy is FailPolicy.CLOSED:
                if isinstance(e, PublishError):
                    raise
                raise PublishError("transport", str(e), type="agent_hooks:agent-output") from e
            return None


__all__ = ["AcdpClaudeHooks", "merge_hooks"]
