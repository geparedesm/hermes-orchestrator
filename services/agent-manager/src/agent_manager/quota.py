"""Isolated, bounded account-only probes. Caller input cannot select a command, image, mount or host."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

from docker.errors import DockerException, NotFound
from docker.types import LogConfig, Mount
from ho_core.quota import snapshot
from requests.exceptions import Timeout

from . import stack
from .plan import ContainerPlan, Rejected, WORKER_UID

PROBE_MEMORY = 512 * 1024**2
PROBE_SECONDS = 40
SCRIPT = Path(__file__).with_name("quota_probe.cjs")


def refresh(ops, provider: str, identity: str) -> dict:
    if provider not in ("claude", "codex") or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", identity):
        raise Rejected("unknown provider or invalid identity")
    lock = ops._quota_locks[provider]
    if not lock.acquire(blocking=False):
        return {"status": "BUSY", "windows": []}
    try:
        return _refresh(ops, provider, identity)
    except (DockerException, Timeout, ValueError, OSError):
        # Never forward a Docker exception or a provider response (either can contain untrusted content).
        return {"status": "UNAVAILABLE", "windows": []}
    finally:
        lock.release()


def _refresh(ops, provider: str, identity: str) -> dict:
    volume_name = f"cred-{provider}-{identity}"
    try:
        volume = ops.client.volumes.get(volume_name)
    except NotFound:
        return {"status": "AUTH_REQUIRED", "windows": []}
    if (volume.attrs.get("Labels") or {}).get("ho.credential") != f"{provider}/{identity}":
        return {"status": "AUTH_REQUIRED", "windows": []}
    image = ops.ensure_image(f"{provider}-generic")
    if image.labels.get("org.hermes-orchestrator.provider") != provider:
        raise Rejected("quota image does not match the provider")
    probe = str(uuid.uuid4())
    short = probe.replace("-", "")[-12:]
    labels = {"ho.managed": "true", "ho.stack": stack.NAME, "ho.quota_probe": probe,
              "ho.provider": provider, "ho.identity": identity,
              "ho.expires_at": (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}
    plan = ContainerPlan(probe, short, "", "", "QUOTA", f"{provider}-generic", [], {}, labels, [],
                         0.5, PROBE_MEMORY, 128, datetime.now(timezone.utc) + timedelta(seconds=90),
                         "PROVIDER_ONLY", allowed_domains=list(ops.platform["machine"]["provider_domains"][provider]))
    name = "ho-q-" + sha256(f"{stack.NAME}/{provider}/{identity}".encode()).hexdigest()[:16]
    container = network = proxy = None
    try:
        with ops._lock:
            if ops._find(name) is not None:
                return {"status": "BUSY", "windows": []}  # another process owns it; expiry reaps crashed probes
            ops._check_capacity(plan)
            network = ops._ensure_network(plan.execution_network, {**labels, "ho.kind": "quota-network"}, internal=True)
            proxy = ops._start_proxy(plan)
            proxy.reload()
            url = f"http://{proxy.attrs['NetworkSettings']['Networks'][plan.execution_network]['IPAddress']}:3128"
            env = {"HO_QUOTA_PROBE": "1", "HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url,
                   "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
            container = ops.client.containers.create(
                image.id, entrypoint=["/opt/ho/pen-node/bin/node"], command=["-e", SCRIPT.read_text(), provider],
                name=name, user=f"{WORKER_UID}:{WORKER_UID}", working_dir="/tmp", environment=env,
                labels={**labels, "ho.kind": "quota-probe"},
                mounts=[Mount(f"/run/ho-credentials/{provider}", volume_name, type="volume")],
                tmpfs={"/tmp": "rw,nosuid,nodev,size=128m", "/home/agent": f"rw,nosuid,size=128m,uid={WORKER_UID},gid={WORKER_UID}"},
                nano_cpus=500_000_000, mem_limit=PROBE_MEMORY, memswap_limit=PROBE_MEMORY, pids_limit=128,
                network_mode=plan.execution_network, dns=["127.0.0.1"], ipc_mode="private",
                read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"], privileged=False,
                log_config=LogConfig(type="local", config={"max-size": "16k", "max-file": "1", "compress": "false"}),
            )
            container.start()
        try:
            exited = container.wait(timeout=PROBE_SECONDS)
        except Timeout:
            return {"status": "TIMEOUT", "windows": []}
        if exited.get("StatusCode") != 0:
            return {"status": "UNAVAILABLE", "windows": []}
        output = container.logs(stdout=True, stderr=False)
        if len(output) > 16 * 1024:
            return {"status": "INVALID_RESPONSE", "windows": []}
        try:
            return snapshot(json.loads(output))
        except (ValueError, UnicodeError):
            return {"status": "INVALID_RESPONSE", "windows": []}
    finally:
        # Proxy creation can fail after creating its container, before returning it.
        if proxy is None and network is not None:
            proxy = ops._find(plan.proxy_name)
        for item in (container, proxy):
            if item is not None:
                with suppress(DockerException):
                    item.remove(force=True, v=True)
        if network is not None:
            with suppress(DockerException):
                network.remove()


def reap(ops) -> None:
    """Remove bounded probes abandoned by a killed Agent Manager. Credential volumes are never removed."""
    now = datetime.now(timezone.utc)
    filters = {"label": ["ho.managed=true", "ho.quota_probe", stack.LABEL]}
    for c in ops.client.containers.list(all=True, filters=filters):
        if datetime.fromisoformat(c.labels["ho.expires_at"]) <= now:
            with suppress(DockerException):
                c.remove(force=True, v=True)
    for n in ops.client.networks.list(filters=filters):
        if datetime.fromisoformat(n.attrs["Labels"]["ho.expires_at"]) <= now:
            with suppress(DockerException):
                n.remove()
