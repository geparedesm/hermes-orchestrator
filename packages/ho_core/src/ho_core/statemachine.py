"""Deterministic task state machine (DATA_MODEL.md section 4.1).

The control plane is the only component that applies transitions. Every
transition is checked here before it is written, so an invalid transition can
never reach the database.
"""

from __future__ import annotations

from typing import NamedTuple

from .enums import StrEnum, TaskState

S = TaskState


class Trigger(StrEnum):
    """Who or what caused a transition."""

    USER = "USER"  # authenticated human command (pause, resume, cancel, retry)
    SCHEDULER = "SCHEDULER"
    ORCHESTRATOR = "ORCHESTRATOR"  # accepted orchestrator action proposal
    APPROVAL = "APPROVAL"  # approval decision
    SYSTEM = "SYSTEM"  # control-plane evaluation (quality gate, merge, recovery)


# Main flow edges.
_FLOW: dict[TaskState, frozenset[TaskState]] = {
    S.BACKLOG: frozenset({S.READY}),
    S.READY: frozenset({S.PLANNING}),
    S.PLANNING: frozenset({S.QUEUED}),
    S.QUEUED: frozenset({S.RUNNING}),
    S.RUNNING: frozenset({S.TESTING}),
    S.TESTING: frozenset({S.REVIEW, S.FIX_REQUIRED}),
    S.REVIEW: frozenset({S.QUALITY_GATE, S.FIX_REQUIRED}),
    S.FIX_REQUIRED: frozenset({S.RUNNING}),
    S.QUALITY_GATE: frozenset({S.READY_FOR_MERGE, S.FIX_REQUIRED}),
    S.READY_FOR_MERGE: frozenset({S.MERGING, S.FIX_REQUIRED, S.RUNNING}),
    S.MERGING: frozenset({S.VERIFYING, S.BLOCKED}),
    S.VERIFYING: frozenset({S.DONE, S.BLOCKED}),
}

ACTIVE_STATES = frozenset(
    {
        S.READY,
        S.PLANNING,
        S.QUEUED,
        S.RUNNING,
        S.TESTING,
        S.REVIEW,
        S.FIX_REQUIRED,
        S.QUALITY_GATE,
        S.READY_FOR_MERGE,
    }
)
WAITING_STATES = frozenset({S.APPROVAL_REQUIRED, S.AUTH_REQUIRED, S.PAUSED, S.PAUSED_BUDGET, S.BLOCKED})
TERMINAL_STATES = frozenset({S.DONE, S.CANCELLED, S.FAILED})
# Cancelling mid-merge is rejected: the merge must be reconciled first.
NON_CANCELLABLE = frozenset({S.MERGING, S.VERIFYING}) | TERMINAL_STATES

# Transitions only the control plane itself may perform (never an orchestrator).
_CONTROL_PLANE_ONLY_TARGETS = frozenset({S.READY_FOR_MERGE, S.MERGING, S.VERIFYING, S.DONE})
# Approval rejection outcomes for APPROVAL_REQUIRED.
_REJECTION_TARGETS = frozenset({S.FIX_REQUIRED, S.BLOCKED, S.CANCELLED})


class InvalidTransition(Exception):
    def __init__(self, current: TaskState, target: TaskState, reason: str) -> None:
        super().__init__(f"{current} -> {target}: {reason}")
        self.current = current
        self.target = target
        self.reason = reason


class TransitionResult(NamedTuple):
    state: TaskState
    resume_state: TaskState | None


def transition(
    current: TaskState,
    target: TaskState,
    *,
    trigger: Trigger,
    resume_state: TaskState | None = None,
) -> TransitionResult:
    """Validate a transition and return the new (state, resume_state).

    `resume_state` is the stored resume state of the task; it is required when
    leaving a waiting state back to active work.
    """
    if current in TERMINAL_STATES:
        raise InvalidTransition(current, target, "terminal state")
    if current == target:
        raise InvalidTransition(current, target, "no-op transition")

    if target in _CONTROL_PLANE_ONLY_TARGETS and trigger == Trigger.ORCHESTRATOR:
        raise InvalidTransition(current, target, "only the control plane may enter this state")
    if target == S.MERGING and trigger != Trigger.APPROVAL:
        raise InvalidTransition(current, target, "merging requires a consumed merge approval")

    if target == S.CANCELLED:
        if current in NON_CANCELLABLE:
            raise InvalidTransition(current, target, "cannot cancel during merge or verification")
        return TransitionResult(S.CANCELLED, None)

    # Leaving a waiting state.
    if current in WAITING_STATES:
        if target == S.FAILED:
            return TransitionResult(S.FAILED, None)
        if resume_state is not None and target == resume_state:
            return TransitionResult(target, None)
        if current == S.APPROVAL_REQUIRED and trigger == Trigger.APPROVAL and target in _REJECTION_TARGETS:
            return TransitionResult(target, None)
        if current == S.BLOCKED and trigger == Trigger.USER and target == S.READY:
            return TransitionResult(S.READY, None)
        raise InvalidTransition(current, target, f"must resume to {resume_state}")

    # Entering a waiting state records where to come back to.
    if target in WAITING_STATES:
        if current in ACTIVE_STATES or current == S.BACKLOG:
            return TransitionResult(target, current)
        if current in (S.MERGING, S.VERIFYING) and target == S.BLOCKED:
            return TransitionResult(S.BLOCKED, None)
        raise InvalidTransition(current, target, "can only wait from active work")

    if target == S.FAILED:
        if current in ACTIVE_STATES:
            return TransitionResult(S.FAILED, None)
        raise InvalidTransition(current, target, "cannot fail from this state")

    if target in _FLOW.get(current, frozenset()):
        return TransitionResult(target, None)
    raise InvalidTransition(current, target, "not an allowed transition")
