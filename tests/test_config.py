from __future__ import annotations

import base64
import os

import pytest

from acdp_hooks import ConfigError, FailPolicy, HooksConfig, format_producer_key, load_producer
from acdp_hooks.cli import main

SEED = bytes(range(32))


def test_did_key_from_base64url_and_hex_agree() -> None:
    a = load_producer("ed25519:" + base64.urlsafe_b64encode(SEED).decode())
    b = load_producer("ed25519:" + SEED.hex())
    c = load_producer(format_producer_key(SEED))
    assert a.agent_did == b.agent_did == c.agent_did
    assert a.agent_did.startswith("did:key:z6Mk")


def test_did_web_key() -> None:
    key_id = "did:web:agents.example.com:my-agent#key-1"
    p = load_producer(format_producer_key(SEED, key_id))
    assert p.agent_did == "did:web:agents.example.com:my-agent"
    assert p.key_id == key_id


@pytest.mark.parametrize(
    "bad",
    [
        "p256:" + SEED.hex(),
        "ed25519",
        "ed25519:" + "zz" * 32,
        "ed25519:" + base64.urlsafe_b64encode(b"short").decode(),
        format_producer_key(SEED) + ";did=did:key:abc#x",
        format_producer_key(SEED) + ";did=did:web:example.com",
        format_producer_key(SEED) + ";kid=did:web:example.com#k",
    ],
)
def test_bad_keys(bad: str) -> None:
    with pytest.raises(ConfigError):
        load_producer(bad)


def test_config_normalises_and_validates() -> None:
    c = HooksConfig("https://r.example/", format_producer_key(SEED), fail_policy="closed")  # type: ignore[arg-type]
    assert c.registry_url == "https://r.example"
    assert c.fail_policy is FailPolicy.CLOSED
    assert c.deadline_s > c.timeout_s
    for kwargs in (
        {"registry_url": "ftp://x"},
        {"visibility": "restricted"},
        {"timeout_s": 0},
        {"retries": -1},
    ):
        base = {"registry_url": "https://r.example", "producer_key": "k"} | kwargs
        with pytest.raises(ConfigError):
            HooksConfig(**base)  # type: ignore[arg-type]


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACDP_REGISTRY_URL", raising=False)
    with pytest.raises(ConfigError, match="ACDP_REGISTRY_URL"):
        HooksConfig.from_env()
    monkeypatch.setenv("ACDP_REGISTRY_URL", "https://r.example")
    monkeypatch.setenv("ACDP_PRODUCER_KEY", format_producer_key(SEED))
    c = HooksConfig.from_env(visibility="private")
    assert c.visibility == "private"
    assert c.fail_policy is FailPolicy.OPEN


def test_keygen(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keygen"]) == 0
    out, err = capsys.readouterr()
    name, _, key = out.strip().partition("=")
    assert name == "ACDP_PRODUCER_KEY"
    assert load_producer(key).agent_did in err

    assert main(["keygen", "--did-web", "did:web:a.example#key-1"]) == 0
    out, err = capsys.readouterr()
    assert out.strip().endswith(";did=did:web:a.example#key-1")
    assert "public key" in err


def test_keygen_is_random() -> None:
    assert format_producer_key(os.urandom(32)) != format_producer_key(os.urandom(32))
