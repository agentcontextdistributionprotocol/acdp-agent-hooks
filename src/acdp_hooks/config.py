"""Producer identity and registry configuration.

Configuration is two values: a registry URL and a producer key.

The producer key is one string:

- ``ed25519:<seed>`` — a ``did:key`` producer. The DID is derived from the
  key, so nothing else is needed and nothing has to be hosted.
- ``ed25519:<seed>;did=<key-id>`` — a ``did:web`` producer. ``<key-id>`` is
  the DID URL of the verification method in your hosted DID document, e.g.
  ``did:web:agents.example.com:my-agent#key-1``.

``<seed>`` is the 32-byte Ed25519 seed, base64url (padding optional) or
64 hex characters. ``acdp-hooks keygen`` prints a fresh one.

Keyless signing (RFC-ACDP-0021) is still a draft and is deliberately not
supported yet.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from enum import Enum
from typing import Literal

import acdp

ENV_REGISTRY = "ACDP_REGISTRY_URL"
ENV_PRODUCER_KEY = "ACDP_PRODUCER_KEY"

Visibility = Literal["public", "private"]


class FailPolicy(str, Enum):
    """What a hook does when publishing a body fails.

    ``OPEN`` (the default) lets the agent carry on with the unpublished
    result and records the failure (span attribute ``acdp.publish.error``,
    the run ledger, a log warning). ``CLOSED`` withholds the result: the
    adapter replaces it with an error the model sees, or raises, so nothing
    unprovenanced leaves the run. The choice and its per-framework effect are
    documented in the README.
    """

    OPEN = "open"
    CLOSED = "closed"


class ConfigError(ValueError):
    """The registry URL or producer key could not be used."""


def _decode_seed(text: str) -> bytes:
    text = text.strip()
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    try:
        seed = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as e:
        raise ConfigError("producer key seed is neither base64url nor 64 hex characters") from e
    if len(seed) != 32:
        raise ConfigError(f"producer key seed must be 32 bytes, got {len(seed)}")
    return seed


def load_producer(producer_key: str) -> acdp.AcdpProducer:
    """Build an ``acdp.AcdpProducer`` from a producer-key string (see module doc)."""
    key, _, rest = producer_key.strip().partition(";")
    algorithm, sep, seed_text = key.partition(":")
    if not sep or algorithm != "ed25519":
        raise ConfigError("producer key must start with 'ed25519:'")
    seed = _decode_seed(seed_text)
    if not rest:
        return acdp.AcdpProducer.from_seed_did_key(seed)
    name, _, key_id = rest.partition("=")
    if name.strip() != "did" or not key_id.startswith("did:web:") or "#" not in key_id:
        raise ConfigError("expected ';did=did:web:<host>[:path]#<fragment>' after the seed")
    return acdp.AcdpProducer.from_seed(seed, key_id.split("#", 1)[0], key_id)


def format_producer_key(seed: bytes, key_id: str | None = None) -> str:
    """The producer-key string for ``seed`` (and a did:web ``key_id``, if any)."""
    text = "ed25519:" + base64.urlsafe_b64encode(seed).rstrip(b"=").decode()
    return f"{text};did={key_id}" if key_id else text


@dataclass(frozen=True)
class HooksConfig:
    """Everything a publisher needs. Only ``registry_url`` and ``producer_key``
    are required; the rest are policy knobs with documented defaults."""

    registry_url: str
    producer_key: str
    fail_policy: FailPolicy = FailPolicy.OPEN
    visibility: Visibility = "public"
    timeout_s: float = 10.0
    retries: int = 2

    def __post_init__(self) -> None:
        url = self.registry_url.rstrip("/")
        if not url.startswith(("https://", "http://")):
            raise ConfigError("registry_url must be an http(s) URL")
        object.__setattr__(self, "registry_url", url)
        object.__setattr__(self, "fail_policy", FailPolicy(self.fail_policy))
        if self.visibility not in ("public", "private"):
            raise ConfigError("visibility must be 'public' or 'private'")
        if self.timeout_s <= 0 or self.retries < 0:
            raise ConfigError("timeout_s must be > 0 and retries >= 0")

    @property
    def deadline_s(self) -> float:
        """Upper bound on one publish, retries and backoff included."""
        return float(self.timeout_s * (self.retries + 1) + 0.5 * 2 ** (self.retries + 1))

    @classmethod
    def from_env(cls, **overrides: object) -> HooksConfig:
        """Read ``ACDP_REGISTRY_URL`` and ``ACDP_PRODUCER_KEY``."""
        try:
            registry_url = os.environ[ENV_REGISTRY]
            producer_key = os.environ[ENV_PRODUCER_KEY]
        except KeyError as e:
            raise ConfigError(f"{e.args[0]} is not set") from None
        return cls(registry_url=registry_url, producer_key=producer_key, **overrides)  # type: ignore[arg-type]
