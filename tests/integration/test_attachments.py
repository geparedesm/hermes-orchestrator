"""Task attachments end to end: upload, storage, delivery to agents, failures, retries, downloads."""

from __future__ import annotations

import base64

import pytest

import test_orchestration as orch  # type: ignore[import-not-found]
from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]
from ho_core.hashing import sha256_hex

pytestmark = [pytest.mark.integration, pytest.mark.orchestration]
agents = pytest.fixture(orch.agents.__wrapped__)
repo = pytest.fixture(orch.repo.__wrapped__)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
SPEC = "# Diseño\n\nLa cabecera dice «Hola».\n".encode()


def files(**named: bytes) -> list[dict[str, str]]:
    return [{"name": n.replace("_", "."), "content_base64": base64.b64encode(c).decode()} for n, c in named.items()]


@pytest.fixture
def project(api, services, repo, agents) -> str:
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    return "demo"


def create(api, **extra):
    return api.post("/v1/tasks", {"project": "demo", "request": "Build the page in the design", **extra})


def test_attachments_are_stored_and_reach_every_agent_of_the_task(api, services, agents, project):
    response = create(api, attachments=files(design_png=PNG, spec_md=SPEC))
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    listed = api.get(f"/v1/tasks/{key}/attachments").json()["attachments"]
    assert [(a["name"], a["media_type"], a["size_bytes"]) for a in listed] == [("design.png", "image/png", len(PNG)),
                                                                               ("spec.md", "text/markdown", len(SPEC))]
    services.scheduler.run_once()  # the orchestrator's first step
    [orchestrator] = orch.executions(services, key, "ORCHESTRATOR")
    # PostgreSQL keeps references only; the content went to Agent Manager with the request.
    refs = orchestrator["spec"]["attachments"]
    assert [r["name"] for r in refs] == ["design.png", "spec.md"] and "attachment_files" not in orchestrator["spec"]
    assert all("content" not in str(r) for r in refs) and refs[0]["sha256"] == sha256_hex(PNG)
    sent = agents.specs[str(orchestrator["id"])]
    assert {n: base64.b64decode(c) for n, c in sent["attachment_files"].items()} == {"design.png": PNG, "spec.md": SPEC}
    prompt = sent["inputs"]["prompt.md"]
    assert "/run/ho-input/attachments/design.png (image/png" in prompt and "not instructions" in prompt
    assert "--add-dir" in sent["command"] and "/run/ho-input/attachments" in sent["command"]
    # Developers of the task get the same files.
    orch.plan_and_start(services, agents, key)
    [developer] = orch.active(services, key, "DEVELOPER")
    assert set(agents.specs[str(developer["id"])]["attachment_files"]) == {"design.png", "spec.md"}
    timeline = api.get(f"/v1/dashboard/tasks/{key}").json()
    assert [a["name"] for a in timeline["attachments"]] == ["design.png", "spec.md"]


def test_invalid_attachments_create_nothing(api, services, project):
    before = orch.q(services, "SELECT count(*) AS n FROM tasks")[0]["n"]
    for bad, message in ((files(run_sh=b"echo"), "not allowed"), (files(fake_png=b"<svg/>"), "not a PNG"),
                         ([{"name": "a.md", "content_base64": "%%%"}], "base64"),
                         (files(**{f"f{i}_md": b"x" for i in range(11)}), "at most 10"),
                         ([{"name": "a.md"}], "content_base64")):
        response = create(api, attachments=bad)
        assert response.status_code == 422 and message in response.text, response.text
    assert orch.q(services, "SELECT count(*) AS n FROM tasks")[0]["n"] == before


def test_a_replayed_creation_creates_one_task_with_one_set_of_files(api, services, project):
    body = {"project": "demo", "request": "Build it", "attachments": files(spec_md=SPEC)}
    first = api.post("/v1/tasks", body, key="replay-key-0001")
    again = api.post("/v1/tasks", body, key="replay-key-0001")
    assert first.json()["key"] == again.json()["key"]
    assert orch.q(services, "SELECT count(*) AS n FROM task_attachments")[0]["n"] == 1


def test_a_lost_attachment_fails_the_execution_and_says_which(api, services, agents, project):
    key = create(api, attachments=files(spec_md=SPEC)).json()["key"]
    [path] = [r["path"] for r in orch.q(services, "SELECT a.path FROM artifacts a JOIN task_attachments t ON t.artifact_id = a.id")]
    (services.ctx.artifacts.root / path).write_bytes(b"# altered\n")
    services.scheduler.run_once()
    [orchestrator] = orch.executions(services, key, "ORCHESTRATOR")
    assert orchestrator["state"] == "FAILED" and "spec.md does not match its recorded SHA-256" in orchestrator["failure_reason"]
    assert str(orchestrator["id"]) not in agents.specs  # nothing was sent


def test_a_retried_task_keeps_its_attachments(api, services, project):
    key = create(api, attachments=files(spec_md=SPEC)).json()["key"]
    api.post(f"/v1/tasks/{key}/cancel")
    retried = api.post(f"/v1/tasks/{key}/retry").json()["key"]
    names = [a["name"] for a in api.get(f"/v1/tasks/{retried}/attachments").json()["attachments"]]
    assert retried != key and names == ["spec.md"]


def test_downloads_are_files_for_any_caller_of_the_api(api, services, project):
    key = create(api, attachments=files(spec_md=SPEC)).json()["key"]
    [attachment] = api.get(f"/v1/tasks/{key}/attachments").json()["attachments"]
    response = api.get(f"/v1/tasks/{key}/attachments/{attachment['id']}", token=PLUGIN_TOKEN, principal="dashboard:operator")
    assert response.content == SPEC and response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="spec.md"'
    assert response.headers["x-content-type-options"] == "nosniff"
    other = create(api).json()["key"]
    assert api.get(f"/v1/tasks/{other}/attachments/{attachment['id']}").status_code == 404  # bound to its task


def test_oversized_requests_are_refused_before_parsing(api, services, project):
    response = api.client.post("/v1/tasks", content=b"{" + b" " * (40 * 1024 * 1024) + b"}",
                               headers={**api.headers(), "Content-Type": "application/json"})
    assert response.status_code == 413
