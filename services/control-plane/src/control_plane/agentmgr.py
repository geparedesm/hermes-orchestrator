"""Client for the private Agent Manager API (ARCHITECTURE.md section 7.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class AgentManagerError(Exception):
    status: int  # 0 when Agent Manager could not be reached
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status >= 500


class AgentManagerClient:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=120)
        self._headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            response = self._client.request(method, path, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise AgentManagerError(0, "unavailable", str(exc)) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise AgentManagerError(response.status_code, payload.get("error", "error"), payload.get("message", response.text[:300]))
        return response.json()

    def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/v1/executions", spec)

    def status(self, execution: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/executions/{execution}")

    def stop(self, execution: str, grace_seconds: int = 10) -> dict[str, Any]:
        return self._call("POST", f"/v1/executions/{execution}/stop", {"grace_seconds": grace_seconds})

    def collect(self, execution: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/executions/{execution}/collect")

    def remove(self, execution: str) -> dict[str, Any]:
        return self._call("DELETE", f"/v1/executions/{execution}")

    def remove_task_environment(self, task: str) -> dict[str, Any]:
        return self._call("DELETE", f"/v1/tasks/{task}/environment")

    def start_environment(self, task: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"/v1/tasks/{task}/environment", body)

    def stop_test_services(self, task: str) -> dict[str, Any]:
        return self._call("DELETE", f"/v1/tasks/{task}/environment?services_only=true")

    def capacity(self) -> dict[str, Any]:
        return self._call("GET", "/v1/capacity")

    def managed(self) -> dict[str, Any]:
        return self._call("GET", "/v1/managed")

    def credentials(self) -> dict[str, Any]:
        return self._call("GET", "/v1/credentials")

    def images(self) -> dict[str, Any]:
        return self._call("GET", "/v1/images")

    def ping(self) -> bool:
        try:
            return self._client.get("/health/live").status_code == 200
        except httpx.HTTPError:
            return False
