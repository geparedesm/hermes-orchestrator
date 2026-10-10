"""Runtime counts working time only; clones left by rolled-back launches are reclaimed."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

import test_git  # type: ignore[import-not-found]
from control_plane import budgets

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
