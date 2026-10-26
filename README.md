# acdp-agent-hooks

Hooks for agent frameworks that sign every tool result and every final output,
publish each one to an [ACDP](https://agentcontextdistributionprotocol.io)
registry, and link them together with `derived_from`. Each publish returns an
`acdp://` link, and OpenTelemetry spans get the spec's `acdp.*` attributes.

- **Claude Agent SDK**: `PostToolUse`, `PostToolUseFailure` and `Stop` hooks,
  appended to whatever hooks you already have (seam's `SeamHooks`, for example).
- **OpenAI Agents SDK**: a `RunHooks` (`on_tool_end`, `on_agent_end`) plus a
  composer, so it runs alongside your own hooks.
- **OpenTelemetry**: optional. Only the names in the spec's
  [`registries/otel-attributes.md`](https://github.com/agentcontextdistributionprotocol/agentcontextdistributionprotocol/blob/main/registries/otel-attributes.md)
  are emitted.

```bash
pip install "acdp-agent-hooks[claude]"   # or [openai]; add ,otel for spans
```

Python 3.10+. Depends on the [`acdp`](https://pypi.org/project/acdp/) SDK (≥ 0.17.3)
for canonicalisation and signing, and on `httpx` for transport.

## Configuration: two lines

```bash
ACDP_REGISTRY_URL=https://registry.agentcontextdistributionprotocol.io
ACDP_PRODUCER_KEY=ed25519:<base64url seed>
```

`acdp-hooks keygen` prints a fresh `ACDP_PRODUCER_KEY` line. The key decides
the producer DID:

| `ACDP_PRODUCER_KEY` | Producer |
|---|---|
| `ed25519:<seed>` | `did:key`, derived from the key. Nothing to host; verifiers check it offline. A new key is a new identity. |
| `ed25519:<seed>;did=did:web:agents.example.com:my-agent#key-1` | `did:web`. Publish the key in that DID document (`acdp-hooks keygen --did-web <key-id>` prints the public key). Use it for a long-lived identity that can rotate keys. |

`<seed>` is the 32-byte Ed25519 seed as base64url or 64 hex characters. It is
the private key. Keep it in your secret store.

Keyless signing (RFC-ACDP-0021) is still a draft, so it is not supported yet.

In code, `HooksConfig(registry_url, producer_key)` does the same job, and
`HooksConfig.from_env()` reads the two variables. The other settings have
defaults: `fail_policy` (`open`), `visibility` (`public`), `timeout_s` (10)
and `retries` (2).

## Claude Agent SDK

```python
from claude_agent_sdk import ClaudeAgentOptions, query
from acdp_hooks import AcdpPublisher
from acdp_hooks.claude_agent import AcdpClaudeHooks

provenance = AcdpClaudeHooks(AcdpPublisher.from_env())
options = ClaudeAgentOptions(hooks=provenance.attach())  # or provenance.attach(existing_hooks)

async for message in query(prompt="...", options=options):
    ...

ledger = provenance.ledger(session_id)
ledger.tool_links  # one Link per tool result
ledger.output_link  # the final output's Link
```

- **Tool results** (`PostToolUse`, and `PostToolUseFailure` with
  `is_error: true`). One body per call. Its `ctx_id` goes back to the model as
  `additionalContext` (`ACDP provenance for this Read result: acdp://…`). Turn
  that off with `inject_link=False`.
- **Final output** (`Stop`). One body over `last_assistant_message`. To publish
  `ResultMessage.result` yourself, use
  `await provenance.publish_final_output(result, session_id)`, and pass
  `publish_output_on_stop=False` so the output isn't published twice.
- `attach(existing)` returns a new hooks map: `existing` with this package's
  matchers **appended** to each event's list. It never removes, replaces or
  reorders a matcher, and it does not mutate `existing`. `install(options)`
  does the same in place on a `ClaudeAgentOptions`. `merge_hooks(a, b, …)`
  concatenates any number of maps.
- Each matcher's timeout (`hook_timeout_s`) is set longer than one publish
  including all its retries. That way the fail policy settles a slow registry,
  not the SDK's hook timeout.
- Every hook body is sentineled. A malformed hook input or an unexpected error
  is handled by the fail policy and never raised into the SDK.

### With seam's `SeamHooks`

seam's `SeamHooks` (package `seam-claude-agent`, from seam-adapters) gates
tool calls with `PreToolUse` and audits them with
`PostToolUse`/`PostToolUseFailure`. The two packages coexist on the same
events:

```python
from seam_claude_agent import SeamHooks
from acdp_hooks.claude_agent import AcdpClaudeHooks

seam = SeamHooks(gate)
provenance = AcdpClaudeHooks(AcdpPublisher.from_env())

options = ClaudeAgentOptions(hooks=provenance.attach(seam.matchers()))
```

- **Order.** Seam's matchers come first in each list and ACDP's are appended
  after them. Neither hook reads the other's output.
- **Seam decides, ACDP records.** `PreToolUse` belongs to seam alone. A call
  seam denies never runs, so it gets no `PostToolUse` and no ACDP body. A call
  seam transforms is recorded with the `tool_input` the SDK reports to
  `PostToolUse`.
- **Both audit, independently.** Seam's `ToolUseAudit` and the ACDP ledger
  both see the same `tool_response`. Seam's post hooks return `{}`. Under
  fail-open, ACDP's return only `additionalContext`, so nothing collides.
- **Fail-closed.** If you set ACDP to fail-closed, a failed publish replaces
  the tool output that reaches the model (`updatedToolOutput` /
  `updatedMCPToolOutput`). Seam's audit row still records the real result,
  because the tool did run.
- **Timeouts are per matcher.** Seam derives its timeout from its gate
  deadline; ACDP derives its own from the publish deadline. Neither one
  stretches the other.
- **`can_use_tool`.** ACDP never sets it, so seam's escalation adjudicator
  keeps working as before.

## OpenAI Agents SDK

```python
from agents import Runner
from acdp_hooks import AcdpPublisher
from acdp_hooks.openai_agents import AcdpRunHooks, compose_run_hooks

publisher = AcdpPublisher.from_env()  # reuse across runs
provenance = AcdpRunHooks(publisher)  # one per run

result = await Runner.run(agent, prompt, hooks=compose_run_hooks(my_hooks, provenance))
provenance.ledger.output_link  # the final output's acdp:// link
```

- `on_tool_end` publishes one body per local tool call. For function tools the
  arguments come from `ToolContext.tool_arguments`, and `on_tool_start` supplies
  `started_at`. `on_agent_end` publishes the final output, with the agent's
  `name` included.
- `Runner.run` accepts a single `hooks` object, so `compose_run_hooks(a, b, …)`
  forwards every `RunHooks` callback to each member in order.
- The SDK gives a hook no way to change a tool result, so **fail-closed raises**
  `PublishError` out of the hook. The SDK wraps it in `UserError` and the run
  stops before the model sees the result.

## What gets published

Bodies never contain the result. Each one is a record of **salted hashes**,
following the model of RFC-ACDP-0019 §4.3: `H(salt ‖ JCS(value))`, with a fresh
32-byte salt for every body.

| | Tool result | Final output |
|---|---|---|
| `type` | `agent_hooks:tool-result` | `agent_hooks:agent-output` |
| `title` | `Tool result: <tool>` | `Agent output[: <agent>]` |
| `metadata.record_version` | `acdp-agent-hooks-tool-result/1` | `acdp-agent-hooks-output/1` |
| `metadata` | `framework`, `tool`, `arguments_hash`, `result_hash`, `is_error`, `salted`, `embedded`, `started_at`?, `completed_at` | `framework`, `agent`?, `output_hash`, `salted`, `embedded`, `completed_at` |
| `tags` | `["acdp-agent-hooks"]` | same |

The `Link` returned for each body (`ctx_id`, `lineage_id`, `version`,
`content_hash`, `registry`, `producer`, `type`, `derived_from`, `salt`) is the
only place the salt exists. Holding the result plus the link lets you prove the
binding: fetch the body, verify it, then recompute the hash with
`acdp_hooks.salted_hash(salt, result)`. Without the salt, the public body
reveals nothing about the content. Treat the salt as carefully as the result.
It is not in `repr(link)` and never goes into a span.

These types are custom (`<namespace>:<type>`), not the RFC-ACDP-0019
`acdp:mcp-tool-result`. That record describes an MCP proxy's view of an MCP
server (`serverInfo`, transport, endpoint hash), and an in-process hook can't
see any of it, especially for built-in tools.

### `derived_from`

Every body links to the `ctx_id`s the run consumed:

- **Declared inputs**: `ledger.consume(ctx_id, verified=…)`, for example a
  context your app placed in the prompt.
- **Cited in a tool's arguments**: picked up by the RFC-ACDP-0019 §4.4 walk
  (JCS order, whole-string matches only).
- **Seen in a tool's result**: any `acdp://` id embedded in what a tool
  returned. It counts as an input to everything the run does *afterwards*, not
  to that tool's own body.
- **The run's own tool results**: the final-output body also derives from every
  tool-result body published during the run.

The list keeps first occurrences in that order and is capped at 1000, the
RFC-ACDP-0002 maximum.

### Visibility

The default is `public`. With salted hashes, a public body shows only *that*
the producer called a named tool at a given time; it never shows what went in
or came out. If even that is sensitive, set `visibility="private"`. In that
case only callers the registry authorises can resolve the link.
RFC-ACDP-0019 §8.3 reasons the same way. The configured value is used as is; a
rejected publish is never retried with a wider visibility.

## Publish failure policy

**The default is fail-open.** A hook that can't publish lets the agent carry
on, and the failure is recorded in three places: `ledger.failures`, the
`acdp.publish.error` span attribute and a `WARNING` on the `acdp_hooks` logger.
Provenance is an observer of the agent and shouldn't take production agents
down when a registry is unreachable. RFC-ACDP-0019 §7 uses the same default.

Set `fail_policy="closed"` when nothing should leave the run without
provenance:

| | Fail-open (default) | Fail-closed |
|---|---|---|
| Claude, tool result | result passes through unchanged | result replaced by a "withheld" message (`updatedToolOutput` / `updatedMCPToolOutput`, `decision: block`) |
| Claude, `Stop` | nothing changes | `continue_: false` with a `stopReason` |
| Claude, `publish_final_output` | returns `None` | raises `PublishError` |
| OpenAI, any hook | the run continues | raises `PublishError`, which stops the run |

A publish counts as failed when the registry returns an error, when the body
can't be built (for example, metadata over 64 KB), or when retries run out:
`retries` extra attempts with exponential backoff, each with a `timeout_s`
limit. Retries resend the same bytes under
`Idempotency-Key: acdp-hooks-<content hash>`, so a retry never publishes twice.
`PublishError.code` holds the registry's RFC-ACDP-0007 error code, or
`transport` when no ACDP error was returned.

## OpenTelemetry

Install the `otel` extra (`opentelemetry-api`) and configure a tracer provider
as usual. Without the extra, every OTel call is a no-op.

- **`acdp.publish` span.** Each publish opens a child span under the active
  span. It gets `acdp.ctx_id`, `acdp.lineage_id`, `acdp.version`,
  `acdp.content_hash`, `acdp.producer`, `acdp.registry`, `acdp.type` and
  `acdp.derived_from`; on failure it gets `acdp.publish.error` instead of the
  identifiers. Giving each body its own span keeps several tool results under
  one agent span from overwriting each other's `acdp.ctx_id`.
- **The active span** (the work that used the inputs) gets
  `acdp.input.ctx_ids`. When you reported verification through
  `consume(..., verified=…)`, it also gets `acdp.input.verified`, which is
  `true` only if every input was reported verified.
- `acdp_hooks.annotate_chain_verdict(verdict)` copies an RFC-ACDP-0017 chain
  verdict's `chain.latest`, `chain.as_of` and `verdict_digest` onto the active
  span (`acdp.input.chain_verdict*`, `acdp.input.verdict_digest`).

Values are copied from the wire without changes. A name outside the registry
raises an error instead of being emitted. Body content, arguments, results and
salts never go into an attribute. CI checks the name list against the pinned
spec checkout.

## Development

```bash
uv venv && uv pip install -e ".[claude,openai,otel,dev]"
ruff format --check . && ruff check . && mypy
coverage run -m pytest && coverage report        # unit tests, coverage-gated

# integration: a local acdp-registry-rs (docker) …
docker compose -f tests/integration/compose.yml up -d --wait
ACDP_IT_REGISTRY=http://localhost:8443 pytest -m integration
# … or production (publishes public bodies under a throwaway did:key)
ACDP_IT_REGISTRY=https://registry.agentcontextdistributionprotocol.io pytest -m integration
```

CI follows the [ACDP Delivery Standard](https://github.com/agentcontextdistributionprotocol/acdp-ci/blob/main/DELIVERY-STANDARD.md):
format, lint, `mypy --strict`, a coverage gate, a no-extras install, a build
with `twine check`, and the integration suite against a local registry
container. `main` is protected by `acdp-ci/scripts/standardize.sh`.

## Releasing

Bump `version` in `pyproject.toml` and `__version__` in
`src/acdp_hooks/__init__.py`, update `CHANGELOG.md`, merge, then push a
`v<version>` tag. `release.yml` builds the package, attests provenance and
publishes to PyPI through **Trusted Publishing**, so no token is stored. The
PyPI trusted publisher names this repo, `release.yml` and the `pypi`
environment.

TypeScript packages will follow.

## License

Apache-2.0
