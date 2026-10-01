"""Phase 8 through the control plane: checkpoints, startup and periodic reconciliation, orphaned resources,
stale intents, missing workspaces, verifications never launched, health and DEGRADED, the Hermes outbox,
orchestrator failback, and cancelled work retained."""

from __future__ import annotations

import json
import shutil

import httpx
import pytest
from control_plane.agentmgr import AgentManagerError
from control_plane.recovery import DEGRADED_AFTER, backoff_seconds

from fake_agents import FakeAgentManager  # type: ignore[import-not-found]
from test_git import clone, commit, set_state, workspace  # type: ignore[import-not-found]
import test_git  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration
# The Phase 5 fixtures: a registered project with a READY task.
agents = pytest.fixture(test_git.agents.__wrapped__)
repo = pytest.fixture(test_git.repo.__wrapped__)
task = pytest.fixture(test_git.task.__wrapped__)


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def run_agent(api, task, provider="codex", ws=None):
    body = {"role": "DEVELOPER", "provider": provider, "prompt": "do it"}
    if ws:
        body |= {"workspace": ws["path"], "capabilities": {"workspace": "WRITE"}}
    response = api.post(f"/v1/tasks/{task}/executions", body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def execution_state(services, execution):
    return q(services, "SELECT state, failure_class FROM executions WHERE id = %s", execution)[0]


# ------------------------------------------------------------------ checkpoints


def test_every_state_change_writes_a_checkpoint(api, services, agents, task):
    api.post(f"/v1/tasks/{task}/pause")
    api.post(f"/v1/tasks/{task}/resume")
    checkpoints = api.get(f"/v1/tasks/{task}/checkpoints").json()["checkpoints"]
    reasons = [c["reason"] for c in reversed(checkpoints)]
    assert "READY -> PAUSED" in reasons and "PAUSED -> READY" in reasons
    latest = checkpoints[0]
    assert latest["snapshot"]["state"] == "READY" and latest["seq"] == len(checkpoints)


def test_execution_results_write_a_checkpoint(api, services, agents, task):
    execution = run_agent(api, task)
    agents.finish_agent(execution, b'{"type":"result","is_error":true,"result":"x"}', exit_code=1)
    services.scheduler.run_once()
    reasons = [c["reason"] for c in api.get(f"/v1/tasks/{task}/checkpoints").json()["checkpoints"]]
    assert "execution failed" in reasons


# ------------------------------------------------------------------ startup reconciliation


def test_startup_reconciles_executions_with_agent_manager(api, services, agents, task):
    vanished = run_agent(api, task)
    agents.vanish(vanished)  # the machine rebooted: the container is gone
    finished = run_agent(api, task, provider="claude")
    agents.finish_agent(finished, b'{"type":"result","is_error":true,"result":"x"}', exit_code=1)
    report = services.recovery.run("STARTUP")
    assert report["executions"]["finalized"] == 2
    assert execution_state(services, vanished)["state"] == "LOST"
    assert execution_state(services, finished)["state"] == "FAILED"
    [event] = q(services, "SELECT summary FROM events WHERE type = 'RECOVERY_COMPLETED'")
    assert "executions: finalized 2" in event["summary"]


def test_requested_executions_are_dispatched_again_without_duplicates(api, services, agents, task):
    agents.fail_with = AgentManagerError(0, "unavailable", "connection refused")
    execution = run_agent(api, task)
    assert execution_state(services, execution)["state"] == "REQUESTED"
    agents.fail_with = None
    services.recovery.run("STARTUP")
    services.recovery.run("STARTUP")  # creation is idempotent per execution
    assert execution_state(services, execution)["state"] in ("STARTING", "RUNNING")
    assert list(agents.specs) == [execution]


def test_orphaned_containers_are_removed(api, services, agents, task):
    agents.containers["01a0f000-0000-7000-8000-000000000001"] = {"state": "running", "exit_code": None, "files": {}, "logs": ""}
    done = run_agent(api, task)
    agents.finish_agent(done, b'{"type":"result","is_error":true,"result":"x"}', exit_code=1)
    services.scheduler.run_once()  # finalized (output collected) but say its removal failed
    agents.containers[done] = {"state": "exited", "exit_code": 1, "files": {}, "logs": ""}
    running = run_agent(api, task, provider="claude")
    report = services.recovery.run("PERIODIC")
    assert report["orphans"]["removed"] == 2
    assert set(agents.containers) == {running}  # live work is never touched


def test_stale_intents_are_resolved(api, services, agents, task):
    started = run_agent(api, task)
    q(services, "UPDATE operation_intents SET state = 'SENT', updated_at = now() - interval '10 minutes' "
      "WHERE target = %s", started)
    report = services.recovery.run("PERIODIC")
    assert report["intents"]["confirmed"] == 1
    assert q(services, "SELECT state FROM operation_intents WHERE target = %s", started)[0]["state"] == "CONFIRMED"


def test_missing_workspaces_are_detected(api, services, agents, repo, task):
    ws = workspace(api, task)
    shutil.rmtree(clone(repo, ws))
    report = services.recovery.run("OPERATOR")
    assert report["workspaces"]["missing"] == 1
    assert q(services, "SELECT status FROM workspaces")[0]["status"] == "REMOVED"
    assert "WORKSPACE_MISSING" in [r["type"] for r in q(services, "SELECT type FROM events")]


def test_verifications_never_launched_are_relaunched(api, services, agents, repo, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "change", {"app.py": "x = 2\n"})
    agents.fail_with = AgentManagerError(0, "unavailable", "connection refused")
    api.post(f"/v1/tasks/{task}/git/integrate")  # the runner launch fails: the verification stays PREPARING
    agents.fail_with = None
    q(services, "UPDATE verifications SET created_at = now() - interval '10 minutes', execution_ids = '{}', "
      "state = 'PREPARING'")
    q(services, "UPDATE tasks SET state = 'TESTING' WHERE key = %s", task)  # documented stand-in for the Phase 7 flow
    report = services.recovery.run("PERIODIC")
    assert report["verifications"]["relaunched"] == 1
    assert q(services, "SELECT state FROM verifications")[0]["state"] == "RUNNING"


# ------------------------------------------------------------------ health and outbox


def test_repeated_agent_manager_failures_degrade_and_hold_dispatch(api, services, agents, task):
    agents.fail_with = AgentManagerError(0, "unavailable", "connection refused")
    for _ in range(DEGRADED_AFTER):
        services.recovery.health.check()
    status = api.get("/v1/recovery").json()
    assert status["health"]["state"] == "DEGRADED"
    assert status["health"]["components"]["agent_manager"]["state"] == "DEGRADED"
    execution = run_agent(api, task)  # requested while degraded: it waits instead of failing the task
    agents.fail_with = None
    q(services, "UPDATE executions SET updated_at = now() - interval '1 minute' WHERE id = %s", execution)
    services.executions.sync()
    assert execution_state(services, execution)["state"] == "REQUESTED"
    services.recovery.health.check()  # healthy again
    services.executions.sync()
    assert execution_state(services, execution)["state"] in ("STARTING", "RUNNING")
    types = [r["type"] for r in q(services, "SELECT type FROM events ORDER BY seq")]
    assert types.index("PLATFORM_DEGRADED") < types.index("PLATFORM_RECOVERED")


def test_outbox_waits_for_hermes_and_delivers_in_order(api, services, agents, task):
    received, up = [], {"value": False}

    def hermes(request: httpx.Request) -> httpx.Response:
        if not up["value"]:
            return httpx.Response(503)
        received.append(json.loads(request.content))
        return httpx.Response(204)

    outbox = services.outbox
    outbox.url, outbox.client = "http://hermes.test/notify", httpx.Client(transport=httpx.MockTransport(hermes))
    pending = q(services, "SELECT count(*) AS n FROM notifications WHERE state = 'PENDING'")[0]["n"]
    assert pending > 0
    assert outbox.deliver() == {"sent": 0, "failed": 1}  # Hermes down: one attempt, then back off
    [first] = q(services, "SELECT * FROM notifications WHERE attempts = 1")
    assert first["state"] == "PENDING" and first["last_error"]
    up["value"] = True
    q(services, "UPDATE notifications SET next_attempt_at = now()")
    assert outbox.deliver()["sent"] == pending
    assert q(services, "SELECT count(*) AS n FROM notifications WHERE state = 'PENDING'")[0]["n"] == 0
    assert received[0]["id"] == str(first["id"])


def test_backoff_schedule():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert backoff_seconds(20) == 900


# ------------------------------------------------------------------ cancel retains work


def test_cancel_stops_work_and_retains_workspaces(api, services, agents, repo, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "unfinished work", {"app.py": "x = 3\n"})
    execution = run_agent(api, task, ws=ws)
    api.post(f"/v1/tasks/{task}/cancel")
    assert execution in agents.stopped
    services.scheduler.run_once()  # the stopped worker is finalized
    report = services.recovery.run("PERIODIC")
    assert report["retained"]["workspaces"] == 1
    [row] = q(services, "SELECT status, head_sha FROM workspaces")
    assert row["status"] == "RETAINED" and row["head_sha"]  # commits collected under platform refs
    assert clone(repo, ws).exists()


def test_ended_tasks_keep_no_lease_or_queued_launches(api, services, agents, task):
    [row] = q(services, "SELECT id FROM tasks WHERE key = %s", task)
    q(services, "INSERT INTO task_leases (task_id, holder, provider, epoch, expires_at) VALUES (%s, 'x', 'claude', 1, now())",
      row["id"])
    set_state(services, task, "FAILED")  # a crash ended it without its wind-down
    report = services.recovery.run("PERIODIC")
    assert report["ended_tasks"]["wound_down"] == 1
    assert q(services, "SELECT * FROM task_leases") == []


# ------------------------------------------------------------------ failback


@pytest.mark.orchestration
def test_failback_to_claude_only_at_a_safe_checkpoint(api, services, repo):
    fake = FakeAgentManager()
    services.ctx.agents = fake
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    key = api.post("/v1/tasks", {"project": "demo", "request": "Add a feature"}).json()["key"]
    services.scheduler.run_once()
    q(services, "UPDATE task_leases SET provider = 'codex'")
    services.scheduler.run_once()
    assert q(services, "SELECT provider FROM task_leases")[0]["provider"] == "codex"  # a step is in flight
    [step] = q(services, "SELECT id FROM executions WHERE role = 'ORCHESTRATOR'")
    final = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "num_turns": 1,
             "structured_output": {"summary": "s", "actions": []}, "usage": {}}
    fake.finish_agent(str(step["id"]), json.dumps(final).encode())
    services.scheduler.run_once()
    [lease] = q(services, "SELECT * FROM task_leases")
    assert (lease["provider"], lease["epoch"]) == ("claude", 2)
    assert "ORCHESTRATOR_FAILBACK" in [r["type"] for r in q(services, "SELECT type FROM events")]
    assert key


@pytest.mark.orchestration
def test_no_failback_right_after_a_failover(api, services, repo):
    fake = FakeAgentManager()
    services.ctx.agents = fake
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    key = api.post("/v1/tasks", {"project": "demo", "request": "Add a feature"}).json()["key"]
    services.scheduler.run_once()
    [step] = q(services, "SELECT id FROM executions WHERE role = 'ORCHESTRATOR'")
    fake.finish_agent(str(step["id"]), b'{"type":"result","is_error":true,"result":"x"}', exit_code=1)
    services.scheduler.run_once()
    fake.finish_agent(str(q(services, "SELECT id FROM executions WHERE role = 'ORCHESTRATOR' AND state = 'RUNNING'")[0]["id"]),
                      b'{"type":"result","is_error":true,"result":"x"}', exit_code=1)
    services.scheduler.run_once()  # second failure: failover to codex
    assert q(services, "SELECT provider FROM task_leases")[0]["provider"] == "codex"
    services.scheduler.run_once()
    assert q(services, "SELECT provider FROM task_leases")[0]["provider"] == "codex"  # held for FAILBACK_HOLD
    assert key
