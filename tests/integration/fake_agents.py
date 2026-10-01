"""In-memory stand-in for the Agent Manager API, for control-plane tests.

The real Agent Manager is tested against Docker in tests/docker.
"""

from __future__ import annotations

import base64
from typing import Any

from control_plane.agentmgr import AgentManagerError


class FakeAgentManager:
    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.specs: dict[str, dict[str, Any]] = {}
        self.fail_with: AgentManagerError | None = None
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.released: list[str] = []
        self.environments: dict[str, dict[str, Any]] = {}  # task -> start request
        self.services_stopped: list[str] = []
        self.environment_error: AgentManagerError | None = None
        self.volumes: set[tuple[str, str]] = {("claude", "default"), ("codex", "default")}
        self.networks: list[dict[str, Any]] = []  # managed networks left behind (orphan tests)

    def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        assert spec["grant"]["capabilities"]["docker"] == "NONE"
        execution = spec["execution_id"]
        self.specs[execution] = spec
        self.containers.setdefault(execution, {"state": "running", "exit_code": None, "files": {}, "logs": ""})
        return {"execution": execution, "state": "running"}

    def finish(self, execution: str, exit_code: int = 0, files: dict[str, bytes] | None = None, logs: str = "") -> None:
        self.containers[execution].update(state="exited", exit_code=exit_code, files=files or {}, logs=logs)

    def finish_agent(self, execution: str, events: bytes, *, exit_code: int = 0, reason: str = "cli_exited",
                     extra: dict[str, bytes] | None = None) -> None:
        """Simulate the in-container runner's output files (see workers/providers/*/ho-agent-run)."""
        files = {"ho/events.jsonl": events,
                 "ho/runner.json": f'{{"exit_code": {exit_code}, "reason": "{reason}"}}'.encode(), **(extra or {})}
        lines = [line for line in events.splitlines() if line.strip()]
        if lines:
            files["ho/final.json"] = lines[-1]
        self.finish(execution, exit_code, files=files)

    def vanish(self, execution: str) -> None:
        self.containers.pop(execution)

    def status(self, execution: str) -> dict[str, Any]:
        if self.fail_with and self.fail_with.status == 0:
            raise self.fail_with
        container = self.containers.get(execution)
        if container is None:
            return {"execution": execution, "state": "absent"}
        return {"execution": execution, "state": container["state"], "exit_code": container["exit_code"], "oom_killed": False}

    def stop(self, execution: str, grace_seconds: int = 10) -> dict[str, Any]:
        self.stopped.append(execution)
        if execution in self.containers:
            self.containers[execution].update(state="exited", exit_code=143)
        return self.status(execution)

    def collect(self, execution: str) -> dict[str, Any]:
        container = self.containers[execution]
        return {
            "files": {k: base64.b64encode(v).decode() for k, v in container["files"].items()},
            "truncated": False,
            "logs": container["logs"],
            "egress": [{"event": "EGRESS_DENIED", "host": "evil.example", "reason": "not in ALLOWLIST allowlist"}],
        }

    def remove(self, execution: str) -> dict[str, int]:
        self.removed.append(execution)
        self.containers.pop(execution, None)
        self.networks = [n for n in self.networks if n["labels"].get("ho.execution") != execution]
        return {"containers": 1, "networks": 0, "volumes": 1}

    def caches(self) -> dict[str, Any]:
        return {"caches": [], "max_bytes_per_cache": 2 * 1024**3}

    def maintain_caches(self) -> dict[str, Any]:
        self.cache_maintenance = getattr(self, "cache_maintenance", 0) + 1
        return {"trimmed": []}

    def clear_cache(self, project: str, ecosystem: str | None = None) -> dict[str, Any]:
        return {"removed": [f"ho-cache-{project}-{ecosystem or 'pip'}"]}

    def stats(self) -> dict[str, Any]:
        return {"workers": [{"execution": e, "task": self.specs.get(e, {}).get("task"), "role": None, "provider": None,
                             "cpu_percent": 12.5, "memory_bytes": 100 * 1024**2, "memory_limit_bytes": 2 * 1024**3}
                            for e, c in self.containers.items() if c["state"] == "running"]}

    def capacity(self) -> dict[str, Any]:
        return {"agent_workers": sum(1 for c in self.containers.values() if c["state"] == "running")}

    def managed(self) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        containers = [{"name": f"worker-{e}", "status": c["state"], "labels": {"ho.execution": e, "ho.kind": "worker"}}
                      for e, c in self.containers.items()]
        return {"containers": containers, "networks": list(self.networks), "volumes": []}

    def start_environment(self, task: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.environment_error:
            raise self.environment_error
        self.environments[task] = body
        return {"project": f"ho-{task.lower()}-{body['project']}", "network": f"ho-t-{task.lower()}-svc",
                "services": [{"service": s, "state": "running", "health": "healthy"} for s in body["services"]]}

    def stop_test_services(self, task: str) -> dict[str, int]:
        self.services_stopped.append(task)
        return {"removed": 1}

    def remove_task_environment(self, task: str) -> dict[str, int]:
        self.released.append(task)
        return {"removed": 1}

    def credentials(self) -> dict[str, Any]:
        return {"credentials": [{"provider": p, "identity": i, "volume": f"cred-{p}-{i}", "created_at": ""}
                                for p, i in sorted(self.volumes)]}

    def images(self) -> dict[str, Any]:
        return {"images": ["agent-base", "claude-generic", "codex-generic", "runner-generic"], "versions": {}}

    def ping(self) -> bool:
        return self.fail_with is None or self.fail_with.status != 0
