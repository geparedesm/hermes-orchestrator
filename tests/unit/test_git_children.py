"""Git Service: child projects created from a parent's template (docs/design/child-projects.md)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from git_service.app import create_app
from ho_core.gitpolicy import sign_child

from test_git_service import FIXTURES, KEY, TOKEN, commit, sh  # type: ignore[import-not-found]

OPERATION = "01a12bf0-0000-7000-8000-000000000001"
MANIFEST = '{"name": "{{title}}", "summary": "{{description}}", "version": "{{odoo_series}}.1.0.0"}\n'


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    parent = root / "odoo-base"
    parent.mkdir(parents=True)
    sh(parent, "init", "-q", "-b", "main")
    commit(parent, "initial", {
        ".gitignore": "/custom-addons/*\n",
        "README.md": "# odoo-base\n",
        ".hermes/templates/odoo-module/__manifest__.py": MANIFEST,
        ".hermes/templates/odoo-module/tests/test_{{name}}.py": "# tests of {{name}}\n",
        ".hermes/templates/odoo-module/logo.bin": "\x00\x01{{name}}",
    })
    (parent / "custom-addons").mkdir()
    return root


@pytest.fixture
def gh_state(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "gh" / "gh-state.json"
    state.parent.mkdir()
    monkeypatch.setenv("HO_GIT_ALLOW_FILE_REMOTES", "1")
    monkeypatch.setenv("HO_GH_BIN", str(FIXTURES / "fake_gh.py"))
    monkeypatch.setenv("HO_TEST_GH_STATE", str(state))
    return state


@pytest.fixture
def api(root: Path, gh_state: Path):
    client = TestClient(create_app(root, TOKEN, KEY))

    def post(path: str, body: dict):
        return client.post(path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    return post


def plan(api, name="job_alerts"):
    response = api("/v1/children/plan", {"path": "odoo-base", "template": ".hermes/templates/odoo-module",
                                         "children_path": "custom-addons", "name": name})
    return response


def token(planned: dict, *, name="job_alerts", operation=OPERATION, private=True, **changes) -> dict:
    subject = {"operation_id": operation, "parent": "odoo-base", "parent_sha": planned["parent_sha"],
               "template_path": ".hermes/templates/odoo-module", "template_tree": planned["template_tree"],
               "children_path": "custom-addons", "name": name,
               "params": {"title": "Job alerts", "description": "Alerts for new jobs", "odoo_series": "19.0"},
               "owner": "geparedesm", "repo": f"odoo-{name}", "private": private, "commit_message": "[ADD] Scaffold {name} module",
               **changes}
    return sign_child(KEY, approval_id="a1", subject=subject, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))


def publish(api, auth):
    return api("/v1/children/publish", {"path": "odoo-base", "authorization": auth})


def remote(api, auth, commit_sha):
    return api("/v1/children/remote", {"path": "odoo-base", "authorization": auth, "commit": commit_sha})


def test_plan_pins_the_parent_and_template_and_writes_nothing(api, root):
    response = plan(api)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["destination"] == "custom-addons/job_alerts" and body["files"] == 3
    assert body["parent_sha"] == sh(root / "odoo-base", "rev-parse", "HEAD")
    assert not (root / "odoo-base" / "custom-addons" / "job_alerts").exists()


def test_publish_renders_the_template_and_commits_with_the_operation_as_evidence(api, root):
    auth = token(plan(api).json())
    response = publish(api, auth)
    assert response.status_code == 200, response.text
    child = root / "odoo-base" / "custom-addons" / "job_alerts"
    assert json.loads((child / "__manifest__.py").read_text()) == {"name": "Job alerts", "summary": "Alerts for new jobs",
                                                                   "version": "19.0.1.0.0"}
    assert (child / "tests" / "test_job_alerts.py").read_text() == "# tests of job_alerts\n"
    assert (child / "logo.bin").read_bytes() == b"\x00\x01{{name}}"  # binary files are copied as they are
    message = sh(child, "log", "-1", "--format=%B")
    assert message.startswith("[ADD] Scaffold job_alerts module") and f"Hermes-Operation: {OPERATION}" in message
    assert sh(child, "branch", "--show-current") == "main" and not sh(root / "odoo-base", "status", "--porcelain")
    again = publish(api, auth)  # a retry reuses what this operation published
    assert again.json() == {**response.json(), "reused": True}


def test_publish_never_takes_over_a_directory_it_did_not_make(api, root):
    auth = token(plan(api).json())
    (root / "odoo-base" / "custom-addons" / "job_alerts").mkdir()  # a person's folder appeared meanwhile
    response = publish(api, auth)
    assert response.status_code == 409 and response.json()["error"] == "unowned"
    assert not any((root / "odoo-base" / "custom-addons" / "job_alerts").iterdir())


def test_the_destination_is_never_reached_through_a_link(api, root, tmp_path):
    parent = root / "odoo-base"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (parent / "custom-addons").rmdir()
    (parent / "custom-addons").symlink_to(elsewhere)
    assert plan(api).status_code == 409
    assert not any(elsewhere.iterdir())


def test_children_must_live_outside_the_parents_history(api, root):
    parent = root / "odoo-base"
    commit(parent, "track it", {".gitignore": "\n"})
    response = plan(api)
    assert response.status_code == 409 and "not ignored" in response.json()["message"]


def test_a_template_with_links_is_refused(api, root):
    parent = root / "odoo-base"
    (parent / ".hermes/templates/odoo-module/link").symlink_to("/etc/passwd")
    sh(parent, "add", "-A")
    sh(parent, "commit", "-q", "-m", "link")
    response = plan(api)
    assert response.status_code == 409 and "not a regular file" in response.json()["message"]


def test_an_altered_or_foreign_authorization_is_refused(api):
    planned = plan(api).json()
    auth = token(planned)
    auth["subject"]["repo"] = "odoo-other"
    assert publish(api, auth).status_code == 403
    assert publish(api, token(planned, template_tree="0" * 40)).status_code == 409  # not the approved template


def test_remote_creates_a_private_repository_and_pushes_main(api, root, gh_state):
    auth = token(plan(api).json())
    commit_sha = publish(api, auth).json()["commit"]
    response = remote(api, auth, commit_sha)
    assert response.status_code == 200, response.text
    repos = json.loads(gh_state.read_text())["repos"]
    created = repos["geparedesm/odoo-job_alerts"]
    assert created["private"] and OPERATION in created["description"]
    assert subprocess.run(["git", "-C", created["path"], "rev-parse", "main"], capture_output=True, text=True).stdout.strip() == commit_sha
    assert remote(api, auth, commit_sha).status_code == 200  # a retry is a no-op


def test_a_lost_answer_is_adopted_only_with_this_operations_evidence(api, gh_state):
    auth = token(plan(api).json())
    commit_sha = publish(api, auth).json()["commit"]
    gh_state.write_text(json.dumps({"prs": [], "calls": [], "checks": [], "logged_in": True, "lose_create_answer": True}))
    assert remote(api, auth, commit_sha).status_code == 502  # created, answer lost
    assert remote(api, auth, commit_sha).status_code == 200  # the retry finds its own repository


def test_a_foreign_repository_is_never_adopted(api, gh_state, tmp_path):
    auth = token(plan(api).json())
    commit_sha = publish(api, auth).json()["commit"]
    bare = tmp_path / "foreign.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    gh_state.write_text(json.dumps({"prs": [], "calls": [], "checks": [], "logged_in": True, "repos": {
        "geparedesm/odoo-job_alerts": {"path": str(bare), "private": True, "description": "my own repository"}}}))
    response = remote(api, auth, commit_sha)
    assert response.status_code == 409 and response.json()["error"] == "unowned"
    assert not os.listdir(bare / "refs" / "heads")  # nothing was pushed


def test_a_component_swapped_for_a_link_after_the_checks_cannot_redirect_writes(root, tmp_path):
    from git_service import children
    from git_service.repo_ops import Refused

    parent = root / "odoo-base"
    (parent / "custom-addons" / "nested").mkdir()
    fd = children._open_anchored(parent, "custom-addons/nested")  # the checks passed: real directories
    os.close(fd)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (parent / "custom-addons" / "nested").rmdir()
    (parent / "custom-addons" / "nested").symlink_to(elsewhere)  # swapped afterwards
    with pytest.raises(Refused):
        children._open_anchored(parent, "custom-addons/nested")
    assert not any(elsewhere.iterdir())
