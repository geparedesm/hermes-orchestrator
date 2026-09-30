"""Client for the private git-service API (ARCHITECTURE.md section 7.4)."""

from __future__ import annotations

from typing import Any

import httpx

from .errors import ApiError, BadRequest, Conflict, Forbidden, NotFound, UpstreamError


class GitServiceClient:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=300)
        self._headers = {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"git-service unavailable: {exc}") from exc
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = response.text[:300]
        if response.status_code == 404:
            raise NotFound(message or "not found")
        if response.status_code in (400, 422):
            raise BadRequest(message or "invalid request")
        if response.status_code == 403:
            raise Forbidden(f"git-service refused: {message}")
        if response.status_code == 409:
            raise Conflict(message)
        if response.status_code >= 400:
            raise UpstreamError(f"git-service error {response.status_code}: {message}")
        return response.json()

    def inspect(self, relative_path: str) -> dict[str, Any]:
        return self._post("/v1/inspect", {"path": relative_path})

    def scan(self, relative_path: str) -> dict[str, Any]:
        return self._post("/v1/scan", {"path": relative_path})

    def prepare_workspace(self, path: str, name: str, branch: str, base_ref: str, pin_ref: str | None = None) -> dict[str, Any]:
        return self._post("/v1/workspaces/prepare", {"path": path, "name": name, "branch": branch, "base_ref": base_ref,
                                                     "pin_ref": pin_ref})

    def collect(self, path: str, name: str, branch: str, base_sha: str) -> dict[str, Any]:
        return self._post("/v1/workspaces/collect", {"path": path, "name": name, "branch": branch, "base_sha": base_sha})

    def conflict_workspace(self, path: str, name: str, branch: str, target_branch: str, incoming_ref: str) -> dict[str, Any]:
        return self._post("/v1/workspaces/conflict", {"path": path, "name": name, "branch": branch,
                                                      "target_branch": target_branch, "incoming_ref": incoming_ref})

    def remove_workspace(self, path: str, name: str) -> dict[str, Any]:
        return self._post("/v1/workspaces/remove", {"path": path, "name": name})

    def divergence(self, path: str, **body: Any) -> dict[str, Any]:
        return self._post("/v1/divergence", {"path": path, **body})

    def integrate(self, path: str, task: str, target_branch: str, heads: list[str]) -> dict[str, Any]:
        return self._post("/v1/integrate", {"path": path, "task": task, "target_branch": target_branch, "heads": heads})

    def changes(self, path: str, base: str, head: str) -> dict[str, Any]:
        return self._post("/v1/changes", {"path": path, "base": base, "head": head})

    def refs(self, path: str, refs: list[str]) -> dict[str, Any]:
        return self._post("/v1/refs", {"path": path, "refs": refs})

    def merge(self, path: str, task: str, authorization: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/merge", {"path": path, "task": task, "authorization": authorization})

    def github_status(self, path: str) -> dict[str, Any]:
        return self._post("/v1/github/status", {"path": path})

    def push(self, path: str, **body: Any) -> dict[str, Any]:
        return self._post("/v1/github/push", {"path": path, **body})

    def pull_request(self, path: str, **body: Any) -> dict[str, Any]:
        return self._post("/v1/github/pr", {"path": path, **body})

    def pr_view(self, path: str, number: int) -> dict[str, Any]:
        return self._post("/v1/github/pr/view", {"path": path, "number": number})

    def pr_checks(self, path: str, number: int) -> dict[str, Any]:
        return self._post("/v1/github/pr/checks", {"path": path, "number": number})

    def delete_branch(self, path: str, **body: Any) -> dict[str, Any]:
        return self._post("/v1/github/delete-branch", {"path": path, **body})

    def ping(self) -> bool:
        try:
            return self._client.get("/health/live").status_code == 200
        except httpx.HTTPError:
            return False


__all__ = ["GitServiceClient", "ApiError"]
