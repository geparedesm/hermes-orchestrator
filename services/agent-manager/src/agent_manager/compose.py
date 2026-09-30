"""Ephemeral test environments from a project's own Compose files
(MASTER_SPEC sections 51-52, NETWORK_MODEL.md section 8, SECURITY_MODEL.md section 9 rule 7).

The project's Compose files are input data. `docker compose config` renders
them (with an empty environment, so nothing from Agent Manager can be
interpolated into them), and `sanitize` turns the rendered model into the only
configuration that is started:

- only the requested services and what they depend on;
- every service on the task's private network `ho-t-<task>-svc` and nothing else;
- published ports removed; host networking, privileges, added capabilities,
  devices, host namespaces, and builds rejected;
- bind mounts only inside the verification workspace (rewritten to host paths),
  named volumes only project-local (removed with the environment);
- memory, CPU, and process limits, `no-new-privileges`, and `ho.*` labels.

The sanitized model is passed to Compose on stdin: nothing is written into the
project, and the project's files are never modified.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .plan import Rejected

_SERVICE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
# Service keys that could reach the host or escape the container boundary.
_FORBIDDEN_KEYS = {
    "privileged": "privileged services",
    "network_mode": "network_mode (host or another container's network)",
    "pid": "sharing a process namespace",
    "ipc": "sharing an IPC namespace",
    "userns_mode": "user namespace changes",
    "uts": "sharing the host UTS namespace",
    "cap_add": "added capabilities",
    "devices": "host devices",
    "device_cgroup_rules": "device rules",
    "cgroup_parent": "cgroup changes",
    "cgroup": "cgroup changes",
    "volumes_from": "volumes from other containers",
    "external_links": "links outside the project",
    "runtime": "alternative runtimes",
    "isolation": "isolation changes",
    "gpus": "GPU access",
    "sysctls": "kernel parameters",
    "build": "image builds (use prebuilt images for test services)",
    "develop": "file watching",
    "provider": "external providers",
    "extends": "unresolved extends",
}
_DROPPED_KEYS = ("ports", "container_name", "networks", "env_file", "hostname", "domainname", "mac_address",
                 "links", "restart", "deploy", "profiles", "logging")


@dataclass(frozen=True)
class Limits:
    memory_bytes: int  # maximum per service
    cpus: float
    pids: int = 512
    default_memory_bytes: int = 1024**3  # for services that set no mem_limit


def project_name(task: str, project: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", f"ho-{task.lower()}-{project.lower()}")[:63]


def _closure(services: dict[str, Any], wanted: list[str]) -> list[str]:
    selected: list[str] = []
    pending = list(wanted)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        if name not in services:
            raise Rejected(f"service {name!r} is not defined in the project's Compose files")
        selected.append(name)
        depends = services[name].get("depends_on") or {}
        pending.extend(depends if isinstance(depends, (list, dict)) else [])
    return sorted(selected)


def _bind_source(source: str, workspace_container: str, workspace_host: str) -> str:
    path = PurePosixPath(source)
    root = PurePosixPath(workspace_container)
    if ".." in path.parts or not path.is_absolute() or not (path == root or root in path.parents):
        raise Rejected(f"bind mount {source} is outside the task workspace")
    return str(PurePosixPath(workspace_host) / path.relative_to(root))


def sanitize(model: dict[str, Any], *, task: str, project: str, network: str, services: list[str],
             workspace_container: str, workspace_host: str, limits: Limits) -> dict[str, Any]:
    """Return the Compose model to start, or raise Rejected with every problem found."""
    all_services = model.get("services") or {}
    if not isinstance(all_services, dict) or not all_services:
        raise Rejected("the Compose files define no services")
    wanted = services or sorted(all_services)
    for name in wanted:
        if not _SERVICE.match(name):
            raise Rejected(f"invalid service name {name!r}")
    selected = _closure(all_services, wanted)
    problems: list[str] = []
    labels = {"ho.managed": "true", "ho.kind": "test-service", "ho.task": task, "ho.project": project}
    top_volumes = model.get("volumes") or {}
    out_services: dict[str, Any] = {}
    used_volumes: set[str] = set()

    for name in selected:
        service = copy.deepcopy(all_services[name])
        for key, reason in _FORBIDDEN_KEYS.items():
            if service.get(key) not in (None, [], {}, ""):
                problems.append(f"{name}: {reason} are not allowed")
        for option in service.get("security_opt") or []:
            if "unconfined" in str(option) or str(option).startswith(("seccomp", "apparmor", "label")):
                problems.append(f"{name}: security_opt {option} is not allowed")
        if not service.get("image"):
            problems.append(f"{name}: an image is required")
        for key in _DROPPED_KEYS:
            service.pop(key, None)
        service.pop("build", None)

        mounts = []
        for volume in service.get("volumes") or []:
            if not isinstance(volume, dict):
                problems.append(f"{name}: unsupported volume syntax {volume!r}")
                continue
            kind = volume.get("type")
            if kind == "bind":
                try:
                    volume["source"] = _bind_source(str(volume.get("source", "")), workspace_container, workspace_host)
                except Rejected as exc:
                    problems.append(f"{name}: {exc}")
                    continue
                volume.pop("bind", None)
            elif kind == "volume":
                source = volume.get("source")
                if source:
                    spec = top_volumes.get(source) or {}
                    if spec.get("external") or spec.get("driver") not in (None, "local") or spec.get("driver_opts"):
                        problems.append(f"{name}: volume {source} must be a plain project volume")
                        continue
                    used_volumes.add(source)
            elif kind != "tmpfs":
                problems.append(f"{name}: {kind} mounts are not allowed")
                continue
            mounts.append(volume)
        service["volumes"] = mounts

        memory = service.get("mem_limit")
        memory_bytes = int(memory) if isinstance(memory, (int, float)) or (isinstance(memory, str) and memory.isdigit()) else None
        service["mem_limit"] = min(memory_bytes or limits.default_memory_bytes, limits.memory_bytes)
        service["memswap_limit"] = service["mem_limit"]
        cpus = float(service.get("cpus") or limits.cpus)
        service["cpus"] = min(cpus, limits.cpus)
        service["pids_limit"] = min(int(service.get("pids_limit") or limits.pids), limits.pids)
        options = [o for o in service.get("security_opt") or [] if "no-new-privileges" not in str(o)]
        service["security_opt"] = [*options, "no-new-privileges:true"]
        service["labels"] = {**(service.get("labels") or {}), **labels, "ho.service": name}
        # `<name>.test` avoids HSTS-preloaded names (for example `app`) in browser tests.
        service["networks"] = {"ho_task": {"aliases": [name, f"{name}.test"]}}
        depends = service.get("depends_on")
        if isinstance(depends, dict):
            service["depends_on"] = {k: v for k, v in depends.items() if k in selected}
        out_services[name] = service

    for key in ("secrets", "configs"):
        if model.get(key):
            problems.append(f"top-level {key} are not supported in test environments")
    if problems:
        raise Rejected("the project's Compose configuration was refused: " + "; ".join(sorted(set(problems))))
    volumes = {name: {"labels": labels} for name in sorted(used_volumes)}
    return {
        "name": project_name(task, project),
        "services": out_services,
        "networks": {"ho_task": {"name": network, "external": True}},
        **({"volumes": volumes} if volumes else {}),
    }
