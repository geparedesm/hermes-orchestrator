"""Task API (ARCHITECTURE.md section 7.2). Internal network only; every call is authenticated."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from ho_core.enums import Autonomy
from ho_core.policy.engine import decide_command
from pydantic import BaseModel, Field

from .approvals import Approvals
from .auth import PRINCIPAL_HEADER, Authenticator, Identity, Principal
from .context import Context, UnitOfWork
from .errors import ApiError, Conflict
from .idempotency import run_idempotent
from .projects import Projects
from .scheduler import Scheduler
from .tasks import Tasks
from .views import approval_view, config_view, project_view

log = logging.getLogger(__name__)


@dataclass
class Services:
    ctx: Context
    auth: Authenticator
    approvals: Approvals
    projects: Projects
    tasks: Tasks
    scheduler: Scheduler
    run_scheduler: bool = True


def build_services(ctx: Context, auth: Authenticator, *, run_scheduler: bool = True, dispatcher: Any = None) -> Services:
    approvals = Approvals(ctx)
    projects = Projects(ctx, approvals)
    tasks = Tasks(ctx, projects, approvals)
    scheduler = Scheduler(ctx, tasks, approvals, dispatcher)
    return Services(ctx, auth, approvals, projects, tasks, scheduler, run_scheduler)


# ------------------------------------------------------------------ request models


class RegisterProject(BaseModel):
    path: str = Field(min_length=1, max_length=1000, description="Host path inside the projects root, or a path relative to it")
    name: str | None = Field(default=None, max_length=100)
    slug: str | None = None


class Decision(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=1000)


class CommandCheck(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    autonomy: Autonomy = Autonomy.BALANCED


# ---------------------------------------------------------------------- the app


def create_app(services: Services) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if services.run_scheduler:
            services.scheduler.start()
        yield
        services.scheduler.stop()

    app = FastAPI(title="Hermes Orchestrator Task API", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/v1/openapi.json")
    ctx = services.ctx

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"error": "internal", "message": "internal error; see control-plane logs"}, status_code=500)

    def identity(authorization: str | None = Header(default=None)) -> Identity:
        return services.auth.identify(authorization)

    def principal(
        who: Identity = Depends(identity), x_ho_principal: str | None = Header(default=None, alias=PRINCIPAL_HEADER)
    ) -> Principal:
        return services.auth.principal(who, x_ho_principal)

    def idempotent(key: str | None, who: Principal, request: Any, command) -> JSONResponse:
        with ctx.unit_of_work() as uow:
            status, body = run_idempotent(uow.cur, principal=who.value, key=key, request=request,
                                          command=lambda: command(uow))
        return JSONResponse(body, status_code=status)

    # ---------------------------------------------------------------- health

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        checks = {"postgres": ctx.db.ping(), "redis": ctx.coordinator.ping(), "git_service": ctx.git.ping()}
        # Redis is not authoritative: its loss degrades, it does not fail readiness.
        healthy = checks["postgres"] and checks["git_service"]
        body = {"status": "ok" if healthy and checks["redis"] else "degraded" if healthy else "unavailable", "checks": checks}
        return JSONResponse(body, status_code=200 if healthy else 503)

    # -------------------------------------------------------------- projects

    @app.post("/v1/projects", status_code=201)
    def register_project(body: RegisterProject, who: Principal = Depends(principal),
                         idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        def command(uow: UnitOfWork):
            row = services.projects.register(uow, principal=who, path=body.path, name=body.name, slug=body.slug)
            return 201, project_view(row)
        return idempotent(idempotency_key, who, {"op": "register", **body.model_dump()}, command)

    @app.get("/v1/projects")
    def list_projects(_: Identity = Depends(identity), include_unregistered: bool = False) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return {"projects": [project_view(r) for r in services.projects.list(uow, include_unregistered=include_unregistered)]}

    @app.get("/v1/projects/{slug}")
    def get_project(slug: str, _: Identity = Depends(identity)) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            project = services.projects.get(uow, slug)
            return {**project_view(project),
                    "active_config": config_view(services.projects.active_config(uow, project["id"])),
                    "latest_config": config_view(services.projects.latest_config(uow, project["id"]))}

    @app.post("/v1/projects/{slug}/scan")
    def scan_project(slug: str, who: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        def command(uow: UnitOfWork):
            result = services.projects.scan(uow, slug=slug, principal=who)
            return 200, {"project": project_view(result["project"]), "config": config_view(result["config"]),
                         "approval": approval_view(result["approval"]) if result["approval"] else None,
                         "changed": result["changed"], "notes": result["notes"]}
        return idempotent(idempotency_key, who, {"op": "scan", "slug": slug}, command)

    @app.delete("/v1/projects/{slug}")
    def unregister_project(slug: str, who: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        def command(uow: UnitOfWork):
            return 200, project_view(services.projects.unregister(uow, slug=slug, principal=who))
        return idempotent(idempotency_key, who, {"op": "unregister", "slug": slug}, command)

    # ----------------------------------------------------------------- tasks

    @app.post("/v1/tasks", status_code=201)
    def create_task(body: dict[str, Any], who: Principal = Depends(principal),
                    idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        key = idempotency_key or body.get("idempotency_key")
        if key and body.get("idempotency_key") not in (None, key):
            raise Conflict("Idempotency-Key header and body idempotency_key differ")
        payload = {**body, "idempotency_key": key}

        def command(uow: UnitOfWork):
            task = services.tasks.create(uow, principal=who, body=payload)
            return 201, services.tasks.summary(uow, task)
        return idempotent(key, who, {"op": "create_task", **payload}, command)

    @app.get("/v1/tasks")
    def list_tasks(_: Identity = Depends(identity), project: str | None = None, state: str | None = None,
                   limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return {"tasks": [services.tasks.summary(uow, t) for t in services.tasks.list(uow, project=project, state=state, limit=limit)]}

    @app.get("/v1/tasks/{key}")
    def get_task(key: str, _: Identity = Depends(identity)) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return services.tasks.summary(uow, services.tasks.get(uow, key))

    def task_command(action: str):
        def endpoint(key: str, who: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)) -> JSONResponse:
            def command(uow: UnitOfWork):
                if action == "retry":
                    task = services.tasks.retry(uow, key, principal=who, idempotency_key=f"retry:{idempotency_key}")
                else:
                    task = getattr(services.tasks, action)(uow, key, principal=who)
                return 200, services.tasks.summary(uow, task)
            return idempotent(idempotency_key, who, {"op": action, "task": key}, command)
        return endpoint

    for action in ("pause", "resume", "cancel", "retry"):
        app.post(f"/v1/tasks/{{key}}/{action}", name=f"{action}_task")(task_command(action))

    @app.get("/v1/queue")
    def queue(_: Identity = Depends(identity)) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return {"dispatcher": type(services.scheduler.dispatcher).__name__,
                    "queue": [e.as_json() for e in services.scheduler.queue(uow)]}

    # ------------------------------------------------------------- approvals

    @app.get("/v1/approvals")
    def list_approvals(_: Identity = Depends(identity), state: str | None = "PENDING") -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return {"approvals": [approval_view(r) for r in services.approvals.list(uow, state=state or None)]}

    @app.get("/v1/approvals/{approval_id}")
    def get_approval(approval_id: str, _: Identity = Depends(identity)) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return approval_view(services.approvals.get(uow, approval_id))

    @app.post("/v1/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: Decision, who: Principal = Depends(principal),
               idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        def command(uow: UnitOfWork):
            row = services.approvals.decide(uow, approval_id, principal=who, approve=body.decision == "APPROVE", note=body.note)
            return (409 if row["state"] == "EXPIRED" else 200), approval_view(row)
        return idempotent(idempotency_key, who, {"op": "decide", "id": approval_id, **body.model_dump()}, command)

    # ---------------------------------------------------------------- events

    @app.get("/v1/events")
    def events(_: Identity = Depends(identity), task: str | None = None, project: str | None = None,
               after: int = 0, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        query = ("SELECT e.seq, e.occurred_at, e.type, e.actor, e.summary, e.data, e.audit, t.key AS task, p.slug AS project "
                 "FROM events e LEFT JOIN tasks t ON t.id = e.task_id LEFT JOIN projects p ON p.id = e.project_id WHERE e.seq > %s")
        params: list[Any] = [after]
        if task:
            query += " AND t.key = %s"
            params.append(task)
        if project:
            query += " AND p.slug = %s"
            params.append(project)
        query += " ORDER BY e.seq LIMIT %s"
        params.append(limit)
        with ctx.unit_of_work() as uow:
            uow.cur.execute(query, params)
            rows = uow.cur.fetchall()
        return {"events": [{**r, "occurred_at": r["occurred_at"].isoformat()} for r in rows]}

    # ---------------------------------------------------------------- policy

    @app.post("/v1/policy/commands/evaluate")
    def evaluate_command(body: CommandCheck, _: Identity = Depends(identity)) -> dict[str, Any]:
        """Dry-run classification; executions record their decisions from Phase 3 onward."""
        decision = decide_command(body.command, autonomy=body.autonomy)
        return {"class": decision.command_class, "decision": decision.decision, "rule_ids": list(decision.rule_ids),
                "summary": decision.summary}

    return app
