"""End to end against a real acdp-registry-rs.

    docker compose -f tests/integration/compose.yml up -d --wait
    ACDP_IT_REGISTRY=http://localhost:8443 pytest -m integration

or ``ACDP_IT_REGISTRY=https://registry.agentcontextdistributionprotocol.io``
(publishes public bodies under a throwaway did:key).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import quote

import acdp
import httpx
import pytest

from acdp_hooks import AcdpPublisher, HooksConfig, Link, format_producer_key, salted_hash
from acdp_hooks.claude_agent import AcdpClaudeHooks
from acdp_hooks.openai_agents import AcdpRunHooks

REGISTRY = os.environ.get("ACDP_IT_REGISTRY", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not REGISTRY, reason="set ACDP_IT_REGISTRY to a registry URL"),
]


@pytest.fixture
async def publisher() -> Any:
    pub = AcdpPublisher(HooksConfig(REGISTRY, format_producer_key(os.urandom(32))))
    yield pub
    await pub.aclose()


async def _fetch(link: Link) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{REGISTRY.rstrip('/')}/contexts/{quote(link.ctx_id, safe='')}")
    r.raise_for_status()
    body: dict[str, Any] = r.json()["body"]
    raw = json.dumps(body)
    acdp.AcdpVerifier.verify_body_offline(raw)
    acdp.AcdpVerifier.verify_ctx_id_binding(body_json=raw, expected_ctx_id=link.ctx_id)
    assert body["content_hash"] == link.content_hash
    assert body["derived_from"] == list(link.derived_from)
    return body


def _salt(link: Link) -> bytes:
    return base64.urlsafe_b64decode(link.salt + "=")


async def test_claude_hooks_publish_a_verifiable_chain(publisher: AcdpPublisher) -> None:
    hooks = AcdpClaudeHooks(publisher)
    response = {"content": "42 widgets"}
    out = await hooks.post_tool_use(
        {
            "session_id": "it",
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "inventory.csv"},
            "tool_response": response,
            "tool_use_id": "tu-1",
        },
        "tu-1",
        None,
    )
    ledger = hooks.ledger("it")
    assert not ledger.failures, ledger.failures
    (tool_link,) = ledger.tool_links
    assert tool_link.ctx_id in out["hookSpecificOutput"]["additionalContext"]

    await hooks.stop(
        {"session_id": "it", "hook_event_name": "Stop", "last_assistant_message": "There are 42 widgets."},
        None,
        None,
    )
    out_link = ledger.output_link
    assert out_link is not None
    assert out_link.derived_from == (tool_link.ctx_id,)

    tool_body = await _fetch(tool_link)
    assert tool_body["metadata"]["result_hash"] == salted_hash(_salt(tool_link), response)
    out_body = await _fetch(out_link)
    assert out_body["metadata"]["output_hash"] == salted_hash(_salt(out_link), "There are 42 widgets.")


async def test_openai_hooks_chain_a_consumed_context(publisher: AcdpPublisher) -> None:
    first = AcdpRunHooks(publisher)
    await first.on_agent_end(None, type("A", (), {"name": "Upstream"})(), "upstream result")
    upstream = first.ledger.output_link
    assert upstream is not None

    hooks = AcdpRunHooks(publisher)
    hooks.ledger.consume(upstream.ctx_id, verified=True)
    await hooks.on_tool_end(None, None, type("T", (), {"name": "summarise"})(), "summary")
    await hooks.on_agent_end(None, type("A", (), {"name": "Downstream"})(), "final")
    tool_link, out_link = hooks.links
    assert tool_link.derived_from == (upstream.ctx_id,)
    assert out_link.derived_from == (upstream.ctx_id, tool_link.ctx_id)
    for link in (upstream, tool_link, out_link):
        await _fetch(link)
