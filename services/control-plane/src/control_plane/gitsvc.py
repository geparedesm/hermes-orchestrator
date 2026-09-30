"""Client for the private git-service API (ARCHITECTURE.md section 7.4)."""

from __future__ import annotations

from typing import Any

import httpx

from .errors import ApiError, BadRequest, NotFound, UpstreamError


class GitServiceClient:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=60)
        self._headers = {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"git-service unavailable: {exc}") from exc
        if response.status_code == 404:
            raise NotFound(response.json().get("message", "not found"))
        if response.status_code in (400, 422):
            raise BadRequest(response.json().get("message", "invalid request"))
        if response.status_code >= 400:
            raise UpstreamError(f"git-service error {response.status_code}")
        return response.json()

    def inspect(self, relative_path: str) -> dict[str, Any]:
        return self._post("/v1/inspect", {"path": relative_path})

    def scan(self, relative_path: str) -> dict[str, Any]:
        return self._post("/v1/scan", {"path": relative_path})

    def ping(self) -> bool:
        try:
            return self._client.get("/health/live").status_code == 200
        except httpx.HTTPError:
            return False


__all__ = ["GitServiceClient", "ApiError"]
