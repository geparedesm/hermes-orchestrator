"""New projects through the control plane: request, CREATE_PROJECT approval, durable steps, registration
(docs/design/new-projects.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture
def gh_state(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "gh" / "gh-state.json"
    state.parent.mkdir()
    monkeypatch.setenv("HO_GIT_ALLOW_FILE_REMOTES", "1")
    monkeypatch.setenv("HO_GH_BIN", str(FIXTURES / "fake_gh.py"))
    monkeypatch.setenv("HO_TEST_GH_STATE", str(state))
    monkeypatch.setenv("HO_TEMPLATES_DIR", str(TEMPLATES))
    monkeypatch.setenv("HO_TEMPLATE_CACHE", str(tmp_path / "template-cache"))
    return state


def request(api, name="rent-portal", github=True, **kw):
    return api.post("/v1/project-creations", {"name": name, "description": "A portal where tenants pay rent.",
                                              "github": github}, **kw)


def approval_of(api, action="CREATE_PROJECT") -> dict:
    """The pending approval as the Dashboard shows it (with its plain details)."""
    approvals = api.get("/v1/dashboard/summary").json()["required_actions"]["approvals"]
    return next(a for a in approvals if a["action"] == action)


def test_a_project_is_created_only_after_its_approval(api, services, projects_root, gh_state):
    response = request(api)
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "AWAITING_APPROVAL" and response.json()["github_repo"] == "geparedesm/rent-portal"
    assert not (projects_root / "rent-portal").exists()
    approval = approval_of(api)
    assert approval["details"]["headline"] == "Create the new project rent-portal with its own private GitHub repository?"
    assert api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"}).status_code == 200
    project = projects_root / "rent-portal"
    assert (project / ".git").is_dir() and "tenants pay rent" in (project / "AGENTS.md").read_text()
    assert "geparedesm/rent-portal" in json.dumps(json.loads(gh_state.read_text()))
    projects = {p["slug"]: p for p in api.get("/v1/projects").json()["projects"]}
    assert projects["rent-portal"]["status"] == "PROPOSED" and projects["rent-portal"]["parent"] is None
    assert any(a["action"] == "PROJECT_READY" for a in api.get("/v1/approvals").json()["approvals"])
    assert api.get("/v1/project-creations").json()["creations"] == []  # done: no longer listed as live


def test_without_github_nothing_is_created_on_github(api, services, projects_root, gh_state):
    request(api, github=False)
    approval = approval_of(api)
    assert approval["details"]["headline"] == "Create the new project rent-portal, without GitHub?"
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert (projects_root / "rent-portal" / ".git").is_dir()
    assert "rent-portal" not in (gh_state.read_text() if gh_state.exists() else "")
    assert any(p["slug"] == "rent-portal" for p in api.get("/v1/projects").json()["projects"])


def test_a_rejected_request_frees_the_name_and_reserved_names_are_kept(api, services, projects_root, gh_state):
    request(api)
    assert request(api).status_code == 409  # one live creation per name
    projects_root.joinpath("rent-portal").mkdir()  # someone registers a folder with that name meanwhile: refused
    (projects_root / "rent-portal" / "x").write_text("x")
    import subprocess
    subprocess.run(["git", "init", "-q", str(projects_root / "rent-portal")], check=True)
    assert api.post("/v1/projects", {"path": "rent-portal"}).status_code == 409  # reserved by the creation
    assert api.post("/v1/projects", {"path": "rent-portal", "slug": "other-name"}).status_code == 409  # its folder too
    approval = approval_of(api)
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "REJECT"})
    assert api.post("/v1/projects", {"path": "rent-portal"}).status_code == 201  # free again


def test_a_folder_that_exists_stops_the_creation_for_a_person(api, services, projects_root, gh_state):
    request(api, github=False)
    (projects_root / "rent-portal").mkdir()  # appears after the request, before the approval
    approval = approval_of(api)
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    [live] = api.get("/v1/project-creations").json()["creations"]
    assert live["state"] == "NEEDS_RECOVERY" and "not created by this operation" in live["error"]
    assert list((projects_root / "rent-portal").iterdir()) == []  # nothing of the person's is touched
    assert api.post(f"/v1/project-creations/{live['id']}/abandon", {}).json()["state"] == "FAILED"


def test_only_approvers_may_ask_for_a_project(api, services, gh_state):
    response = request(api, token=PLUGIN_TOKEN, principal="telegram:12345")
    assert response.status_code == 403, response.text
    assert request(api, name="Bad_Name").status_code == 422


def test_expired_requests_of_different_projects_stay_renewable(api, services, gh_state):
    assert request(api, name="alpha-app").status_code == 201
    second = request(api, name="beta-app")
    assert second.status_code == 201, second.text
    with services.ctx.db.transaction() as cur:  # stand-in: both approvals expired
        cur.execute("UPDATE approvals SET state = 'EXPIRED' WHERE action = 'CREATE_PROJECT'")
    expired = api.get("/v1/approvals?state=EXPIRED").json()["approvals"]
    assert sorted(a["subject"]["name"] for a in expired if a["action"] == "CREATE_PROJECT") == ["alpha-app", "beta-app"]
