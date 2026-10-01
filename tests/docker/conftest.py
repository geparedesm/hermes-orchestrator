"""Fixtures for tests that drive a real Docker daemon through Agent Manager.

Requires Docker, the images from scripts/build-images.sh (config/images.lock.yaml),
and HO_TEST_DOCKER=1. Some tests also need Internet access (marked `internet`).
"""

from __future__ import annotations

import copy
import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import docker
import pytest
from fastapi.testclient import TestClient

# A stack of its own: the operator's live stack on the same Docker host must not take these workers for its orphans.
os.environ.setdefault("HO_STACK", "ho-test-docker")

from agent_manager.app import create_app  # noqa: E402  (reads HO_STACK at import)
from agent_manager.docker_ops import DockerOps
from agent_manager.secrets import SecretStore
from ho_core.config import build_project_config, load_platform_config
from ho_core.enums import Role
from ho_core.policy.engine import GrantRequest, evaluate_grant

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "agent-manager-test-token-0123456789"
ENABLED = os.environ.get("HO_TEST_DOCKER") == "1" and (ROOT / "config" / "images.lock.yaml").is_file()


def pytest_collection_modifyitems(items):
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="set HO_TEST_DOCKER=1 and run scripts/build-images.sh (make test-docker)")
    for item in items:
        if "tests/docker" in str(item.fspath):
            item.add_marker(skip)


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    return path


TEST_IDENTITIES = ("hotest", "bogus", "empty", "refresh")


@pytest.fixture(scope="session")
def client() -> Iterator[docker.DockerClient]:
    docker_client = docker.from_env()
    yield docker_client
    # Remove the throwaway credential volumes these tests create; never the operator's identities.
    for volume in docker_client.volumes.list(filters={"label": "ho.credential"}):
        if volume.name.rsplit("-", 1)[-1] in TEST_IDENTITIES:
            volume.remove(force=True)


@pytest.fixture
def platform() -> dict[str, Any]:
    config = load_platform_config(ROOT / "config", "mac-m2-pro")
    return copy.deepcopy(config)


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = (tmp_path / "HermesProjects").resolve()
    for name in ("proj-a", "proj-b"):
        repo = git_repo(root / name)
        (repo / ".hermes" / "worktrees" / "w1").mkdir(parents=True)
        (repo / "secret.txt").write_text(f"{name} private data")
    return root


def credential_volume(client: docker.DockerClient, provider: str, identity: str, files: dict[str, str] | None = None) -> str:
    """Create (or reset) a credential volume like scripts/auth-login.sh does, optionally with files."""
    name = f"cred-{provider}-{identity}"
    try:
        client.volumes.get(name).remove(force=True)
    except docker.errors.NotFound:
        pass
    client.volumes.create(name, labels={"ho.credential": f"{provider}/{identity}"})
    script = "chown 10001:10001 /c && chmod 700 /c"
    for path, content in (files or {}).items():
        script += f" && printf %s '{content}' > /c/{path} && chown 10001:10001 /c/{path} && chmod 600 /c/{path}"
    client.containers.run("debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251",
                          ["sh", "-c", script], volumes={name: {"bind": "/c", "mode": "rw"}}, remove=True, network_mode="none")
    return name


@pytest.fixture
def credential(client: docker.DockerClient) -> Iterator[str]:
    credential_volume(client, "codex", "hotest")
    yield "hotest"


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    root = tmp_path / "project-secrets"
    (root / "proj-a" / "test").mkdir(parents=True)
    return root


@pytest.fixture
def ops(platform: dict[str, Any], client: docker.DockerClient, secrets_dir: Path) -> DockerOps:
    return DockerOps(platform, ROOT / "config", client=client, secrets=SecretStore(secrets_dir))


@pytest.fixture
def api(ops: DockerOps, projects_root: Path, client: docker.DockerClient) -> Iterator["Api"]:
    app = create_app(ops, token=TOKEN, projects_root=projects_root, projects_root_host=str(projects_root), reap_seconds=0)
    with TestClient(app) as test_client:
        wrapper = Api(test_client, projects_root, ops.platform)
        yield wrapper
    # Remove everything these tests created.
    for task in wrapper.tasks:
        for container in client.containers.list(all=True, filters={"label": f"ho.task={task}"}):
            container.remove(force=True, v=True)
        for volume in client.volumes.list(filters={"label": f"ho.task={task}"}):
            volume.remove(force=True)
        for network in client.networks.list(filters={"label": f"ho.task={task}"}):
            network.remove()


class Api:
    def __init__(self, client: TestClient, projects_root: Path, platform: dict[str, Any]) -> None:
        self.client = client
        self.projects_root = projects_root
        self.platform = platform
        self.tasks: set[str] = set()
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def request(
        self,
        role: Role,
        command: str,
        *,
        task: str | None = None,
        project: str = "proj-a",
        workspace_path: str | None = "proj-a/.hermes/worktrees/w1",
        provider: str | None = "codex",
        identity: str = "hotest",
        network: dict[str, Any] | None = None,
        grant_overrides: dict[str, Any] | None = None,
        **capabilities: Any,
    ) -> dict[str, Any]:
        task = task or f"T-9{uuid.uuid4().int % 10**6:06d}"
        self.tasks.add(task)
        execution = str(uuid.uuid4())
        project_yaml = {"version": 1, "project": {"name": project}, "network": network or {"development": "standard"}}
        config = build_project_config(self.platform, project_yaml).data
        grant, _ = evaluate_grant(
            GrantRequest(grant_id=f"G-{execution[:8]}", project=project, task=task, execution=execution,
                         worker=f"w-{task[2:]}-{execution[:4]}", role=role, provider=provider,
                         provider_identity=identity, resource_profile="LIGHT", timeout_minutes=10, **capabilities),
            config, self.platform, now=datetime.now(timezone.utc),
        )
        for key, value in (grant_overrides or {}).items():
            grant[key] = value
        # Provider executions run in that provider's image; runners in agent-base.
        image = f"{grant['provider_credential']['provider']}-generic" if grant["provider_credential"] else "agent-base"
        body: dict[str, Any] = {
            "execution_id": execution, "task": task, "project": project, "role": role.value,
            "image": image, "command": ["bash", "-c", command], "grant": grant, "project_path": project,
        }
        if workspace_path:
            body["workspace"] = workspace_path
        return body

    def create(self, body: dict[str, Any]):
        return self.client.post("/v1/executions", json=body, headers=self.headers)

    def wait(self, execution: str, timeout: float = 60) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.client.get(f"/v1/executions/{execution}", headers=self.headers).json()
            if status["state"] in ("exited", "absent"):
                return status
            time.sleep(0.5)
        raise TimeoutError(execution)

    def run(self, body: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        response = self.create(body)
        assert response.status_code == 201, response.text
        status = self.wait(body["execution_id"], timeout)
        collected = self.client.post(f"/v1/executions/{body['execution_id']}/collect", headers=self.headers).json()
        return {"status": status, **collected}


def expired(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
