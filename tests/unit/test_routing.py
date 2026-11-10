"""Phase 7 pure logic: provider routing, plan validation, request similarity, and the step schema."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from control_plane.orchestration import _check_acyclic, _similarity, _words
from ho_core import schemas
from ho_core.routing import ProviderStats, QuotaWindow, quota_availability, route


def test_default_preferences_by_kind():
    assert route("PLANNING", available=["claude", "codex"]).provider == "claude"
    assert route("IMPLEMENT", available=["claude", "codex"]).provider == "codex"


def test_unavailable_and_excluded_providers_are_never_chosen():
    assert route("IMPLEMENT", available=["claude"]).provider == "claude"
    assert route("IMPLEMENT", available=["claude", "codex"], exclude=["codex"]).provider == "claude"
    assert route("IMPLEMENT", available=[]).provider is None


def test_suggestion_project_preference_mix_and_history_are_weighed():
    decision = route("IMPLEMENT", available=["claude", "codex"], suggested="claude")
    assert decision.provider == "claude" and "orchestrator suggestion" in decision.factors["claude"]
    busy = route("IMPLEMENT", available=["claude", "codex"], running={"codex": 2}, mix={"claude": 1, "codex": 2})
    assert busy.scores["codex"] == 0 and busy.provider == "codex"  # tie: kind default breaks it
    poor = route("IMPLEMENT", available=["claude", "codex"], running={"codex": 2}, mix={"codex": 2},
                 stats={"codex": ProviderStats(10, 1, 5.0), "claude": ProviderStats(10, 9, 5.0)})
    assert poor.provider == "claude"


NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def allowance(session, week, **kwargs):
    windows = {k: QuotaWindow(v, NOW + timedelta(hours=1), NOW) for k, v in (("session", session), ("week", week))}
    return quota_availability(windows, now=kwargs.pop("now", NOW), **kwargs)


@pytest.mark.parametrize("claude,codex,expected", [((25, 85), (55, 40), "codex"), ((20, 30), (10, 90), "claude")])
def test_most_remaining_quota_overrides_all_preferences(claude, codex, expected):
    other = "codex" if expected == "claude" else "claude"
    decision = route("IMPLEMENT", available=["claude", "codex"], policy="MOST_REMAINING_QUOTA",
                     suggested=other, project_preferred={"implementation": other},
                     quota={"claude": allowance(*claude), "codex": allowance(*codex)})
    assert decision.provider == expected
    assert decision.as_json()["quota"][expected]["remaining_percent"] == (70 if expected == "claude" else 45)


@pytest.mark.parametrize("change,status", [
    ({"observed_at": NOW - timedelta(seconds=901)}, "STALE"),
    ({"observed_at": NOW + timedelta(seconds=1)}, "FUTURE_OBSERVATION"),
    ({"resets_at": NOW}, "RESET_UNCONFIRMED"),
    ({"used_percent": float("nan")}, "INVALID"),
    ({"used_percent": 101}, "INVALID"),
])
def test_unreliable_windows_do_not_become_free_quota(change, status):
    window = {"used_percent": 30, "observed_at": NOW, "resets_at": NOW + timedelta(hours=1), **change}
    quota = quota_availability({"session": QuotaWindow(**window),
                                "week": QuotaWindow(20, NOW + timedelta(days=1), NOW)}, now=NOW)
    assert quota.remaining_percent is None and quota.status == status


def test_missing_window_and_freshness_boundary():
    quota = quota_availability({"session": QuotaWindow(0, NOW + timedelta(hours=1), NOW)}, now=NOW)
    assert quota.remaining_percent is None and quota.status == "MISSING"
    assert allowance(20, 30, now=NOW + timedelta(seconds=900)).remaining_percent == 70
    assert allowance(20, 30, now=NOW + timedelta(seconds=901), max_age_seconds=1000).remaining_percent == 70


def test_unknown_quota_falls_back_for_both_providers_and_records_why():
    decision = route("IMPLEMENT", available=["claude", "codex"], suggested="claude", policy="MOST_REMAINING_QUOTA",
                     quota={"codex": allowance(0, 0)})
    assert decision.provider == "claude" and "not comparable" in decision.reason


def test_quota_ties_use_load_then_existing_scores_and_never_override_exclusions():
    quota = {p: allowance(20, 30) for p in ("claude", "codex")}
    assert route("IMPLEMENT", available=quota, quota=quota, policy="MOST_REMAINING_QUOTA").provider == "codex"
    assert route("IMPLEMENT", available=quota, quota=quota, policy="MOST_REMAINING_QUOTA",
                 running={"codex": 1}).provider == "claude"
    assert route("IMPLEMENT", available=quota, quota=quota, policy="MOST_REMAINING_QUOTA",
                 exclude=["codex"]).provider == "claude"
    assert route("IMPLEMENT", available=[], quota=quota, policy="MOST_REMAINING_QUOTA").provider is None


def test_observe_and_scored_modes_keep_the_existing_assignment():
    quota = {"claude": allowance(0, 0), "codex": allowance(90, 90)}
    observed = route("IMPLEMENT", available=quota, quota=quota, policy="MOST_REMAINING_QUOTA", mode="OBSERVE")
    assert observed.provider == "codex" and observed.candidate_provider == "claude"
    assert route("IMPLEMENT", available=quota, quota=quota, policy="SCORED").provider == "codex"


def test_plan_cycles_are_rejected():
    _check_acyclic({"a": [], "b": ["a"], "c": ["a", "b"]})
    with pytest.raises(ValueError, match="cycle"):
        _check_acyclic({"a": ["c"], "b": ["a"], "c": ["b"]})


def test_request_similarity():
    a = _words("Add a greeting endpoint")
    assert _similarity(a, _words("add a GREETING endpoint!")) == 1.0
    assert _similarity(a, _words("Rewrite the billing exporter")) == 0.0
    assert _similarity(set(), a) == 0.0


def test_orchestrator_step_schema_is_closed():
    action = {"type": "WAIT", "text": None, "subtasks": None, "subtask": None, "role": None, "provider": None, "prompt": None,
              "level": None, "reversible": None, "category": None, "anchors": None, "questions": None}
    assert schemas.errors_for("orchestrator-step", {"summary": "s", "actions": [action]}) == []
    assert schemas.errors_for("orchestrator-step", {"summary": "s", "actions": [{**action, "type": "MERGE"}]})
