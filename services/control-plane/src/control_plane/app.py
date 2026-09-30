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
from .credentials import Credentials
from .agentmgr import AgentManagerError
from .errors import ApiError, Conflict, Forbidden, UpstreamError
from .idempotency import run_idempotent
from .projects import Projects
from .executions import BudgetExhausted, ExecutionRequest, Executions
from .scheduler import Scheduler
from .tasks import Tasks
from .views import approval_view, config_view, execution_view, project_view

log = logging.getLogger(__name__)


@dataclass
class Services:
    ctx: Context
    auth: Authenticator
    approvals: Approvals
    projects: Projects
    tasks: Tasks
    scheduler: Scheduler
    executions: Executions
    run_scheduler: bool = True
    credentials: Credentials | None = None


def build_services(ctx: Context, auth: Authenticator, *, run_scheduler: bool = True, dispatcher: Any = None) -> Services:
    approvals = Approvals(ctx)
    projects = Projects(ctx, approvals)
    tasks = Tasks(ctx, projects, approvals)
    executions = Executions(ctx, tasks)
    scheduler = Scheduler(ctx, tasks, approvals, dispatcher, executions)
    credentials = Credentials(ctx, tasks, executions)
    return Services(ctx, auth, approvals, projects, tasks, scheduler, executions, run_scheduler, credentials)


# ------------------------------------------------------------------ request models


class RegisterProject(BaseModel):
    path: str = Field(min_length=1, max_length=1000, description="Host path inside the projects root, or a path relative to it")
    name: str | None = Field(default=None, max_length=100)
    slug: str | None = None


class Decision(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=1000)


class CreateExecution(BaseModel):
    role: Literal["ORCHESTRATOR", "DEVELOPER", "REVIEWER", "TESTER", "BROWSER"]
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000,
                               description="Agent assignment, run through the provider's adapter (agent roles)")
    command: list[str] = Field(default_factory=list, max_length=64, description="Raw command instead of a prompt")
    provider: Literal["claude", "codex"] | None = None
    image: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    workspace: str | None = Field(default=None, description=".hermes/worktrees/<name> inside the project")
    capabilities: dict[str, Any] = Field(default_factory=dict)
    resource_profile: Literal["LIGHT", "NORMAL", "HEAVY"] | None = None
    timeout_minutes: int = Field(default=60, ge=1, le=1440)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list, max_length=32, description="Secret NAMEs from the project configuration")
    max_turns: int = Field(default=60, ge=1, le=500)
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")


class ResumeExecution(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class StopExecution(BaseModel):
    reason: str = Field(default="stopped by operator", max_length=300)


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
        if ctx.agents is not None:
            checks["agent_manager"] = ctx.agents.ping()
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

    # ------------------------------------------------------------ executions

    @app.post("/v1/tasks/{key}/executions", status_code=201)
    def create_execution(key: str, body: CreateExecution, who_identity: Identity = Depends(identity),
                         who: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        # Phase 3: operators start executions by hand to exercise Agent Manager. From Phase 4/7
        # the orchestrator requests them through accepted action proposals instead.
        if who_identity.name != "operator":
            raise Forbidden("only the operator can start executions directly")
        request = ExecutionRequest(**body.model_dump())

        def command(uow: UnitOfWork):
            row = services.executions.request(uow, principal=who, task_key=key, req=request)
            return 201, execution_view(row)
        try:
            return idempotent(idempotency_key, who, {"op": "execution", "task": key, **body.model_dump()}, command)
        except BudgetExhausted as exc:
            services.executions.pause_for_budget(key, str(exc))
            raise

    @app.get("/v1/executions")
    def list_executions(_: Identity = Depends(identity), task: str | None = None, state: str | None = None) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return {"executions": [execution_view(r) for r in services.executions.list(uow, task=task, state=state)]}

    @app.get("/v1/executions/{execution_id}")
    def get_execution(execution_id: str, _: Identity = Depends(identity)) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            row = services.executions.get(uow, execution_id)
            return execution_view(row, grant=services.executions.grant(uow, row["id"]))

    @app.post("/v1/executions/{execution_id}/stop")
    def stop_execution(execution_id: str, body: StopExecution, who: Principal = Depends(principal),
                       idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        def command(uow: UnitOfWork):
            return 200, execution_view(services.executions.stop(uow, execution_id, actor=who.value, reason=body.reason))
        return idempotent(idempotency_key, who, {"op": "stop", "id": execution_id, **body.model_dump()}, command)

    @app.post("/v1/executions/{execution_id}/replace", status_code=201)
    def replace_execution(execution_id: str, body: StopExecution, who_identity: Identity = Depends(identity),
                          who: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        if who_identity.name != "operator":
            raise Forbidden("only the operator can replace executions directly")

        def command(uow: UnitOfWork):
            return 201, execution_view(services.executions.replace(uow, execution_id, principal=who, reason=body.reason))
        return idempotent(idempotency_key, who, {"op": "replace", "id": execution_id, **body.model_dump()}, command)

    @app.post("/v1/executions/{execution_id}/resume", status_code=201)
    def resume_execution(execution_id: str, body: ResumeExecution, who_identity: Identity = Depends(identity),
                         who: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        """Continue a finished agent execution's provider session with a new prompt (resume_task)."""
        if who_identity.name != "operator":
            raise Forbidden("only the operator can start executions directly")

        def command(uow: UnitOfWork):
            old = services.executions.get(uow, execution_id)
            uow.cur.execute("SELECT key FROM tasks WHERE id = %s", (old["task_id"],))
            task_key = uow.cur.fetchone()["key"]  # type: ignore[index]
            uow.cur.execute("SELECT requested FROM capability_grants WHERE execution_id = %s", (old["id"],))
            requested = uow.cur.fetchone()["requested"]  # type: ignore[index]
            assignment = (old["spec"] or {}).get("assignment") or {}
            request = ExecutionRequest(role=old["role"], prompt=body.prompt, provider=old["provider"], resume=str(old["id"]),
                                       capabilities=requested, resource_profile=old["resource_profile"],
                                       secrets=list(assignment.get("secrets") or []),
                                       max_turns=int(assignment.get("max_turns") or 60), model=assignment.get("model"))
            return 201, execution_view(services.executions.request(uow, principal=who, task_key=task_key, req=request))
        return idempotent(idempotency_key, who, {"op": "resume", "id": execution_id, **body.model_dump()}, command)

    # ---------------------------------------------------------- credentials

    @app.get("/v1/credentials")
    def credentials(_: Identity = Depends(identity)) -> dict[str, Any]:
        with ctx.unit_of_work() as uow:
            return services.credentials.status(uow)

    @app.post("/v1/credentials/{provider}/{credential_identity}/ready")
    def credential_ready(provider: str, credential_identity: str, who_identity: Identity = Depends(identity),
                         who: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        # A human operator attests that they completed the provider login on the host.
        if who_identity.name != "operator":
            raise Forbidden("only the operator can confirm a provider login")

        def command(uow: UnitOfWork):
            return 200, services.credentials.mark_ready(uow, provider, credential_identity, principal=who)
        return idempotent(idempotency_key, who, {"op": "credential-ready", "provider": provider, "identity": credential_identity},
                          command)

    @app.get("/v1/workers")
    def workers(_: Identity = Depends(identity)) -> dict[str, Any]:
        if ctx.agents is None:
            raise UpstreamError("agent-manager is not configured")
        try:
            return {"capacity": ctx.agents.capacity(), "managed": ctx.agents.managed()}
        except AgentManagerError as exc:
            raise UpstreamError(str(exc)) from exc

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
