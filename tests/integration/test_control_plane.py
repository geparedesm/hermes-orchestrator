from __future__ import annotations

import subprocess
from datetime import timedelta

import psycopg
import pytest
import yaml

from conftest import OPERATOR_TOKEN, PLUGIN_TOKEN, git_repo  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration


def register(api, projects_root, name="demo", files=None):
    git_repo(projects_root / name, files)
    response = api.post("/v1/projects", {"path": str(projects_root / name)})
    assert response.status_code == 201, response.text
    return response.json()


def make_ready(api, slug="demo"):
    scan = api.post(f"/v1/projects/{slug}/scan")
    assert scan.status_code == 200, scan.text
    approval = scan.json()["approval"]
    decided = api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert decided.status_code == 200, decided.text
    return scan.json()


def create_task(api, project="demo", **extra):
    response = api.post("/v1/tasks", {"project": project, "request": "Add OAuth authentication", **extra})
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ health and auth


def test_health(api):
    response = api.client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"postgres": True, "redis": True, "git_service": True}


def test_authentication_is_required(api):
    assert api.client.get("/v1/projects").status_code == 401
    assert api.client.get("/v1/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_plugin_must_forward_a_human_principal(api, projects_root):
    git_repo(projects_root / "demo")
    body = {"path": "demo"}
    assert api.post("/v1/projects", body, token=PLUGIN_TOKEN).status_code == 401
    assert api.post("/v1/projects", body, token=PLUGIN_TOKEN, principal="host-cli:operator").status_code == 401
    ok = api.post("/v1/projects", body, token=PLUGIN_TOKEN, principal="telegram:123")
    assert ok.status_code == 201
    assert ok.json()["registered_by"] == "telegram:123"


# ---------------------------------------------------------------- registration


def test_registration_is_confined_to_the_projects_root(api, projects_root, tmp_path):
    git_repo(tmp_path / "outside")
    for path in (str(tmp_path / "outside"), str(projects_root / ".." / "outside"), str(projects_root)):
        response = api.post("/v1/projects", {"path": path})
        assert response.status_code == 400, path
    (projects_root / "plain").mkdir()
    assert api.post("/v1/projects", {"path": "plain"}).status_code == 400  # not a Git repository
    assert api.post("/v1/projects", {"path": "missing"}).status_code == 404


def test_registration_is_idempotent_and_unique(api, projects_root):
    git_repo(projects_root / "demo")
    first = api.post("/v1/projects", {"path": "demo"}, key="register-demo-1")
    replay = api.post("/v1/projects", {"path": "demo"}, key="register-demo-1")
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert api.post("/v1/projects", {"path": "demo", "name": "Other"}, key="register-demo-1").status_code == 409
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 409
    project = first.json()
    assert project["status"] == "REGISTERED"
    assert project["default_branch"] == "main"
    assert len(project["head_commit"]) == 40


# ------------------------------------------------------------------- onboarding


def test_onboarding_requires_an_allowed_approver(api, projects_root):
    register(api, projects_root, files={"package.json": '{"scripts": {"test": "vitest"}}', "src/a.test.ts": ""})
    scan = api.post("/v1/projects/demo/scan").json()
    assert scan["project"]["status"] == "PROPOSED"
    assert scan["config"]["source"] == "PROPOSAL"
    effective = scan["config"]["effective_config"]
    assert effective["toolchain"]["profiles"] == ["node"]
    assert {"main", "master"} <= set(effective["git"]["protected_branches"])

    approval_id = scan["approval"]["id"]
    denied = api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "APPROVE"}, token=PLUGIN_TOKEN, principal="telegram:999")
    assert denied.status_code == 403
    approved = api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "APPROVE"})
    assert approved.json()["state"] == "CONSUMED"
    project = api.get("/v1/projects/demo").json()
    assert project["status"] == "PROJECT_READY"
    assert project["active_config"]["status"] == "ACTIVE"
    # A decided approval cannot be decided again.
    assert api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "REJECT"}).status_code == 409


def test_approval_is_invalidated_when_the_repository_changes(api, projects_root):
    register(api, projects_root)
    approval_id = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    git_repo(projects_root / "demo", {"new-file.txt": "human change"})
    result = api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "APPROVE"}).json()
    assert result["state"] == "INVALIDATED"
    assert api.get("/v1/projects/demo").json()["status"] == "PROPOSED"
    events = api.get("/v1/events?project=demo").json()["events"]
    assert any(e["type"] == "BLOCKED" and "rescan" in e["summary"] for e in events)


def test_rejected_proposal_returns_project_to_registered(api, projects_root):
    register(api, projects_root)
    approval_id = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "REJECT", "note": "not yet"})
    project = api.get("/v1/projects/demo").json()
    assert project["status"] == "REGISTERED"
    assert project["latest_config"]["status"] == "REJECTED"


def test_repository_config_is_used_and_clamped(api, projects_root):
    project_yaml = {"version": 1, "project": {"name": "demo"}, "autonomy": "SUPERVISED",
                    "quality_gate": {"block_on_findings": ["MEDIUM"]}, "git": {"protected_branches": ["release"]}}
    register(api, projects_root, files={".hermes/project.yaml": yaml.safe_dump(project_yaml),
                                        ".hermes.local.yaml": "autonomy: AUTONOMOUS\n"})
    config = api.post("/v1/projects/demo/scan").json()["config"]
    assert config["source"] == "REPOSITORY"
    effective = config["effective_config"]
    assert effective["autonomy"] == "SUPERVISED"  # the local file cannot weaken it
    assert effective["quality_gate"]["block_on_findings"] == ["MEDIUM", "HIGH", "CRITICAL"]
    assert set(effective["git"]["protected_branches"]) == {"main", "master", "release"}
    assert config["rejected_layers"] and config["clamped"]


def test_invalid_repository_config_falls_back_to_a_proposal(api, projects_root):
    register(api, projects_root, files={".hermes/project.yaml": "version: 1\nproject: {name: demo}\ndocker: WRITE\n"})
    result = api.post("/v1/projects/demo/scan").json()
    assert result["config"]["source"] == "PROPOSAL"
    assert any("invalid" in note for note in result["notes"])


def test_rescan_detects_drift(api, projects_root):
    register(api, projects_root)
    make_ready(api)
    unchanged = api.post("/v1/projects/demo/scan").json()
    assert unchanged["changed"] is False and unchanged["approval"] is None
    git_repo(projects_root / "demo", {"package.json": '{"scripts": {"test": "jest"}}'})
    drift = api.post("/v1/projects/demo/scan").json()
    assert drift["changed"] is True
    assert drift["project"]["status"] == "DRIFT_DETECTED"
    assert drift["approval"]["action"] == "PROJECT_READY"


# ------------------------------------------------------------------------ tasks


def test_task_waits_in_backlog_until_project_is_ready(api, services, projects_root):
    register(api, projects_root)
    task = create_task(api, priority="HIGH")
    assert task["state"] == "BACKLOG" and task["key"].startswith("T-")
    assert services.scheduler.run_once()["promoted"] == 0
    make_ready(api)
    assert services.scheduler.run_once()["promoted"] == 1
    assert api.get(f"/v1/tasks/{task['key']}").json()["state"] == "READY"
    queue = api.get("/v1/queue").json()
    assert queue["dispatcher"] == "NoWorkersDispatcher"
    assert [e["key"] for e in queue["queue"]] == [task["key"]]
    # No workers exist in Phase 2: the task stays READY.
    assert services.scheduler.run_once()["dispatched"] == 0


def test_task_requested_by_must_match_principal(api, projects_root):
    register(api, projects_root)
    body = {"project": "demo", "request": "x", "requested_by": {"channel": "telegram", "subject": "someone-else"}}
    assert api.post("/v1/tasks", body).status_code == 403
    assert api.post("/v1/tasks", {"project": "demo", "request": ""}).status_code == 422
    assert api.post("/v1/tasks", {"project": "missing", "request": "x"}).status_code == 404


def test_pause_resume_cancel_and_retry(api, services, projects_root):
    register(api, projects_root)
    make_ready(api)
    key = create_task(api)["key"]
    services.scheduler.run_once()

    paused = api.post(f"/v1/tasks/{key}/pause").json()
    assert paused["state"] == "PAUSED" and paused["resume_state"] == "READY"
    assert api.post(f"/v1/tasks/{key}/pause").status_code == 409
    assert api.post(f"/v1/tasks/{key}/resume").json()["state"] == "READY"

    assert api.post(f"/v1/tasks/{key}/cancel").json()["state"] == "CANCELLED"
    assert api.post(f"/v1/tasks/{key}/cancel").status_code == 409
    assert api.post(f"/v1/tasks/{key}/resume").status_code == 409

    retried = api.post(f"/v1/tasks/{key}/retry").json()
    assert retried["key"] != key and retried["state"] == "BACKLOG"
    events = api.get(f"/v1/events?task={key}").json()["events"]
    assert [e["data"]["to"] for e in events if e["type"] == "TASK_STATE_CHANGED"] == ["READY", "PAUSED", "READY", "CANCELLED"]


def test_dependencies_hold_tasks_in_backlog(api, services, projects_root):
    register(api, projects_root)
    make_ready(api)
    first = create_task(api)["key"]
    second = create_task(api, related_tasks=[{"task": first, "kind": "DEPENDENCY"}])["key"]
    services.scheduler.run_once()
    assert api.get(f"/v1/tasks/{first}").json()["state"] == "READY"
    assert api.get(f"/v1/tasks/{second}").json()["state"] == "BACKLOG"


def test_unlimited_budget_requires_approval(api, services, projects_root):
    register(api, projects_root)
    make_ready(api)
    task = create_task(api, budget_profile="UNLIMITED")
    assert task["state"] == "APPROVAL_REQUIRED" and task["budget"]["profile"] == "NORMAL"
    approval_id = task["pending_approvals"][0]
    api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "APPROVE"})
    approved = api.get(f"/v1/tasks/{task['key']}").json()
    assert approved["state"] == "BACKLOG" and approved["budget"]["profile"] == "UNLIMITED"

    other = create_task(api, budget_profile="UNLIMITED")
    api.post(f"/v1/approvals/{other['pending_approvals'][0]}/decision", {"decision": "REJECT"})
    rejected = api.get(f"/v1/tasks/{other['key']}").json()
    assert rejected["state"] == "BACKLOG" and rejected["budget"]["profile"] == "NORMAL"


def test_priority_aging_prevents_starvation(api, services, projects_root):
    register(api, projects_root)
    make_ready(api)
    low = create_task(api, priority="LOW")["key"]
    high = create_task(api, priority="HIGH")["key"]
    services.scheduler.run_once()
    assert [e["key"] for e in api.get("/v1/queue").json()["queue"]] == [high, low]
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE tasks SET ready_at = ready_at - %s WHERE key = %s", (timedelta(hours=2), low))
    assert [e["key"] for e in api.get("/v1/queue").json()["queue"]] == [low, high]


def test_approvals_expire(api, services, projects_root):
    register(api, projects_root)
    approval_id = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s", (approval_id,))
    assert services.scheduler.run_once()["expired"] == 1
    assert api.get(f"/v1/approvals/{approval_id}").json()["state"] == "EXPIRED"


def test_unregister_never_deletes_files(api, services, projects_root):
    register(api, projects_root)
    make_ready(api)
    key = create_task(api)["key"]
    assert api.delete("/v1/projects/demo").status_code == 409  # unfinished task
    api.post(f"/v1/tasks/{key}/cancel")
    assert api.delete("/v1/projects/demo").json()["status"] == "UNREGISTERED"
    assert (projects_root / "demo" / "README.md").exists()
    # The path can be registered again.
    assert api.post("/v1/projects", {"path": "demo", "slug": "demo-again"}).status_code == 201


# ---------------------------------------------------------------- persistence


def test_audit_events_are_append_only_for_the_application_role(api, services, projects_root):
    register(api, projects_root)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with services.ctx.db.transaction() as cur:
            cur.execute("UPDATE events SET summary = 'tampered'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with services.ctx.db.transaction() as cur:
            cur.execute("DELETE FROM events")


def test_state_survives_a_new_service_instance(api, services, projects_root, database):
    from control_plane.db import Database

    register(api, projects_root)
    make_ready(api)
    key = create_task(api)["key"]
    fresh = Database(database, max_size=1)
    fresh.open()
    try:
        with fresh.transaction() as cur:
            cur.execute("SELECT state FROM tasks WHERE key = %s", (key,))
            assert cur.fetchone()["state"] == "BACKLOG"
            cur.execute("SELECT status FROM projects WHERE slug = 'demo'")
            assert cur.fetchone()["status"] == "PROJECT_READY"
    finally:
        fresh.close()


def test_command_policy_endpoint(api):
    response = api.client.post("/v1/policy/commands/evaluate", json={"command": "git push --force origin main"},
                               headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"})
    assert response.json()["class"] == "HIGH_RISK"
    assert response.json()["decision"] == "REQUIRE_APPROVAL"


def test_git_service_never_runs_repository_hooks(api, projects_root):
    repo = git_repo(projects_root / "hooked")
    marker = projects_root / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {marker}"], check=True)
    api.post("/v1/projects", {"path": "hooked"})
    api.post("/v1/projects/hooked/scan")
    assert not marker.exists()
