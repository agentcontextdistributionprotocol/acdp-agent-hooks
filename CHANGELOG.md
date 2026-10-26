# Changelog

## 0.1.0 — 2026-10-26

First release.

- `AcdpPublisher`: signs one body per tool result / final output with a
  did:key or did:web Ed25519 producer key and publishes it, with retries under
  a content-hash idempotency key.
- Claude Agent SDK: `AcdpClaudeHooks` (`PostToolUse`, `PostToolUseFailure`,
  `Stop`), appended to existing hooks via `attach` / `install` / `merge_hooks`.
- OpenAI Agents SDK: `AcdpRunHooks` and `compose_run_hooks`.
- `derived_from` from declared inputs, cited and observed ctx_ids, and the
  run's own tool-result bodies.
- OpenTelemetry `acdp.*` attributes from the spec registry (optional extra).
- Fail-open by default; fail-closed opt-in.
- `acdp-hooks keygen`.
