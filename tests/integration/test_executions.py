from __future__ import annotations

import pytest

from conftest import PLUGIN_TOKEN, git_repo  # type: ignore[import-not-found]
from control_plane.agentmgr import AgentManagerError
from fake_agents import FakeAgentManager  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration


@pytest.fixture
def agents(services) -> FakeAgentManager:
    fake = FakeAgentManager()
    services.ctx.agents = fake
    return fake


@pytest.fixture
def task(api, services, projects_root, agents) -> str:
    git_repo(projects_root / "demo")
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    key = api.post("/v1/tasks", {"project": "demo", "request": "Add OAuth"}).json()["key"]
    services.scheduler.run_once()
    assert api.get(f"/v1/tasks/{key}").json()["state"] == "READY"
    return key


def run(api, task, **body):
    payload = {"role": "TESTER", "command": ["bash", "-c", "true"], **body}
    return api.post(f"/v1/tasks/{task}/executions", payload)


def events(api, task):
    return [e["type"] for e in api.get(f"/v1/events?task={task}").json()["events"]]


def test_execution_lifecycle_collects_redacts_and_revokes(api, services, agents, task):
    response = run(api, task, capabilities={"tests": "EXECUTE"})
    assert response.status_code == 201, response.text
    execution = response.json()["id"]
    assert api.get(f"/v1/executions/{execution}").json()["state"] == "RUNNING"

    agents.finish(execution, 0, files={"result.json": b'{"ok": true}'}, logs="done; token=abcdefghijklmnop12345 end")
    services.scheduler.run_once()
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "SUCCEEDED" and view["exit_code"] == 0
    assert view["grant_revoked_at"] is not None
    assert len(view["artifacts"]) == 3  # result.json, logs.txt, egress.jsonl
    assert execution in agents.removed

    with services.ctx.db.transaction() as cur:
        cur.execute("SELECT path FROM artifacts WHERE id = ANY(%s)", ([a for a in view["artifacts"]],))
        contents = {row["path"].rsplit("-", 1)[-1]: services.ctx.artifacts.read(row["path"]) for row in cur.fetchall()}
    assert b"abcdefghijklmnop12345" not in contents["logs.txt"] and b"[REDACTED]" in contents["logs.txt"]
    assert contents["result.json"] == b'{"ok": true}'
    for expected in ("GRANT_ISSUED", "AGENT_ASSIGNED", "WORKER_CREATED", "WORKER_STOPPED", "GRANT_REVOKED"):
        assert expected in events(api, task)


def test_grant_is_the_intersection_not_the_request(api, agents, task):
    response = run(api, task, role="DEVELOPER", provider="codex",
                   capabilities={"workspace": "WRITE", "production": "PROD_WRITE", "egress": "STANDARD", "git": "LOCAL_COMMIT"})
    assert response.status_code == 201, response.text
    view = api.get(f"/v1/executions/{response.json()['id']}").json()
    caps = view["grant"]["capabilities"]
    assert caps["production"] == "NONE" and caps["docker"] == "NONE"
    assert any("production" in r for r in view["grant_reductions"])
    spec = agents.specs[response.json()["id"]]
    assert spec["grant"] == view["grant"]


def test_only_the_operator_starts_executions_in_phase_3(api, task):
    payload = {"role": "TESTER", "command": ["true"]}
    response = api.post(f"/v1/tasks/{task}/executions", payload, token=PLUGIN_TOKEN, principal="telegram:1")
    assert response.status_code == 403


def test_executions_need_an_active_task(api, agents, task):
    api.post(f"/v1/tasks/{task}/pause")
    assert run(api, task).status_code == 409


def test_invalid_requests(api, agents, task):
    assert run(api, task, role="DEVELOPER").status_code == 400  # agent roles need a provider
    assert run(api, task, provider="codex").status_code == 400  # runners do not use one
    assert run(api, task, workspace="../other").status_code == 400
    assert run(api, task, capabilities={"docker": "WRITE"}).status_code == 400
    assert run(api, task, capabilities={"egress": "EVERYTHING"}).status_code == 400


def test_agent_worker_limit(api, agents, task):
    for _ in range(3):  # mac-m2-pro: three agent workers
        assert run(api, task, role="DEVELOPER", provider="codex").status_code == 201
    response = run(api, task, role="DEVELOPER", provider="codex")
    assert response.status_code == 409 and "limit" in response.json()["message"]
    assert run(api, task).status_code == 201  # runners are not agent workers


def test_budget_exhaustion_pauses_the_task(api, services, agents, task):
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE budgets SET limits = limits || '{\"agent_launches\": 1}' WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", (task,))
    assert run(api, task).status_code == 201
    response = run(api, task)
    assert response.status_code == 409
    view = api.get(f"/v1/tasks/{task}").json()
    assert view["state"] == "PAUSED_BUDGET" and view["budget"]["state"] == "EXHAUSTED"
    assert "PAUSED_BUDGET" in events(api, task)


def test_cancelling_a_task_stops_its_executions(api, services, agents, task):
    execution = run(api, task).json()["id"]
    api.post(f"/v1/tasks/{task}/cancel")
    assert execution in agents.stopped
    services.scheduler.run_once()
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "CANCELLED" and view["grant_revoked_at"]


def test_dispatch_retries_when_agent_manager_is_down(api, services, agents, task):
    agents.fail_with = AgentManagerError(0, "unavailable", "connection refused")
    execution = run(api, task).json()["id"]
    assert api.get(f"/v1/executions/{execution}").json()["state"] == "REQUESTED"
    agents.fail_with = None
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE executions SET updated_at = now() - interval '1 minute' WHERE id = %s", (execution,))
    services.scheduler.run_once()
    assert api.get(f"/v1/executions/{execution}").json()["state"] == "RUNNING"


def test_missing_credential_fails_as_auth_required(api, agents, task):
    agents.fail_with = AgentManagerError(424, "auth_required", "provider credential cred-codex-default is not set up")
    execution = run(api, task, role="DEVELOPER", provider="codex").json()["id"]
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "FAILED" and view["failure_class"] == "AUTH"
    assert "AUTH_REQUIRED" in events(api, task)


def test_vanished_container_is_lost(api, services, agents, task):
    execution = run(api, task).json()["id"]
    agents.vanish(execution)
    services.scheduler.run_once()
    assert api.get(f"/v1/executions/{execution}").json()["state"] == "LOST"


def test_expired_grant_stops_the_worker(api, services, agents, task):
    execution = run(api, task).json()["id"]
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE capability_grants SET expires_at = now() - interval '1 second' WHERE execution_id = %s", (execution,))
    services.scheduler.run_once()  # stop requested
    services.scheduler.run_once()  # exit observed
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "FAILED" and view["failure_class"] == "TIMEOUT"


def test_failed_exit_code(api, services, agents, task):
    execution = run(api, task).json()["id"]
    agents.finish(execution, 2, logs="tests failed")
    services.scheduler.run_once()
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "FAILED" and view["failure_class"] == "TASK" and view["exit_code"] == 2


def test_replace_stops_the_old_worker_and_starts_a_fresh_one(api, services, agents, task):
    for _ in range(3):
        old = run(api, task, role="DEVELOPER", provider="codex", capabilities={"egress": "STANDARD"}).json()["id"]
    response = api.post(f"/v1/executions/{old}/replace", {"reason": "worker unhealthy"})
    assert response.status_code == 201, response.text  # the replacement reuses the slot at the worker limit
    new = response.json()["id"]
    assert new != old and old in agents.stopped
    assert api.get(f"/v1/executions/{new}").json()["state"] == "RUNNING"
    assert agents.specs[new]["command"] == agents.specs[old]["command"]
    assert agents.specs[new]["grant"]["grant_id"] != agents.specs[old]["grant"]["grant_id"]
    services.scheduler.run_once()
    assert api.get(f"/v1/executions/{old}").json()["state"] == "CANCELLED"
    assert "WORKER_REPLACED" in events(api, task)
