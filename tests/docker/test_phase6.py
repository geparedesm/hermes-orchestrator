"""Phase 6 against a real Docker daemon: project Compose test environments on the task's private
network, the verification runner, and the browser runner (MASTER_SPEC sections 50-55)."""

from __future__ import annotations

import base64
import json
import os
import shutil

import pytest

from ho_core.enums import Role

T = Role.TESTER
POSTGRES = "postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
PYTHON = "python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e"
COMPOSE = f"""
services:
  db:
    image: {POSTGRES}
    environment: {{POSTGRES_PASSWORD: test}}
    ports: ["5432:5432"]
    volumes: [dbdata:/var/lib/postgresql/data]
    healthcheck: {{test: ["CMD", "pg_isready", "-U", "postgres"], interval: 1s, retries: 30}}
  app:
    image: {PYTHON}
    command: ["sh", "-c", "mkdir -p /s && echo '<h1>Shop</h1>' > /s/index.html && cd /s && python -m http.server 8000"]
    ports: ["8000:8000"]
    depends_on: {{db: {{condition: service_healthy}}}}
    healthcheck: {{test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000')"], interval: 1s, retries: 30}}
volumes: {{dbdata: {{}}}}
"""


def compose_binary() -> str | None:
    for candidate in (shutil.which("docker-compose"), os.path.expanduser("~/.docker/cli-plugins/docker-compose"),
                      "/usr/local/lib/docker/cli-plugins/docker-compose", "/usr/libexec/docker/cli-plugins/docker-compose"):
        if candidate and os.path.exists(candidate):
            return os.path.realpath(candidate)
    return None


@pytest.fixture(autouse=True)
def compose_bin(monkeypatch):
    binary = compose_binary()
    if not binary:
        pytest.skip("Docker Compose is not installed")
    monkeypatch.setenv("HO_COMPOSE_BIN", binary)


def environment(api, task: str, compose: str = COMPOSE, services=("db", "app")):
    ws = api.projects_root / "proj-a" / ".hermes" / "worktrees" / "w1"
    (ws / "compose.yaml").write_text(compose)
    body = {"project": "proj-a", "project_path": "proj-a", "workspace": "proj-a/.hermes/worktrees/w1",
            "compose_files": ["compose.yaml"], "services": list(services), "startup_timeout": 120}
    return api.client.post(f"/v1/tasks/{task}/environment", json=body, headers=api.headers)


def runner(api, task, command, **extra):
    body = api.request(T, command, task=task, provider=None, workspace="WRITE", egress="NONE", test_services=True, **extra)
    return body


def test_project_services_run_isolated_on_the_task_network(api, client):
    task = "T-960001"
    api.tasks.add(task)
    response = environment(api, task)
    assert response.status_code == 201, response.text
    services = {s["service"]: s for s in response.json()["services"]}
    assert services["db"]["health"] == "healthy" and services["app"]["health"] == "healthy"
    for container in client.containers.list(filters={"label": [f"ho.task={task}", "ho.kind=test-service"]}):
        attrs = container.attrs
        assert set(attrs["NetworkSettings"]["Networks"]) == {"ho-t-t-960001-svc"}  # only the private task network
        assert not any(attrs["NetworkSettings"]["Ports"].get(p) for p in attrs["NetworkSettings"]["Ports"])  # no published ports
        assert "no-new-privileges:true" in attrs["HostConfig"]["SecurityOpt"] and not attrs["HostConfig"]["Privileged"]
        assert attrs["HostConfig"]["Memory"] > 0 and attrs["HostConfig"]["PidsLimit"] == 512
    network = client.networks.get("ho-t-t-960001-svc")
    assert network.attrs["Internal"] is True

    # A test runner on the same network reaches the services, but not the Internet.
    body = runner(api, task, """
        timeout 5 bash -c '</dev/tcp/db/5432' && echo db=reached || echo db=blocked
        curl -s -o /dev/null -w 'app=%{http_code}\\n' --max-time 5 http://app:8000/
        curl -s -o /dev/null --max-time 5 https://example.com && echo internet=reached || echo internet=blocked""")
    logs = api.run(body)["logs"]
    assert "db=reached" in logs and "app=200" in logs and "internet=blocked" in logs

    # Another task cannot reach these services.
    other = runner(api, "T-960002", "timeout 5 bash -c '</dev/tcp/db/5432' && echo db=reached || echo db=blocked")
    api.tasks.add("T-960002")
    assert "db=blocked" in api.run(other)["logs"]

    # Test services are removed with their volumes; the task network stays until the task ends.
    assert api.client.delete(f"/v1/tasks/{task}/environment?services_only=true", headers=api.headers).status_code == 200
    assert not client.containers.list(all=True, filters={"label": [f"ho.task={task}", "ho.kind=test-service"]})
    assert not [v for v in client.volumes.list() if v.name.startswith("ho-t-960001-proj-a")]
    assert client.networks.get("ho-t-t-960001-svc")
    api.client.delete(f"/v1/tasks/{task}/environment", headers=api.headers)
    assert not client.networks.list(names=["ho-t-t-960001-svc"])


@pytest.mark.parametrize("patch", ["network_mode: host", "privileged: true", "cap_add: [SYS_ADMIN]",
                                   "volumes: ['/var/run/docker.sock:/var/run/docker.sock']", "build: ."])
def test_unsafe_compose_is_refused_before_start(api, client, patch):
    task = "T-960003"
    api.tasks.add(task)
    compose = f"services:\n  db:\n    image: {POSTGRES}\n    {patch}\n"
    response = environment(api, task, compose, services=("db",))
    assert response.status_code == 403 and "refused" in response.json()["message"]
    assert not client.containers.list(all=True, filters={"label": f"ho.task={task}"})


def test_verification_runner_records_every_step(api):
    body = runner(api, "T-960004", "true")
    body["command"] = ["/opt/ho/bin/ho-verify"]
    body["inputs"] = {
        "step-10-build.sh": "echo building",
        "step-20-test.sh": "if [ -e /tmp/second ]; then echo passed; else touch /tmp/second; echo flaky; exit 1; fi",
        "step-30-lint.sh": "echo 'lint error' >&2; exit 3",
        "retries": "1",
    }
    body["grant"]["capabilities"]["network"]["test_services"] = False
    result = api.run(body)
    assert result["status"]["exit_code"] == 1  # a step failed
    report = json.loads(base64.b64decode(result["files"]["test_results.json"]))
    steps = {s["name"]: s for s in report["steps"]}
    assert steps["build"]["status"] == "PASSED"
    assert steps["test"]["status"] == "PASSED" and steps["test"]["attempts"] == 2  # flaky, passed on retry
    assert steps["lint"]["status"] == "FAILED" and steps["lint"]["exit_code"] == 3
    assert b"lint error" in base64.b64decode(result["files"]["steps/lint.log"])


def test_browser_runner_validates_the_app(api, client):
    task = "T-960005"
    api.tasks.add(task)
    assert environment(api, task).status_code == 201
    body = api.request(Role.BROWSER, "true", task=task, provider=None, workspace="READ", egress="NONE", test_services=True)
    body.update(image="browser-runner", command=["/opt/ho/bin/ho-verify"], inputs={
        "step-10-browser.sh": "python3 /opt/ho/bin/ho-browser-check",
        "browser.json": json.dumps({"base_url": "http://app:8000", "paths": ["/", "/missing"]})})
    result = api.run(body, timeout=180)
    browser = json.loads(base64.b64decode(result["files"]["browser/results.json"]))
    pages = {p["url"]: p for p in browser["pages"]}
    assert pages["http://app.test:8000/"]["passed"] and pages["http://app.test:8000/"]["status"] == 200
    assert pages["http://app.test:8000/missing"]["status"] == 404 and not browser["passed"]
    assert "browser/page0.png" in result["files"] and "browser/page0-trace.zip" in result["files"]
    host_config = client.containers.get(f"ho-w-{body['execution_id'].replace('-', '')[-12:]}").attrs["HostConfig"]
    assert host_config["ReadonlyRootfs"] and host_config["CapDrop"] == ["ALL"]
    api.client.delete(f"/v1/tasks/{task}/environment", headers=api.headers)


