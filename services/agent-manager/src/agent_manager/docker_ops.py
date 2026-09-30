"""Docker operations for executions. The only code in the platform that creates containers.

Every object carries `ho.*` labels, so the Docker daemon itself is the record
of what exists; Agent Manager keeps no other state and can restart safely.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import tarfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, NotFound
from docker.types import Mount as DockerMount

from . import images
from .plan import PROXY_PORT, WORKER_UID, ContainerPlan, Rejected

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
    def __init__(self, platform: dict[str, Any], config_dir: Path, client: docker.DockerClient | None = None) -> None:
        self.platform = platform
        self.config_dir = config_dir
        self.client = client or docker.from_env(timeout=60)
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

        with self._lock:
            self._check_capacity(plan)
            created: list[Any] = []
            try:
                env = dict(plan.env)
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
                container = self.client.containers.create(
                    image_id,
                    command=plan.command,
                    name=plan.worker_name,
                    user=f"{WORKER_UID}:{WORKER_UID}",
                    working_dir="/workspace",
                    environment=env,
                    labels=self._labels(plan, "worker"),
                    mounts=mounts,
                    tmpfs={"/tmp": "rw,nosuid,size=512m", "/home/agent": f"rw,nosuid,size=256m,uid={WORKER_UID},gid={WORKER_UID}"},
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
                container.start()
            except Exception:
                self._remove_objects(plan)
                raise
        log.info("execution started", extra={"execution": plan.execution, "task": plan.task, "event": "WORKER_CREATED"})
        return self.status(plan.execution)

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
                for member in archive.getmembers():
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
        for volume in self.client.volumes.list(filters={"label": [f"ho.execution={execution}", "ho.kind=output"]}):
            volume.remove(force=True)
            removed["volumes"] += 1
        return removed

    def remove_task_environment(self, task: str) -> int:
        count = 0
        for network in self.client.networks.list(filters={"label": [f"ho.task={task}", "ho.kind=network"]}):
            network.reload()
            if network.attrs.get("Containers"):
                raise Rejected(f"network {network.name} still has containers attached")
            network.remove()
            count += 1
        return count

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
