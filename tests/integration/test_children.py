"""Child projects through the control plane: request, CREATE_CHILD approval, durable steps, registration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import PLUGIN_TOKEN, git_repo  # type: ignore[import-not-found]
from test_git import sh  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PARENT_YAML = """\
version: 1
project: {name: odoo-base}
commands: {test: "python -m unittest"}
children:
  path: custom-addons
  template: .hermes/templates/odoo-module
  label: module
  variables: {odoo_series: "19.0"}
  github: {owner: geparedesm, repo_name: "odoo-{name}", private: true}
"""
CHILD_YAML = """\
version: 1
project: {name: "{{name}}"}
commands: {test: "python -m unittest"}
"""


@pytest.fixture
def gh_state(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "gh" / "gh-state.json"
    state.parent.mkdir()
    monkeypatch.setenv("HO_GIT_ALLOW_FILE_REMOTES", "1")
    monkeypatch.setenv("HO_GH_BIN", str(FIXTURES / "fake_gh.py"))
    monkeypatch.setenv("HO_TEST_GH_STATE", str(state))
    return state


@pytest.fixture
def parent(api, projects_root, gh_state) -> Path:
    repo = git_repo(projects_root / "odoo-base", {
        ".gitignore": "/custom-addons/*\n", ".hermes/project.yaml": PARENT_YAML, "README.md": "# odoo-base\n",
        ".hermes/templates/odoo-module/.hermes/project.yaml": CHILD_YAML,
        ".hermes/templates/odoo-module/__manifest__.py": '{"name": "{{title}}", "version": "{{odoo_series}}.1.0.0"}\n',
        ".hermes/templates/odoo-module/README.md": "# {{title}}\n\n{{description}}\n"})
    (repo / "custom-addons").mkdir()
    assert api.post("/v1/projects", {"path": "odoo-base"}).status_code == 201
    approval = api.post("/v1/projects/odoo-base/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    return repo


def request(api, name="job_alerts", **kw):
    return api.post("/v1/projects/odoo-base/children", {"name": name, "title": "Job alerts", "description": "Alerts"}, **kw)


def creation(api) -> dict:
    return api.get("/v1/projects/odoo-base/children").json()["creations"][0]


def test_a_module_is_created_only_after_its_approval(api, services, parent, gh_state):
    response = request(api)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "AWAITING_APPROVAL" and body["github_repo"] == "geparedesm/odoo-job_alerts"
    child = parent / "custom-addons" / "job_alerts"
    assert not child.exists() and "repos" not in json.loads(gh_state.read_text() if gh_state.exists() else "{}")
    [approval] = [a for a in api.get("/v1/approvals").json()["approvals"] if a["action"] == "CREATE_CHILD"]
    assert "geparedesm/odoo-job_alerts" in approval["summary"]

    assert api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"}).status_code == 200
    done = creation(api)
    assert done["state"] == "DONE" and done["step"] == "REGISTERED" and done["child"] == "job-alerts"
    assert json.loads((child / "__manifest__.py").read_text()) == {"name": "Job alerts", "version": "19.0.1.0.0"}
    assert sh(child, "log", "-1", "--format=%s") == "[ADD] Scaffold job_alerts"
    assert json.loads(gh_state.read_text())["repos"]["geparedesm/odoo-job_alerts"]["private"] is True
    projects = {p["slug"]: p for p in api.get("/v1/projects").json()["projects"]}
    assert projects["job-alerts"]["parent"] == "odoo-base" and projects["odoo-base"]["children"] == {"label": "module"}
    assert projects["job-alerts"]["status"] == "PROPOSED"  # you approve its configuration, as for any project
    assert any(a["action"] == "PROJECT_READY" for a in api.get("/v1/approvals").json()["approvals"])


def test_a_rejected_request_writes_nothing(api, parent):
    request(api)
    [approval] = [a for a in api.get("/v1/approvals").json()["approvals"] if a["action"] == "CREATE_CHILD"]
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "REJECT"})
    assert creation(api)["state"] == "REJECTED" and not (parent / "custom-addons" / "job_alerts").exists()
    assert request(api).status_code == 201  # the name is free again


def test_a_parent_that_changed_after_the_request_invalidates_it(api, parent):
    request(api)
    sh(parent, "commit", "-q", "--allow-empty", "-m", "moved on")
    [approval] = [a for a in api.get("/v1/approvals").json()["approvals"] if a["action"] == "CREATE_CHILD"]
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert creation(api)["state"] == "INVALIDATED" and not (parent / "custom-addons" / "job_alerts").exists()


def test_a_folder_that_is_not_ours_stops_for_a_person_then_resumes(api, parent):
    request(api)
    foreign = parent / "custom-addons" / "job_alerts"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("mine\n")
    [approval] = [a for a in api.get("/v1/approvals").json()["approvals"] if a["action"] == "CREATE_CHILD"]
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    stopped = creation(api)
    assert stopped["state"] == "NEEDS_RECOVERY" and "not created by this operation" in stopped["error"]
    assert (foreign / "notes.txt").read_text() == "mine\n"  # never touched
    shutil.rmtree(foreign)  # the person moved it away
    resumed = api.post(f"/v1/projects/odoo-base/children/{stopped['id']}/retry")
    assert resumed.status_code == 200, resumed.text
    assert creation(api)["state"] == "DONE"


def test_only_people_who_approve_may_create_modules(api, parent):
    response = request(api, token=PLUGIN_TOKEN, principal="telegram:999")
    assert response.status_code in (403, 401), response.text


def test_a_project_without_children_cannot_create_them(api, projects_root):
    git_repo(projects_root / "plain", {".hermes/project.yaml": "version: 1\nproject: {name: plain}\n", "README.md": "x\n"})
    api.post("/v1/projects", {"path": "plain"})
    approval = api.post("/v1/projects/plain/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    response = api.post("/v1/projects/plain/children", {"name": "x_mod", "title": "X"})
    assert response.status_code == 409 and "declare `children`" in response.json()["message"]
