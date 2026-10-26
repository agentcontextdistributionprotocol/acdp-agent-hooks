from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from acdp_hooks import (
    OUTPUT_TYPE,
    TOOL_RESULT_TYPE,
    AcdpPublisher,
    Link,
    PublishError,
    RunLedger,
    salted_hash,
)
from acdp_hooks.publisher import rfc3339_ms

from .conftest import FakeRegistry

INPUT = "acdp://registry.example.com/12345678-1234-4321-8123-123456781234"
ARG_ID = "acdp://r.example/aaaaaaaa-bbbb-4ccc-9ddd-eeeeeeeeeeee"
RESULT_ID = "acdp://localhost/00000000-0000-4000-a000-000000000000"


def _salt(link: Link) -> bytes:
    return base64.urlsafe_b64decode(link.salt + "=")


async def _tool(publisher: AcdpPublisher, ledger: RunLedger, **kw: Any) -> Link:
    args: dict[str, Any] = {
        "framework": "test",
        "tool": "lookup",
        "arguments": {"q": "secret query", "ref": ARG_ID},
        "result": {"content": [{"type": "text", "text": f"found {RESULT_ID}"}]},
        "is_error": False,
        "completed_at": datetime(2026, 10, 26, 12, 0, 0, 123456, tzinfo=timezone.utc),
    }
    args.update(kw)
    return await publisher.publish_tool_result(ledger, **args)


async def test_tool_result_body(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    ledger = RunLedger()
    ledger.consume(INPUT)
    started = datetime(2026, 10, 26, 11, 59, 59, tzinfo=timezone.utc)
    link = await _tool(publisher, ledger, started_at=started)

    (body,) = registry.bodies
    assert body["type"] == TOOL_RESULT_TYPE
    assert body["title"] == "Tool result: lookup"
    assert body["visibility"] == "public"
    assert body["tags"] == ["acdp-agent-hooks"]
    assert body["agent_id"] == publisher.producer_did == link.producer
    # Inputs consumed before the call, then ids its arguments cite. The id in
    # the result is an output of this call, not one of its inputs.
    assert body["derived_from"] == [INPUT, ARG_ID] == list(link.derived_from)

    record = body["metadata"]
    assert record == {
        "record_version": "acdp-agent-hooks-tool-result/1",
        "framework": "test",
        "tool": "lookup",
        "arguments_hash": record["arguments_hash"],
        "result_hash": record["result_hash"],
        "is_error": False,
        "salted": True,
        "embedded": False,
        "started_at": "2026-10-26T11:59:59.000Z",
        "completed_at": "2026-10-26T12:00:00.123Z",
    }
    salt = _salt(link)
    assert len(salt) == 32
    assert record["result_hash"] == salted_hash(
        salt, {"content": [{"type": "text", "text": f"found {RESULT_ID}"}]}
    )
    assert record["arguments_hash"] == salted_hash(salt, {"ref": ARG_ID, "q": "secret query"})

    # Neither the content nor the salt is anywhere in the body.
    raw = json.dumps(body)
    assert "secret query" not in raw
    assert link.salt not in raw
    assert salt.hex() not in raw

    assert link.content_hash == body["content_hash"]
    assert link.registry == "registry.test"
    assert link.version == 1
    assert str(link) == link.ctx_id
    assert link.salt not in repr(link)

    # The result's ctx_id is now an input for whatever the run does next.
    assert ledger.consumed == [INPUT, RESULT_ID]
    assert ledger.tool_links == [link]


async def test_fresh_salt_per_body(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    ledger = RunLedger()
    a = await _tool(publisher, ledger)
    b = await _tool(publisher, ledger)
    assert a.salt != b.salt
    assert registry.bodies[0]["metadata"]["result_hash"] != registry.bodies[1]["metadata"]["result_hash"]


async def test_output_derives_from_inputs_and_tool_results(
    publisher: AcdpPublisher, registry: FakeRegistry
) -> None:
    ledger = RunLedger()
    ledger.consume(INPUT, verified=True)
    t1 = await _tool(publisher, ledger)
    out = await publisher.publish_output(ledger, framework="test", output="final answer", agent="Planner")

    body = registry.bodies[-1]
    assert body["type"] == OUTPUT_TYPE
    assert body["title"] == "Agent output: Planner"
    assert body["derived_from"] == [INPUT, RESULT_ID, t1.ctx_id]
    assert body["metadata"]["output_hash"] == salted_hash(_salt(out), "final answer")
    assert body["metadata"]["agent"] == "Planner"
    assert "final answer" not in json.dumps(body)
    assert ledger.output_link == out
    assert ledger.links == [t1, out]


async def test_output_without_agent_or_inputs(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    await publisher.publish_output(RunLedger(), framework="test", output={"x": math.nan})
    body = registry.bodies[-1]
    assert body["title"] == "Agent output"
    assert body["derived_from"] == []
    assert "agent" not in body["metadata"]


async def test_private_visibility(make_publisher: Any, registry: FakeRegistry) -> None:
    await _tool(make_publisher(visibility="private"), RunLedger())
    assert registry.bodies[0]["visibility"] == "private"


async def test_idempotency_key_and_retry_resend_identical_bytes(
    publisher: AcdpPublisher, registry: FakeRegistry
) -> None:
    registry.script = [503]
    await _tool(publisher, RunLedger())
    first, second = registry.requests
    assert first.content == second.content
    key = first.headers["Idempotency-Key"]
    assert key == second.headers["Idempotency-Key"]
    assert key == "acdp-hooks-" + registry.bodies[0]["content_hash"].removeprefix("sha256:")


async def test_retries_transport_errors(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    registry.script = [httpx.ConnectError("down"), 429]
    with pytest.raises(PublishError) as e:
        await _tool(publisher, RunLedger())
    # retries=1: two attempts, the last answer wins.
    assert e.value.code == "code_429"
    assert len(registry.requests) == 2


async def test_client_error_is_not_retried(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    registry.script = [400]
    ledger = RunLedger()
    with pytest.raises(PublishError) as e:
        await _tool(publisher, ledger)
    assert e.value.code == "code_400"
    assert e.value.type == TOOL_RESULT_TYPE
    assert len(registry.requests) == 1
    assert ledger.failures == [e.value]
    assert ledger.tool_links == []
    assert ledger.consumed == [RESULT_ID]


async def test_transport_code_when_no_acdp_error(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    registry.script = [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")]
    with pytest.raises(PublishError) as e:
        await _tool(publisher, RunLedger())
    assert e.value.code == "transport"


@pytest.mark.parametrize("payload", [b"not json", b'{"error": "flat"}', b"[]"])
async def test_non_acdp_error_bodies(make_publisher: Any, payload: bytes) -> None:
    pub = AcdpPublisher(
        make_publisher().config,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(418, content=payload))
        ),
    )
    with pytest.raises(PublishError) as e:
        await _tool(pub, RunLedger())
    assert e.value.code == "transport"


async def test_unbuildable_body_is_a_publish_error(publisher: AcdpPublisher, registry: FakeRegistry) -> None:
    ledger = RunLedger()
    with pytest.raises(PublishError, match="could not build body"):
        await _tool(publisher, ledger, tool="x" * 70000)
    assert registry.requests == []
    assert len(ledger.failures) == 1


async def test_on_publish_callback(make_publisher: Any) -> None:
    seen: list[Link] = []
    pub = make_publisher(on_publish=seen.append)
    link = await _tool(pub, RunLedger())
    assert seen == [link]


async def test_owned_client_lifecycle(make_publisher: Any) -> None:
    pub = AcdpPublisher(make_publisher().config)
    assert pub._http() is pub._http()
    await pub.aclose()
    assert pub._client is None
    await pub.aclose()


def test_ledger_consume_rules() -> None:
    ledger = RunLedger()
    with pytest.raises(ValueError, match="not a ctx_id"):
        ledger.consume("acdp://nope")
    assert ledger.inputs_verified() is None
    ledger.consume(INPUT)
    assert ledger.inputs_verified() is None
    ledger.consume(ARG_ID, verified=True)
    assert ledger.inputs_verified() is False  # INPUT was never checked
    ledger.consume(INPUT, verified=True)
    assert ledger.inputs_verified() is True
    ledger.consume(INPUT)  # a later unverified sighting keeps the verdict
    assert ledger.inputs_verified() is True
    ledger.consume(ARG_ID, verified=False)
    assert ledger.inputs_verified() is False


def test_rfc3339_ms_normalises_to_utc() -> None:
    t = datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=timezone(timedelta(hours=2)))
    assert rfc3339_ms(t) == "2026-01-02T01:04:05.999Z"


def test_salted_hash_handles_non_json_values() -> None:
    class Model:
        def model_dump(self, mode: str) -> dict[str, Any]:
            return {"a": 1}

    salt = b"\0" * 32
    assert salted_hash(salt, Model()) == salted_hash(salt, {"a": 1})
    assert salted_hash(salt, {1: (1, 2)}) == salted_hash(salt, {"1": [1, 2]})
    assert salted_hash(salt, object()).startswith("sha256:")
