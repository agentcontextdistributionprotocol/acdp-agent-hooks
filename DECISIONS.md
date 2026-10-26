# Decisions

Design approved by the owner on 2026-10-26 (via the spec session). Each entry
below is a choice the brief left open, with the reason.

## D1. Publish failure policy: fail-open by default, fail-closed opt-in
A failed publish lets the agent continue; the failure is recorded in the run
ledger, the `acdp.publish.error` span attribute and a warning log.
`fail_policy="closed"` withholds the result instead (Claude: replaces the
tool output; OpenAI: raises, which aborts the run). Provenance observes the
agent and must not take it down when a registry is unreachable; this matches
the RFC-ACDP-0019 §7 default. Per-framework effects: README "Publish failure
policy".

## D2. Salted hashes, never content
Bodies carry `H(salt ‖ JCS(value))` of arguments/result/output with a fresh
32-byte salt per body (RFC-ACDP-0019 §4.3 model). The salt is returned only in
the `Link`. Results can hold secrets and the default registry is public.
Embedding results (RFC-ACDP-0019 §5) is not implemented.

## D3. Custom types, not `acdp:mcp-tool-result`
`agent_hooks:tool-result` / `agent_hooks:agent-output` with records
`acdp-agent-hooks-tool-result/1` / `acdp-agent-hooks-output/1`. The
RFC-ACDP-0019 record is closed and requires MCP server identity
(`serverInfo`, transport, endpoint hash) that an in-process hook does not
have, least of all for built-in tools. The `acdp:` namespace is reserved for
RFC-defined types.

## D4. derived_from sources
Declared inputs (`ledger.consume`), ctx_ids in the tool's arguments
(RFC-ACDP-0019 §4.4 walk), ctx_ids seen in earlier tool results, and for the
final output every tool-result body of the run. A result's ctx_ids are inputs
to later steps, never to their own tool's body.

## D5. OTel placement
Producer attributes on a child `acdp.publish` span per body (the span that did
the work; avoids clobbering when one agent span covers many tool calls).
`acdp.input.*` on the active span. Unregistered names raise. CI compares the
name set with the pinned spec's `registries/otel-attributes.md`.

## D6. Default visibility `public`
The link is meant to be resolvable by others, and salted records disclose only
that a named tool was called and when. `visibility="private"` is one setting
away; RFC-ACDP-0019 §8.3 recommends private for proxies, which is noted in the
README.

## D7. Producer key string
`ed25519:<seed>` (did:key) or `ed25519:<seed>;did=<did:web key id>` — one
environment variable next to `ACDP_REGISTRY_URL`. No keyless (RFC-ACDP-0021,
draft).

## D8. No `bump-acdp` consumer lane
The package is a library with an `acdp>=0.17.3` floor, not a pinned
application, so an `acdp-released` dispatch has nothing to bump. Dependabot
covers dev tooling and actions. `bump-spec.yml` exists for the OTel registry
pin.

## D9. Integration test target
CI runs the integration suite against a local acdp-registry-rs container
(GHCR image, Postgres, auth off, did:key allowed) so CI never writes public
bodies to production. The same suite runs against production by setting
`ACDP_IT_REGISTRY`; it passed there on 2026-10-26.
