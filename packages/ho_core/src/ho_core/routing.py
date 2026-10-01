"""Scored agent routing (MASTER_SPEC section 8). Pure: the control plane supplies the facts.

Scores come from operational facts only (no personality scoring): the default preference by work
kind, the project's preferences, the orchestrator's suggestion, availability, current load against
the machine's starting mix, and each provider's recent success rate and duration for that kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

PROVIDERS = ("claude", "codex")
# Section 8 default: architecture, planning, complex debugging, research -> Claude;
# backend, frontend, refactoring, tests, routine implementation -> Codex.
DEFAULT_PREFERENCE = {"PLANNING": "claude", "RESEARCH": "claude", "DEBUGGING": "claude", "ARCHITECTURE": "claude",
                      "IMPLEMENT": "codex", "TEST_AUTHORING": "codex", "REFACTOR": "codex"}
_PROJECT_KEY = {"PLANNING": "planning", "RESEARCH": "planning", "ARCHITECTURE": "planning", "IMPLEMENT": "implementation",
                "REFACTOR": "implementation", "DEBUGGING": "debugging", "TEST_AUTHORING": "tests"}


@dataclass(frozen=True)
class ProviderStats:
    runs: int = 0
    successes: int = 0
    mean_minutes: float = 0.0


@dataclass
class RoutingDecision:
    provider: str | None
    scores: dict[str, float]
    factors: dict[str, list[str]] = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"provider": self.provider, "scores": self.scores, "factors": self.factors}


def route(kind: str, *, available: Iterable[str], project_preferred: dict[str, str] | None = None,
          suggested: str | None = None, running: dict[str, int] | None = None, mix: dict[str, int] | None = None,
          stats: dict[str, ProviderStats] | None = None, exclude: Iterable[str] = ()) -> RoutingDecision:
    """Pick a provider for a unit of work; `exclude` removes providers that must not do it (for
    example the developer, when choosing a reviewer)."""
    available = [p for p in PROVIDERS if p in set(available) and p not in set(exclude)]
    scores: dict[str, float] = {}
    factors: dict[str, list[str]] = {}
    running = running or {}
    mix = mix or {}
    project_choice = (project_preferred or {}).get(_PROJECT_KEY.get(kind, "implementation"))
    for provider in available:
        score, why = 0.0, []
        if DEFAULT_PREFERENCE.get(kind) == provider:
            score += 2
            why.append(f"default for {kind.lower()}")
        if project_choice == provider:
            score += 3
            why.append("project preference")
        if suggested == provider:
            score += 4
            why.append("orchestrator suggestion")
        share = mix.get(provider)
        if share is not None and running.get(provider, 0) >= share:
            score -= 2
            why.append(f"at its share of the mix ({running.get(provider, 0)}/{share})")
        record = (stats or {}).get(provider)
        if record and record.runs >= 3:
            rate = record.successes / record.runs
            score += 2 * (rate - 0.5)
            why.append(f"success rate {rate:.0%} over {record.runs} runs")
        scores[provider] = round(score, 2)
        factors[provider] = why
    provider = max(available, key=lambda p: (scores[p], p == DEFAULT_PREFERENCE.get(kind))) if available else None
    return RoutingDecision(provider, scores, factors)
