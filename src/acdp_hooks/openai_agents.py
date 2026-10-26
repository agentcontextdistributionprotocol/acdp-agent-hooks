"""OpenAI Agents SDK run hooks: one signed body per tool result, one per final output.

Wiring::

    from acdp_hooks import AcdpPublisher
    from acdp_hooks.openai_agents import AcdpRunHooks, compose_run_hooks

    acdp_hooks = AcdpRunHooks(AcdpPublisher.from_env())
    result = await Runner.run(agent, prompt, hooks=compose_run_hooks(my_hooks, acdp_hooks))
    print(acdp_hooks.ledger.output_link)

``Runner.run`` takes a single ``hooks`` object, so :func:`compose_run_hooks`
fans every callback out to each hooks object in order. Use one
:class:`AcdpRunHooks` per run: its ledger is the run's ledger.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Any

from agents import RunHooks

from .config import FailPolicy
from .publisher import AcdpPublisher, Link, PublishError, RunLedger, now_ms

log = logging.getLogger("acdp_hooks")

FRAMEWORK = "openai-agents"


def _tool_call(context: Any, tool: Any) -> tuple[str, str | None, Any]:
    """(tool name, call id, arguments) from a ``ToolContext`` when the SDK
    passes one; other tool families only give us the tool."""
    name = getattr(context, "tool_name", None) or getattr(tool, "name", None) or type(tool).__name__
    return str(name), getattr(context, "tool_call_id", None), getattr(context, "tool_arguments", None)


class AcdpRunHooks(RunHooks[Any]):
    """Publishes an ACDP body for every local tool result (``on_tool_end``)
    and for the final output (``on_agent_end``).

    The SDK gives a hook no way to alter a tool result, so fail-closed here
    means raising :class:`~acdp_hooks.PublishError` out of the hook, which
    aborts the run before the model sees the unprovenanced result.
    Fail-open logs, records the failure in the ledger and carries on.
    """

    def __init__(self, publisher: AcdpPublisher, *, ledger: RunLedger | None = None) -> None:
        self.publisher = publisher
        self.ledger = ledger or RunLedger()
        self._started: dict[str, datetime] = {}

    @property
    def links(self) -> list[Link]:
        return self.ledger.links

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        _, call_id, _ = _tool_call(context, tool)
        if call_id:
            self._started[call_id] = now_ms()

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: object) -> None:
        name, call_id, arguments = _tool_call(context, tool)
        await self._guard(
            name,
            self.publisher.publish_tool_result(
                self.ledger,
                framework=FRAMEWORK,
                tool=name,
                arguments=arguments,
                result=result,
                is_error=False,
                completed_at=now_ms(),
                started_at=self._started.pop(call_id, None) if call_id else None,
            ),
        )

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        await self._guard(
            "final output",
            self.publisher.publish_output(
                self.ledger,
                framework=FRAMEWORK,
                output=output,
                agent=getattr(agent, "name", None),
            ),
        )

    async def _guard(self, what: str, publish: Any) -> None:
        try:
            await publish
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("acdp: publishing the %s failed: %s", what, e)
            if self.publisher.fail_policy is FailPolicy.CLOSED:
                if isinstance(e, PublishError):
                    raise
                raise PublishError("transport", str(e), type="unknown") from e


_CALLBACKS = tuple(
    name for name, fn in inspect.getmembers(RunHooks, inspect.iscoroutinefunction) if name.startswith("on_")
)


class CompositeRunHooks(RunHooks[Any]):
    """Forwards every ``RunHooks`` callback to each member, in order. A member
    that raises stops the remaining members for that callback and aborts the
    run, exactly as it would on its own."""

    def __init__(self, *members: RunHooks[Any]) -> None:
        self.members = tuple(m for m in members if m is not None)


def _forward(name: str) -> Any:
    async def method(self: CompositeRunHooks, *args: Any, **kwargs: Any) -> None:
        for member in self.members:
            await getattr(member, name)(*args, **kwargs)

    method.__name__ = name
    return method


for _name in _CALLBACKS:
    setattr(CompositeRunHooks, _name, _forward(_name))


def compose_run_hooks(*members: RunHooks[Any] | None) -> CompositeRunHooks:
    """One ``hooks=`` value from several; ``None`` members are skipped."""
    return CompositeRunHooks(*(m for m in members if m is not None))


__all__ = ["AcdpRunHooks", "CompositeRunHooks", "compose_run_hooks"]
