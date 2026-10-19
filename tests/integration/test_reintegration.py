"""A target that moves between integration and the merge request sends the task back to be integrated again."""

from __future__ import annotations

import pytest

import test_git  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration
agents = pytest.fixture(test_git.agents.__wrapped__)
repo = pytest.fixture(test_git.repo.__wrapped__)
task = pytest.fixture(test_git.task.__wrapped__)


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def test_a_target_that_moved_after_integration_is_integrated_again(api, services, repo, agents, task):
    from control_plane.auth import Principal

    ws = test_git.workspace(api, task)
    test_git.commit(test_git.clone(repo, ws), "task feature", {"feature.py": "print('feature')\n"})
    test_git.integrate_and_pass(api, services, agents, task)
    test_git.pass_gate(api, services, agents, task)  # READY_FOR_MERGE
    test_git.commit(repo, "another change lands on main", {"other.py": "x = 1\n"})  # main moves before the request
    with services.ctx.unit_of_work() as uow:
        result = services.git.request_merge(uow, task, principal=Principal("control-plane", "quality-gate"),
                                            reintegrate_if_moved=True)
    assert result is None
    [row] = q(services, "SELECT state, state_reason FROM tasks WHERE key = %s", task)
    assert row["state"] == "RUNNING" and "moved since integration; integrating again" in row["state_reason"]
    assert api.post(f"/v1/tasks/{task}/git/merge-request").status_code == 409  # being integrated again


def test_the_gate_compares_a_github_target_with_origin_not_the_local_branch(api, services, repo, agents, task,
                                                                             monkeypatch, tmp_path):
    """A GitHub project integrates onto origin's branch; unpushed commits on the user's local branch are not a move."""
    monkeypatch.setenv("HO_GIT_ALLOW_FILE_REMOTES", "1")
    origin = tmp_path / "origin.git"
    test_git.sh(tmp_path, "clone", "-q", "--bare", str(repo), str(origin))
    test_git.sh(repo, "remote", "add", "origin", str(origin))
    test_git.sh(repo, "fetch", "-q", "origin")
    ws = test_git.workspace(api, task)
    test_git.commit(test_git.clone(repo, ws), "task feature", {"feature.py": "print('feature')\n"})
    test_git.commit(repo, "the user's unpushed design", {"design.txt": "draft\n"})  # local main ahead of origin
    test_git.integrate_and_pass(api, services, agents, task)
    changes = test_git.git_state(api, task)["changes"]
    assert changes["integration_target_sha"] == test_git.sh(origin, "rev-parse", "refs/heads/main")
    test_git.review(api, services, agents, task)
    test_git.set_state(services, task, "QUALITY_GATE")
    evaluation = api.post(f"/v1/tasks/{task}/quality-gate").json()
    results = {r["name"]: r for r in evaluation["requirements"]}
    assert results["no_conflicts"]["status"] == "PASS", results["no_conflicts"]
