from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import acdp
import httpx
import pytest

from acdp_hooks import AcdpPublisher, FailPolicy, HooksConfig, format_producer_key

SEED = bytes(range(32))
PRODUCER_KEY = format_producer_key(SEED)
REGISTRY = "https://registry.test"


@dataclass
class FakeRegistry:
    """A registry that verifies every publish offline and records it.

    ``script`` holds responses to hand out before behaving normally: an int
    status (with an ACDP error body), or an exception to raise.
    """

    bodies: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    script: list[int | Exception] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return httpx.Response(step, json={"error": {"code": f"code_{step}", "message": "scripted"}})
        raw = request.content.decode()
        acdp.AcdpVerifier.verify_publish_request_offline(raw)
        body = json.loads(raw)
        self.bodies.append(body)
        return httpx.Response(
            201,
            json={
                "ctx_id": f"acdp://registry.test/{uuid.uuid4()}",
                "lineage_id": "lin:sha256:" + "0" * 64,
                "version": body["version"],
                "created_at": "2026-10-26T00:00:00.000Z",
                "status": "active",
            },
        )


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def make_publisher(registry: FakeRegistry) -> Callable[..., AcdpPublisher]:
    def make(**overrides: Any) -> AcdpPublisher:
        on_publish = overrides.pop("on_publish", None)
        config = HooksConfig(registry_url=REGISTRY, producer_key=PRODUCER_KEY, **overrides)
        client = httpx.AsyncClient(transport=httpx.MockTransport(registry.handler))
        return AcdpPublisher(config, client=client, on_publish=on_publish)

    return make


@pytest.fixture
def publisher(make_publisher: Callable[..., AcdpPublisher]) -> AcdpPublisher:
    return make_publisher(retries=1, timeout_s=1.0)


@pytest.fixture
def closed_publisher(make_publisher: Callable[..., AcdpPublisher]) -> AcdpPublisher:
    return make_publisher(retries=0, timeout_s=1.0, fail_policy=FailPolicy.CLOSED)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("acdp_hooks.publisher.asyncio.sleep", no_sleep)
