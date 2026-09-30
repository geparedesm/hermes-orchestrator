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

from agent_manager.app import create_app
from agent_manager.docker_ops import DockerOps
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


@pytest.fixture(scope="session")
def client() -> docker.DockerClient:
    return docker.from_env()


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


@pytest.fixture
def credential(client: docker.DockerClient) -> Iterator[str]:
    name = "cred-codex-hotest"
    try:
        client.volumes.get(name)
    except docker.errors.NotFound:
        client.volumes.create(name, labels={"ho.credential": "codex/hotest"})
    yield "hotest"


@pytest.fixture
def ops(platform: dict[str, Any], client: docker.DockerClient) -> DockerOps:
    return DockerOps(platform, ROOT / "config", client=client)


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
        body: dict[str, Any] = {
            "execution_id": execution, "task": task, "project": project, "role": role.value,
            "image": "agent-base", "command": ["bash", "-c", command], "grant": grant, "project_path": project,
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
