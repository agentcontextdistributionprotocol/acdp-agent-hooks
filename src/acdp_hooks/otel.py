"""OpenTelemetry span attributes, from the spec's ``registries/otel-attributes.md``.

Only names from that registry are emitted, with its types, values copied
verbatim from the wire. Body content, arguments, results and salts never go
into an attribute. Everything here is a no-op when ``opentelemetry-api`` is
not installed (the ``otel`` extra).

Producer-side attributes go on a child span named ``acdp.publish``, opened
under whatever span is active: that span did the publishing, and a separate
span per body keeps several tool results in one agent span from overwriting
each other's ``acdp.ctx_id``. Consumer-side ``acdp.input.*`` attributes go on
the span that was active when the run used its inputs.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace as _trace
except ImportError:  # pragma: no cover - exercised by the no-otel CI job
    _trace = None  # type: ignore[assignment]

# The registry's names, and nothing else.
CTX_ID = "acdp.ctx_id"
LINEAGE_ID = "acdp.lineage_id"
VERSION = "acdp.version"
CONTENT_HASH = "acdp.content_hash"
PRODUCER = "acdp.producer"
REGISTRY = "acdp.registry"
TYPE = "acdp.type"
DERIVED_FROM = "acdp.derived_from"
PUBLISH_ERROR = "acdp.publish.error"
INPUT_CTX_IDS = "acdp.input.ctx_ids"
INPUT_VERIFIED = "acdp.input.verified"
INPUT_CHAIN_VERDICT = "acdp.input.chain_verdict"
INPUT_CHAIN_VERDICT_AS_OF = "acdp.input.chain_verdict_as_of"
INPUT_VERDICT_DIGEST = "acdp.input.verdict_digest"

REGISTERED = frozenset(
    {
        CTX_ID,
        LINEAGE_ID,
        VERSION,
        CONTENT_HASH,
        PRODUCER,
        REGISTRY,
        TYPE,
        DERIVED_FROM,
        PUBLISH_ERROR,
        INPUT_CTX_IDS,
        INPUT_VERIFIED,
        INPUT_CHAIN_VERDICT,
        INPUT_CHAIN_VERDICT_AS_OF,
        INPUT_VERDICT_DIGEST,
    }
)

SPAN_NAME = "acdp.publish"


def available() -> bool:
    return _trace is not None


class _Span:
    """The subset of the OTel span API this package uses."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, attributes: Mapping[str, Any]) -> None:
        if self._span is None:
            return
        for name, value in attributes.items():
            if name not in REGISTERED:
                raise ValueError(f"{name} is not in the ACDP OTel attribute registry")
            if value is None:
                continue
            self._span.set_attribute(name, list(value) if isinstance(value, tuple) else value)


@contextmanager
def publish_span() -> Iterator[_Span]:
    """A child ``acdp.publish`` span under the active span (no-op without OTel)."""
    if _trace is None:
        yield _Span(None)
        return
    tracer = _trace.get_tracer("acdp_hooks")
    with tracer.start_as_current_span(SPAN_NAME, record_exception=False) as span:
        yield _Span(span)


def current_span() -> _Span:
    """The active span (no-op without OTel or when nothing is recording)."""
    if _trace is None:
        return _Span(None)
    span = _trace.get_current_span()
    return _Span(span if span.is_recording() else None)


def input_attributes(ctx_ids: Sequence[str], verified: bool | None) -> dict[str, Any]:
    """``acdp.input.*`` for a span that consumed ``ctx_ids``.

    ``acdp.input.verified`` is emitted only when the caller reported a
    verification outcome for the inputs; it is ``true`` only when every
    consumed body was reported verified.
    """
    if not ctx_ids:
        return {}
    attrs: dict[str, Any] = {INPUT_CTX_IDS: list(ctx_ids)}
    if verified is not None:
        attrs[INPUT_VERIFIED] = verified
    return attrs


def annotate_chain_verdict(verdict: Mapping[str, Any]) -> None:
    """Put an RFC-ACDP-0017 chain verdict's identifiers on the active span.

    ``verdict`` is the verdict object as returned by a chain verifier; only
    ``chain.latest``, ``chain.as_of`` and ``verdict_digest`` are read.
    """
    chain = verdict.get("chain") or {}
    current_span().set(
        {
            INPUT_CHAIN_VERDICT: chain.get("latest"),
            INPUT_CHAIN_VERDICT_AS_OF: chain.get("as_of"),
            INPUT_VERDICT_DIGEST: verdict.get("verdict_digest"),
        }
    )
