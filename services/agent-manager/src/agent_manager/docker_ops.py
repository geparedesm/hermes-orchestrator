"""Docker operations for executions. The only code in the platform that creates containers.

Every object carries `ho.*` labels, so the Docker daemon itself is the record
of what exists; Agent Manager keeps no other state and can restart safely.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import docker
from docker.errors import APIError, NotFound
from docker.types import Mount as DockerMount

from . import compose, images
from .plan import INPUT_MOUNT, PROXY_PORT, SECRETS_MOUNT, WORKER_UID, ContainerPlan, Rejected, resolve_workspace
from .secrets import SecretStore

log = logging.getLogger(__name__)

EGRESS_NETWORK = "ho-egress"
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_FILES = 200
MAX_LOG_BYTES = 256 * 1024
PROXY_READY_SECONDS = 15
_SECURITY = {"read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "privileged": False}
_AGENT_ROLES = ("ORCHESTRATOR", "DEVELOPER", "REVIEWER")


class CapacityExceeded(RuntimeError):
    pass


class CredentialMissing(RuntimeError):
    pass


class EnvironmentFailed(RuntimeError):
    """Test services did not start (image pull, health check, or Compose error)."""


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid = len(content), 0o644, WORKER_UID, WORKER_UID
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@dataclass
class ExecutionStatus:
    execution: str
    state: str  # absent | created | running | exited
    exit_code: int | None = None
    oom_killed: bool = False
    started_at: str | None = None
    finished_at: str | None = None

    def as_json(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DockerOps:
    def __init__(self, platform: dict[str, Any], config_dir: Path, client: docker.DockerClient | None = None,
                 secrets: SecretStore | None = None) -> None:
        self.platform = platform
        self.config_dir = config_dir
        self.client = client or docker.from_env(timeout=60)
        self.secrets = secrets or SecretStore(None)
        self._lock = threading.Lock()  # serialize capacity checks and creation

    # ----------------------------------------------------------------- helpers

    def _find(self, name: str):
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def _labels(self, plan: ContainerPlan, kind: str) -> dict[str, str]:
        return {**plan.labels, "ho.kind": kind}

    def _ensure_network(self, name: str, labels: dict[str, str], *, internal: bool) -> Any:
        try:
            network = self.client.networks.get(name)
        except NotFound:
            return self.client.networks.create(name, driver="bridge", internal=internal, labels=labels, check_duplicate=True)
        if network.attrs.get("Internal") != internal or network.attrs.get("Labels", {}).get("ho.managed") != "true":
            raise Rejected(f"network {name} exists but is not a platform network with the expected isolation")
        return network

    # ---------------------------------------------------------------- capacity

    def _running_workers(self) -> list[Any]:
        return self.client.containers.list(filters={"label": ["ho.managed=true", "ho.kind=worker"]})

    def capacity(self) -> dict[str, Any]:
        workers = self._running_workers()
        agents = [c for c in workers if c.labels.get("ho.role") in _AGENT_ROLES]
        used = sum(int(c.attrs["HostConfig"].get("Memory") or 0) for c in self.client.containers.list(filters={"label": "ho.managed=true"}))
        total = int(self.client.info()["MemTotal"])
        reserve = int(float(self.platform["machine"].get("docker_memory_reserve_gb", 2)) * 1024**3)
        return {
            "agent_workers": len(agents),
            "max_agent_workers": int(self.platform["machine"]["max_agent_workers"]),
            "workers": len(workers),
            "memory_limit_bytes_in_use": used,
            "memory_available_for_executions": max(0, total - reserve - used),
            "docker_memory_total": total,
        }

    def _check_capacity(self, plan: ContainerPlan) -> None:
        cap = self.capacity()
        if plan.role in _AGENT_ROLES and cap["agent_workers"] >= cap["max_agent_workers"]:
            raise CapacityExceeded(f"{cap['agent_workers']} agent workers running; machine maximum is {cap['max_agent_workers']}")
        proxy_memory = 64 * 1024**2 if plan.egress != "NONE" else 0
        if plan.memory_bytes + proxy_memory > cap["memory_available_for_executions"]:
            raise CapacityExceeded(
                f"not enough Docker memory: needs {plan.memory_bytes // 1024**2} MiB, "
                f"{cap['memory_available_for_executions'] // 1024**2} MiB available after the reserve"
            )

    # ------------------------------------------------------------------ create

    def create(self, plan: ContainerPlan) -> ExecutionStatus:
        existing = self._find(plan.worker_name)
        if existing is not None:
            if existing.labels.get("ho.execution") != plan.execution:
                raise Rejected(f"container name {plan.worker_name} belongs to another execution")
            return self.status(plan.execution)  # idempotent retry

        image_id = images.resolve(self.config_dir, plan.image)
        try:
            image = self.client.images.get(image_id)
        except NotFound as exc:
            raise images.ImageNotAllowed(f"pinned image {plan.image} ({image_id[:19]}) is not present locally") from exc
        if not image.labels.get("org.hermes-orchestrator.role"):
            raise images.ImageNotAllowed(f"image {plan.image} is not a platform image")
        if plan.credential_volume:
            try:
                volume = self.client.volumes.get(plan.credential_volume)
            except NotFound as exc:
                raise CredentialMissing(f"provider credential {plan.credential_volume} is not set up (AUTH_REQUIRED)") from exc
            if volume.attrs.get("Labels", {}).get("ho.credential") is None:
                raise CredentialMissing(f"volume {plan.credential_volume} is not a platform credential volume")

        # Read granted secret values now, so a missing secret fails before anything is created.
        secret_values = {ref: self.secrets.read(ref) for ref in plan.secrets}

        with self._lock:
            self._check_capacity(plan)
            created: list[Any] = []
            try:
                env = dict(plan.env)
                env.update({name: secret_values[ref] for ref, (name, delivery) in plan.secrets.items() if delivery == "env"})
                file_secrets = {name: secret_values[ref] for ref, (name, delivery) in plan.secrets.items() if delivery == "file"}
                networks: list[str] = []
                if plan.egress != "NONE":
                    exec_net = self._ensure_network(plan.execution_network, self._labels(plan, "network"), internal=True)
                    created.append(exec_net)
                    proxy = self._start_proxy(plan)
                    created.append(proxy)
                    proxy.reload()
                    proxy_ip = proxy.attrs["NetworkSettings"]["Networks"][plan.execution_network]["IPAddress"]
                    url = f"http://{proxy_ip}:{PROXY_PORT}"
                    env.update({"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url,
                                "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"})
                    networks.append(plan.execution_network)
                if plan.test_services:
                    svc_labels = {k: v for k, v in self._labels(plan, "network").items() if k in ("ho.managed", "ho.kind", "ho.task", "ho.project")}
                    self._ensure_network(plan.service_network, svc_labels, internal=True)
                    networks.append(plan.service_network)

                self.client.volumes.create(plan.output_volume, labels=self._labels(plan, "output"))
                mounts = [
                    DockerMount(m.target, m.source, type=m.kind, read_only=m.read_only)
                    for m in plan.mounts
                ]
                if plan.inputs:
                    self.client.volumes.create(plan.input_volume, labels=self._labels(plan, "input"))
                    mounts.append(DockerMount(INPUT_MOUNT, plan.input_volume, type="volume"))
                if plan.session_volume:
                    self._ensure_session_volume(plan)
                tmpfs = {"/tmp": "rw,nosuid,size=512m", "/home/agent": f"rw,nosuid,size=256m,uid={WORKER_UID},gid={WORKER_UID}"}
                command = list(plan.command)
                if file_secrets:
                    # Secret files live only in memory; the command waits until they are delivered.
                    tmpfs[SECRETS_MOUNT] = f"rw,nosuid,nodev,noexec,size=1m,mode=0700,uid={WORKER_UID},gid={WORKER_UID}"
                    command = ["/opt/ho/bin/ho-wait-secrets", *command]
                container = self.client.containers.create(
                    image_id,
                    command=command,
                    name=plan.worker_name,
                    user=f"{WORKER_UID}:{WORKER_UID}",
                    working_dir="/workspace",
                    environment=env,
                    labels=self._labels(plan, "worker"),
                    mounts=mounts,
                    tmpfs=tmpfs,
                    nano_cpus=int(plan.cpus * 1e9),
                    mem_limit=plan.memory_bytes,
                    memswap_limit=plan.memory_bytes,
                    pids_limit=plan.pids_limit,
                    network_mode="none" if not networks else networks[0],
                    # Resolve only names inside the attached internal networks; no external DNS.
                    dns=["127.0.0.1"],
                    ipc_mode="private",
                    stop_signal="SIGTERM",
                    **_SECURITY,
                )
                created.append(container)
                for extra in networks[1:]:
                    self.client.networks.get(extra).connect(container)
                if plan.inputs:
                    container.put_archive(INPUT_MOUNT, _tar({k: v.encode() for k, v in plan.inputs.items()}))
                container.start()
                if file_secrets:
                    self._deliver_secrets(container, file_secrets)
            except Exception:
                self._remove_objects(plan)
                raise
        log.info("execution started", extra={"execution": plan.execution, "task": plan.task, "event": "WORKER_CREATED"})
        return self.status(plan.execution)

    def _ensure_session_volume(self, plan: ContainerPlan) -> None:
        """Per-task, per-provider session store for resume; removed with the task environment."""
        assert plan.session_volume is not None
        try:
            volume = self.client.volumes.get(plan.session_volume)
        except NotFound:
            labels = {"ho.managed": "true", "ho.kind": "session", "ho.task": plan.task, "ho.project": plan.project,
                      "ho.provider": plan.provider or ""}
            self.client.volumes.create(plan.session_volume, labels=labels)
            return
        labels = volume.attrs.get("Labels") or {}
        if labels.get("ho.kind") != "session" or labels.get("ho.task") != plan.task:
            raise Rejected(f"volume {plan.session_volume} exists but is not this task's session store")

    def _deliver_secrets(self, container: Any, files: dict[str, str]) -> None:
        """Write secret files into the container's in-memory mount, then release the command.

        The value travels in the environment of a short exec process (never in a
        command line, image, or volume) and is written with mode 0600.
        """
        for name, value in files.items():
            result = container.exec_run(
                ["/bin/sh", "-c", 'umask 077 && printf %s "$HO_SECRET_VALUE" > "$0/$1"', SECRETS_MOUNT, name],
                environment={"HO_SECRET_VALUE": value}, user=f"{WORKER_UID}:{WORKER_UID}",
            )
            if result.exit_code != 0:
                raise RuntimeError(f"could not deliver secret {name}")
        result = container.exec_run(["/bin/sh", "-c", f"touch {SECRETS_MOUNT}/.ready"], user=f"{WORKER_UID}:{WORKER_UID}")
        if result.exit_code != 0:
            raise RuntimeError("could not release the execution after delivering secrets")

    def _start_proxy(self, plan: ContainerPlan) -> Any:
        image_id = images.resolve(self.config_dir, "egress-proxy")
        proxy = self.client.containers.create(
            image_id,
            name=plan.proxy_name,
            environment={
                "HO_EGRESS_MODE": plan.egress,
                "HO_ALLOWED_DOMAINS": ",".join(plan.allowed_domains),
                "HO_DENIED_DOMAINS": ",".join(plan.denied_domains),
                "HO_EXECUTION": plan.execution,
            },
            labels=self._labels(plan, "proxy"),
            network=plan.execution_network,
            mem_limit=64 * 1024**2,
            memswap_limit=64 * 1024**2,
            nano_cpus=int(0.25 * 1e9),
            pids_limit=64,
            **_SECURITY,
        )
        self._ensure_network(EGRESS_NETWORK, {"ho.managed": "true", "ho.kind": "network"}, internal=False).connect(proxy)
        proxy.start()
        # The worker must not start before its only route out is listening.
        deadline = time.monotonic() + PROXY_READY_SECONDS
        while time.monotonic() < deadline:
            proxy.reload()
            if proxy.status != "running":
                raise RuntimeError(f"egress proxy for {plan.execution} exited during startup")
            if b"EGRESS_PROXY_STARTED" in proxy.logs(tail=20):
                return proxy
            time.sleep(0.1)
        raise RuntimeError(f"egress proxy for {plan.execution} did not become ready")

    # ------------------------------------------------------------ inspection

    def status(self, execution: str) -> ExecutionStatus:
        containers = self.client.containers.list(all=True, filters={"label": [f"ho.execution={execution}", "ho.kind=worker"]})
        if not containers:
            return ExecutionStatus(execution, "absent")
        state = containers[0].attrs["State"]
        return ExecutionStatus(
            execution,
            {"created": "created", "running": "running", "restarting": "running", "paused": "running"}.get(state["Status"], "exited"),
            exit_code=state.get("ExitCode") if state["Status"] in ("exited", "dead") else None,
            oom_killed=bool(state.get("OOMKilled")),
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt") if state["Status"] in ("exited", "dead") else None,
        )

    def list_managed(self) -> dict[str, list[dict[str, Any]]]:
        containers = [
            {"name": c.name, "status": c.status, "labels": {k: v for k, v in c.labels.items() if k.startswith("ho.")}}
            for c in self.client.containers.list(all=True, filters={"label": "ho.managed=true"})
        ]
        networks = [{"name": n.name, "labels": n.attrs.get("Labels") or {}} for n in self.client.networks.list(filters={"label": "ho.managed=true"})]
        volumes = [{"name": v.name, "labels": v.attrs.get("Labels") or {}} for v in self.client.volumes.list(filters={"label": "ho.managed=true"})]
        return {"containers": containers, "networks": networks, "volumes": volumes}

    # --------------------------------------------------------------- control

    def stop(self, execution: str, *, grace_seconds: int = 10) -> ExecutionStatus:
        for container in self.client.containers.list(filters={"label": [f"ho.execution={execution}", "ho.kind=worker"]}):
            container.stop(timeout=grace_seconds)  # SIGTERM, then SIGKILL after the grace period
        return self.status(execution)

    def collect(self, execution: str) -> dict[str, Any]:
        containers = self.client.containers.list(all=True, filters={"label": [f"ho.execution={execution}", "ho.kind=worker"]})
        if not containers:
            raise NotFound(f"execution {execution} has no container")
        container = containers[0]
        files: dict[str, str] = {}
        truncated = False
        try:
            stream, _ = container.get_archive("/output")
            data = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                total = 0
                # Smallest first: result files survive when a large log fills the limit.
                for member in sorted(archive.getmembers(), key=lambda m: m.size):
                    if not member.isfile():
                        continue
                    if len(files) >= MAX_OUTPUT_FILES or total + member.size > MAX_OUTPUT_BYTES:
                        truncated = True
                        break
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    name = member.name.split("/", 1)[1] if "/" in member.name else member.name
                    files[name] = base64.b64encode(extracted.read()).decode()
                    total += member.size
        except (NotFound, APIError, tarfile.TarError):
            pass
        logs = container.logs(stdout=True, stderr=True, tail=2000)[-MAX_LOG_BYTES:].decode("utf-8", "replace")
        # Replace delivered secret values in everything returned (SECURITY_MODEL.md section 7.3).
        refs = [r for r in (container.labels.get("ho.secrets") or "").split(",") if r]
        if refs:
            values = self.secrets.values_for(refs)
            logs = self.secrets.redact(logs, values)
            for name, encoded in list(files.items()):
                raw = base64.b64decode(encoded)
                redacted = self.secrets.redact_bytes(raw, values)
                if redacted != raw:
                    files[name] = base64.b64encode(redacted).decode()
        egress: list[dict[str, Any]] = []
        proxy = self._find(f"ho-p-{execution.replace('-', '')[-12:]}")
        if proxy is not None:
            for line in proxy.logs(tail=5000).decode("utf-8", "replace").splitlines():
                try:
                    egress.append(json.loads(line))
                except ValueError:
                    continue
        return {"files": files, "truncated": truncated, "logs": logs, "egress": egress}

    def remove(self, execution: str) -> dict[str, int]:
        short = execution.replace("-", "")[-12:]
        return self._remove_by_label(execution, short)

    def _remove_objects(self, plan: ContainerPlan) -> None:
        self._remove_by_label(plan.execution, plan.short)

    def _remove_by_label(self, execution: str, short: str) -> dict[str, int]:
        removed = {"containers": 0, "networks": 0, "volumes": 0}
        for container in self.client.containers.list(all=True, filters={"label": f"ho.execution={execution}"}):
            container.remove(force=True, v=True)
            removed["containers"] += 1
        for network in self.client.networks.list(filters={"label": [f"ho.execution={execution}", "ho.kind=network"]}):
            if network.name == f"ho-e-{short}":
                network.remove()
                removed["networks"] += 1
        for kind in ("output", "input"):
            for volume in self.client.volumes.list(filters={"label": [f"ho.execution={execution}", f"ho.kind={kind}"]}):
                volume.remove(force=True)
                removed["volumes"] += 1
        return removed

    # ------------------------------------------------------- test environments

    def _compose(self, *args: str, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        """Run the pinned Compose binary with an empty environment: nothing from Agent Manager
        can be interpolated into a project's Compose files."""
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "DOCKER_CONFIG": "/tmp/.docker",
               "DOCKER_HOST": os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")}
        return subprocess.run([os.environ.get("HO_COMPOSE_BIN", "docker-compose"), *args], capture_output=True, text=True,
                              input=input_text, timeout=timeout, env=env, check=False)

    def start_environment(self, *, task: str, project: str, project_path: str, workspace: str, compose_files: list[str],
                          services: list[str], startup_timeout: int, projects_root: Path, projects_root_host: str) -> dict[str, Any]:
        """Start the project's test services on the task's private network (sections 51-52)."""
        if not re.match(r"^T-[0-9]+$", task):
            raise Rejected("invalid task key")
        relative, resolved = resolve_workspace(project_path, workspace, projects_root)
        files = []
        for name in compose_files or ["compose.yaml"]:
            candidate = resolved / name
            if ".." in PurePosixPath(name).parts or candidate.is_symlink() or not candidate.is_file():
                raise Rejected(f"Compose file {name} must be a regular file inside the workspace")
            files += ["-f", str(candidate)]
        name = compose.project_name(task, project)
        rendered = self._compose("-p", name, "--project-directory", str(resolved), *files, "config", "--format", "json")
        if rendered.returncode != 0:
            raise Rejected(f"the project's Compose files could not be read: {rendered.stderr.strip()[:300]}")
        limits = self.platform["machine"]["runners"]["test"]
        network = f"ho-t-{task.lower()}-svc"
        model = compose.sanitize(json.loads(rendered.stdout), task=task, project=project, network=network, services=services,
                                 workspace_container=str(resolved),
                                 workspace_host=f"{self._projects_root_host(projects_root_host)}/{relative}",
                                 limits=compose.Limits(int(float(limits["memory_gb"]) * 1024**3), float(limits["cpus"])))
        needed = sum(int(s["mem_limit"]) for s in model["services"].values())
        with self._lock:
            available = self.capacity()["memory_available_for_executions"]
            if needed > available:
                raise CapacityExceeded(f"test services need {needed // 1024**2} MiB, {available // 1024**2} MiB available")
            self._ensure_network(network, {"ho.managed": "true", "ho.kind": "network", "ho.task": task, "ho.project": project},
                                 internal=True)
            started = self._compose("-p", name, "-f", "-", "up", "-d", "--wait", "--wait-timeout", str(startup_timeout),
                                    "--no-build", "--pull", "missing", "--quiet-pull", "--remove-orphans",
                                    input_text=json.dumps(model), timeout=startup_timeout + 300)
        if started.returncode != 0:
            self._compose("-p", name, "down", "-v", "--remove-orphans", timeout=120)
            raise EnvironmentFailed(f"test services did not start: {started.stderr.strip()[-500:]}")
        return {"project": name, "network": network, "services": self.environment_status(task)}

    @staticmethod
    def _projects_root_host(value: str) -> str:
        return value.rstrip("/")

    def environment_status(self, task: str) -> list[dict[str, Any]]:
        result = []
        for container in self.client.containers.list(all=True, filters={"label": [f"ho.task={task}", "ho.kind=test-service"]}):
            health = (container.attrs.get("State", {}).get("Health") or {}).get("Status")
            result.append({"service": container.labels.get("ho.service"), "state": container.status, "health": health,
                           "image": container.attrs.get("Config", {}).get("Image")})
        return sorted(result, key=lambda s: s["service"] or "")

    def remove_task_environment(self, task: str, *, services_only: bool = False) -> int:
        """Remove the task's test services (and, unless `services_only`, its networks and session stores)."""
        count = 0
        projects = {c.labels.get("com.docker.compose.project") for c in self.client.containers.list(
            all=True, filters={"label": [f"ho.task={task}", "ho.kind=test-service"]})} - {None}
        for name in sorted(projects):
            self._compose("-p", name, "down", "-v", "--remove-orphans", timeout=180)
            count += 1
        for volume in self.client.volumes.list(filters={"label": [f"ho.task={task}", "ho.kind=test-service"]}):
            volume.remove(force=True)
            count += 1
        if services_only:
            return count
        for network in self.client.networks.list(filters={"label": [f"ho.task={task}", "ho.kind=network"]}):
            network.reload()
            if network.attrs.get("Containers"):
                raise Rejected(f"network {network.name} still has containers attached")
            network.remove()
            count += 1
        for volume in self.client.volumes.list(filters={"label": [f"ho.task={task}", "ho.kind=session"]}):
            volume.remove(force=True)
            count += 1
        return count

    def credentials(self) -> list[dict[str, str]]:
        """Provider credential volumes (names and labels only; contents are never read here)."""
        result = []
        for volume in self.client.volumes.list(filters={"label": "ho.credential"}):
            provider, _, identity = (volume.attrs.get("Labels") or {}).get("ho.credential", "").partition("/")
            if volume.name == f"cred-{provider}-{identity}":
                result.append({"provider": provider, "identity": identity, "volume": volume.name,
                               "created_at": volume.attrs.get("CreatedAt", "")})
        return sorted(result, key=lambda c: c["volume"])

    def images(self) -> dict[str, Any]:
        return {"images": sorted(images.load_allowlist(self.config_dir)), "versions": images.load_versions(self.config_dir)}

    def reap_expired(self) -> list[str]:
        """Defense in depth: stop workers whose grant expired, even if the control plane is down."""
        now = datetime.now(timezone.utc)
        stopped = []
        for container in self._running_workers():
            expires = container.labels.get("ho.expires_at")
            if expires and datetime.fromisoformat(expires) <= now:
                container.stop(timeout=10)
                stopped.append(container.labels.get("ho.execution", container.name))
        return stopped
