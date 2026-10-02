"""Task API commands and deterministic state transitions (MASTER_SPEC sections 27-28, 63, 68-70, 76).

Every state change goes through `transition`, which validates it with
ho_core.statemachine, updates the row with an optimistic version check, and
writes the event in the same transaction.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from ho_core import attachments as att
from ho_core import schemas
from ho_core.config import stricter_autonomy
from ho_core.enums import ApprovalAction, BudgetProfile, ProjectStatus, Risk, TaskState
from ho_core.hashing import hash_value
from ho_core.ids import uuid7
from ho_core.statemachine import InvalidTransition, Trigger, transition

from .approvals import Approvals
from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .events import record_event
from .projects import Projects

S = TaskState

# Event emitted in addition to TASK_STATE_CHANGED for states that need attention.
_STATE_EVENTS = {
    S.BLOCKED: "BLOCKED",
    S.PAUSED_BUDGET: "PAUSED_BUDGET",
    S.AUTH_REQUIRED: "AUTH_REQUIRED",
    S.READY_FOR_MERGE: "READY_FOR_MERGE",
    S.DONE: "TASK_COMPLETED",
    S.CANCELLED: "TASK_CANCELLED",
    S.FAILED: "TASK_FAILED",
}
_BUDGET_COUNTERS = ("runtime_minutes", "agent_launches", "retries", "review_cycles", "subtasks", "provider_usage_units")


class Tasks:
    def __init__(self, ctx: Context, projects: Projects, approvals: Approvals) -> None:
        self.ctx = ctx
        self.projects = projects
        self.approvals = approvals
        # Called with (uow, task, reason) before a task is cancelled (for example to stop its executions).
        self.on_cancel: list[Any] = []
        # Called with (uow, updated task, previous state) after every state change (checkpoints, wind-down).
        self.on_transition: list[Any] = []
        approvals.register_handler(ApprovalAction.BUDGET_UNLIMITED, self._on_budget_unlimited_decision)

    # ------------------------------------------------------------------ queries

    def get(self, uow: UnitOfWork, key: str, *, lock: bool = False) -> Row:
        uow.cur.execute(f"SELECT * FROM tasks WHERE key = %s{' FOR UPDATE' if lock else ''}", (key,))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound(f"task {key} not found")
        return row

    def list(self, uow: UnitOfWork, *, project: str | None = None, state: str | None = None, limit: int = 100,
             active: bool = False) -> list[Row]:
        query = "SELECT t.*, p.slug AS project_slug FROM tasks t JOIN projects p ON p.id = t.project_id WHERE true"
        params: list[Any] = []
        if active:  # filtered before the limit, so old active tasks are never hidden by newer finished ones
            query += " AND t.state NOT IN ('DONE', 'CANCELLED', 'FAILED')"
        if project:
            query += " AND p.slug = %s"
            params.append(project)
        if state:
            query += " AND t.state = %s"
            params.append(state)
        query += " ORDER BY t.created_at DESC LIMIT %s"
        params.append(limit)
        uow.cur.execute(query, params)
        return uow.cur.fetchall()

    def summary(self, uow: UnitOfWork, task: Row) -> dict[str, Any]:
        from .views import task_view

        uow.cur.execute("SELECT slug FROM projects WHERE id = %s", (task["project_id"],))
        slug = uow.cur.fetchone()["slug"]  # type: ignore[index]
        uow.cur.execute("SELECT * FROM budgets WHERE task_id = %s", (task["id"],))
        budget = uow.cur.fetchone()
        uow.cur.execute("SELECT id FROM approvals WHERE task_id = %s AND state = 'PENDING'", (task["id"],))
        pending = [str(r["id"]) for r in uow.cur.fetchall()]
        return task_view(task, project_slug=slug, budget=budget, pending_approvals=pending)

    # ----------------------------------------------------------------- commands

    def create(self, uow: UnitOfWork, *, principal: Principal, body: dict[str, Any],
               inherit_attachments_from: UUID | None = None) -> Row:
        body = dict(body)
        files = _decode_attachments(body.pop("attachments", None))
        claimed = body.get("requested_by")
        if claimed is not None and claimed != principal.as_json():
            raise Forbidden("requested_by must match the authenticated principal")
        body["requested_by"] = principal.as_json()
        body.setdefault("schema_version", 1)
        errors = schemas.errors_for("task", body)
        if errors:
            raise ValidationFailed("invalid task request", details=errors)

        project = self.projects.get(uow, body["project"])
        if project["status"] in (ProjectStatus.UNREGISTERED, ProjectStatus.SUSPENDED):
            raise Conflict(f"project {project['slug']} is {project['status']}")
        config = self.projects.active_config(uow, project["id"])
        effective = config["effective_config"] if config else self.ctx.platform["project_defaults"]

        requested_budget = body.get("budget_profile") or effective["budget"]["profile"]
        fallback_budget = effective["budget"]["profile"]
        if fallback_budget == BudgetProfile.UNLIMITED:
            fallback_budget = BudgetProfile.NORMAL.value
        budget_profile = fallback_budget if requested_budget == BudgetProfile.UNLIMITED else requested_budget
        autonomy = stricter_autonomy(effective.get("autonomy"), body.get("autonomy"))

        task_id = uuid7()
        uow.cur.execute("SELECT nextval('task_key_seq') AS n")
        key = f"T-{uow.cur.fetchone()['n']}"  # type: ignore[index]
        title = body.get("title") or body["request"].strip().splitlines()[0][:80]
        artifact = self.ctx.artifacts.write(
            uow.cur, project_id=project["id"], task_id=task_id, kind="request", name="original_request.md",
            content=body["request"].encode(), media_type="text/markdown",
        )
        uow.cur.execute(
            """
            INSERT INTO tasks (id, key, project_id, title, original_request_artifact_id, requested_by, priority, state,
                               autonomy, budget_profile, expansion_profile, labels, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'BACKLOG', %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (task_id, key, project["id"], title, artifact.id, principal.value, body.get("priority", "NORMAL"),
             autonomy, budget_profile, body.get("expansion_profile"), body.get("labels", []), body["idempotency_key"]),
        )
        task = uow.cur.fetchone()
        assert task is not None
        self._create_budget(uow, task_id, budget_profile)
        attached: list[Row] = []
        if files:
            attached = self._attach(uow, task, files, principal)
        elif inherit_attachments_from:
            attached = self._inherit(uow, task, inherit_attachments_from)

        for related in body.get("related_tasks", []):
            target = self.get(uow, related["task"])
            uow.cur.execute(
                "INSERT INTO task_relationships (id, from_task_id, to_task_id, kind, classified_by) VALUES (%s, %s, %s, %s, %s)",
                (uuid7(), task_id, target["id"], related["kind"], principal.value),
            )

        record_event(
            uow.cur, "TASK_CREATED", actor=principal.value, project_id=project["id"], task_id=task_id,
            summary=f"{key} created: {title}",
            data={"priority": task["priority"], "budget_profile": budget_profile, "project_status": project["status"],
                  **({"attachments": [a["name"] for a in attached]} if attached else {})},
            pending=uow.events,
        )
        if requested_budget == BudgetProfile.UNLIMITED:
            self.approvals.request(
                uow, action=ApprovalAction.BUDGET_UNLIMITED, project_id=project["id"], task_id=task_id,
                subject=self._unlimited_subject(task), config_hash=self._config_hash(config),
                requested_by=principal.value, risk=Risk.HIGH, from_task_state=S.BACKLOG.value,
                summary=f"Authorize an UNLIMITED budget for {key}",
            )
            task = self.transition(uow, task, S.APPROVAL_REQUIRED, trigger=Trigger.SYSTEM, actor="control-plane",
                                   reason="UNLIMITED budget requires explicit authorization")
        uow.wake_scheduler = True
        return task

    def transition(self, uow: UnitOfWork, task: Row, target: TaskState, *, trigger: Trigger, actor: str, reason: str | None = None) -> Row:
        current = S(task["state"])
        resume = S(task["resume_state"]) if task["resume_state"] else None
        try:
            result = transition(current, target, trigger=trigger, resume_state=resume)
        except InvalidTransition as exc:
            raise Conflict(f"{task['key']}: cannot move from {current} to {target} ({exc.reason})") from exc
        now = datetime.now(timezone.utc)
        uow.cur.execute(
            """
            UPDATE tasks SET state = %s, resume_state = %s, state_reason = %s, updated_at = %s, version = version + 1,
                             ready_at = CASE WHEN %s = 'READY' THEN %s ELSE ready_at END,
                             completed_at = CASE WHEN %s IN ('DONE', 'CANCELLED', 'FAILED') THEN %s ELSE completed_at END
            WHERE id = %s AND version = %s
            RETURNING *
            """,
            (result.state.value, result.resume_state.value if result.resume_state else None, reason, now,
             result.state.value, now, result.state.value, now, task["id"], task["version"]),
        )
        updated = uow.cur.fetchone()
        if updated is None:
            raise Conflict(f"{task['key']} was modified concurrently; retry")
        data = {"from": current.value, "to": result.state.value, "trigger": trigger.value}
        record_event(
            uow.cur, "TASK_STATE_CHANGED", actor=actor, project_id=task["project_id"], task_id=task["id"],
            summary=f"{task['key']}: {current.value} -> {result.state.value}" + (f" ({reason})" if reason else ""),
            data=data, pending=uow.events,
        )
        extra = _STATE_EVENTS.get(result.state)
        if extra:
            record_event(
                uow.cur, extra, actor=actor, project_id=task["project_id"], task_id=task["id"],
                summary=f"{task['key']}: {reason or result.state.value}", data=data, pending=uow.events,
            )
        uow.wake_scheduler = True
        for hook in self.on_transition:
            hook(uow, updated, current.value)
        return updated

    def pause(self, uow: UnitOfWork, key: str, *, principal: Principal) -> Row:
        task = self.get(uow, key, lock=True)
        # Phase 2 has no running executions, so the pause checkpoint is immediate.
        # Phase 8 adds: stop new actions, let atomic operations finish, checkpoint.
        return self.transition(uow, task, S.PAUSED, trigger=Trigger.USER, actor=principal.value, reason="paused by user")

    def resume(self, uow: UnitOfWork, key: str, *, principal: Principal) -> Row:
        task = self.get(uow, key, lock=True)
        if task["state"] != S.PAUSED:
            raise Conflict(f"{key} is {task['state']}; only PAUSED tasks can be resumed (budget and approval waits resolve through approvals)")
        return self.transition(uow, task, S(task["resume_state"]), trigger=Trigger.USER, actor=principal.value, reason="resumed by user")

    def cancel(self, uow: UnitOfWork, key: str, *, principal: Principal) -> Row:
        task = self.get(uow, key, lock=True)
        self.approvals.invalidate_open(uow, project_id=task["project_id"], task_id=task["id"], action=None, reason="task cancelled")
        for hook in self.on_cancel:
            hook(uow, task, "task cancelled")
        return self.transition(uow, task, S.CANCELLED, trigger=Trigger.USER, actor=principal.value, reason="cancelled by user")

    def retry(self, uow: UnitOfWork, key: str, *, principal: Principal, idempotency_key: str) -> Row:
        task = self.get(uow, key, lock=True)
        if task["state"] == S.BLOCKED:
            return self.transition(uow, task, S.READY, trigger=Trigger.USER, actor=principal.value, reason="retried by user")
        if task["state"] not in (S.FAILED, S.CANCELLED):
            raise Conflict(f"{key} is {task['state']}; only BLOCKED, FAILED, or CANCELLED tasks can be retried")
        # Terminal tasks stay terminal; a retry is a new task linked to the old one.
        uow.cur.execute("SELECT a.path, p.slug FROM artifacts a JOIN projects p ON p.id = a.project_id WHERE a.id = %s",
                        (task["original_request_artifact_id"],))
        found = uow.cur.fetchone()
        assert found is not None
        request_text = self.ctx.artifacts.read(found["path"]).decode()
        new_task = self.create(
            uow, principal=principal,
            body={"project": found["slug"], "title": task["title"], "request": request_text, "priority": task["priority"],
                  "budget_profile": task["budget_profile"], "idempotency_key": idempotency_key},
            inherit_attachments_from=task["id"],  # the same files, so the retry sees what the original saw
        )
        uow.cur.execute(
            "INSERT INTO task_relationships (id, from_task_id, to_task_id, kind, classified_by, evidence) VALUES (%s, %s, %s, 'RELATED', %s, %s)",
            (uuid7(), new_task["id"], task["id"], principal.value, f"retry of {key}"),
        )
        return new_task

    # ------------------------------------------------------------------ attachments

    def attachments(self, uow: UnitOfWork, task_id: UUID) -> list[Row]:
        uow.cur.execute("SELECT ta.*, a.path FROM task_attachments ta JOIN artifacts a ON a.id = ta.artifact_id "
                        "WHERE ta.task_id = %s ORDER BY ta.position", (task_id,))
        return uow.cur.fetchall()

    def _attach(self, uow: UnitOfWork, task: Row, files: list[tuple[att.Checked, bytes]], principal: Principal) -> list[Row]:
        rows = []
        for position, (checked, content) in enumerate(files):
            stored = self.ctx.artifacts.write(uow.cur, project_id=task["project_id"], task_id=task["id"], kind="attachment",
                                              name=checked.name, content=content, media_type=checked.media_type)
            rows.append(self._attachment_row(uow, task["id"], stored.id, checked.name, checked.media_type, stored.size_bytes,
                                             stored.sha256, position, principal.value))
        return rows

    def _inherit(self, uow: UnitOfWork, task: Row, original_id: UUID) -> list[Row]:
        return [self._attachment_row(uow, task["id"], a["artifact_id"], a["name"], a["media_type"], a["size_bytes"], a["sha256"],
                                     a["position"], a["added_by"]) for a in self.attachments(uow, original_id)]

    @staticmethod
    def _attachment_row(uow: UnitOfWork, task_id: UUID, artifact_id: UUID, name: str, media_type: str, size: int, sha: str,
                        position: int, added_by: str) -> Row:
        uow.cur.execute(
            "INSERT INTO task_attachments (id, task_id, artifact_id, name, media_type, size_bytes, sha256, position, added_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (uuid7(), task_id, artifact_id, name, media_type, size, sha, position, added_by))
        row = uow.cur.fetchone()
        assert row is not None
        return row

    # ------------------------------------------------------------------ helpers

    def _create_budget(self, uow: UnitOfWork, task_id: UUID, profile: str) -> None:
        budgets = self.ctx.platform["budgets"]
        limits = {k: None for k in _BUDGET_COUNTERS} if profile == BudgetProfile.UNLIMITED else dict(budgets[profile])
        uow.cur.execute(
            "INSERT INTO budgets (task_id, profile, limits, consumed, thresholds, state) VALUES (%s, %s, %s, %s, %s, 'OK')",
            (task_id, profile, jsonb(limits), jsonb({k: 0 for k in _BUDGET_COUNTERS}), jsonb(budgets["thresholds"])),
        )

    @staticmethod
    def _config_hash(config: Row | None) -> str:
        return config["effective_hash"] if config else hash_value(None)

    @staticmethod
    def _unlimited_subject(task: Row) -> dict[str, Any]:
        return {"task": task["key"], "requested_profile": "UNLIMITED", "project_id": str(task["project_id"])}

    def _on_budget_unlimited_decision(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (approval["task_id"],))
        task = uow.cur.fetchone()
        assert task is not None
        if task["state"] != S.APPROVAL_REQUIRED:
            return  # the task moved on (for example it was cancelled); nothing to apply
        config = self.projects.active_config(uow, task["project_id"])
        if approved:
            if not self.approvals.consume(uow, approval, subject=self._unlimited_subject(task), config_hash=self._config_hash(config)):
                # Something changed since the request: ask again with the current state.
                self.approvals.request(
                    uow, action=ApprovalAction.BUDGET_UNLIMITED, project_id=task["project_id"], task_id=task["id"],
                    subject=self._unlimited_subject(task), config_hash=self._config_hash(config),
                    requested_by="control-plane", risk=Risk.HIGH, from_task_state=task["resume_state"],
                    summary=f"Authorize an UNLIMITED budget for {task['key']} (re-requested after a change)",
                )
                return
            limits = {k: None for k in _BUDGET_COUNTERS}
            uow.cur.execute(
                "UPDATE budgets SET profile = 'UNLIMITED', limits = %s, unlimited_approval_id = %s, updated_at = now() WHERE task_id = %s",
                (jsonb(limits), approval["id"], task["id"]),
            )
            uow.cur.execute("UPDATE tasks SET budget_profile = 'UNLIMITED' WHERE id = %s RETURNING *", (task["id"],))
            task = uow.cur.fetchone()
            reason = "UNLIMITED budget authorized"
        else:
            reason = f"UNLIMITED budget rejected; continuing with {task['budget_profile']}"
        assert task is not None
        self.transition(uow, task, S(task["resume_state"]), trigger=Trigger.APPROVAL, actor=principal.value, reason=reason)



def _decode_attachments(raw: Any) -> list[tuple[att.Checked, bytes]]:
    """`attachments: [{name, content_base64}]` from a creation request, decoded and validated (ho_core.attachments)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(a, dict) or set(a) != {"name", "content_base64"} for a in raw):
        raise ValidationFailed("attachments must be a list of {name, content_base64}")
    if len(raw) > att.MAX_FILES:
        raise ValidationFailed(f"at most {att.MAX_FILES} attachments per task")
    limit = (att.MAX_FILE_BYTES + 2) // 3 * 4  # base64 length of the largest allowed file
    files = []
    for item in raw:
        if not isinstance(item["name"], str) or not isinstance(item["content_base64"], str) or len(item["content_base64"]) > limit:
            raise ValidationFailed(f"attachment {str(item['name'])[:100]!r} is too large or malformed")
        try:
            files.append((item["name"], base64.b64decode(item["content_base64"], validate=True)))
        except (binascii.Error, ValueError) as exc:
            raise ValidationFailed(f"attachment {item['name'][:100]!r} is not valid base64") from exc
    try:
        checked = att.check_all(files)
    except att.InvalidAttachment as exc:
        raise ValidationFailed(str(exc)) from exc
    return [(c, content) for c, (_, content) in zip(checked, files, strict=True)]
