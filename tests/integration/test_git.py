"""Phase 5 through the control plane with the real Git Service (in-process) and an in-memory
Agent Manager: workspaces, human change protection, integration and retest, conflicts, and
approval-controlled merges into local repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import PLUGIN_TOKEN, git_repo  # type: ignore[import-not-found]
from fake_agents import FakeAgentManager  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration

APP = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"
PROJECT_YAML = """\
version: 1
project: {name: demo}
agents: {allowed_providers: [claude, codex]}
commands: {test: "python -m unittest"}
verification: {critical_paths: ["schema/*"]}
"""
ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "User",
       "GIT_AUTHOR_EMAIL": "user@example.com", "GIT_COMMITTER_NAME": "User", "GIT_COMMITTER_EMAIL": "user@example.com",
       "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", message)
    return sh(repo, "rev-parse", "HEAD")


def edit_line(text: str, number: int, new: str) -> str:
    lines = text.splitlines()
    lines[number - 1] = new
    return "\n".join(lines) + "\n"


@pytest.fixture
def agents(services) -> FakeAgentManager:
    fake = FakeAgentManager()
    services.ctx.agents = fake
    return fake


@pytest.fixture
def repo(projects_root) -> Path:
    return git_repo(projects_root / "demo", {".hermes/project.yaml": PROJECT_YAML, "app.py": APP, "schema/api.txt": "v1\n",
                                             "README.md": "# demo\n"})


def new_task(api, services) -> str:
    key = api.post("/v1/tasks", {"project": "demo", "request": "Add a feature"}).json()["key"]
    services.scheduler.run_once()
    assert api.get(f"/v1/tasks/{key}").json()["state"] == "READY"
    return key


@pytest.fixture
def task(api, services, repo, agents) -> str:
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    return new_task(api, services)


def workspace(api, task, suffix="w1") -> dict:
    response = api.post(f"/v1/tasks/{task}/git/workspaces", {"suffix": suffix})
    assert response.status_code == 201, response.text
    return response.json()


def clone(repo: Path, ws: dict) -> Path:
    return repo / ws["path"]


def git_state(api, task) -> dict:
    return api.get(f"/v1/tasks/{task}/git").json()


def events(api, task) -> list[str]:
    return [e["type"] for e in api.get(f"/v1/events?task={task}").json()["events"]]


def set_state(services, task, state) -> None:
    """Test harness: stands in for the Quality Gate (Phase 7), the only path to READY_FOR_MERGE."""
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE tasks SET state = %s, resume_state = NULL WHERE key = %s", (state, task))


def integrate_and_pass(api, services, agents, task) -> dict:
    result = api.post(f"/v1/tasks/{task}/git/integrate").json()
    assert result["ok"], result
    retest = result["retest"]["execution"]
    assert agents.specs[retest]["command"] == ["bash", "-lc", "python -m unittest"]
    agents.finish(retest, 0)
    services.scheduler.run_once()
    assert git_state(api, task)["changes"]["retest_status"] == "PASSED"
    return result


# -------------------------------------------------------------------- workspaces


def test_workspaces_are_isolated_per_task(api, services, repo, agents, task):
    ws = workspace(api, task)
    assert ws["path"] == f".hermes/worktrees/{task.lower()}-w1" and ws["branch"] == f"hermes/{task.lower()}/w1"
    state = git_state(api, task)
    assert state["base_commit"] == sh(repo, "rev-parse", "main") and state["target_branch"] == "main"
    commit(repo, "user moves main", {"README.md": "# moved\n"})
    second = workspace(api, task, "w2")
    assert second["base_sha"] == state["base_commit"]  # every workspace starts from the task's pinned base

    other = new_task(api, services)
    payload = {"role": "DEVELOPER", "provider": "codex", "prompt": "x", "workspace": ws["path"],
               "capabilities": {"workspace": "WRITE"}}
    response = api.post(f"/v1/tasks/{other}/executions", payload)
    assert response.status_code == 400 and "not an active workspace" in response.json()["message"]
    payload["workspace"] = ".hermes/worktrees/made-up"
    assert api.post(f"/v1/tasks/{task}/executions", payload).status_code == 400
    payload["workspace"] = ws["path"]
    assert api.post(f"/v1/tasks/{task}/executions", payload).status_code == 201
    assert api.post(f"/v1/tasks/{task}/git/workspaces", {"suffix": "w1"}).status_code == 409


def test_only_the_operator_runs_git_operations(api, task):
    response = api.post(f"/v1/tasks/{task}/git/workspaces", {"suffix": "w1"}, token=PLUGIN_TOKEN, principal="telegram:1")
    assert response.status_code == 403


# ---------------------------------------------------------- human change protection


def test_human_changes_are_classified_and_acted_on(api, services, repo, agents, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "task edits line 5", {"app.py": edit_line(APP, 5, "task line 5")})
    api.post(f"/v1/tasks/{task}/git/collect")
    assert "COMMIT_CREATED" in events(api, task)

    commit(repo, "user edits README", {"README.md": "# user\n"})
    services.scheduler.run_once()  # the monitor notices the moved target
    assert git_state(api, task)["changes"]["divergence_level"] == "LOW"
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "READY"

    commit(repo, "user edits line 5", {"app.py": edit_line(APP, 5, "user line 5")})
    services.scheduler.run_once()
    changes = git_state(api, task)["changes"]
    assert changes["divergence_level"] == "HIGH" and changes["reconcile_required"]
    assert events(api, task).count("HUMAN_CHANGE_DETECTED") == 2


def test_critical_human_change_needs_an_approval(api, services, repo, agents, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "task changes the schema", {"schema/api.txt": "v2 by task\n"})
    api.post(f"/v1/tasks/{task}/git/collect")
    commit(repo, "user changes the schema", {"schema/api.txt": "v2 by user\n"})
    services.scheduler.run_once()
    view = api.get(f"/v1/tasks/{task}").json()
    assert view["state"] == "APPROVAL_REQUIRED"
    approval = api.get("/v1/approvals").json()["approvals"][0]
    assert approval["action"] == "HIGH_RISK_OPERATION" and approval["risk"] == "CRITICAL"
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "READY"


def test_uncommitted_human_work_is_seen(api, services, repo, agents, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "task edits README", {"README.md": "# task\n"})
    api.post(f"/v1/tasks/{task}/git/collect")
    (repo / "README.md").write_text("# the user is typing this\n")
    result = api.post(f"/v1/tasks/{task}/git/divergence").json()
    assert result["level"] == "HIGH" and result["checks"][0]["uncommitted_changes"]


# ------------------------------------------------------------------ integration


def test_integration_keeps_human_changes_and_retests(api, services, repo, agents, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "task feature", {"feature.py": "print('feature')\n"})
    user = commit(repo, "user change", {"README.md": "# user\n"})
    result = integrate_and_pass(api, services, agents, task)
    assert result["target_sha"] == user and result["files_changed"] == 1
    assert sh(repo, "rev-parse", "main") == user  # integration never touches the user's branch
    assert "INTEGRATION_COMPLETED" in events(api, task) and "TEST_PASSED" in events(api, task)


def test_conflict_is_reported_and_resolved_by_an_agent(api, services, repo, agents, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "task line 5", {"app.py": edit_line(APP, 5, "task line 5")})
    commit(repo, "user line 5", {"app.py": edit_line(APP, 5, "user line 5")})
    result = api.post(f"/v1/tasks/{task}/git/integrate").json()
    assert not result["ok"] and result["conflicts"] == ["app.py"]
    assert "INTEGRATION_CONFLICT" in events(api, task)

    resolved = api.post(f"/v1/tasks/{task}/git/resolve", {"provider": "claude"})
    assert resolved.status_code == 201, resolved.text
    body = resolved.json()
    spec = agents.specs[body["execution"]]
    assert "app.py" in spec["inputs"]["prompt.md"] and spec["workspace"].endswith(body["workspace"])
    # The agent resolves the conflict in its workspace and commits the merge.
    conflict_clone = repo / ".hermes" / "worktrees" / body["workspace"]
    (conflict_clone / "app.py").write_text(edit_line(APP, 5, "user line 5 + task line 5"))
    sh(conflict_clone, "add", "app.py")
    sh(conflict_clone, "commit", "-q", "--no-edit")
    result = api.post(f"/v1/tasks/{task}/git/integrate").json()
    assert result["ok"], result
    assert sh(repo, "show", f"{result['integration_sha']}:app.py").splitlines()[4] == "user line 5 + task line 5"


# ------------------------------------------------------------------------ merge


def ready_for_merge(api, services, repo, agents, task) -> dict:
    ws = workspace(api, task)
    commit(clone(repo, ws), "task feature", {"feature.py": "print('feature')\n"})
    integrate_and_pass(api, services, agents, task)
    set_state(services, task, "READY_FOR_MERGE")
    response = api.post(f"/v1/tasks/{task}/git/merge-request")
    assert response.status_code == 200, response.text
    return response.json()


def test_approved_merge_is_verified_before_done(api, services, repo, agents, task):
    (repo / "notes.txt").write_text("the user's uncommitted notes\n")
    approval = ready_for_merge(api, services, repo, agents, task)
    assert approval["action"] == "MERGE" and approval["subject"]["target_branch"] == "main"
    assert sh(repo, "log", "-1", "--format=%s", "main") == "commit"  # nothing merged before the decision

    decided = api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert decided.status_code == 200, decided.text
    view = api.get(f"/v1/tasks/{task}").json()
    assert view["state"] == "VERIFYING"
    assert (repo / "feature.py").exists() and (repo / "notes.txt").read_text() == "the user's uncommitted notes\n"
    assert sh(repo, "log", "-1", "--format=%s", "main").startswith(f"Merge {task}")
    changes = git_state(api, task)["changes"]
    verification = changes["verification_execution_id"]
    agents.finish(verification, 0)
    services.scheduler.run_once()
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "DONE"
    history = events(api, task)
    assert history.index("MERGE_COMPLETED") < history.index("POST_MERGE_VERIFIED") < history.index("TASK_COMPLETED")
    assert all(w["status"] == "REMOVED" for w in git_state(api, task)["workspaces"])
    assert not (repo / ".hermes" / "worktrees" / f"{task.lower()}-w1").exists()


def test_failed_post_merge_tests_block_the_task(api, services, repo, agents, task):
    approval = ready_for_merge(api, services, repo, agents, task)
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    agents.finish(git_state(api, task)["changes"]["verification_execution_id"], 1)
    services.scheduler.run_once()
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "BLOCKED"


def test_target_moved_after_request_invalidates_the_approval(api, services, repo, agents, task):
    approval = ready_for_merge(api, services, repo, agents, task)
    before = commit(repo, "user commits after the merge request", {"README.md": "# late\n"})
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert api.get(f"/v1/approvals/{approval['id']}").json()["state"] == "INVALIDATED"
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "RUNNING"
    assert sh(repo, "rev-parse", "main") == before  # nothing merged


def test_monitor_withdraws_a_pending_merge_request_when_main_moves(api, services, repo, agents, task):
    approval = ready_for_merge(api, services, repo, agents, task)
    commit(repo, "user commits", {"README.md": "# moved\n"})
    services.scheduler.run_once()
    assert api.get(f"/v1/approvals/{approval['id']}").json()["state"] == "INVALIDATED"
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "RUNNING"


def test_rejected_merge_returns_to_fix_required(api, services, repo, agents, task):
    approval = ready_for_merge(api, services, repo, agents, task)
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "REJECT", "note": "not yet"})
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "FIX_REQUIRED"
    assert sh(repo, "log", "-1", "--format=%s", "main") == "commit"


def test_merge_never_overwrites_uncommitted_user_changes(api, services, repo, agents, task):
    approval = ready_for_merge(api, services, repo, agents, task)
    (repo / "feature.py").write_text("the user's own draft\n")  # same path the task adds
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    view = api.get(f"/v1/tasks/{task}").json()
    assert view["state"] == "BLOCKED" and "uncommitted" in view["state_reason"]
    assert (repo / "feature.py").read_text() == "the user's own draft\n"
    assert sh(repo, "log", "-1", "--format=%s", "main") == "commit"


def test_merge_request_rules(api, services, repo, agents, task):
    assert api.post(f"/v1/tasks/{task}/git/merge-request").status_code == 409  # not READY_FOR_MERGE
    ws = workspace(api, task)
    commit(clone(repo, ws), "task feature", {"feature.py": "x\n"})
    result = api.post(f"/v1/tasks/{task}/git/integrate").json()
    set_state(services, task, "READY_FOR_MERGE")
    response = api.post(f"/v1/tasks/{task}/git/merge-request")
    assert response.status_code == 409 and "RUNNING" in response.json()["message"]  # retest still running
    agents.finish(result["retest"]["execution"], 0)
    services.scheduler.run_once()
    set_state(services, task, "READY_FOR_MERGE")
    commit(repo, "user moves main", {"README.md": "# moved\n"})
    response = api.post(f"/v1/tasks/{task}/git/merge-request")
    assert response.status_code == 409 and "integrate" in response.json()["message"]


def test_only_allowed_approvers_decide_merges(api, services, repo, agents, task):
    approval = ready_for_merge(api, services, repo, agents, task)
    response = api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"},
                        token=PLUGIN_TOKEN, principal="telegram:12345")
    assert response.status_code == 403
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "READY_FOR_MERGE"
