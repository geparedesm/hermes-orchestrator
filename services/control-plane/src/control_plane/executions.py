"""Executions: capability grants, dispatch to Agent Manager, and reconciliation
(MASTER_SPEC sections 10-12, 33-34, 70; ARCHITECTURE.md section 6.3).

Order of operations for every side effect on Docker:
1. decide (Policy Engine grant, capacity, budget) and persist the execution,
   grant, and a PENDING intent in one transaction;
2. after commit, call Agent Manager (idempotent on the execution ID);
3. record the outcome. `sync` repeats step 2 for executions still REQUESTED
   and reconciles running ones with Docker's actual state.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from ho_core.adapters import AgentAssignment, ExecutionResult, FailureClass, OutputBundle, adapter_for, image_suffix
from ho_core.enums import ProjectStatus, Role, TaskState
from ho_core.ids import uuid7
from ho_core.policy.engine import GrantRequest, evaluate_grant
from ho_core.redact import redact
from ho_core.statemachine import ACTIVE_STATES, TERMINAL_STATES, Trigger

from . import budgets
from .agentmgr import AgentManagerError
from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import BadRequest, Conflict, NotFound, UpstreamError
from .events import record_event
from .tasks import Tasks

log = logging.getLogger(__name__)

ACTIVE = ("REQUESTED", "STARTING", "RUNNING", "STOPPING")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "LOST")
AGENT_ROLES = {Role.ORCHESTRATOR, Role.DEVELOPER, Role.REVIEWER}
MAX_DISPATCH_ATTEMPTS = 5
_WORKSPACE = re.compile(r"^\.hermes/worktrees/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FAILURE_BY_CODE = {"capacity_exceeded": "CAPACITY", "auth_required": "AUTH", "rejected": "POLICY",
                    "image_not_allowed": "POLICY", "not_found": "UNKNOWN", "docker_error": "TRANSIENT"}


@dataclass
class ExecutionRequest:
    """One execution. Agent roles take a `prompt` (run through the provider's adapter);
    any role may instead run a raw `command` (operator diagnostics)."""

    role: str
    command: list[str] = field(default_factory=list)
    prompt: str | None = None
    provider: str | None = None
    image: str | None = None  # default: <provider>-<toolchains> or runner-<toolchains>
    workspace: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    resource_profile: str | None = None
    timeout_minutes: int = 60
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)  # secret NAMEs from the project configuration
    resume: str | None = None  # execution whose provider session to continue
    max_turns: int = 60
    model: str | None = None
    inputs: dict[str, str] = field(default_factory=dict)  # small input files (for example runner steps)
    result_schema: str = "agent-result"  # structured answer an agent must return
    purpose: dict[str, Any] = field(default_factory=dict)  # control-plane bookkeeping (review, verification)
    subtask_id: UUID | None = None
    lease_epoch: int | None = None  # orchestration decisions: the lease epoch they were made under (fencing)


class Executions:
    def __init__(self, ctx: Context, tasks: Tasks) -> None:
        self.ctx = ctx
        self.tasks = tasks
        # Set by GitChanges: rejects workspaces that are not the task's own (Phase 5).
        self.workspace_check: Any = None
        # Called with (uow, execution row, ExecutionResult) after an agent execution is parsed.
        self.on_agent_result: list[Any] = []
        # Called with (uow, execution row, final state, failure class) when any execution ends.
        self.on_finished: list[Any] = []
        tasks.on_cancel.append(self.stop_task_executions)

    @property
    def agents(self):
        if self.ctx.agents is None:
            raise UpstreamError("agent-manager is not configured")
        return self.ctx.agents

    # ------------------------------------------------------------------ queries

    def get(self, uow: UnitOfWork, execution_id: UUID | str, *, lock: bool = False) -> Row:
        try:
            UUID(str(execution_id))
        except ValueError as exc:
            raise NotFound(f"execution {execution_id} not found") from exc
        uow.cur.execute(f"SELECT * FROM executions WHERE id = %s{' FOR UPDATE' if lock else ''}", (str(execution_id),))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound(f"execution {execution_id} not found")
        return row

    def list(self, uow: UnitOfWork, *, task: str | None = None, state: str | None = None, limit: int = 100) -> list[Row]:
        query = "SELECT e.*, t.key AS task_key FROM executions e JOIN tasks t ON t.id = e.task_id WHERE true"
        params: list[Any] = []
        if task:
            query += " AND t.key = %s"
            params.append(task)
        if state:
            query += " AND e.state = %s"
            params.append(state)
        uow.cur.execute(query + " ORDER BY e.created_at DESC LIMIT %s", [*params, limit])
        return uow.cur.fetchall()

    def grant(self, uow: UnitOfWork, execution_id: UUID) -> Row | None:
        uow.cur.execute("SELECT * FROM capability_grants WHERE execution_id = %s", (execution_id,))
        return uow.cur.fetchone()

    # ----------------------------------------------------------------- request

    def request(self, uow: UnitOfWork, *, principal: Principal, task_key: str, req: ExecutionRequest,
                replacing: UUID | None = None, retry_of: UUID | None = None, allow_waiting: bool = False) -> Row:
        if self.ctx.agents is None:
            raise UpstreamError("agent-manager is not configured")
        task = self.tasks.get(uow, task_key, lock=True)
        # Post-merge verification is the only work allowed while a task is VERIFYING.
        if TaskState(task["state"]) not in ACTIVE_STATES and not (allow_waiting and task["state"] == TaskState.VERIFYING):
            raise Conflict(f"{task_key} is {task['state']}; executions need an active task")
        uow.cur.execute("SELECT * FROM projects WHERE id = %s", (task["project_id"],))
        project = uow.cur.fetchone()
        assert project is not None
        if project["status"] != ProjectStatus.PROJECT_READY:
            raise Conflict(f"project {project['slug']} is {project['status']}")
        uow.cur.execute("SELECT effective_config FROM project_configs WHERE id = %s", (task["config_id"],))
        config_row = uow.cur.fetchone()
        if config_row is None:
            raise Conflict(f"{task_key} has no pinned project configuration")
        config = config_row["effective_config"]

        try:
            role = Role(req.role)
        except ValueError as exc:
            raise BadRequest(f"unknown role {req.role}") from exc
        if role in AGENT_ROLES:
            if req.provider not in config["agents"]["allowed_providers"]:
                raise BadRequest(f"provider must be one of {config['agents']['allowed_providers']}")
        elif req.provider not in (None, "none"):
            raise BadRequest(f"{role.value} executions do not use a provider")
        provider = req.provider if role in AGENT_ROLES else None
        if bool(req.prompt) == bool(req.command):
            raise BadRequest("give either a prompt (agent roles) or a command")
        if req.prompt and role not in AGENT_ROLES:
            raise BadRequest(f"{role.value} executions run commands; only agent roles take a prompt")

        resume_row = None
        if req.resume:
            if not req.prompt:
                raise BadRequest("resuming a provider session needs a prompt")
            resume_row = self.get(uow, req.resume)
            if resume_row["task_id"] != task["id"] or resume_row["provider"] != provider or not resume_row["agent_run"]:
                raise BadRequest("only an agent execution of the same task and provider can be resumed")
            if resume_row["state"] not in TERMINAL:
                raise Conflict("the execution to resume is still active")
            if not resume_row["provider_session_id"]:
                raise Conflict("that execution has no provider session to resume")
            if req.workspace is None and resume_row["workspace"]:
                req.workspace = resume_row["workspace"][len(project["relative_path"]) + 1:]

        workspace = None
        if req.workspace:
            if not _WORKSPACE.match(req.workspace):
                raise BadRequest("workspace must be .hermes/worktrees/<name>")
            if self.workspace_check is not None:
                self.workspace_check(uow, task, req.workspace)
            workspace = f"{project['relative_path']}/{req.workspace}"
        toolchain = image_suffix((config.get("toolchain") or {}).get("profiles") or ["generic"])
        image = req.image or (f"{provider}-{toolchain}" if provider else f"runner-{toolchain}")

        self._check_capacity(uow, config, role, replacing=replacing)
        epoch = self._lease_epoch(uow, task, req.lease_epoch)
        reservation, req.timeout_minutes = budgets.reserve(uow, task, agent=bool(req.prompt), retry=bool(req.purpose.get("retry")),
                                                           timeout_minutes=req.timeout_minutes)

        execution_id = uuid7()
        caps = dict(req.capabilities)
        unknown = set(caps) - {"workspace", "git", "egress", "test_services", "allowed_domains", "environments",
                               "production", "tests", "artifacts", "project_read"}
        if unknown:
            raise BadRequest(f"unknown capabilities: {sorted(unknown)}")
        grant_request = GrantRequest(
            grant_id=f"G-{execution_id}",
            project=project["slug"],
            task=task["key"],
            execution=str(execution_id),
            worker=f"{provider or role.value.lower()}-{task['key'][2:]}-{str(execution_id)[-8:]}",
            role=role,
            provider=provider,
            workspace=caps.get("workspace", "NONE"),
            git=caps.get("git", "NONE"),
            egress=caps.get("egress", "NONE"),
            test_services=bool(caps.get("test_services", False)),
            allowed_domains=caps.get("allowed_domains", ()),
            secrets=req.secrets,
            environments=caps.get("environments", ()),
            production=caps.get("production", "NONE"),
            tests=caps.get("tests", "NONE"),
            artifacts=caps.get("artifacts", "NONE"),
            project_read=[project["slug"]] if caps.get("project_read") else (),
            resource_profile=req.resource_profile or config["resources"]["default_profile"],
            timeout_minutes=req.timeout_minutes,
            provider_identity=self.ctx.provider_identity,
            lease_epoch=epoch,
        )
        try:
            grant, reductions = evaluate_grant(grant_request, config, self.ctx.platform, now=datetime.now(timezone.utc))
        except (KeyError, ValueError) as exc:
            raise BadRequest(f"invalid capability request: {exc}") from exc

        command = list(req.command)
        inputs: dict[str, str] = dict(req.inputs)
        if req.prompt:
            assert provider is not None
            caps_granted = grant["capabilities"]
            plan = adapter_for(provider).build_execution(AgentAssignment(
                role=role, prompt=req.prompt, toolchain=toolchain, egress=caps_granted["network"]["egress"],
                workspace=caps_granted["workspace"], git=caps_granted["git"],
                resume_session=resume_row["provider_session_id"] if resume_row else None,
                max_turns=req.max_turns, model=req.model, result_schema=req.result_schema,
            ))
            command, inputs = plan.command, {**inputs, **plan.inputs}
            image = req.image or plan.image

        delivery = {s["name"]: s.get("delivery", "file") for s in config.get("secrets", [])}
        production_hosts = config.get("environments", {}).get("production", {}).get("hosts", [])
        spec: dict[str, Any] = {
            "execution_id": str(execution_id),
            "task": task["key"],
            "project": project["slug"],
            "project_path": project["relative_path"],
            "role": role.value,
            "image": image,
            "command": command,
            "env": req.env,
            "grant": grant,
            "denied_domains": production_hosts,
            "secret_env": [ref for ref in grant["capabilities"]["secrets"] if delivery.get(ref.rsplit("/", 1)[1]) == "env"],
        }
        if inputs:
            spec["inputs"] = inputs
        if req.purpose:
            spec["purpose"] = req.purpose
        if req.prompt:
            spec["session"] = True
            # What the agent was asked, so a replacement or resume can be rebuilt (no secrets here).
            spec["assignment"] = {"prompt": req.prompt, "max_turns": req.max_turns, "model": req.model,
                                  "secrets": list(req.secrets), "resume_of": str(resume_row["id"]) if resume_row else None,
                                  "result_schema": req.result_schema}
        if workspace:
            spec["workspace"] = workspace
        if grant["capabilities"]["project_read"]:
            spec["project_read"] = [{"slug": project["slug"], "path": project["relative_path"]}]

        uow.cur.execute(
            """
            INSERT INTO executions (id, task_id, project_id, role, provider, provider_identity, image, command, workspace,
                                    resource_profile, state, spec, requested_by, agent_run, resume_of, lease_epoch,
                                    subtask_id, budget_reservation)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'REQUESTED', %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (execution_id, task["id"], project["id"], role.value, provider or "none",
             grant["provider_credential"]["identity"] if grant["provider_credential"] else None,
             image, jsonb(command), workspace, grant["resources"]["profile"], jsonb(spec), principal.value,
             bool(req.prompt), resume_row["id"] if resume_row else retry_of, epoch, req.subtask_id, jsonb(reservation)),
        )
        row = uow.cur.fetchone()
        assert row is not None
        uow.cur.execute(
            """
            INSERT INTO capability_grants (id, grant_key, execution_id, project_id, task_id, grant_doc, requested,
                                           reductions, issued_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid7(), grant["grant_id"], execution_id, project["id"], task["id"], jsonb(grant), jsonb(caps),
             jsonb(reductions), grant["issued_at"], grant["expires_at"]),
        )
        uow.cur.execute(
            """
            INSERT INTO policy_decisions (id, project_id, task_id, execution_id, subject, decision, rule_ids, summary)
            VALUES (%s, %s, %s, %s, 'CAPABILITY_GRANT', 'ALLOW', %s, %s)
            """,
            (uuid7(), project["id"], task["id"], execution_id, ["GRANT-INTERSECTION"],
             "; ".join(reductions) or "granted as requested"),
        )
        uow.cur.execute(
            "INSERT INTO operation_intents (id, project_id, task_id, kind, target, request, state) VALUES (%s, %s, %s, 'CREATE_EXECUTION', %s, %s, 'PENDING')",
            (uuid7(), project["id"], task["id"], str(execution_id), jsonb({"execution_id": str(execution_id)})),
        )
        record_event(uow.cur, "GRANT_ISSUED", actor="policy-engine", project_id=project["id"], task_id=task["id"],
                     summary=f"{grant['grant_id']} for {role.value} ({grant['capabilities']['network']['egress']} egress)",
                     data={"execution_id": str(execution_id), "reductions": reductions}, pending=uow.events)
        record_event(uow.cur, "AGENT_ASSIGNED", actor=principal.value, project_id=project["id"], task_id=task["id"],
                     summary=f"{role.value} execution requested ({provider or 'runner'}, {grant['resources']['profile']})",
                     data={"execution_id": str(execution_id)}, pending=uow.events)
        uow.after_commit.append(lambda: self.dispatch(execution_id))
        return row

    def _check_capacity(self, uow: UnitOfWork, config: dict[str, Any], role: Role, *, replacing: UUID | None = None) -> None:
        if role not in AGENT_ROLES:
            return
        cap = min(int(self.ctx.platform["machine"]["max_agent_workers"]), int(config["resources"]["max_agent_workers"]))
        # A replacement takes over the slot of the execution it stops.
        uow.cur.execute(
            "SELECT count(*) AS n FROM executions WHERE state IN ('REQUESTED', 'STARTING', 'RUNNING', 'STOPPING') "
            "AND role IN ('ORCHESTRATOR', 'DEVELOPER', 'REVIEWER') AND id IS DISTINCT FROM %s",
            (replacing,),
        )
        if uow.cur.fetchone()["n"] >= cap:  # type: ignore[index]
            raise Conflict(f"agent worker limit reached ({cap}); try again when a worker finishes")

    def _lease_epoch(self, uow: UnitOfWork, task: Row, expected: int | None) -> int:
        """The task's current lease epoch (1 without a lease). Work decided under an older epoch is
        refused here, and again at dispatch (docs/design/phase-7.md, change 1)."""
        uow.cur.execute("SELECT epoch FROM task_leases WHERE task_id = %s FOR UPDATE", (task["id"],))
        lease = uow.cur.fetchone()
        current = int(lease["epoch"]) if lease else 1
        if expected is not None and expected != current:
            raise Conflict(f"{task['key']}: stale orchestration decision (epoch {expected}, lease is at {current})")
        return current

    def pause_for_budget(self, task_key: str, reason: str) -> None:
        with self.ctx.unit_of_work() as uow:
            task = self.tasks.get(uow, task_key, lock=True)
            if TaskState(task["state"]) in ACTIVE_STATES:
                from ho_core.statemachine import Trigger

                self.tasks.transition(uow, task, TaskState.PAUSED_BUDGET, trigger=Trigger.SYSTEM, actor="control-plane", reason=reason)

    # ---------------------------------------------------------------- dispatch

    def dispatch(self, execution_id: UUID | str) -> None:
        with self.ctx.unit_of_work() as uow:
            row = self.get(uow, execution_id, lock=True)
            if row["state"] != "REQUESTED":
                return
            if (row["spec"].get("purpose") or {}).get("fenced"):
                uow.cur.execute("SELECT epoch FROM task_leases WHERE task_id = %s FOR UPDATE", (row["task_id"],))
                lease = uow.cur.fetchone()
                if lease is None or int(lease["epoch"]) != int(row["lease_epoch"]):
                    self._intent(uow, row["id"], "ABANDONED", "lease epoch changed before dispatch")
                    self._finish(uow, row, "CANCELLED", failure_class="POLICY", reason="lease epoch changed before dispatch")
                    return
            uow.cur.execute("UPDATE executions SET dispatch_attempts = dispatch_attempts + 1, updated_at = now() WHERE id = %s",
                            (row["id"],))
            spec, attempts = row["spec"], row["dispatch_attempts"] + 1
        try:
            status = self.agents.create(spec)
        except AgentManagerError as exc:
            with self.ctx.unit_of_work() as uow:
                row = self.get(uow, execution_id, lock=True)
                if row["state"] != "REQUESTED":
                    return
                self._intent(uow, row["id"], "SENT" if exc.retryable else "FAILED", str(exc))
                if exc.retryable and attempts < MAX_DISPATCH_ATTEMPTS:
                    log.warning("dispatch of %s will be retried: %s", execution_id, exc)
                    return
                failure = _FAILURE_BY_CODE.get(exc.code, "UNKNOWN")
                self._finish(uow, row, "FAILED", failure_class=failure, reason=exc.message)
                if failure == "AUTH":
                    self._auth_required(uow, row, exc.message)
            return
        with self.ctx.unit_of_work() as uow:
            row = self.get(uow, execution_id, lock=True)
            if row["state"] != "REQUESTED":
                return
            state = "RUNNING" if status.get("state") in ("running", "exited") else "STARTING"
            uow.cur.execute("UPDATE executions SET state = %s, started_at = now(), updated_at = now(), version = version + 1 WHERE id = %s",
                            (state, row["id"]))
            self._intent(uow, row["id"], "CONFIRMED", None)
            record_event(uow.cur, "WORKER_CREATED", actor="agent-manager", project_id=row["project_id"], task_id=row["task_id"],
                         summary=f"{row['role']} worker started", data={"execution_id": str(row["id"])}, pending=uow.events)

    def _intent(self, uow: UnitOfWork, execution_id: UUID, state: str, error: str | None) -> None:
        uow.cur.execute(
            "UPDATE operation_intents SET state = %s, attempts = attempts + 1, last_error = %s, updated_at = now() "
            "WHERE kind = 'CREATE_EXECUTION' AND target = %s",
            (state, error, str(execution_id)),
        )

    # -------------------------------------------------------------------- stop

    def stop(self, uow: UnitOfWork, execution_id: UUID | str, *, actor: str, reason: str) -> Row:
        row = self.get(uow, execution_id, lock=True)
        if row["state"] in TERMINAL:
            raise Conflict(f"execution is already {row['state']}")
        if row["state"] == "REQUESTED":
            self._intent(uow, row["id"], "ABANDONED", reason)
            self._finish(uow, row, "CANCELLED", failure_class="CANCELLED", reason=reason)
        elif row["state"] != "STOPPING":
            uow.cur.execute(
                "UPDATE executions SET state = 'STOPPING', failure_reason = %s, updated_at = now(), version = version + 1 WHERE id = %s",
                (reason, row["id"]),
            )
            record_event(uow.cur, "WORKER_STOPPING", actor=actor, project_id=row["project_id"], task_id=row["task_id"],
                         summary=f"stopping {row['role']} execution: {reason}", data={"execution_id": str(row["id"])}, pending=uow.events)
            exec_id = str(row["id"])
            uow.after_commit.append(lambda: self._send_stop(exec_id))
        return self.get(uow, row["id"])

    def replace(self, uow: UnitOfWork, execution_id: UUID | str, *, principal: Principal, reason: str) -> Row:
        """Stop an execution and start a new one with the same request (MASTER_SPEC section 11).

        The new execution gets a fresh grant evaluated under current policy; nothing
        from the old grant carries over.
        """
        old = self.get(uow, execution_id, lock=True)
        if old["state"] not in ("STARTING", "RUNNING"):
            raise Conflict(f"only running executions can be replaced (this one is {old['state']})")
        uow.cur.execute("SELECT requested FROM capability_grants WHERE execution_id = %s", (old["id"],))
        requested = uow.cur.fetchone()["requested"]  # type: ignore[index]
        spec = old["spec"]
        workspace = old["workspace"]
        project_prefix = spec["project_path"] + "/"
        uow.cur.execute("SELECT key FROM tasks WHERE id = %s", (old["task_id"],))
        task_key = uow.cur.fetchone()["key"]  # type: ignore[index]
        self.stop(uow, old["id"], actor=principal.value, reason=f"replaced: {reason}")
        assignment = spec.get("assignment") or {}
        request = ExecutionRequest(
            role=old["role"],
            command=[] if old["agent_run"] else list(old["command"]),
            prompt=assignment.get("prompt") if old["agent_run"] else None,
            provider=None if old["provider"] == "none" else old["provider"],
            image=None if old["agent_run"] else old["image"],
            workspace=workspace[len(project_prefix):] if workspace else None,
            capabilities=requested,
            resource_profile=old["resource_profile"],
            timeout_minutes=max(1, round((datetime.fromisoformat(spec["grant"]["expires_at"])
                                          - datetime.fromisoformat(spec["grant"]["issued_at"])).total_seconds() / 60)),
            env=spec.get("env") or {},
            secrets=list(assignment.get("secrets") or []),
            max_turns=int(assignment.get("max_turns") or 60),
            model=assignment.get("model"),
        )
        new = self.request(uow, principal=principal, task_key=task_key, req=request, replacing=old["id"])
        record_event(uow.cur, "WORKER_REPLACED", actor=principal.value, project_id=old["project_id"], task_id=old["task_id"],
                     summary=f"execution {str(old['id'])[:8]} replaced by {str(new['id'])[:8]}: {reason}",
                     data={"old": str(old["id"]), "new": str(new["id"])}, pending=uow.events)
        return new

    CONTINUE_PROMPT = ("Continue the assignment from where you stopped. The previous run was interrupted because "
                       "the model provider login had expired; it has been renewed.")

    def retry_after_auth(self, uow: UnitOfWork, row: Row) -> Row | None:
        """After re-authentication, continue an agent execution that failed with AUTH:
        resume its provider session when one exists, otherwise run the assignment again."""
        assignment = (row["spec"] or {}).get("assignment")
        if not row["agent_run"] or not assignment:
            return None
        if ((row["spec"] or {}).get("purpose") or {}).get("orchestrator_step"):
            return None  # orchestration asks for a fresh step instead (its context and epoch would be stale)
        uow.cur.execute("SELECT requested FROM capability_grants WHERE execution_id = %s", (row["id"],))
        requested = uow.cur.fetchone()["requested"]  # type: ignore[index]
        uow.cur.execute("SELECT key FROM tasks WHERE id = %s", (row["task_id"],))
        task_key = uow.cur.fetchone()["key"]  # type: ignore[index]
        spec = row["spec"]
        prefix = spec["project_path"] + "/"
        req = ExecutionRequest(
            role=row["role"],
            prompt=self.CONTINUE_PROMPT if row["provider_session_id"] else assignment["prompt"],
            provider=row["provider"],
            workspace=row["workspace"][len(prefix):] if row["workspace"] else None,
            capabilities=requested,
            resource_profile=row["resource_profile"],
            timeout_minutes=max(1, round((datetime.fromisoformat(spec["grant"]["expires_at"])
                                          - datetime.fromisoformat(spec["grant"]["issued_at"])).total_seconds() / 60)),
            env=spec.get("env") or {},
            secrets=list(assignment.get("secrets") or []),
            resume=str(row["id"]) if row["provider_session_id"] else None,
            max_turns=int(assignment.get("max_turns") or 60),
            model=assignment.get("model"),
            result_schema=assignment.get("result_schema") or "agent-result",
            purpose=spec.get("purpose") or {},
            subtask_id=row["subtask_id"],
        )
        return self.request(uow, principal=Principal("control-plane", "auth-resume"), task_key=task_key, req=req,
                            retry_of=None if row["provider_session_id"] else row["id"])

    def _send_stop(self, execution_id: str) -> None:
        try:
            self.agents.stop(execution_id)
        except AgentManagerError as exc:
            log.warning("stop of %s not delivered yet; sync will retry: %s", execution_id, exc)

    def stop_task_executions(self, uow: UnitOfWork, task: Row, reason: str) -> None:
        uow.cur.execute("SELECT id FROM executions WHERE task_id = %s AND state IN ('REQUESTED', 'STARTING', 'RUNNING')", (task["id"],))
        for row in uow.cur.fetchall():
            self.stop(uow, row["id"], actor="control-plane", reason=reason)

    # -------------------------------------------------------------------- sync

    def sync(self) -> dict[str, int]:
        """Reconcile active executions with Agent Manager (called by the scheduler loop)."""
        stats = {"dispatched": 0, "finished": 0, "timed_out": 0}
        if self.ctx.agents is None:
            return stats
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                "SELECT e.id, e.state, e.updated_at, g.expires_at FROM executions e "
                "LEFT JOIN capability_grants g ON g.execution_id = e.id WHERE e.state = ANY(%s)",
                (list(ACTIVE),),
            )
            rows = uow.cur.fetchall()
        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                if row["state"] == "REQUESTED":
                    if now - row["updated_at"] > timedelta(seconds=5):
                        self.dispatch(row["id"])
                        stats["dispatched"] += 1
                    continue
                status = self.agents.status(str(row["id"]))
                if status["state"] in ("absent", "exited"):
                    self._finalize(row["id"], status)
                    stats["finished"] += 1
                elif row["state"] == "STOPPING":
                    self._send_stop(str(row["id"]))
                elif row["expires_at"] and row["expires_at"] <= now:
                    with self.ctx.unit_of_work() as uow:
                        current = self.get(uow, row["id"], lock=True)
                        uow.cur.execute("UPDATE executions SET failure_class = 'TIMEOUT' WHERE id = %s", (current["id"],))
                        self.stop(uow, current["id"], actor="control-plane", reason="capability grant expired")
                    stats["timed_out"] += 1
            except AgentManagerError as exc:
                log.warning("sync of execution %s deferred: %s", row["id"], exc)
        stats["released"] = self._release_task_environments()
        return stats

    def _release_task_environments(self) -> int:
        """Remove service networks and session volumes of tasks that ended (their workers are gone)."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                """
                SELECT t.id, t.key FROM tasks t
                WHERE t.state = ANY(%s) AND t.environment_released_at IS NULL
                  AND EXISTS (SELECT 1 FROM executions e WHERE e.task_id = t.id)
                  AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.task_id = t.id AND e.state = ANY(%s))
                LIMIT 20
                """,
                ([s.value for s in TERMINAL_STATES], list(ACTIVE)),
            )
            tasks = uow.cur.fetchall()
        released = 0
        for task in tasks:
            try:
                self.agents.remove_task_environment(task["key"])
            except AgentManagerError as exc:
                log.warning("environment of %s not released yet: %s", task["key"], exc)
                continue
            with self.ctx.unit_of_work() as uow:
                uow.cur.execute("UPDATE tasks SET environment_released_at = now() WHERE id = %s", (task["id"],))
            released += 1
        return released

    def _finalize(self, execution_id: UUID, status: dict[str, Any]) -> None:
        collected: dict[str, Any] = {}
        if status["state"] == "exited":
            try:
                collected = self.agents.collect(str(execution_id))
            except AgentManagerError as exc:
                log.warning("could not collect output of %s: %s", execution_id, exc)
        files = {path: base64.b64decode(encoded) for path, encoded in (collected.get("files") or {}).items()}
        with self.ctx.unit_of_work() as uow:
            row = self.get(uow, execution_id, lock=True)
            if row["state"] in TERMINAL:
                return
            result: ExecutionResult | None = None
            if row["agent_run"] and status["state"] == "exited":
                adapter = adapter_for(row["provider"])
                bundle = OutputBundle(files=files, logs=collected.get("logs") or "", exit_code=status.get("exit_code"),
                                      oom_killed=bool(status.get("oom_killed")))
                schema = ((row["spec"] or {}).get("assignment") or {}).get("result_schema", "agent-result")
                result = adapter.collect_result(bundle, schema)
                self._record_agent_result(uow, row, result, adapter.collect_usage(bundle).units)
                for hook in self.on_agent_result:
                    hook(uow, row, result)
            artifact_ids = self._store_outputs(uow, row, files, collected, result)
            if status["state"] == "absent":
                state, failure, reason = "LOST", "LOST", "container disappeared without a result"
            elif row["state"] == "STOPPING":
                timed_out = row["failure_class"] == "TIMEOUT"
                state, failure, reason = ("FAILED" if timed_out else "CANCELLED"), ("TIMEOUT" if timed_out else "CANCELLED"), row["failure_reason"]
            elif result is not None:
                if status.get("exit_code") == 0 and result.ok:
                    state, failure, reason = "SUCCEEDED", None, None
                else:
                    state = "FAILED"
                    failure = (result.failure_class or FailureClass.TASK).value
                    reason = "out of memory" if status.get("oom_killed") else (result.error or f"exit code {status.get('exit_code')}")
            elif status.get("exit_code") == 0:
                state, failure, reason = "SUCCEEDED", None, None
            else:
                state, failure = "FAILED", "TASK"
                reason = "out of memory" if status.get("oom_killed") else f"exit code {status.get('exit_code')}"
            if failure == "AUTH":  # before _finish, so its hooks see the task waiting for the login
                self._auth_required(uow, row, reason or "provider authentication failed")
            self._finish(uow, row, state, failure_class=failure, reason=redact(reason)[0] if reason else None,
                         exit_code=status.get("exit_code"), artifacts=artifact_ids)
            exec_id = str(row["id"])
            uow.after_commit.append(lambda: self._remove(exec_id))

    def _record_agent_result(self, uow: UnitOfWork, row: Row, result: ExecutionResult, units: dict[str, Any]) -> None:
        clean = json.loads(redact(json.dumps(result.as_json()))[0])
        uow.cur.execute("UPDATE executions SET provider_session_id = %s, result = %s WHERE id = %s",
                        (result.session_id, jsonb(clean), row["id"]))
        wall = int((datetime.now(timezone.utc) - row["started_at"]).total_seconds()) if row["started_at"] else None
        uow.cur.execute(
            "INSERT INTO usage_records (id, execution_id, task_id, project_id, provider, units, wall_seconds) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (execution_id) DO NOTHING",
            (uuid7(), row["id"], row["task_id"], row["project_id"], row["provider"], jsonb(units), wall),
        )
        if result.ok:
            self._set_credential(uow, row["provider"], row["provider_identity"] or "default", "READY", None)
        if result.high_risk_commands:
            record_event(uow.cur, "COMMAND_HIGH_RISK", actor="agent-manager", project_id=row["project_id"], task_id=row["task_id"],
                         summary=f"{result.high_risk_commands} high-risk command(s) observed in execution {str(row['id'])[:8]} (advisory)",
                         data={"execution_id": str(row["id"])}, pending=uow.events)

    def _set_credential(self, uow: UnitOfWork, provider: str, identity: str, status: str, error: str | None) -> None:
        uow.cur.execute(
            """
            INSERT INTO credential_refs (provider, identity, status, last_verified_at, last_error, updated_at)
            VALUES (%s, %s, %s, CASE WHEN %s = 'READY' THEN now() END, %s, now())
            ON CONFLICT (provider, identity) DO UPDATE SET status = EXCLUDED.status, last_error = EXCLUDED.last_error,
                last_verified_at = COALESCE(EXCLUDED.last_verified_at, credential_refs.last_verified_at), updated_at = now()
            """,
            (provider, identity, status, status, error),
        )

    def _auth_required(self, uow: UnitOfWork, row: Row, reason: str) -> None:
        """Provider session expired or missing: mark the identity and wait for re-authentication (section 20)."""
        identity = f"{row['provider']}/{row['provider_identity'] or 'default'}"
        self._set_credential(uow, row["provider"], row["provider_identity"] or "default", "AUTH_REQUIRED", redact(reason)[0][:300])
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (row["task_id"],))
        task = uow.cur.fetchone()
        assert task is not None
        if TaskState(task["state"]) in ACTIVE_STATES:
            self.tasks.transition(uow, task, TaskState.AUTH_REQUIRED, trigger=Trigger.SYSTEM, actor="control-plane",
                                  reason=f"{identity} needs re-authentication (make auth-{row['provider']})")
            uow.cur.execute("UPDATE tasks SET waiting_on_credential = %s WHERE id = %s", (identity, task["id"]))
        else:
            record_event(uow.cur, "AUTH_REQUIRED", actor="control-plane", project_id=row["project_id"], task_id=row["task_id"],
                         summary=f"{identity} needs re-authentication", data={"execution_id": str(row["id"])}, pending=uow.events)

    def _remove(self, execution_id: str) -> None:
        try:
            self.agents.remove(execution_id)
        except AgentManagerError as exc:
            log.warning("cleanup of %s deferred: %s", execution_id, exc)

    def _store_outputs(self, uow: UnitOfWork, row: Row, files: dict[str, bytes], collected: dict[str, Any],
                       result: ExecutionResult | None) -> list[UUID]:
        ids: list[UUID] = []

        def store(name: str, content: bytes, media_type: str) -> None:
            artifact = self.ctx.artifacts.write(uow.cur, project_id=row["project_id"], task_id=row["task_id"],
                                                kind=f"executions/{row['id']}", name=name, content=content, media_type=media_type)
            ids.append(artifact.id)

        for path, content in sorted(files.items()):
            if row["agent_run"] and path.startswith("ho/"):
                continue  # raw provider stream: parsed by the adapter, never stored (section 86)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", path.replace("/", "__"))[:120] or "output"
            try:
                text, _ = redact(content.decode("utf-8"))
                store(safe.lstrip("."), text.encode(), "text/plain")
            except UnicodeDecodeError:
                store(safe.lstrip("."), content, "application/octet-stream")
        if result is not None:
            store("result.json", redact(json.dumps(result.as_json(), indent=2, sort_keys=True))[0].encode(), "application/json")
            if result.events:
                lines = "\n".join(json.dumps({"type": e.type, **e.data}, sort_keys=True) for e in result.events)
                store("events.jsonl", redact(lines)[0].encode(), "application/x-ndjson")
        if collected.get("logs"):
            text, _ = redact(collected["logs"])
            store("logs.txt", text.encode(), "text/plain")
        if collected.get("egress"):
            store("egress.jsonl", "\n".join(json.dumps(e, sort_keys=True) for e in collected["egress"]).encode(), "application/x-ndjson")
        return ids

    def _finish(self, uow: UnitOfWork, row: Row, state: str, *, failure_class: str | None, reason: str | None,
                exit_code: int | None = None, artifacts: list[UUID] | None = None) -> None:
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s", (row["task_id"],))
        task = uow.cur.fetchone()
        reservation = row.get("budget_reservation")
        if reservation and task is not None:
            uow.cur.execute("SELECT units FROM usage_records WHERE execution_id = %s", (row["id"],))
            usage = uow.cur.fetchone()
            if usage:
                budgets.settle(uow, task, reservation, usage_units=budgets.usage_units(usage["units"]), lost=False)
            elif row["state"] == "REQUESTED":
                budgets.release(uow, task, reservation)  # never started
            else:
                budgets.settle(uow, task, reservation, usage_units=None, lost=True)
            uow.cur.execute("UPDATE executions SET budget_reservation = NULL WHERE id = %s", (row["id"],))
        uow.cur.execute(
            """
            UPDATE executions SET state = %s, failure_class = %s, failure_reason = %s, exit_code = %s,
                   result_artifact_ids = %s, ended_at = now(), updated_at = now(), version = version + 1
            WHERE id = %s
            """,
            (state, failure_class, reason, exit_code, artifacts or [], row["id"]),
        )
        uow.cur.execute(
            "UPDATE capability_grants SET revoked_at = now(), revoked_reason = %s WHERE execution_id = %s AND revoked_at IS NULL",
            (f"execution {state.lower()}", row["id"]),
        )
        record_event(uow.cur, "WORKER_STOPPED", actor="control-plane", project_id=row["project_id"], task_id=row["task_id"],
                     summary=f"{row['role']} execution {state}" + (f": {reason}" if reason else ""),
                     data={"execution_id": str(row["id"]), "state": state, "exit_code": exit_code, "failure_class": failure_class},
                     pending=uow.events)
        record_event(uow.cur, "GRANT_REVOKED", actor="control-plane", project_id=row["project_id"], task_id=row["task_id"],
                     summary=f"grant for execution {str(row['id'])[:8]} revoked", data={"execution_id": str(row["id"])},
                     pending=uow.events)
        for hook in self.on_finished:
            hook(uow, row, state, failure_class)
