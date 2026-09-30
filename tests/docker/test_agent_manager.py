"""Agent Manager against a real Docker daemon: forbidden operations must fail (MASTER_SPEC section 89)."""

from __future__ import annotations

import base64
import json
import time

import pytest

from conftest import expired  # type: ignore[import-not-found]
from ho_core.enums import Role

D = Role.DEVELOPER


def test_worker_is_hardened_and_confined(api, client, credential):
    body = api.request(
        D, """
        echo "uid=$(id -u)"
        echo hello > /workspace/from-worker.txt && echo workspace=writable
        touch /etc/probe 2>/dev/null && echo rootfs=writable || echo rootfs=read-only
        test -e /var/run/docker.sock && echo docker-sock=present || echo docker-sock=absent
        test -e /projects && echo other-projects=visible || echo other-projects=absent
        cat /workspace/../secret.txt 2>/dev/null && echo parent=readable || echo parent=unreadable
        echo '{"ok": true}' > /output/result.json
        """, workspace="WRITE", git="LOCAL_COMMIT", egress="NONE")
    body["workspace"] = "proj-a/.hermes/worktrees/w1"
    execution = body["execution_id"]
    result = api.run(body)
    logs = result["logs"]
    for expected in ("uid=10001", "workspace=writable", "rootfs=read-only", "docker-sock=absent",
                     "other-projects=absent", "parent=unreadable"):
        assert expected in logs, logs
    assert (api.projects_root / "proj-a/.hermes/worktrees/w1/from-worker.txt").read_text() == "hello\n"
    assert json.loads(base64.b64decode(result["files"]["result.json"])) == {"ok": True}

    host_config = client.containers.get(f"ho-w-{execution.replace('-', '')[-12:]}").attrs["HostConfig"]
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert host_config["Privileged"] is False
    assert host_config["Memory"] == 2 * 1024**3
    assert host_config["NanoCpus"] == 1_000_000_000
    assert host_config["PidsLimit"] == 512
    assert not any("docker.sock" in (m.get("Source") or "") for m in client.containers.get(f"ho-w-{execution.replace('-', '')[-12:]}").attrs["Mounts"])


@pytest.mark.parametrize(
    "workspace",
    [
        "proj-b/.hermes/worktrees/w1",          # another project
        "proj-a",                                # the user's main checkout
        "proj-a/.hermes/worktrees/../../..",     # traversal
        "proj-a/.hermes/worktrees/w1/../../../proj-b",
        "/var/run/docker.sock",
        "proj-a/.hermes/worktrees/missing",
    ],
)
def test_forbidden_workspaces_are_rejected(api, credential, workspace):
    body = api.request(D, "true", workspace="WRITE", egress="NONE")
    body["workspace"] = workspace
    response = api.create(body)
    assert response.status_code == 403, response.text


def test_symlinked_workspace_is_rejected(api, credential):
    link = api.projects_root / "proj-a/.hermes/worktrees/escape"
    link.symlink_to(api.projects_root / "proj-b")
    body = api.request(D, "true", workspace="WRITE", egress="NONE")
    body["workspace"] = "proj-a/.hermes/worktrees/escape"
    assert api.create(body).status_code == 403


@pytest.mark.parametrize(
    "tamper",
    [
        lambda b: b["grant"]["capabilities"].__setitem__("docker", "WRITE"),
        lambda b: b["grant"].__setitem__("expires_at", expired(-5)),
        lambda b: b.__setitem__("execution_id", "00000000-0000-4000-8000-000000000000"),
        lambda b: b.__setitem__("project", "proj-b"),
        lambda b: b["grant"]["capabilities"].__setitem__("secrets", ["proj-a/test/DB"]),
        lambda b: b.__setitem__("image", "codex-unpinned"),
        lambda b: b.__setitem__("env", {"DOCKER_HOST": "tcp://host.docker.internal:2375"}),
        lambda b: b.__setitem__("command", "rm -rf /"),
    ],
    ids=["docker-write", "expired", "wrong-execution", "wrong-project", "secret-not-in-store", "unpinned-image", "docker-host-env", "string-command"],
)
def test_invalid_requests_are_rejected(api, credential, tamper):
    body = api.request(D, "true", egress="NONE", workspace="WRITE")
    body["workspace"] = "proj-a/.hermes/worktrees/w1"
    tamper(body)
    assert api.create(body).status_code == 403


def test_unknown_request_fields_cannot_add_privileges(api, client, credential):
    body = api.request(D, "sleep 1", egress="NONE")
    body.pop("workspace")
    body.update({"privileged": True, "mounts": ["/var/run/docker.sock:/var/run/docker.sock"], "network_mode": "host"})
    assert api.create(body).status_code == 201
    attrs = client.containers.get(f"ho-w-{body['execution_id'].replace('-', '')[-12:]}").attrs
    assert attrs["HostConfig"]["Privileged"] is False
    assert attrs["HostConfig"]["NetworkMode"] != "host"
    assert not any("docker.sock" in (m.get("Source") or "") for m in attrs["Mounts"])


def test_missing_provider_credential_is_auth_required(api):
    body = api.request(D, "true", egress="NONE", identity="nobody")
    body.pop("workspace")
    assert api.create(body).status_code == 424


def test_runner_without_network_has_no_route(api):
    body = api.request(Role.TESTER, "curl -sS --max-time 5 https://example.com && echo reached || echo no-network",
                       provider=None, egress="NONE")
    body.pop("workspace")
    assert "no-network" in api.run(body)["logs"]


@pytest.mark.internet
def test_egress_goes_only_through_the_allowlisting_proxy(api, credential):
    body = api.request(
        D,
        """
        curl -sS -o /dev/null -w "allowlisted=%{http_code}\\n" --max-time 20 https://example.com
        curl -sS -o /dev/null --max-time 20 https://www.wikipedia.org 2>&1 | grep -q 403 && echo other=denied || echo other=reached
        curl -sS -o /dev/null --noproxy '*' --max-time 8 https://example.com && echo direct=reached || echo direct=blocked
        curl -sS -o /dev/null --max-time 8 https://host.docker.internal 2>&1 | grep -q 403 && echo host=denied || echo host=reached
        curl -sS -o /dev/null --max-time 8 https://169.254.169.254 2>&1 | grep -q 403 && echo metadata=denied || echo metadata=reached
        curl -s -o /dev/null -w "plain-http=%{http_code}\\n" --max-time 8 http://example.com
        getent hosts example.com >/dev/null && echo dns=resolves || echo dns=blocked
        """,
        network={"development": "restricted", "allowed_domains": ["example.com"]},
        egress="ALLOWLIST", allowed_domains=["example.com", "www.wikipedia.org"],
    )
    body.pop("workspace")
    result = api.run(body, timeout=120)
    logs = result["logs"]
    for expected in ("allowlisted=200", "other=denied", "direct=blocked", "host=denied", "metadata=denied",
                     "plain-http=405", "dns=blocked"):
        assert expected in logs, logs
    decisions = {(e.get("event"), e.get("host")) for e in result["egress"]}
    assert ("EGRESS_ALLOWED", "example.com") in decisions
    assert ("EGRESS_DENIED", "www.wikipedia.org") in decisions


def test_task_service_networks_are_isolated(api, client, credential):
    task_a, task_b = "T-9100001", "T-9100002"
    api.tasks.update({task_a, task_b})
    target = api.request(Role.TESTER, "sleep 30", task=task_a, provider=None, egress="NONE", test_services=True)
    target.pop("workspace")
    assert api.create(target).status_code == 201
    name = f"ho-w-{target['execution_id'].replace('-', '')[-12:]}"
    ip = client.containers.get(name).attrs["NetworkSettings"]["Networks"][f"ho-t-{task_a.lower()}-svc"]["IPAddress"]
    probe = "timeout 3 bash -c '</dev/tcp/{ip}/9' 2>&1 | grep -qi refused && echo reachable || echo unreachable".format(ip=ip)

    same = api.request(Role.TESTER, probe, task=task_a, provider=None, egress="NONE", test_services=True)
    same.pop("workspace")
    other = api.request(Role.TESTER, probe, task=task_b, provider=None, egress="NONE", test_services=True)
    other.pop("workspace")
    assert "reachable" in api.run(same)["logs"].split()
    assert "unreachable" in api.run(other)["logs"]


def test_machine_worker_limit_is_enforced(api, ops, credential):
    ops.platform["machine"]["max_agent_workers"] = 1
    first = api.request(D, "sleep 30", egress="NONE")
    first.pop("workspace")
    second = api.request(D, "sleep 30", egress="NONE")
    second.pop("workspace")
    assert api.create(first).status_code == 201
    response = api.create(second)
    assert response.status_code == 409 and response.json()["error"] == "capacity_exceeded"


def test_memory_capacity_is_enforced(api, ops, credential):
    ops.platform["machine"]["resource_profiles"]["LIGHT"]["memory_gb"] = 1024
    body = api.request(D, "true", egress="NONE")
    body.pop("workspace")
    response = api.create(body)
    assert response.status_code == 409 and "memory" in response.json()["message"]


def test_create_is_idempotent_and_remove_cleans_up(api, client, credential):
    body = api.request(D, "sleep 30", egress="ALLOWLIST", network={"development": "restricted"})
    body.pop("workspace")
    assert api.create(body).status_code == 201
    assert api.create(body).status_code == 201  # retry of the same execution
    execution = body["execution_id"]
    assert len(client.containers.list(filters={"label": f"ho.execution={execution}"})) == 2  # worker + proxy
    stopped = api.client.post(f"/v1/executions/{execution}/stop", json={"grace_seconds": 1}, headers=api.headers).json()
    assert stopped["state"] == "exited"
    removed = api.client.delete(f"/v1/executions/{execution}", headers=api.headers).json()
    assert removed == {"containers": 2, "networks": 1, "volumes": 1}
    assert not client.containers.list(all=True, filters={"label": f"ho.execution={execution}"})
    assert not client.volumes.list(filters={"label": f"ho.execution={execution}"})


def test_reaper_stops_workers_whose_grant_expired(api, ops, credential):
    body = api.request(D, "sleep 60", egress="NONE", grant_overrides={"expires_at": expired(3)})
    body.pop("workspace")
    assert api.create(body).status_code == 201
    time.sleep(4)
    assert body["execution_id"] in ops.reap_expired()
    assert api.wait(body["execution_id"], timeout=20)["state"] == "exited"
