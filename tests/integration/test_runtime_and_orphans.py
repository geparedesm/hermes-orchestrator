"""Runtime counts working time only; clones left by rolled-back launches are reclaimed."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

import test_git  # type: ignore[import-not-found]
from control_plane import budgets
from ho_core.enums import TaskState as S
from ho_core.statemachine import Trigger

pytestmark = pytest.mark.integration
agents = pytest.fixture(test_git.agents.__wrapped__)
repo = pytest.fixture(test_git.repo.__wrapped__)
task = pytest.fixture(test_git.task.__wrapped__)


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def row(services, key):
    return q(services, "SELECT * FROM tasks WHERE key = %s", key)[0]


def test_time_waiting_for_people_is_not_runtime(api, services, agents, task):
    # Stand-ins for hours passing: the task started three hours ago and waited two of them on a pause.
    q(services, "UPDATE tasks SET started_at = now() - interval '3 hours' WHERE key = %s", task)
    assert api.post(f"/v1/tasks/{task}/pause").status_code == 200
    assert row(services, task)["waiting_since"] is not None
    q(services, "UPDATE tasks SET waiting_since = now() - interval '2 hours' WHERE key = %s", task)
    assert 55 <= budgets.runtime_minutes(row(services, task)) <= 65  # while paused, the clock is stopped
    assert api.post(f"/v1/tasks/{task}/resume").status_code == 200
    resumed = row(services, task)
    assert resumed["waiting_since"] is None and 7190 <= resumed["waited_seconds"] <= 7210
    assert 55 <= budgets.runtime_minutes(resumed) <= 65


def test_existing_tasks_are_backfilled_from_their_history(api, services, agents, task):
    q(services, "UPDATE tasks SET started_at = now() - interval '1 hour' WHERE key = %s", task)
    api.post(f"/v1/tasks/{task}/pause")
    q(services, "UPDATE tasks SET waited_seconds = 0, waiting_since = NULL WHERE key = %s", task)  # as before 0013
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0013_working_runtime.py"
    spec = importlib.util.spec_from_file_location("m0013", path)
    module = importlib.util.module_from_spec(spec)
    statements: list[str] = []
    sys.modules["alembic"] = types.SimpleNamespace(op=types.SimpleNamespace(execute=statements.append))
    try:
        spec.loader.exec_module(module)
        module.upgrade()
    finally:
        del sys.modules["alembic"]
    backfill = statements[0][statements[0].index("WITH changes AS"):]
    q(services, backfill)
    [paused] = q(services, "SELECT occurred_at FROM events e JOIN tasks t ON t.id = e.task_id WHERE t.key = %s "
                           "AND e.type = 'TASK_STATE_CHANGED' AND e.data->>'to' = 'PAUSED'", task)
    assert row(services, task)["waiting_since"] == paused["occurred_at"]


def test_a_clone_left_by_a_rolled_back_launch_is_reclaimed(api, services, repo, agents, task):
    name = f"{task.lower()}-w1"
    services.ctx.git.prepare_workspace("demo", name, f"hermes/{task.lower()}/w1", "main", None)  # recorded nowhere
    workspace = test_git.workspace(api, task)
    assert workspace["name"] == name
    assert q(services, "SELECT count(*) AS n FROM events WHERE type = 'WORKSPACE_RECLAIMED'")[0]["n"] == 1


def test_a_recorded_workspace_is_never_reclaimed(api, services, repo, agents, task):
    test_git.workspace(api, task)
    response = api.post(f"/v1/tasks/{task}/git/workspaces", {"suffix": "w1"})  # the same name again
    assert response.status_code == 409 and "already exists" in response.text
    assert q(services, "SELECT count(*) AS n FROM events WHERE type = 'WORKSPACE_RECLAIMED'")[0]["n"] == 0


def test_waits_before_the_task_started_do_not_count(api, services, agents, task):
    q(services, "UPDATE tasks SET started_at = NULL WHERE key = %s", task)
    api.post(f"/v1/tasks/{task}/pause")
    q(services, "UPDATE tasks SET waiting_since = now() - interval '2 hours' WHERE key = %s", task)
    api.post(f"/v1/tasks/{task}/resume")
    assert row(services, task)["waited_seconds"] == 0  # nothing to subtract: it had not started
    # Started an hour ago, in the middle of a wait that began three hours ago: only the last hour is waiting.
    q(services, "UPDATE tasks SET started_at = now() - interval '1 hour' WHERE key = %s", task)
    api.post(f"/v1/tasks/{task}/pause")
    q(services, "UPDATE tasks SET waiting_since = now() - interval '3 hours' WHERE key = %s", task)
    assert budgets.runtime_minutes(row(services, task)) < 1


def test_a_first_workspace_left_with_its_base_pin_is_reclaimed(api, services, repo, agents, task):
    name = f"{task.lower()}-w1"
    pin = f"refs/hermes/tasks/{task}/base"
    left = services.ctx.git.prepare_workspace("demo", name, f"hermes/{task.lower()}/w1", "main", pin)  # pinned, unrecorded
    workspace = test_git.workspace(api, task)
    assert workspace["name"] == name and workspace["base_sha"] == left["base_sha"]
    assert str(row(services, task)["base_commit"]) == left["base_sha"]


def test_a_task_paused_for_budget_resumes_once_the_budget_suffices(api, services, agents, task):
    with services.ctx.unit_of_work() as uow:
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.PAUSED_BUDGET, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason=f"{task} would exceed its agent_launches budget (10)")
    q(services, "UPDATE budgets SET consumed = jsonb_set(consumed, '{agent_launches}', limits->'agent_launches') "
                "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)  # stand-in: every launch used
    refused = api.post(f"/v1/tasks/{task}/resume")
    assert refused.status_code == 409 and "agent_launches" in refused.text and f"/orch budget {task}" in refused.text
    q(services, "UPDATE budgets SET limits = jsonb_set(limits, '{agent_launches}', '100') "
                "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)  # stand-in: the budget was raised
    resumed = api.post(f"/v1/tasks/{task}/resume")
    assert resumed.status_code == 200 and resumed.json()["state"] == "READY"


def test_resume_is_refused_while_the_next_launch_would_not_fit(api, services, agents, task):
    from control_plane.budgets import DEFAULT_USAGE_RESERVATION

    with services.ctx.unit_of_work() as uow:
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.PAUSED_BUDGET, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason=f"{task} would exceed its provider_usage_units budget (1)")
    limit = DEFAULT_USAGE_RESERVATION * 5
    q(services, "UPDATE budgets SET limits = jsonb_set(limits, '{provider_usage_units}', to_jsonb(%s::int)), "
                "consumed = jsonb_set(consumed, '{provider_usage_units}', to_jsonb(%s::int)) "
                "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", limit, limit - DEFAULT_USAGE_RESERVATION // 2, task)
    refused = api.post(f"/v1/tasks/{task}/resume")  # under 100 % used, but a launch's reservation does not fit
    assert refused.status_code == 409 and "provider_usage_units" in refused.text


def test_resume_checks_a_queued_retry_and_relaunches_waiting_verifications(api, services, agents, task, monkeypatch):
    import json as _json
    import uuid as _uuid

    with services.ctx.unit_of_work() as uow:
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.PAUSED_BUDGET, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason=f"{task} would exceed its retries budget (6)")
    q(services, "INSERT INTO pending_launches (id, task_id, kind, request, reason) SELECT %s, id, 'DEVELOP', %s, 'budget' "
                "FROM tasks WHERE key = %s", _uuid.uuid4(), _json.dumps({"purpose": {"retry": True}}), task)
    q(services, "UPDATE budgets SET consumed = jsonb_set(consumed, '{retries}', limits->'retries') "
                "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)  # every retry used
    refused = api.post(f"/v1/tasks/{task}/resume")
    assert refused.status_code == 409 and "retries" in refused.text
    relaunched = []
    monkeypatch.setattr(services.verifications, "relaunch_waiting", lambda uow, t: relaunched.append(t["key"]))
    services.tasks.on_budget_resume[:] = [services.verifications.relaunch_waiting]
    q(services, "UPDATE budgets SET limits = jsonb_set(limits, '{retries}', '50') WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)
    assert api.post(f"/v1/tasks/{task}/resume").status_code == 200 and relaunched == [task]



@pytest.mark.parametrize("reason,counter", [("would exceed its review_cycles budget (4)", "review_cycles"),
                                            ("something else", None)])
def test_resume_judges_the_counter_that_paused_the_task(api, services, agents, task, reason, counter):
    with services.ctx.unit_of_work() as uow:
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.PAUSED_BUDGET, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason=f"{task} {reason}")
    q(services, "UPDATE budgets SET consumed = jsonb_set(consumed, '{review_cycles}', limits->'review_cycles') "
                "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)
    refused = api.post(f"/v1/tasks/{task}/resume")
    assert refused.status_code == 409 and (counter or "raise the budget") in refused.text
    if counter:
        q(services, "UPDATE budgets SET limits = jsonb_set(limits, '{review_cycles}', '9') "
                    "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)
        assert api.post(f"/v1/tasks/{task}/resume").status_code == 200



def test_resume_counts_what_running_work_reserved(api, services, agents, task):
    with services.ctx.unit_of_work() as uow:
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.PAUSED_BUDGET, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason=f"{task} would exceed its retries budget (6)")
    q(services, "UPDATE budgets SET limits = jsonb_set(limits, '{retries}', '6'), consumed = jsonb_set(consumed, '{retries}', '5'), "
                "reserved = jsonb_set(reserved, '{retries}', '1') WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)
    refused = api.post(f"/v1/tasks/{task}/resume")  # 5 used + 1 reserved: the next retry does not fit
    assert refused.status_code == 409 and "retries" in refused.text
