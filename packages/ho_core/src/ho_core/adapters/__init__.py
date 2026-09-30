"""Provider adapters (MASTER_SPEC section 7). Add a provider by implementing AgentAdapter."""

from __future__ import annotations

from .base import (
    AdapterEvent,
    AgentAdapter,
    AgentAssignment,
    ExecutionPlan,
    ExecutionResult,
    FailureClass,
    OutputBundle,
    ProviderHealth,
    UsageRecord,
    image_suffix,
)
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, AgentAdapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}


def adapter_for(provider: str) -> AgentAdapter:
    try:
        return ADAPTERS[provider]
    except KeyError:
        raise ValueError(f"no adapter for provider {provider!r}") from None


__all__ = [
    "ADAPTERS",
    "AdapterEvent",
    "AgentAdapter",
    "AgentAssignment",
    "ClaudeAdapter",
    "CodexAdapter",
    "ExecutionPlan",
    "ExecutionResult",
    "FailureClass",
    "OutputBundle",
    "ProviderHealth",
    "UsageRecord",
    "adapter_for",
    "image_suffix",
]
