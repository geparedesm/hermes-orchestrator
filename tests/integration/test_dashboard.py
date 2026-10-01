"""Phase 10 Dashboard read models and metrics."""

from __future__ import annotations

import pytest

import test_git  # type: ignore[import-not-found]
from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration
agents = pytest.fixture(test_git.agents.__wrapped__)
repo = pytest.fixture(test_git.repo.__wrapped__)
task = pytest.fixture(test_git.task.__wrapped__)


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def test_summary_shows_required_actions_queue_and_usage(api, services, agents, task):
    execution = api.post(f"/v1/tasks/{task}/executions", {"role": "DEVELOPER", "provider": "codex", "prompt": "x"}).json()["id"]
    api.post(f"/v1/tasks/{task}/pause")
    second = api.post("/v1/tasks", {"project": "demo", "request": "Another feature"}).json()["key"]
    services.scheduler.run_once()
    summary = api.get("/v1/dashboard/summary", token=PLUGIN_TOKEN, principal="dashboard:operator").json()
    assert summary["tasks_by_state"]["PAUSED"] == 1
    assert [t["key"] for t in summary["required_actions"]["waiting_tasks"]] == [task]
    assert [e["key"] for e in summary["queue"]] == [second]
    assert [r["id"] for r in summary["running"]] == [execution] and summary["running"][0]["provider"] == "codex"
    assert "codex" in summary["provider_usage"]["24_hours"]
    assert summary["health"] is not None and summary["pending_notifications"] >= 1


def test_workers_include_cpu_and_memory(api, services, agents, task):
    execution = api.post(f"/v1/tasks/{task}/executions", {"role": "DEVELOPER", "provider": "codex", "prompt": "x"}).json()["id"]
    q(services, "UPDATE executions SET state = 'RUNNING', started_at = now() WHERE id = %s", execution)
    agents.specs[execution]["task"] = task
    [worker] = api.get("/v1/workers").json()["running"]
    assert worker["id"] == execution and worker["task"] == task
    assert worker["cpu_percent"] == 12.5 and worker["memory_bytes"] == 100 * 1024**2


def test_metrics_are_prometheus_text_for_the_operator(api, services, agents, task):
    response = api.get("/metrics")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert '# TYPE ho_tasks gauge' in text and 'ho_tasks{state="READY"} 1' in text
    assert "ho_queue_length 1" in text and "# TYPE ho_executions gauge" in text
    assert api.get("/metrics", token=PLUGIN_TOKEN, principal="dashboard:operator").status_code == 403


def test_task_view_for_a_plain_task(api, services, agents, task):
    view = api.get(f"/v1/dashboard/tasks/{task}").json()
    assert view["task"]["key"] == task and view["dag"]["subtasks"] == [] and view["manifests"] == []
    assert view["timeline"][0]["type"] and "orchestration" not in view  # orchestration is off in this suite
    assert api.get(f"/v1/tasks/{task}/manifests").json()["manifests"] == []
    assert api.get(f"/v1/tasks/{task}/manifests/01a0f000-0000-7000-8000-000000000009").status_code == 404


def test_budget_shows_runtime_consumed(api, services, agents, task):
    q(services, "UPDATE tasks SET started_at = now() - interval '30 minutes' WHERE key = %s", task)
    view = api.get(f"/v1/dashboard/tasks/{task}").json()
    assert 29 <= view["budget"]["consumed"]["runtime_minutes"] <= 31
