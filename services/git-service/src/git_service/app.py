"""Git Service API (ARCHITECTURE.md section 7.4).

Phase 2 provides the read-only operations needed for registration and
onboarding. The projects root is mounted read-only; workspace, integration,
push, PR, and merge operations are added in Phase 5.
"""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from ho_core.detect import MAX_FILE_BYTES, detect
from ho_core.paths import PathOutsideRoot, resolve_inside
from pydantic import BaseModel, Field

from . import gitcmd


class ProjectPath(BaseModel):
    path: str = Field(min_length=1, max_length=1000, description="Path relative to the projects root")


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


def _read_yaml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read a small YAML mapping without following symlinks."""
    if not path.exists() and not path.is_symlink():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, f"{path.name} must be a regular file"
    if path.stat().st_size > MAX_FILE_BYTES:
        return None, f"{path.name} is larger than {MAX_FILE_BYTES} bytes"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, f"{path.name} is not valid YAML: {str(exc)[:200]}"
    if data is None:
        return None, None
    if not isinstance(data, dict):
        return None, f"{path.name} must contain a mapping"
    return data, None


def create_app(projects_root: Path, token: str) -> FastAPI:
    app = FastAPI(title="Hermes Orchestrator Git Service", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"error": "git_service", "message": exc.message}, status_code=exc.status)

    def authorized(authorization: str | None = Header(default=None)) -> None:
        presented = (authorization or "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(presented.encode(), token.encode()):
            raise ServiceError(401, "unauthorized")

    def repository(relative: str) -> Path:
        try:
            path = resolve_inside(projects_root, relative)
        except PathOutsideRoot as exc:
            raise ServiceError(400, str(exc)) from exc
        if path == projects_root.resolve():
            raise ServiceError(400, "the projects root itself is not a project")
        if not path.is_dir():
            raise ServiceError(404, f"{relative} does not exist in the projects root")
        return path

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/inspect", dependencies=[Depends(authorized)])
    def inspect(body: ProjectPath) -> dict[str, Any]:
        return {"path": body.path, **gitcmd.inspect(repository(body.path))}

    @app.post("/v1/scan", dependencies=[Depends(authorized)])
    def scan(body: ProjectPath) -> dict[str, Any]:
        repo = repository(body.path)
        info = gitcmd.inspect(repo)
        if not info.get("is_git"):
            raise ServiceError(400, f"{body.path} is not a Git repository")
        project_yaml, project_error = _read_yaml(repo / ".hermes" / "project.yaml")
        local_yaml, local_error = _read_yaml(repo / ".hermes.local.yaml")
        return {
            "path": body.path,
            "head": info.get("head"),
            "report": detect(repo).to_dict(),
            "project_yaml": project_yaml,
            "project_yaml_error": project_error,
            "local_yaml": local_yaml,
            "local_yaml_error": local_error,
        }

    return app
