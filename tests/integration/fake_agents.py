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
        return {"containers": 1, "networks": 0, "volumes": 1}

    def capacity(self) -> dict[str, Any]:
        return {"agent_workers": sum(1 for c in self.containers.values() if c["state"] == "running")}

    def managed(self) -> dict[str, Any]:
        return {"containers": [], "networks": [], "volumes": []}

    def ping(self) -> bool:
        return self.fail_with is None or self.fail_with.status != 0
