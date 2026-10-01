"""Phase 7 pure logic: provider routing, plan validation, request similarity, and the step schema."""

from __future__ import annotations

import pytest
from control_plane.orchestration import _check_acyclic, _similarity, _words
from ho_core import schemas
from ho_core.routing import ProviderStats, route


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
              "level": None, "reversible": None, "category": None, "anchors": None}
    assert schemas.errors_for("orchestrator-step", {"summary": "s", "actions": [action]}) == []
    assert schemas.errors_for("orchestrator-step", {"summary": "s", "actions": [{**action, "type": "MERGE"}]})
