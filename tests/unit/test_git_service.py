"""Git Service against real Git repositories (no network): workspaces, hardened collection,
human change classification, integration, approved merges, and GitHub rules with a fake `gh`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from git_service.app import create_app
from ho_core.gitpolicy import sign_merge

TOKEN = "git-token-0123456789"
KEY = b"merge-key-0123456789"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "User", "GIT_AUTHOR_EMAIL": "user@example.com",
       "GIT_COMMITTER_NAME": "User", "GIT_COMMITTER_EMAIL": "user@example.com"}


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def write(repo: Path, rel: str, content: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content)


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for rel, content in files.items():
        write(repo, rel, content)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", message)
    return sh(repo, "rev-parse", "HEAD")


APP = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    repo = root / "app"
    repo.mkdir(parents=True)
    sh(repo, "init", "-q", "-b", "main")
    commit(repo, "initial", {"app.py": APP, "README.md": "# app\n", "docs/guide.md": "guide\n"})
    sh(repo, "branch", "feature-user")  # a user branch the clone must not see
    return root


@pytest.fixture
def api(root: Path, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HO_GIT_ALLOW_FILE_REMOTES", "1")
    monkeypatch.setenv("HO_GH_BIN", str(FIXTURES / "fake_gh.py"))
    monkeypatch.setenv("HO_TEST_GH_STATE", str(tmp_path / "gh-state.json"))
    client = TestClient(create_app(root, TOKEN, KEY))

    def post(path: str, body: dict):
        return client.post(path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    return post


def prepare(api, name="w1", branch="hermes/t-1/w1", base="main"):
    response = api("/v1/workspaces/prepare", {"path": "app", "name": name, "branch": branch, "base_ref": base})
    assert response.status_code == 200, response.text
    return response.json()


def work(root: Path, name: str, files: dict[str, str], message="agent change") -> str:
    return commit(root / "app" / ".hermes" / "worktrees" / name, message, files)


# ------------------------------------------------------------------- workspaces


def test_prepare_creates_an_isolated_clone(api, root):
    repo = root / "app"
    ws = prepare(api)
    clone = repo / ws["path"]
    assert ws["base_sha"] == sh(repo, "rev-parse", "main") and ws["excluded_added"]
    assert sh(clone, "rev-parse", "--abbrev-ref", "HEAD") == "hermes/t-1/w1"
    assert (clone / "app.py").read_text() == APP
    assert sh(clone, "remote") == ""  # no path back to the user's repository
    assert sh(clone, "branch", "--format=%(refname:short)") == "hermes/t-1/w1"  # user branches not copied
    assert (clone / ".git").is_dir() and not (clone / ".git" / "objects" / "info" / "alternates").exists()
    assert sh(repo, "status", "--porcelain") == ""  # the workspace is excluded from the user's status
    assert api("/v1/workspaces/prepare", {"path": "app", "name": "w1", "branch": "hermes/t-1/w1",
                                          "base_ref": "main"}).status_code == 409
    for bad in ({"name": "../x"}, {"branch": "-x"}, {"base_ref": "refs/remotes/origin/main"}):
        body = {"path": "app", "name": "w2", "branch": "hermes/t-1/w2", "base_ref": "main", **bad}
        assert api("/v1/workspaces/prepare", body).status_code in (409, 422)


def test_collect_reads_commits_without_running_the_clone(api, root, tmp_path):
    repo = root / "app"
    ws = prepare(api)
    clone = repo / ws["path"]
    marker = tmp_path / "executed"
    head = work(root, "w1", {"feature.py": "print('hi')\n"}, "add feature")
    # Then the (model-controlled) worker leaves traps: each would run a program if Git honored it here.
    hooks = clone / ".git" / "evil-hooks"
    hooks.mkdir()
    for hook in ("reference-transaction", "post-checkout", "pre-push", "post-update"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / hook).chmod(0o755)
    for key, value in (("core.fsmonitor", f"touch {marker}"), ("core.hooksPath", str(hooks)),
                       ("uploadpack.packObjectsHook", f"touch {marker}; git pack-objects"),
                       ("core.alternateRefsCommand", f"touch {marker}")):
        sh(clone, "config", key, value)
    body = {"path": "app", "name": "w1", "branch": "hermes/t-1/w1", "base_sha": ws["base_sha"]}
    result = api("/v1/workspaces/collect", body).json()
    assert result["head_sha"] == head and result["descends_from_base"]
    assert result["commits"][0]["subject"] == "add feature" and result["files"] == [{"status": "A", "path": "feature.py"}]
    assert sh(repo, "rev-parse", "refs/hermes/workspaces/w1") == head
    assert not marker.exists()


@pytest.mark.parametrize("attack", ["gitfile", "alternates", "symlinked-objects"])
def test_collect_refuses_clones_that_point_elsewhere(api, root, attack):
    repo = root / "app"
    other = root / "other"
    other.mkdir()
    sh(other, "init", "-q")
    ws = prepare(api)
    git_dir = repo / ws["path"] / ".git"
    if attack == "gitfile":
        subprocess.run(["rm", "-rf", str(git_dir)], check=True)
        git_dir.write_text(f"gitdir: {other / '.git'}\n")
    elif attack == "alternates":
        (git_dir / "objects" / "info" / "alternates").write_text(str(other / ".git" / "objects") + "\n")
    else:
        subprocess.run(["rm", "-rf", str(git_dir / "objects")], check=True)
        (git_dir / "objects").symlink_to(other / ".git" / "objects")
    response = api("/v1/workspaces/collect", {"path": "app", "name": "w1", "branch": "hermes/t-1/w1", "base_sha": ws["base_sha"]})
    assert response.status_code == 409


def test_remove_workspace(api, root):
    ws = prepare(api)
    assert api("/v1/workspaces/remove", {"path": "app", "name": "w1"}).json()["removed"]
    assert not (root / "app" / ws["path"]).exists()


# ------------------------------------------------------------------ divergence


def divergence(api, ws, head_ref="refs/hermes/workspaces/w1", **extra):
    body = {"path": "app", "base_sha": ws["base_sha"], "head_ref": head_ref, "target_branch": "main", **extra}
    response = api("/v1/divergence", body)
    assert response.status_code == 200, response.text
    return response.json()


def collect(api, ws, name="w1"):
    body = {"path": "app", "name": name, "branch": ws["branch"], "base_sha": ws["base_sha"]}
    response = api("/v1/workspaces/collect", body)
    assert response.status_code == 200, response.text
    return response.json()


def edit_line(text: str, number: int, new: str) -> str:
    lines = text.splitlines()
    lines[number - 1] = new
    return "\n".join(lines) + "\n"


def test_divergence_levels(api, root):
    repo = root / "app"
    ws = prepare(api)
    work(root, "w1", {"app.py": edit_line(APP, 5, "task line 5")})
    collect(api, ws)
    assert divergence(api, ws)["level"] == "NONE"

    commit(repo, "user edits another file", {"README.md": "# app, edited by the user\n"})
    result = divergence(api, ws)
    assert result["level"] == "LOW" and result["human_commits"] == 1

    commit(repo, "user edits a far part of app.py", {"app.py": edit_line(APP, 35, "user line 35")})
    result = divergence(api, ws)
    assert result["level"] == "MEDIUM" and result["overlapping"][0]["path"] == "app.py"

    commit(repo, "user edits the same line", {"app.py": edit_line(edit_line(APP, 35, "user line 35"), 5, "user line 5")})
    assert divergence(api, ws)["level"] == "HIGH"
    assert divergence(api, ws, critical_paths=["*.py"])["level"] == "CRITICAL"


def test_divergence_sees_uncommitted_work_and_deletions(api, root):
    repo = root / "app"
    ws = prepare(api)
    work(root, "w1", {"docs/guide.md": "guide rewritten by the task\n"})
    collect(api, ws)
    write(repo, "docs/guide.md", "user is editing this right now\n")  # uncommitted, main checked out
    result = divergence(api, ws)
    assert result["uncommitted_changes"] and result["level"] == "HIGH"
    sh(repo, "checkout", "-q", "--", "docs/guide.md")
    sh(repo, "rm", "-q", "docs/guide.md")
    sh(repo, "commit", "-q", "-m", "user removes the guide")
    assert divergence(api, ws)["level"] == "CRITICAL"


# ------------------------------------------------------------------ integration


def test_integrate_merges_workspaces_in_the_object_database(api, root):
    repo = root / "app"
    ws1, ws2 = prepare(api), prepare(api, "w2", "hermes/t-1/w2")
    work(root, "w1", {"app.py": edit_line(APP, 5, "task A")})
    work(root, "w2", {"docs/guide.md": "task B\n"})
    collect(api, ws1)
    collect(api, ws2, "w2")
    user_head = commit(repo, "user change meanwhile", {"README.md": "# user\n"})
    status_before = sh(repo, "status", "--porcelain")
    result = api("/v1/integrate", {"path": "app", "task": "T-1", "target_branch": "main",
                                   "heads": ["refs/hermes/workspaces/w1", "refs/hermes/workspaces/w2"]}).json()
    assert result["ok"] and result["target_sha"] == user_head
    integration = result["integration_sha"]
    assert sh(repo, "rev-parse", "refs/hermes/tasks/T-1/integration") == integration
    assert sh(repo, "show", f"{integration}:app.py").splitlines()[4] == "task A"
    assert sh(repo, "show", f"{integration}:README.md") == "# user"  # the user's change is kept
    assert sh(repo, "rev-parse", "main") == user_head and sh(repo, "status", "--porcelain") == status_before


def test_integration_conflict_changes_nothing_and_can_be_resolved(api, root):
    repo = root / "app"
    ws = prepare(api)
    work(root, "w1", {"app.py": edit_line(APP, 5, "task line 5")})
    collect(api, ws)
    commit(repo, "user edits the same line", {"app.py": edit_line(APP, 5, "user line 5")})
    result = api("/v1/integrate", {"path": "app", "task": "T-1", "target_branch": "main",
                                   "heads": ["refs/hermes/workspaces/w1"]}).json()
    assert not result["ok"] and result["conflicts"] == ["app.py"]
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/hermes/tasks/T-1/integration"],
                          capture_output=True).returncode != 0
    conflict = api("/v1/workspaces/conflict", {"path": "app", "name": "t-1-resolve", "branch": "hermes/t-1/resolve",
                                               "target_branch": "main", "incoming_ref": "refs/hermes/workspaces/w1"}).json()
    assert conflict["conflicts"] == ["app.py"] and not conflict["clean"]
    assert "<<<<<<<" in (repo / conflict["path"] / "app.py").read_text()


# ------------------------------------------------------------------------ merge


def authorization(repo: Path, head: str, *, method="merge", pr=None, target="main", approval="a-1", project="app",
                  expires=None, key=KEY):
    subject = {"project": project, "target_branch": target, "target_sha": sh(repo, "rev-parse", target),
               "head_sha": head, "method": method, "pr_number": pr}
    return sign_merge(key, approval_id=approval, subject=subject,
                      expires_at=expires or datetime.now(timezone.utc) + timedelta(hours=1))


def integrated(api, root) -> str:
    ws = prepare(api)
    work(root, "w1", {"feature.py": "print('feature')\n"})
    collect(api, ws)
    return api("/v1/integrate", {"path": "app", "task": "T-1", "target_branch": "main",
                                 "heads": ["refs/hermes/workspaces/w1"]}).json()["integration_sha"]


def test_approved_merge_updates_the_checked_out_branch_once(api, root):
    repo = root / "app"
    head = integrated(api, root)
    write(repo, "notes.txt", "user's uncommitted notes\n")  # unrelated uncommitted work survives
    token = authorization(repo, head)
    result = api("/v1/merge", {"path": "app", "task": "T-1", "authorization": token}).json()
    assert not result["already_merged"]
    assert sh(repo, "rev-parse", "main") == result["merge_sha"]
    assert sh(repo, "log", "-1", "--format=%P", "main").split() == [token["subject"]["target_sha"], head]
    assert (repo / "feature.py").exists() and (repo / "notes.txt").read_text() == "user's uncommitted notes\n"
    again = api("/v1/merge", {"path": "app", "task": "T-1", "authorization": token}).json()
    assert again["already_merged"] and again["merge_sha"] == result["merge_sha"]


def test_merge_refuses_moved_target_forged_expired_or_foreign_authorizations(api, root):
    repo = root / "app"
    head = integrated(api, root)
    stale = authorization(repo, head)
    commit(repo, "user commits after approval", {"README.md": "# changed\n"})
    response = api("/v1/merge", {"path": "app", "task": "T-1", "authorization": stale})
    assert response.status_code == 409 and "moved" in response.json()["message"]

    forged = authorization(repo, head, key=b"attacker-key")
    assert api("/v1/merge", {"path": "app", "task": "T-1", "authorization": forged}).status_code == 403
    tampered = authorization(repo, head)
    tampered["subject"]["target_branch"] = "feature-user"
    assert api("/v1/merge", {"path": "app", "task": "T-1", "authorization": tampered}).status_code == 403
    expired = authorization(repo, head, expires=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert api("/v1/merge", {"path": "app", "task": "T-1", "authorization": expired}).status_code == 403
    foreign = authorization(repo, head, project="other")
    assert api("/v1/merge", {"path": "app", "task": "T-1", "authorization": foreign}).status_code == 403
    assert api("/v1/merge", {"path": "app", "task": "T-1", "authorization": {"approval_id": "x"}}).status_code == 403


def test_merge_never_overwrites_uncommitted_changes(api, root):
    repo = root / "app"
    head = integrated(api, root)
    write(repo, "feature.py", "the user's own uncommitted file\n")
    response = api("/v1/merge", {"path": "app", "task": "T-1", "authorization": authorization(repo, head)})
    assert response.status_code == 409 and "uncommitted" in response.json()["message"]
    assert (repo / "feature.py").read_text() == "the user's own uncommitted file\n"
    assert sh(repo, "log", "-1", "--format=%s", "main") == "initial"


def test_merge_into_a_branch_not_checked_out_and_squash(api, root):
    repo = root / "app"
    head = integrated(api, root)
    sh(repo, "checkout", "-q", "feature-user")
    result = api("/v1/merge", {"path": "app", "task": "T-1", "authorization": authorization(repo, head, method="squash")}).json()
    assert sh(repo, "rev-parse", "main") == result["merge_sha"]
    assert len(sh(repo, "log", "-1", "--format=%P", "main").split()) == 1  # squash: one parent
    assert sh(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feature-user" and not (repo / "feature.py").exists()
    response = api("/v1/merge", {"path": "app", "task": "T-1",
                                 "authorization": authorization(repo, head, method="rebase", approval="a-2")})
    assert response.status_code == 409


# ----------------------------------------------------------------------- GitHub


@pytest.fixture
def origin(root: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, env=ENV)
    repo = root / "app"
    sh(repo, "remote", "add", "origin", str(bare))
    sh(repo, "push", "-q", "origin", "main")
    (tmp_path / "gh-state.json").write_text(json.dumps({"prs": [], "calls": [], "checks": [], "logged_in": True,
                                                         "origin": str(bare)}))
    return bare


def branch_policy(**extra):
    return {"path": "app", "prefix": "hermes/", "protected": ["release"], **extra}


def test_push_only_platform_branches_with_a_lease(api, root, origin):
    head = integrated(api, root)
    ok = api("/v1/github/push", branch_policy(ref="refs/hermes/tasks/T-1/integration", branch="hermes/t-1"))
    assert ok.status_code == 200, ok.text
    assert subprocess.run(["git", "-C", str(origin), "rev-parse", "refs/heads/hermes/t-1"], capture_output=True,
                          text=True).stdout.strip() == head
    for branch in ("main", "master", "release", "feature-user", "hermes-evil", "--force"):
        response = api("/v1/github/push", branch_policy(ref="refs/hermes/tasks/T-1/integration", branch=branch))
        assert response.status_code == 403, branch
    # Someone else moved the platform branch: the lease refuses the update instead of overwriting.
    subprocess.run(["git", "-C", str(origin), "update-ref", "refs/heads/hermes/t-1", sh(root / "app", "rev-parse", "main")],
                   check=True)
    response = api("/v1/github/push", branch_policy(ref="refs/hermes/tasks/T-1/integration", branch="hermes/t-1",
                                                    expected_remote_sha=head))
    assert response.status_code == 502
    assert api("/v1/github/delete-branch", branch_policy(branch="main")).status_code == 403
    assert api("/v1/github/delete-branch", branch_policy(branch="hermes/t-1")).status_code == 200


def test_pull_request_checks_and_approved_merge(api, root, origin, tmp_path):
    repo = root / "app"
    head = integrated(api, root)
    api("/v1/github/push", branch_policy(ref="refs/hermes/tasks/T-1/integration", branch="hermes/t-1"))
    pr = api("/v1/github/pr", branch_policy(branch="hermes/t-1", base="main", title="T-1: feature", body="details")).json()
    assert pr["created"] and pr["number"] == 1 and pr["headRefOid"] == head
    again = api("/v1/github/pr", branch_policy(branch="hermes/t-1", base="main", title="T-1: feature v2", body="more")).json()
    assert not again["created"] and again["number"] == 1
    state = json.loads((tmp_path / "gh-state.json").read_text())
    state["checks"] = [{"name": "ci", "state": "SUCCESS", "bucket": "pass", "link": ""}]
    (tmp_path / "gh-state.json").write_text(json.dumps(state))
    assert api("/v1/github/pr/checks", {"path": "app", "number": 1}).json()["summary"] == "PASS"

    remote_main = subprocess.run(["git", "-C", str(origin), "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    token = sign_merge(KEY, approval_id="gh-1", expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                       subject={"project": "app", "target_branch": "main", "target_sha": remote_main, "head_sha": head,
                                "method": "merge", "pr_number": 1})
    result = api("/v1/merge", {"path": "app", "task": "T-1", "authorization": token}).json()
    assert result["pr"] == 1 and result["merge_sha"]
    calls = json.loads((tmp_path / "gh-state.json").read_text())["calls"]
    merge_call = next(c for c in calls if c[:2] == ["pr", "merge"])
    assert "--match-head-commit" in merge_call and "--admin" not in merge_call and "--auto" not in merge_call
    assert sh(repo, "rev-parse", "refs/remotes/origin/main") == result["merge_sha"]


def test_pr_merge_refuses_a_changed_head(api, root, origin, tmp_path):
    head = integrated(api, root)
    api("/v1/github/push", branch_policy(ref="refs/hermes/tasks/T-1/integration", branch="hermes/t-1"))
    api("/v1/github/pr", branch_policy(branch="hermes/t-1", base="main", title="T-1", body=""))
    remote_main = subprocess.run(["git", "-C", str(origin), "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    token = sign_merge(KEY, approval_id="gh-2", expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                       subject={"project": "app", "target_branch": "main", "target_sha": remote_main,
                                "head_sha": remote_main, "method": "merge", "pr_number": 1})  # approved a different head
    response = api("/v1/merge", {"path": "app", "task": "T-1", "authorization": token})
    assert response.status_code == 409 and head


def test_remote_detection(api, root, monkeypatch):
    repo = root / "app"
    assert api("/v1/refs", {"path": "app", "refs": ["main"]}).json()["remote"]["kind"] == "local"
    sh(repo, "remote", "add", "origin", "git@github.com:octo/app.git")
    remote = api("/v1/refs", {"path": "app", "refs": ["main"]}).json()["remote"]
    assert remote == {"kind": "github", "remote": "git@github.com:octo/app.git", "repo": "octo/app"}
    sh(repo, "remote", "set-url", "origin", "https://user:secret-token@github.com/octo/app")
    assert "secret-token" not in json.dumps(api("/v1/refs", {"path": "app", "refs": ["main"]}).json())


def test_fake_gh_is_executable():
    assert os.access(FIXTURES / "fake_gh.py", os.X_OK) or sys.platform == "win32"
