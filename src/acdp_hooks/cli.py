"""``acdp-hooks keygen``: print a fresh producer key."""

from __future__ import annotations

import argparse
import os
import sys

from .config import ENV_PRODUCER_KEY, format_producer_key, load_producer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acdp-hooks")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="print a new Ed25519 producer key")
    keygen.add_argument(
        "--did-web",
        metavar="KEY_ID",
        help="did:web key id (e.g. did:web:agents.example.com:my-agent#key-1); default: did:key",
    )
    args = parser.parse_args(argv)

    key = format_producer_key(os.urandom(32), args.did_web)
    producer = load_producer(key)
    print(f"{ENV_PRODUCER_KEY}={key}")
    print(f"# producer DID: {producer.agent_did}", file=sys.stderr)
    if args.did_web:
        print(
            f"# publish this Ed25519 public key (base64) as {args.did_web} "
            f"in your DID document: {producer.public_key_b64}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
