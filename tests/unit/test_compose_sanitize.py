"""Project Compose files are input data (SECURITY_MODEL.md section 9 rule 7, NETWORK_MODEL.md N07)."""

from __future__ import annotations

import pytest

from agent_manager.compose import Limits, project_name, sanitize
from agent_manager.plan import Rejected

WS = "/projects/shop/.hermes/worktrees/t-1-verify1"
HOST = "/Users/me/HermesProjects/shop/.hermes/worktrees/t-1-verify1"
LIMITS = Limits(memory_bytes=4 * 1024**3, cpus=2.0)


def model(**db):
    return {
        "name": "shop",
        "services": {
            "db": {"image": "postgres:17", "mem_limit": "536870912", "ports": [{"target": 5432, "published": "5432"}],
                   "networks": {"default": None}, "environment": {"POSTGRES_PASSWORD": "test"},
                   "volumes": [{"type": "volume", "source": "dbdata", "target": "/var/lib/postgresql/data"},
                               {"type": "bind", "source": f"{WS}/init", "target": "/init", "read_only": True, "bind": {}}],
                   **db},
            "app": {"build": {"context": WS}, "depends_on": {"db": {"condition": "service_healthy"}}},
            "cache": {"image": "redis:8"},
        },
        "volumes": {"dbdata": {"name": "shop_dbdata"}},
        "networks": {"default": {"name": "shop_default"}},
    }


def run(m, services=("db",)):
    return sanitize(m, task="T-1", project="shop", network="ho-t-t-1-svc", services=list(services),
                    workspace_container=WS, workspace_host=HOST, limits=LIMITS)


def test_selected_services_are_confined_to_the_task_network():
    result = run(model())
    assert set(result["services"]) == {"db"} and result["name"] == project_name("T-1", "shop") == "ho-t-1-shop"
    db = result["services"]["db"]
    assert "ports" not in db  # published ports removed
    assert db["networks"] == {"ho_task": {"aliases": ["db", "db.test"]}}
    assert result["networks"] == {"ho_task": {"name": "ho-t-t-1-svc", "external": True}}
    assert db["volumes"][1]["source"] == f"{HOST}/init"  # bind rewritten to the host path of the workspace
    assert db["mem_limit"] == 536870912 and db["cpus"] == 2.0 and db["pids_limit"] == 512
    assert "no-new-privileges:true" in db["security_opt"]
    assert db["labels"]["ho.task"] == "T-1" and db["labels"]["ho.kind"] == "test-service"
    labels = {"ho.managed": "true", "ho.stack": "hermes-orchestrator", "ho.kind": "test-service", "ho.task": "T-1", "ho.project": "shop"}
    assert result["volumes"] == {"dbdata": {"labels": labels}}  # project-local volume, removed with the environment


def test_dependencies_are_included_and_limits_capped():
    m = model()
    m["services"]["app"] = {"image": "shop-app:test", "depends_on": {"db": {"condition": "service_healthy"}}, "mem_limit": "99999999999"}
    m["services"]["db"].pop("mem_limit")
    result = run(m, services=("app",))
    assert set(result["services"]) == {"app", "db"}
    assert result["services"]["app"]["mem_limit"] == LIMITS.memory_bytes  # capped at the maximum
    assert result["services"]["db"]["mem_limit"] == LIMITS.default_memory_bytes  # default when unset


@pytest.mark.parametrize("key,value", [
    ("network_mode", "host"), ("privileged", True), ("cap_add", ["SYS_ADMIN"]), ("pid", "host"), ("ipc", "host"),
    ("devices", ["/dev/kvm"]), ("volumes_from", ["other"]), ("security_opt", ["seccomp=unconfined"]),
    ("userns_mode", "host"), ("sysctls", {"net.ipv4.ip_forward": "1"}), ("runtime", "runc"),
])
def test_escapes_are_rejected_before_start(key, value):
    with pytest.raises(Rejected, match="refused"):
        run(model(**{key: value}))


@pytest.mark.parametrize("volume", [
    {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
    {"type": "bind", "source": "/projects/other/secret", "target": "/x"},
    {"type": "bind", "source": f"{WS}/../../../etc", "target": "/x"},
    {"type": "npipe", "source": "x", "target": "/x"},
])
def test_host_mounts_outside_the_workspace_are_rejected(volume):
    m = model()
    m["services"]["db"]["volumes"].append(volume)
    with pytest.raises(Rejected):
        run(m)


def test_builds_external_volumes_and_unknown_services_are_rejected():
    with pytest.raises(Rejected, match="builds"):
        run(model(), services=("app",))
    m = model()
    m["volumes"]["dbdata"] = {"external": True}
    with pytest.raises(Rejected, match="plain project volume"):
        run(m)
    with pytest.raises(Rejected, match="not defined"):
        run(model(), services=("missing",))
    m = model()
    m["secrets"] = {"key": {"file": "/etc/shadow"}}
    with pytest.raises(Rejected, match="secrets"):
        run(m)
