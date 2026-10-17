"""Git Service: new projects at the projects root from a platform template (docs/design/new-projects.md)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from git_service.app import create_app
from ho_core.gitpolicy import sign_child, sign_project

from test_git_service import FIXTURES, KEY, TOKEN, sh  # type: ignore[import-not-found]

OPERATION = "01a12bf0-0000-7000-8000-0000000000aa"
TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("HO_TEMPLATES_DIR", str(TEMPLATES))
    monkeypatch.setenv("HO_TEMPLATE_CACHE", str(tmp_path / "cache"))
    state = tmp_path / "gh" / "gh-state.json"
    state.parent.mkdir()
    monkeypatch.setenv("HO_GIT_ALLOW_FILE_REMOTES", "1")
    monkeypatch.setenv("HO_GH_BIN", str(FIXTURES / "fake_gh.py"))
    monkeypatch.setenv("HO_TEST_GH_STATE", str(state))
    return root


@pytest.fixture
def api(root: Path):
    client = TestClient(create_app(root, TOKEN, KEY))

    def post(path: str, body: dict):
        return client.post(path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    return post


def token(tree: str, *, name="rent-portal", github=True, operation=OPERATION, **changes) -> dict:
    subject = {"operation_id": operation, "template": "generic", "template_tree": tree, "name": name,
               "params": {"description": "A portal where tenants pay rent."}, "github": github, "owner": "geparedesm",
               "repo": name, "private": True, "commit_message": "[ADD] Create {name}", **changes}
    return sign_project(KEY, approval_id="a1", subject=subject, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))


def test_a_new_project_is_published_at_the_root_and_pushed(api, root, tmp_path):
    planned = api("/v1/projects/plan", {"template": "generic", "name": "rent-portal"}).json()
    assert planned["destination"] == "rent-portal" and planned["files"] == 5
    auth = token(planned["template_tree"])
    published = api("/v1/projects/publish", {"authorization": auth}).json()
    project = root / "rent-portal"
    assert published["reused"] is False and (project / ".git").is_dir()
    assert "A portal where tenants pay rent." in (project / "README.md").read_text()
    assert 'name: "rent-portal"' in (project / ".hermes" / "project.yaml").read_text()
    assert f"Hermes-Operation: {OPERATION}" in sh(project, "log", "-1", "--format=%B")
    again = api("/v1/projects/publish", {"authorization": auth}).json()  # a retry reuses its own directory
    assert again["reused"] is True and again["commit"] == published["commit"]
    pushed = api("/v1/projects/remote", {"authorization": auth, "commit": published["commit"]})
    assert pushed.status_code == 200, pushed.text
    state = json.loads((tmp_path / "gh" / "gh-state.json").read_text())
    assert "geparedesm/rent-portal" in json.dumps(state)


@pytest.mark.parametrize("case", ["exists", "link", "bad-name", "child-token", "forged", "template-changed", "no-github"])
def test_a_new_project_is_refused_without_writing(api, root, tmp_path, case):
    planned = api("/v1/projects/plan", {"template": "generic", "name": "rent-portal"}).json()
    tree = planned["template_tree"]
    if case == "exists":
        (root / "rent-portal").mkdir()
        (root / "rent-portal" / "notes.txt").write_text("mine")
    elif case == "link":
        os.symlink(tmp_path, root / "rent-portal")
    auth = {"bad-name": lambda: token(tree, name="Rent_Portal"),
            "forged": lambda: {**token(tree), "signature": "0" * 64},
            "template-changed": lambda: token("1" * 40),
            "child-token": lambda: sign_child(KEY, approval_id="a1", subject={"name": "rent-portal"},
                                              expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))}.get(case, lambda: token(tree))()
    if case == "no-github":
        commit = api("/v1/projects/publish", {"authorization": token(tree, github=False)}).json()["commit"]
        response = api("/v1/projects/remote", {"authorization": token(tree, github=False), "commit": commit})
    else:
        response = api("/v1/projects/publish", {"authorization": auth})
    assert response.status_code in (400, 403, 409, 422), response.text
    if case == "exists":
        assert sorted(os.listdir(root / "rent-portal")) == ["notes.txt"]  # nothing of the person's is touched
    if case == "link":
        assert os.listdir(tmp_path / "projects") == ["rent-portal"] and not (tmp_path / "README.md").exists()
