import pytest

from ho_core.enums import TaskState as S
from ho_core.statemachine import InvalidTransition, Trigger, transition


def test_happy_path_to_done():
    path = [
        (S.BACKLOG, S.READY, Trigger.SCHEDULER),
        (S.READY, S.PLANNING, Trigger.SCHEDULER),
        (S.PLANNING, S.QUEUED, Trigger.ORCHESTRATOR),
        (S.QUEUED, S.RUNNING, Trigger.SCHEDULER),
        (S.RUNNING, S.TESTING, Trigger.SYSTEM),
        (S.TESTING, S.REVIEW, Trigger.SYSTEM),
        (S.REVIEW, S.QUALITY_GATE, Trigger.SYSTEM),
        (S.QUALITY_GATE, S.READY_FOR_MERGE, Trigger.SYSTEM),
        (S.READY_FOR_MERGE, S.MERGING, Trigger.APPROVAL),
        (S.MERGING, S.VERIFYING, Trigger.SYSTEM),
        (S.VERIFYING, S.DONE, Trigger.SYSTEM),
    ]
    for current, target, trigger in path:
        assert transition(current, target, trigger=trigger).state == target


def test_ready_for_merge_is_not_done():
    with pytest.raises(InvalidTransition):
        transition(S.READY_FOR_MERGE, S.DONE, trigger=Trigger.SYSTEM)


def test_merging_requires_approval_trigger():
    for trigger in (Trigger.SYSTEM, Trigger.USER, Trigger.SCHEDULER, Trigger.ORCHESTRATOR):
        with pytest.raises(InvalidTransition):
            transition(S.READY_FOR_MERGE, S.MERGING, trigger=trigger)


def test_orchestrator_cannot_enter_control_plane_states():
    with pytest.raises(InvalidTransition):
        transition(S.QUALITY_GATE, S.READY_FOR_MERGE, trigger=Trigger.ORCHESTRATOR)
    with pytest.raises(InvalidTransition):
        transition(S.VERIFYING, S.DONE, trigger=Trigger.ORCHESTRATOR)


def test_waiting_state_records_and_requires_resume_state():
    result = transition(S.RUNNING, S.PAUSED, trigger=Trigger.USER)
    assert result == (S.PAUSED, S.RUNNING)
    with pytest.raises(InvalidTransition):
        transition(S.PAUSED, S.TESTING, trigger=Trigger.USER, resume_state=S.RUNNING)
    assert transition(S.PAUSED, S.RUNNING, trigger=Trigger.USER, resume_state=S.RUNNING).state == S.RUNNING


def test_approval_rejection_targets():
    assert transition(S.APPROVAL_REQUIRED, S.BLOCKED, trigger=Trigger.APPROVAL, resume_state=S.RUNNING).state == S.BLOCKED
    with pytest.raises(InvalidTransition):
        transition(S.APPROVAL_REQUIRED, S.QUALITY_GATE, trigger=Trigger.APPROVAL, resume_state=S.RUNNING)


def test_cancel_rules():
    assert transition(S.RUNNING, S.CANCELLED, trigger=Trigger.USER).state == S.CANCELLED
    assert transition(S.PAUSED, S.CANCELLED, trigger=Trigger.USER).state == S.CANCELLED
    for state in (S.MERGING, S.VERIFYING, S.DONE, S.CANCELLED, S.FAILED):
        with pytest.raises(InvalidTransition):
            transition(state, S.CANCELLED, trigger=Trigger.USER)


def test_terminal_states_are_final():
    for state in (S.DONE, S.CANCELLED, S.FAILED):
        with pytest.raises(InvalidTransition):
            transition(state, S.READY, trigger=Trigger.USER)


def test_blocked_retry_and_post_merge_failure():
    assert transition(S.BLOCKED, S.READY, trigger=Trigger.USER, resume_state=S.RUNNING).state == S.READY
    assert transition(S.VERIFYING, S.BLOCKED, trigger=Trigger.SYSTEM) == (S.BLOCKED, None)


def test_every_state_value_matches_task_schema():
    import json
    from ho_core.schemas import schema_dir

    schema = json.loads((schema_dir() / "task.schema.json").read_text())
    assert set(schema["$defs"]["taskState"]["enum"]) == {s.value for s in S}
