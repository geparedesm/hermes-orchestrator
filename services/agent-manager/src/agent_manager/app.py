"""Agent Manager private API (ARCHITECTURE.md section 7.3).

Only the control plane holds the token. Every create request carries the full
capability grant; the plan is rebuilt and every hard invariant re-checked here,
independently of the Policy Engine.
"""

from __future__ import annotations

import hmac
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from docker.errors import APIError, NotFound
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .docker_ops import CapacityExceeded, CredentialMissing, DockerOps
from .images import ImageNotAllowed
from .plan import Rejected, build_plan

log = logging.getLogger(__name__)


class StopRequest(BaseModel):
    grace_seconds: int = Field(default=10, ge=0, le=300)


def create_app(ops: DockerOps, *, token: str, projects_root: Path, projects_root_host: str, reap_seconds: float = 15) -> FastAPI:
    stop_reaper = threading.Event()

    def reaper() -> None:
        while not stop_reaper.wait(reap_seconds):
            try:
                expired = ops.reap_expired()
                if expired:
                    log.warning("stopped executions with expired grants: %s", expired)
            except Exception:  # noqa: BLE001 - keep reaping
                log.exception("reaper pass failed")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        thread = threading.Thread(target=reaper, name="reaper", daemon=True)
        if reap_seconds > 0:
            thread.start()
        yield
        stop_reaper.set()

    app = FastAPI(title="Hermes Orchestrator Agent Manager", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    @app.exception_handler(Rejected)
    async def rejected(_: Request, exc: Rejected) -> JSONResponse:
        log.warning("request rejected: %s", exc, extra={"event": "INVARIANT_REJECTED"})
        return error(403, "rejected", str(exc))

    @app.exception_handler(ImageNotAllowed)
    async def image_not_allowed(_: Request, exc: ImageNotAllowed) -> JSONResponse:
        return error(403, "image_not_allowed", str(exc))

    @app.exception_handler(CapacityExceeded)
    async def capacity(_: Request, exc: CapacityExceeded) -> JSONResponse:
        return error(409, "capacity_exceeded", str(exc))

    @app.exception_handler(CredentialMissing)
    async def credential(_: Request, exc: CredentialMissing) -> JSONResponse:
        return error(424, "auth_required", str(exc))

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return error(404, "not_found", str(exc))

    @app.exception_handler(APIError)
    async def docker_error(_: Request, exc: APIError) -> JSONResponse:
        log.error("docker error: %s", exc)
        return error(502, "docker_error", "Docker rejected the operation; see agent-manager logs")

    def authorized(authorization: str | None = Header(default=None)) -> None:
        presented = (authorization or "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(presented.encode(), token.encode()):
            raise Rejected("unauthorized") from None

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        try:
            ops.client.ping()
            return JSONResponse({"status": "ok"})
        except Exception:  # noqa: BLE001 - readiness reports failure only
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/v1/executions", dependencies=[Depends(authorized)])
    def create_execution(body: dict[str, Any]) -> JSONResponse:
        plan = build_plan(body, platform=ops.platform, projects_root=projects_root, projects_root_host=projects_root_host)
        status = ops.create(plan)
        return JSONResponse(status.as_json(), status_code=201)

    @app.get("/v1/executions/{execution}", dependencies=[Depends(authorized)])
    def get_execution(execution: str) -> dict[str, Any]:
        return ops.status(execution).as_json()

    @app.post("/v1/executions/{execution}/stop", dependencies=[Depends(authorized)])
    def stop_execution(execution: str, body: StopRequest) -> dict[str, Any]:
        return ops.stop(execution, grace_seconds=body.grace_seconds).as_json()

    @app.post("/v1/executions/{execution}/collect", dependencies=[Depends(authorized)])
    def collect(execution: str) -> dict[str, Any]:
        return ops.collect(execution)

    @app.delete("/v1/executions/{execution}", dependencies=[Depends(authorized)])
    def remove(execution: str) -> dict[str, Any]:
        return ops.remove(execution)

    @app.delete("/v1/tasks/{task}/environment", dependencies=[Depends(authorized)])
    def remove_environment(task: str) -> dict[str, Any]:
        return {"networks_removed": ops.remove_task_environment(task)}

    @app.get("/v1/managed", dependencies=[Depends(authorized)])
    def managed() -> dict[str, Any]:
        return ops.list_managed()

    @app.get("/v1/capacity", dependencies=[Depends(authorized)])
    def capacity_view() -> dict[str, Any]:
        return ops.capacity()

    return app
