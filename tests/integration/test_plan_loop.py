"""Plans that go in circles (T-49): an unchanged plan is refused, a revised assignment unblocks a subtask, the
orchestrator is told how to continue, and repeated plans without work stop the task."""

from __future__ import annotations

import pytest

import test_orchestration  # type: ignore[import-not-found]
from test_clarifications import SUPERVISED, bundle, make_task, plan_approval  # type: ignore[import-not-found]
from test_orchestration import DONE, active, actions, answer, plan_item, q, state, step, subtasks  # type: ignore[import-not-found]

pytestmark = [pytest.mark.integration, pytest.mark.orchestration]
agents = pytest.fixture(test_orchestration.agents.__wrapped__)
BLOCKED = {**DONE, "status": "blocked", "summary": "Stopped.", "changed_files": [],
           "blocked_reason": "AGENTS.md requires tests for every feature; the assignment forbids writing tests"}
NO_TESTS = plan_item("a", description="Build the model. Do not write tests: S2 does.")


@pytest.fixture
def task(api, services, agents, projects_root) -> str:
    return make_task(api, services, projects_root)


def block_a(services, agents, task, item=NO_TESTS):
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Tenant CRUD."}, {"type": "SET_PLAN", "subtasks": [item]},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"})
    [dev] = active(services, task, "DEVELOPER")
    answer(services, agents, dev["id"], BLOCKED)
    assert subtasks(services, task)["a"]["state"] == "BLOCKED"


def plan_version(services, task) -> int:
    return q(services, "SELECT current_plan_version FROM tasks WHERE key = %s", task)[0]["current_plan_version"]


def test_an_unchanged_plan_is_refused_and_says_how_to_continue(api, services, agents, task):
    block_a(services, agents, task)
    version = plan_version(services, task)
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [NO_TESTS]})
    kind, outcome, reason = actions(services, task)[-1]
    assert (kind, outcome) == ("SET_PLAN", "REJECTED") and "unchanged" in reason and "a stays BLOCKED" in reason
    assert plan_version(services, task) == version


def test_a_revised_assignment_unblocks_the_subtask(api, services, agents, task):
    block_a(services, agents, task)
    fixed = plan_item("a", description="Build the model and its TransactionCase tests.")
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [fixed]},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"})
    assert actions(services, task)[-1][:2] == ("REQUEST_EXECUTION", "ACCEPTED")
    assert active(services, task, "DEVELOPER")


def test_rejecting_a_blocked_subtask_with_guidance_sends_it_back_for_a_fix(api, services, agents, task):
    block_a(services, agents, task)
    step(services, agents, task, {"type": "REJECT_SUBTASK", "subtask": "a", "text": "Write the model's tests too."},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"})
    assert subtasks(services, task)["a"]["state"] == "IN_PROGRESS"


def test_the_orchestrator_is_told_how_to_continue_and_to_follow_the_project_rules(api, services, agents, task):
    block_a(services, agents, task)
    text = bundle(services, task)
    assert "## Blocked subtasks: how to continue" in text and "REJECT_SUBTASK with `text`" in text
    assert "never tell a developer not to write or touch tests" in text


def test_plans_going_in_circles_stop_the_task(api, services, agents, task):
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Tenant CRUD."}, {"type": "SET_PLAN", "subtasks": [plan_item("a")]})
    for n in range(2, 6):
        with services.ctx.db.transaction() as cur:  # stand-in: back to planning without starting work
            cur.execute("UPDATE tasks SET state = 'PLANNING', resume_state = NULL, step_requested = true WHERE key = %s", (task,))
        services.scheduler.run_once()
        step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a", description=f"attempt {n}")]})
    assert state(api, task) == "BLOCKED"
    [row] = q(services, "SELECT state_reason FROM tasks WHERE key = %s", task)
    assert "going in circles" in row["state_reason"]


def test_a_supervised_task_is_not_asked_again_for_an_unchanged_plan(api, services, agents, projects_root):
    """T-49: plan approved, S1 blocked, the same plan resubmitted again and again, each one a new approval."""
    task = make_task(api, services, projects_root, yaml=SUPERVISED)
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Tenant CRUD."}, {"type": "SET_PLAN", "subtasks": [NO_TESTS]})
    api.post(f"/v1/approvals/{plan_approval(api)['id']}/decision", {"decision": "APPROVE"})
    services.scheduler.run_once()
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"})
    [dev] = active(services, task, "DEVELOPER")
    answer(services, agents, dev["id"], BLOCKED)
    for _ in range(3):
        services.scheduler.run_once()
        if active(services, task, "ORCHESTRATOR"):
            step(services, agents, task, {"type": "SET_PLAN", "subtasks": [NO_TESTS]})
    assert plan_version(services, task) == 1
    assert not [a for a in api.get("/v1/approvals").json()["approvals"] if a["action"] == "PLAN"]
