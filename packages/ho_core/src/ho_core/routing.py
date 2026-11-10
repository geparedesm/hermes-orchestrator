"""Scored agent routing (MASTER_SPEC section 8). Pure: the control plane supplies the facts.

Scores come from operational facts only (no personality scoring): the default preference by work
kind, the project's preferences, the orchestrator's suggestion, availability, current load against
the machine's starting mix, and each provider's recent success rate and duration for that kind.
The optional quota policy compares complete recent allowances before these scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
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


@dataclass(frozen=True)
class QuotaWindow:
    used_percent: float
    resets_at: datetime
    observed_at: datetime


@dataclass
class QuotaAvailability:
    remaining_percent: float | None
    status: str
    windows: dict[str, dict] = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"remaining_percent": self.remaining_percent, "status": self.status, "windows": self.windows}


def quota_availability(windows: dict[str, QuotaWindow], *, now: datetime, max_age_seconds: int = 900) -> QuotaAvailability:
    """A conservative relative allowance, never a token/currency estimate. Both windows must be
    recent and unexpired; passing a reset does not prove a new allowance was observed."""
    details = {}
    statuses = []
    for kind in ("session", "week"):
        window = windows.get(kind)
        if window is None:
            details[kind] = {"status": "MISSING"}
            statuses.append("MISSING")
            continue
        age = (now - window.observed_at).total_seconds()
        status = ("INVALID" if not isfinite(window.used_percent) or not 0 <= window.used_percent <= 100
                  else "RESET_UNCONFIRMED" if window.resets_at <= now
                  else "FUTURE_OBSERVATION" if age < 0
                  else "STALE" if age > max_age_seconds else "RECENT")
        details[kind] = {"status": status, "used_percent": window.used_percent if status != "INVALID" else None,
                         "observed_at": window.observed_at.isoformat(), "resets_at": window.resets_at.isoformat(),
                         "age_seconds": round(age, 1)}
        statuses.append(status)
    status = next((s for s in statuses if s != "RECENT"), "RECENT")
    remaining = round(min(100 - windows[k].used_percent for k in ("session", "week")), 1) if status == "RECENT" else None
    return QuotaAvailability(remaining, status, details)


@dataclass
class RoutingDecision:
    provider: str | None
    scores: dict[str, float]
    factors: dict[str, list[str]] = field(default_factory=dict)
    policy: str = "SCORED"
    mode: str = "ENFORCE"
    candidate_provider: str | None = None
    reason: str = "scored routing"
    quota: dict[str, QuotaAvailability] = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"provider": self.provider, "scores": self.scores, "factors": self.factors,
                "policy": self.policy, "mode": self.mode, "candidate_provider": self.candidate_provider,
                "reason": self.reason, "quota": {p: q.as_json() for p, q in self.quota.items()}}


def route(kind: str, *, available: Iterable[str], project_preferred: dict[str, str] | None = None,
          suggested: str | None = None, running: dict[str, int] | None = None, mix: dict[str, int] | None = None,
          stats: dict[str, ProviderStats] | None = None, exclude: Iterable[str] = (),
          policy: str = "SCORED", mode: str = "ENFORCE",
          quota: dict[str, QuotaAvailability] | None = None) -> RoutingDecision:
    """Pick a provider for a unit of work; `exclude` removes providers that must not do it (for
    example the developer, when choosing a reviewer)."""
    if policy not in ("SCORED", "MOST_REMAINING_QUOTA") or mode not in ("ENFORCE", "OBSERVE"):
        raise ValueError("invalid routing policy or mode")
    eligible, excluded = set(available), set(exclude)
    available = [p for p in PROVIDERS if p in eligible and p not in excluded]
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
    candidate, reason = provider, "scored routing"
    quota = quota or {}
    if policy == "MOST_REMAINING_QUOTA":
        if len(available) < 2:
            reason = "only one eligible provider" if available else "no eligible provider"
        elif all(p in quota and quota[p].remaining_percent is not None for p in available):
            candidate = max(available, key=lambda p: (quota[p].remaining_percent, -running.get(p, 0),
                                                     scores[p], p == DEFAULT_PREFERENCE.get(kind)))
            reason = "most remaining quota; ties use load, then scored routing"
        else:
            reason = "quota not comparable; using scored routing"
        for p in available:
            q = quota.get(p)
            factors[p].append(f"remaining quota {q.remaining_percent:g}%" if q and q.remaining_percent is not None
                              else f"quota {(q.status if q else 'MISSING').lower()}")
        if mode == "ENFORCE":
            provider = candidate
    return RoutingDecision(provider, scores, factors, policy, mode, candidate, reason, quota)
