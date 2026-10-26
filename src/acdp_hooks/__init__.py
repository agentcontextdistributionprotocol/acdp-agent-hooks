"""Sign agent tool results and final outputs, publish them to an ACDP registry,
and link them with ``derived_from``.

Framework adapters live in :mod:`acdp_hooks.claude_agent` (Claude Agent SDK)
and :mod:`acdp_hooks.openai_agents` (OpenAI Agents SDK); each needs its extra.
"""

from .config import ConfigError, FailPolicy, HooksConfig, format_producer_key, load_producer
from .otel import annotate_chain_verdict
from .publisher import (
    OUTPUT_TYPE,
    TOOL_RESULT_TYPE,
    AcdpPublisher,
    Link,
    PublishError,
    RunLedger,
    salted_hash,
)

__version__ = "0.1.0"

__all__ = [
    "OUTPUT_TYPE",
    "TOOL_RESULT_TYPE",
    "AcdpPublisher",
    "ConfigError",
    "FailPolicy",
    "HooksConfig",
    "Link",
    "PublishError",
    "RunLedger",
    "annotate_chain_verdict",
    "format_producer_key",
    "load_producer",
    "salted_hash",
]
