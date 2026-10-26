from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from acdp_hooks import AcdpPublisher, PublishError, RunLedger, annotate_chain_verdict, otel

from .conftest import FakeRegistry

INPUT = "acdp://registry.example.com/12345678-1234-4321-8123-123456781234"

# The spec registry's names, copied from registries/otel-attributes.md. The
# spec checkout (ACDP_SPEC_DIR) is compared against this list when present.
SPEC_NAMES = {
    "acdp.ctx_id",
    "acdp.lineage_id",
    "acdp.version",
    "acdp.content_hash",
    "acdp.producer",
    "acdp.registry",
    "acdp.type",
    "acdp.derived_from",
    "acdp.publish.error",
    "acdp.input.ctx_ids",
    "acdp.input.verified",
    "acdp.input.chain_verdict",
    "acdp.input.chain_verdict_as_of",
    "acdp.input.verdict_digest",
}

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def _by_name(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def test_registered_names_match_the_spec_registry() -> None:
    assert otel.REGISTERED == SPEC_NAMES


def test_registered_names_match_the_spec_checkout() -> None:
    import os

    spec_dir = os.environ.get("ACDP_SPEC_DIR")
    if not spec_dir:
        if os.environ.get("ACDP_REQUIRE_CONFORMANCE") == "1":
            pytest.fail("ACDP_REQUIRE_CONFORMANCE=1 but ACDP_SPEC_DIR is not set")
        pytest.skip("ACDP_SPEC_DIR not set")
    table = (Path(spec_dir) / "registries" / "otel-attributes.md").read_text()
    names = {line.split("`")[1] for line in table.splitlines() if line.startswith("| `acdp.")}
    assert names == otel.REGISTERED


async def _publish(publisher: AcdpPublisher, ledger: RunLedger) -> None:
    await publisher.publish_tool_result(
        ledger,
        framework="test",
        tool="lookup",
        arguments={"q": "the secret argument"},
        result="the secret result",
        is_error=False,
        completed_at=datetime.now(timezone.utc),
    )


async def test_publish_span_attributes(
    publisher: AcdpPublisher, registry: FakeRegistry, spans: InMemorySpanExporter
) -> None:
    ledger = RunLedger()
    ledger.consume(INPUT, verified=True)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("agent.tool"):
        await _publish(publisher, ledger)

    (pub,) = _by_name(spans, "acdp.publish")
    (parent,) = _by_name(spans, "agent.tool")
    assert pub.parent is not None
    assert pub.parent.span_id == parent.context.span_id

    body = registry.bodies[0]
    link = ledger.tool_links[0]
    assert dict(pub.attributes or {}) == {
        "acdp.ctx_id": link.ctx_id,
        "acdp.lineage_id": link.lineage_id,
        "acdp.version": 1,
        "acdp.content_hash": body["content_hash"],
        "acdp.producer": body["agent_id"],
        "acdp.registry": "registry.test",
        "acdp.type": body["type"],
        "acdp.derived_from": (INPUT,),
    }
    assert dict(parent.attributes or {}) == {
        "acdp.input.ctx_ids": (INPUT,),
        "acdp.input.verified": True,
    }

    # No attribute anywhere carries content, arguments or the salt.
    salt = base64.urlsafe_b64decode(link.salt + "=")
    dumped = json.dumps([dict(s.attributes or {}) for s in spans.get_finished_spans()], default=list)
    for secret in ("the secret", link.salt, salt.hex()):
        assert secret not in dumped
    assert set().union(*(s.attributes or {} for s in spans.get_finished_spans())) <= SPEC_NAMES


async def test_publish_error_attribute(
    publisher: AcdpPublisher, registry: FakeRegistry, spans: InMemorySpanExporter
) -> None:
    registry.script = [400]
    with pytest.raises(PublishError):
        await _publish(publisher, RunLedger())
    (pub,) = _by_name(spans, "acdp.publish")
    attrs = dict(pub.attributes or {})
    assert attrs["acdp.publish.error"] == "code_400"
    assert "acdp.ctx_id" not in attrs


async def test_no_input_attributes_without_inputs(
    publisher: AcdpPublisher, spans: InMemorySpanExporter
) -> None:
    with trace.get_tracer("test").start_as_current_span("agent.tool"):
        await _publish(publisher, RunLedger())
    (parent,) = _by_name(spans, "agent.tool")
    assert not parent.attributes


def test_chain_verdict_attributes(spans: InMemorySpanExporter) -> None:
    with trace.get_tracer("test").start_as_current_span("consume"):
        annotate_chain_verdict(
            {
                "chain": {"latest": "ok_with_caveats", "as_of": "2026-10-01T00:00:00.000Z"},
                "verdict_digest": "sha256:ab",
            }
        )
        annotate_chain_verdict({})
    (span,) = _by_name(spans, "consume")
    assert dict(span.attributes or {}) == {
        "acdp.input.chain_verdict": "ok_with_caveats",
        "acdp.input.chain_verdict_as_of": "2026-10-01T00:00:00.000Z",
        "acdp.input.verdict_digest": "sha256:ab",
    }


def test_unregistered_name_is_refused() -> None:
    with (
        trace.get_tracer("test").start_as_current_span("x") as s,
        pytest.raises(ValueError, match="not in the ACDP OTel attribute registry"),
    ):
        otel._Span(s).set({"acdp.body": "x"})


def test_noop_without_opentelemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel, "_trace", None)
    assert not otel.available()
    otel.current_span().set({"acdp.ctx_id": INPUT})
    with otel.publish_span() as span:
        span.set({"acdp.ctx_id": INPUT})
    assert otel.input_attributes([], None) == {}
