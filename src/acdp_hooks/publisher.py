"""Signing and publishing one body per tool result or final output.

A body never carries the result itself. Its ``metadata`` is a record of
salted hashes, after RFC-ACDP-0019 §4.3: H(salt ‖ JCS(value)) with a fresh
32-byte salt per body. The salt is returned to the caller in the
:class:`Link` and nowhere else, so whoever holds the result and the link can
prove the binding, and anyone holding the public body learns nothing about
the content.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import acdp
import httpx

from . import otel
from .config import FailPolicy, HooksConfig, load_producer
from .ctxids import ctx_ids_in_arguments, ctx_ids_in_text, dedupe, is_ctx_id

log = logging.getLogger("acdp_hooks")

TOOL_RESULT_TYPE = "agent_hooks:tool-result"
OUTPUT_TYPE = "agent_hooks:agent-output"
TOOL_RESULT_RECORD = "acdp-agent-hooks-tool-result/1"
OUTPUT_RECORD = "acdp-agent-hooks-output/1"
TITLE_MAX = 500
TAG = "acdp-agent-hooks"


@dataclass(frozen=True)
class Link:
    """A published body. ``str(link)`` is its ``acdp://`` ctx_id.

    ``salt`` is base64url without padding; it is the capability that proves
    the link against the result. Treat it like the result itself.
    """

    ctx_id: str
    lineage_id: str
    version: int
    content_hash: str
    registry: str
    producer: str
    type: str
    derived_from: tuple[str, ...]
    salt: str = field(repr=False)

    def __str__(self) -> str:
        return self.ctx_id


class PublishError(Exception):
    """A body could not be published. ``code`` is the registry's RFC-ACDP-0007
    error code, or ``transport`` when no ACDP error came back."""

    def __init__(self, code: str, message: str, *, type: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.type = type


def now_ms() -> datetime:
    t = datetime.now(timezone.utc)
    return t.replace(microsecond=t.microsecond - t.microsecond % 1000)


def rfc3339_ms(t: datetime) -> str:
    """Canonical millisecond RFC 3339 UTC (``.000Z`` form)."""
    t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _jsonable(value: Any) -> Any:
    """A JSON value for ``value``: pydantic models dumped, NaN/inf and other
    non-JSON leaves rendered with ``str``, so hashing never fails on shape."""

    def fallback(o: Any) -> Any:
        dump = getattr(o, "model_dump", None)
        if callable(dump):
            return dump(mode="json")
        return str(o)

    def scrub(o: Any) -> Any:
        if isinstance(o, float) and not math.isfinite(o):
            return str(o)
        if isinstance(o, dict):
            return {str(k): scrub(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [scrub(v) for v in o]
        return o

    return json.loads(json.dumps(scrub(value), default=fallback, allow_nan=False))


def salted_hash(salt: bytes, value: Any) -> str:
    canonical = acdp.AcdpCanonicalizer.canonicalize(json.dumps(_jsonable(value)))
    return "sha256:" + hashlib.sha256(salt + canonical.encode()).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class RunLedger:
    """What one agent run consumed and published.

    ``consume()`` records an input the run used (for example a context the
    app put in the prompt); the hooks also record every ``ctx_id`` that shows
    up in a tool's result. A tool-result body derives from everything consumed
    so far plus the ``ctx_id``s in its own arguments; the final-output body
    derives from everything consumed plus every tool-result body the run
    published.
    """

    def __init__(self) -> None:
        self._consumed: dict[str, bool | None] = {}
        self.tool_links: list[Link] = []
        self.output_link: Link | None = None
        self.failures: list[PublishError] = []

    def consume(self, ctx_id: str, *, verified: bool | None = None) -> None:
        """Record that the run used ``ctx_id``. ``verified`` is whether the
        caller verified the body (RFC-ACDP-0001 §5.8); leave it ``None`` when
        it was not checked."""
        if not is_ctx_id(ctx_id):
            raise ValueError(f"not a ctx_id: {ctx_id!r}")
        if ctx_id not in self._consumed or verified is not None:
            self._consumed[ctx_id] = verified

    def consume_from(self, value: Any) -> None:
        for ctx_id in ctx_ids_in_text(value):
            self._consumed.setdefault(ctx_id, None)

    @property
    def consumed(self) -> list[str]:
        return list(self._consumed)

    @property
    def links(self) -> list[Link]:
        """Every body this run published, final output last."""
        return self.tool_links + ([self.output_link] if self.output_link else [])

    def inputs_verified(self) -> bool | None:
        reported = [v for v in self._consumed.values() if v is not None]
        if not reported:
            return None
        return len(reported) == len(self._consumed) and all(reported)

    def derived_for_tool(self, arguments: Any) -> list[str]:
        return dedupe([*self._consumed, *ctx_ids_in_arguments(arguments)])

    def derived_for_output(self) -> list[str]:
        return dedupe([*self._consumed, *(link.ctx_id for link in self.tool_links)])


OnPublish = Callable[[Link], None]


class AcdpPublisher:
    """Signs bodies with the configured producer key and POSTs them to the
    registry. Shared by every framework adapter; safe to reuse across runs."""

    def __init__(
        self,
        config: HooksConfig,
        *,
        client: httpx.AsyncClient | None = None,
        on_publish: OnPublish | None = None,
    ) -> None:
        self.config = config
        self._producer = load_producer(config.producer_key)
        self._client = client
        self._owns_client = client is None
        self._on_publish = on_publish

    @classmethod
    def from_env(cls, **overrides: Any) -> AcdpPublisher:
        return cls(HooksConfig.from_env(**overrides))

    @property
    def producer_did(self) -> str:
        return str(self._producer.agent_did)

    @property
    def fail_policy(self) -> FailPolicy:
        return self.config.fail_policy

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_s)
        return self._client

    async def publish_tool_result(
        self,
        ledger: RunLedger,
        *,
        framework: str,
        tool: str,
        arguments: Any,
        result: Any,
        is_error: bool,
        completed_at: datetime,
        started_at: datetime | None = None,
    ) -> Link:
        """Publish one tool call's record. Raises :class:`PublishError`; the
        ``ctx_id``s in ``result`` are recorded as consumed either way."""
        salt = os.urandom(32)
        record: dict[str, Any] = {
            "record_version": TOOL_RESULT_RECORD,
            "framework": framework,
            "tool": tool,
            "arguments_hash": salted_hash(salt, arguments if arguments is not None else {}),
            "result_hash": salted_hash(salt, result),
            "is_error": bool(is_error),
            "salted": True,
            "embedded": False,
            "completed_at": rfc3339_ms(completed_at),
        }
        if started_at is not None:
            record["started_at"] = rfc3339_ms(started_at)
        derived = ledger.derived_for_tool(arguments)
        try:
            link = await self._publish(
                ledger,
                type=TOOL_RESULT_TYPE,
                title=f"Tool result: {tool}",
                record=record,
                derived_from=derived,
                salt=salt,
            )
            ledger.tool_links.append(link)
            return link
        finally:
            ledger.consume_from(result)

    async def publish_output(
        self,
        ledger: RunLedger,
        *,
        framework: str,
        output: Any,
        agent: str | None = None,
        completed_at: datetime | None = None,
    ) -> Link:
        """Publish the run's final output. Raises :class:`PublishError`."""
        salt = os.urandom(32)
        record: dict[str, Any] = {
            "record_version": OUTPUT_RECORD,
            "framework": framework,
            "output_hash": salted_hash(salt, output),
            "salted": True,
            "embedded": False,
            "completed_at": rfc3339_ms(completed_at or now_ms()),
        }
        if agent:
            record["agent"] = agent[:200]
        link = await self._publish(
            ledger,
            type=OUTPUT_TYPE,
            title=f"Agent output: {agent}" if agent else "Agent output",
            record=record,
            derived_from=ledger.derived_for_output(),
            salt=salt,
        )
        ledger.output_link = link
        return link

    async def _publish(
        self,
        ledger: RunLedger,
        *,
        type: str,
        title: str,
        record: dict[str, Any],
        derived_from: Sequence[str],
        salt: bytes,
    ) -> Link:
        otel.current_span().set(otel.input_attributes(ledger.consumed, ledger.inputs_verified()))
        with otel.publish_span() as span:
            span.set(
                {
                    otel.PRODUCER: self.producer_did,
                    otel.TYPE: type,
                    otel.DERIVED_FROM: list(derived_from),
                }
            )
            try:
                raw = self._producer.build_publish_request(
                    title=title[:TITLE_MAX],
                    context_type=type,
                    visibility=self.config.visibility,
                    tags=[TAG],
                    metadata=json.dumps(record),
                    derived_from=list(derived_from) or None,
                )
                body = json.loads(raw)
                response = await self._post(raw, body["content_hash"], body_type=type)
            except PublishError as e:
                span.set({otel.PUBLISH_ERROR: e.code})
                ledger.failures.append(e)
                raise
            except (ValueError, RuntimeError) as e:
                # The SDK refused to build the body; nothing reached the wire.
                err = PublishError("transport", f"could not build body: {e}", type=type)
                span.set({otel.PUBLISH_ERROR: err.code})
                ledger.failures.append(err)
                raise err from e
            ctx_id = response["ctx_id"]
            link = Link(
                ctx_id=ctx_id,
                lineage_id=response["lineage_id"],
                version=int(response.get("version", body["version"])),
                content_hash=body["content_hash"],
                registry=ctx_id.split("/")[2],
                producer=body["agent_id"],
                type=type,
                derived_from=tuple(derived_from),
                salt=_b64url(salt),
            )
            span.set(
                {
                    otel.CTX_ID: link.ctx_id,
                    otel.LINEAGE_ID: link.lineage_id,
                    otel.VERSION: link.version,
                    otel.CONTENT_HASH: link.content_hash,
                    otel.REGISTRY: link.registry,
                }
            )
        if self._on_publish is not None:
            self._on_publish(link)
        return link

    async def _post(self, raw: str, content_hash: str, body_type: str) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            # Retries resend the byte-identical body under the same key, so a
            # retry never creates a second publication (RFC-ACDP-0003 §6).
            "Idempotency-Key": "acdp-hooks-" + content_hash.removeprefix("sha256:"),
        }
        url = f"{self.config.registry_url}/contexts"
        last = PublishError("transport", "no attempt made", type=body_type)
        for attempt in range(self.config.retries + 1):
            if attempt:
                await asyncio.sleep(0.25 * 2**attempt)
            try:
                r = await self._http().post(url, content=raw, headers=headers)
            except httpx.HTTPError as e:
                last = PublishError("transport", f"{type(e).__name__}: {e}", type=body_type)
                continue
            if r.status_code in (200, 201):
                data: dict[str, Any] = r.json()
                return data
            code = _error_code(r)
            last = PublishError(code or "transport", f"HTTP {r.status_code}", type=body_type)
            if r.status_code < 500 and r.status_code != 429:
                break
        raise last


def _error_code(r: httpx.Response) -> str | None:
    try:
        payload = r.json()
    except ValueError:
        return None
    err = payload.get("error") if isinstance(payload, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, str) and code else None
