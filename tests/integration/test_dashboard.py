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


def test_request_tab_shows_the_request_and_every_requirements_version(api, services, agents, task):
    [row] = q(services, "SELECT t.id, t.project_id, a.path FROM tasks t JOIN artifacts a ON a.id = t.original_request_artifact_id "
                        "WHERE t.key = %s", task)
    before = api.get(f"/v1/dashboard/tasks/{task}/request", token=PLUGIN_TOKEN, principal="dashboard:operator").json()
    original = services.ctx.artifacts.read(row["path"]).decode()
    assert before["original"]["text"] == original and before["original"]["truncated"] is False
    assert before["requirements"] == [] and before["original"]["requested_by"]

    # Stand-in for the orchestrator writing v1 and a human revision v2 (orchestration is off in this suite).
    with services.ctx.unit_of_work() as uow:
        for version, source, text in ((1, "ORCHESTRATOR", "v1 text"), (2, "USER", "v2 text")):
            stored = services.ctx.artifacts.write(uow.cur, project_id=row["project_id"], task_id=row["id"], kind="requirements",
                                                  name=f"requirements-v{version}.md", content=text.encode(), media_type="text/markdown")
            uow.cur.execute("INSERT INTO requirement_versions (id, task_id, version, artifact_id, source, change_reason) "
                            "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s)", (row["id"], version, stored.id, source, source.lower()))
        uow.cur.execute("UPDATE tasks SET current_requirements_version = 2 WHERE id = %s", (row["id"],))
    view = api.get(f"/v1/dashboard/tasks/{task}/request").json()
    assert view["current_version"] == 2
    assert [(v["version"], v["source"], v["text"]) for v in view["requirements"]] == [(2, "USER", "v2 text"), (1, "ORCHESTRATOR", "v1 text")]

    services.ctx.artifacts.delete(row["path"])  # retention removed the file: the tab still answers
    assert api.get(f"/v1/dashboard/tasks/{task}/request").json()["original"]["text"] is None
    assert api.get("/v1/dashboard/tasks/T-999/request").status_code == 404


def test_budget_shows_runtime_consumed(api, services, agents, task):
    q(services, "UPDATE tasks SET started_at = now() - interval '30 minutes' WHERE key = %s", task)
    view = api.get(f"/v1/dashboard/tasks/{task}").json()
    assert 29 <= view["budget"]["consumed"]["runtime_minutes"] <= 31


def test_the_model_each_execution_reported_is_shown(api, services, agents, task):
    from pathlib import Path

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
    execution = api.post(f"/v1/tasks/{task}/executions", {"role": "DEVELOPER", "provider": "codex", "prompt": "x"}).json()["id"]
    agents.finish(execution, 0, {"ho/events.jsonl": (fixtures / "codex-success.jsonl").read_bytes(),
                                 "ho/runner.json": b'{"exit_code": 0, "reason": "cli_exited"}', "ho/models.txt": b"gpt-6.1-sol\n"})
    services.scheduler.run_once()
    [row] = q(services, "SELECT units->>'model' AS model FROM usage_records WHERE execution_id = %s", execution)
    assert row["model"] == "gpt-6.1-sol"
    usage = api.get("/v1/dashboard/summary", token=PLUGIN_TOKEN, principal="dashboard:operator").json()["provider_usage"]["24_hours"]
    assert usage["codex"]["models"]["gpt-6.1-sol"]["executions"] == 1
    [shown] = [e for e in api.get(f"/v1/dashboard/tasks/{task}").json()["executions"] if e["id"] == execution]
    assert shown["model"] == "gpt-6.1-sol"
    assert 'ho_model_tokens_total{provider="codex",model="gpt-6.1-sol"}' in api.get("/metrics").text


def test_board_shows_active_and_recently_finished_tasks_with_progress(api, services, agents, task):
    done = api.post("/v1/tasks", {"project": "demo", "request": "Something small"}).json()["key"]
    api.post(f"/v1/tasks/{done}/cancel")
    board = api.get("/v1/dashboard/board", token=PLUGIN_TOKEN, principal="dashboard:operator").json()["tasks"]
    rows = {t["key"]: t for t in board}
    assert rows[done]["state"] == "CANCELLED"  # finished recently: still on the board
    assert rows[task]["project"] == "demo" and rows[task]["subtasks"] == 0 and rows[task]["subtasks_done"] == 0
    assert {"pending_approvals", "running"} <= set(rows[task])
    with services.ctx.unit_of_work() as uow:  # finished history is limited apart: it never hides active work
        from control_plane import dashboard

        keys = [t["key"] for t in dashboard.board(uow, finished_limit=0)]
    assert task in keys and done not in keys


def test_approvals_are_explained_in_plain_words_and_activity_is_listed(api, services, agents, task):
    from ho_core.enums import ApprovalAction

    [row] = q(services, "SELECT id, project_id FROM tasks WHERE key = %s", task)
    with services.ctx.unit_of_work() as uow:
        services.approvals.request(uow, action=ApprovalAction.BUDGET_INCREASE, project_id=row["project_id"], task_id=row["id"],
                                   subject={"add": {"agent_launches": 5}}, config_hash="0" * 64,
                                   summary=f"{task}: raise budget", requested_by="test")
    summary = api.get("/v1/dashboard/summary", token=PLUGIN_TOKEN, principal="dashboard:operator").json()
    [approval] = summary["required_actions"]["approvals"]
    assert approval["plain"]["question"].startswith("The task needs more budget") and "*" not in approval["plain"]["question"]
    assert approval["plain"]["yes"] == "yes, raise it" and approval["task"] == task and "subject" not in approval
    assert any(a["type"] == "TASK_CREATED" and a["task"] == task and a["label"] == "Task created" for a in summary["recent_activity"])
    view = api.get(f"/v1/dashboard/tasks/{task}", token=PLUGIN_TOKEN, principal="dashboard:operator").json()
    assert view["approvals"][0]["plain"]["no"] == "no, leave it paused"
