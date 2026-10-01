"""Recovery (MASTER_SPEC sections 6 and 64-68; ARCHITECTURE section 14; DATA_MODEL section 5; docs/design/phase-8.md).

PostgreSQL is the truth. Containers, Redis, and in-memory state are rebuilt from it; dead workers are
never resurrected; every recovery decision is an event.

Checkpoints     a snapshot of a task's durable state at every state change, accepted step, and result.
Health          consecutive failures of Agent Manager, Git Service, Redis, and Hermes; DEGRADED after a
                threshold, with attention events on both edges.
Outbox          delivers pending notifications to Hermes with capped exponential backoff; while Hermes is
                unreachable they stay pending and authorized work continues.
Recovery        reconciliation at startup (before the scheduler's first pass), periodically, and on demand:
                executions vs managed containers, orphaned resources, stale intents, missing workspaces,
                verifications never launched, ended tasks' leases and queued launches, cancelled work retained.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from ho_core.ids import uuid7
from ho_core.statemachine import TERMINAL_STATES

from .agentmgr import AgentManagerError
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import ApiError
from .events import record_event
from .executions import ACTIVE, TERMINAL, Executions
from .gitops import GitChanges
from .tasks import Tasks
from .verification import Verifications

log = logging.getLogger(__name__)
DEGRADED_AFTER = 3  # consecutive failed checks
INTENT_GRACE = timedelta(minutes=2)
PERIODIC_EVERY = 12  # scheduler passes between periodic reconciliations
OUTBOX_BATCH = 50
OUTBOX_MAX_DELAY = 900  # seconds


# ------------------------------------------------------------------ checkpoints


def checkpoint(uow: UnitOfWork, task: Row, reason: str) -> int:
    """Record the task's durable state; returns the checkpoint sequence number."""
    tid = task["id"]
    uow.cur.execute("SELECT key, local_key, state, developer_provider, workspace_id, review_cycles, attempts FROM subtasks "
                    "WHERE task_id = %s AND plan_version = %s ORDER BY key", (tid, task.get("current_plan_version") or 0))
    subtasks = uow.cur.fetchall()
    uow.cur.execute("SELECT w.id, w.name, w.head_sha FROM workspaces w WHERE w.task_id = %s AND w.status = 'ACTIVE'", (tid,))
    heads = {str(w["id"]): {"name": w["name"], "head": w["head_sha"]} for w in uow.cur.fetchall()}
    uow.cur.execute("SELECT provider, epoch, holder FROM task_leases WHERE task_id = %s", (tid,))
    lease = uow.cur.fetchone()
    uow.cur.execute("SELECT consumed, reserved FROM budgets WHERE task_id = %s", (tid,))
    budget = uow.cur.fetchone()
    uow.cur.execute("SELECT id, action FROM approvals WHERE task_id = %s AND state IN ('PENDING', 'APPROVED')", (tid,))
    approvals = [{"id": str(a["id"]), "action": a["action"]} for a in uow.cur.fetchall()]
    uow.cur.execute("SELECT kind, count(*) AS n FROM pending_launches WHERE task_id = %s GROUP BY kind", (tid,))
    pending = {r["kind"]: int(r["n"]) for r in uow.cur.fetchall()}
    uow.cur.execute("SELECT integration_sha, merge_commit_sha FROM git_changes WHERE task_id = %s", (tid,))
    changes = uow.cur.fetchone() or {}
    snapshot = {
        "state": task["state"], "resume_state": task.get("resume_state"),
        "requirements_version": task.get("current_requirements_version"), "plan_version": task.get("current_plan_version"),
        "orchestrator_cursor": task.get("orchestrator_cursor_seq"),
        "subtasks": [{**{k: s[k] for k in ("key", "local_key", "state", "developer_provider", "review_cycles", "attempts")},
                      "workspace": heads.get(str(s["workspace_id"]))} for s in subtasks],
        "lease": {"provider": lease["provider"], "epoch": int(lease["epoch"]), "holder": lease["holder"]} if lease else None,
        "budget": {"consumed": budget["consumed"], "reserved": budget.get("reserved") or {}} if budget else None,
        "open_approvals": approvals, "pending_launches": pending,
        "integration_sha": changes.get("integration_sha"), "merge_commit_sha": changes.get("merge_commit_sha"),
    }
    uow.cur.execute(
        "INSERT INTO task_checkpoints (id, task_id, seq, reason, state, epoch, snapshot) "
        "SELECT %s, %s, COALESCE(max(seq), 0) + 1, %s, %s, %s, %s FROM task_checkpoints WHERE task_id = %s RETURNING seq",
        (uuid7(), tid, reason[:200], task["state"], snapshot["lease"]["epoch"] if lease else None, jsonb(snapshot), tid))
    return int(uow.cur.fetchone()["seq"])  # type: ignore[index]


def latest_checkpoint(uow: UnitOfWork, task_id: UUID) -> Row | None:
    uow.cur.execute("SELECT * FROM task_checkpoints WHERE task_id = %s ORDER BY seq DESC LIMIT 1", (task_id,))
    return uow.cur.fetchone()


def at_safe_checkpoint(uow: UnitOfWork, task: Row) -> bool:
    """No orchestrator step in flight and no fenced launch of the current epoch queued (design, Codex finding 2)."""
    uow.cur.execute("SELECT 1 FROM executions WHERE task_id = %s AND role = 'ORCHESTRATOR' AND state = ANY(%s)",
                    (task["id"], list(ACTIVE)))
    if uow.cur.fetchone() is not None:
        return False
    uow.cur.execute("SELECT 1 FROM pending_launches WHERE task_id = %s AND (request->'purpose'->>'fenced')::boolean IS TRUE",
                    (task["id"],))
    return uow.cur.fetchone() is None


# ------------------------------------------------------------------ health


class Health:
    COMPONENTS = ("agent_manager", "git_service", "redis")

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def check(self) -> dict[str, str]:
        probes = {"agent_manager": lambda: self.ctx.agents is None or self.ctx.agents.ping(),
                  "git_service": self.ctx.git.ping,
                  "redis": lambda: not self.ctx.coordinator.enabled or self.ctx.coordinator.ping()}
        states = {}
        for component, probe in probes.items():
            try:
                ok, error = bool(probe()), None
            except Exception as exc:  # noqa: BLE001 - any failure counts as unhealthy
                ok, error = False, str(exc)[:300]
            states[component] = self.record(component, ok, error)
        return states

    def record(self, component: str, ok: bool, error: str | None = None) -> str:
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("INSERT INTO component_health (component, state) VALUES (%s, 'HEALTHY') ON CONFLICT DO NOTHING",
                            (component,))
            uow.cur.execute("SELECT * FROM component_health WHERE component = %s FOR UPDATE", (component,))
            row = uow.cur.fetchone()
            assert row is not None
            failures = 0 if ok else int(row["consecutive_failures"]) + 1
            state = "HEALTHY" if ok else ("DEGRADED" if failures >= DEGRADED_AFTER else row["state"])
            uow.cur.execute("UPDATE component_health SET state = %s, consecutive_failures = %s, last_error = %s, checked_at = now(), "
                            "since = CASE WHEN state <> %s THEN now() ELSE since END WHERE component = %s",
                            (state, failures, None if ok else (error or "health check failed"), state, component))
            if state != row["state"]:
                event = "PLATFORM_DEGRADED" if state == "DEGRADED" else "PLATFORM_RECOVERED"
                record_event(uow.cur, event, actor="control-plane",
                             summary=f"{component.replace('_', ' ')} " + (f"unavailable after {failures} checks: {error or ''}"
                                                                            if state == "DEGRADED" else "available again"),
                             data={"component": component, "state": state}, pending=uow.events)
            return state

    def degraded(self, uow: UnitOfWork, component: str) -> bool:
        uow.cur.execute("SELECT state FROM component_health WHERE component = %s", (component,))
        row = uow.cur.fetchone()
        return bool(row and row["state"] == "DEGRADED")

    def summary(self, uow: UnitOfWork) -> dict[str, Any]:
        uow.cur.execute("SELECT * FROM component_health ORDER BY component")
        rows = uow.cur.fetchall()
        return {"state": "DEGRADED" if any(r["state"] == "DEGRADED" for r in rows) else "HEALTHY",
                "components": {r["component"]: {"state": r["state"], "consecutive_failures": r["consecutive_failures"],
                                                "last_error": r["last_error"], "since": r["since"]} for r in rows}}


# ------------------------------------------------------------------ outbox


def backoff_seconds(attempts: int) -> int:
    """10 s, 20 s, 40 s, ... capped at 15 minutes: notifications are retried until Hermes returns."""
    return min(10 * 2 ** max(0, attempts - 1), OUTBOX_MAX_DELAY)


class Outbox:
    def __init__(self, ctx: Context, health: Health, url: str | None, token: str | None,
                 client: httpx.Client | None = None) -> None:
        self.ctx = ctx
        self.health = health
        self.url = url
        self.token = token
        self.client = client or httpx.Client(timeout=5)

    def deliver(self) -> dict[str, int]:
        stats = {"sent": 0, "failed": 0}
        if not self.url:
            return stats  # Hermes integration (Phase 9) not configured: notifications wait in the outbox
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT * FROM notifications WHERE state = 'PENDING' AND next_attempt_at <= now() "
                            "ORDER BY priority, created_at LIMIT %s", (OUTBOX_BATCH,))
            rows = uow.cur.fetchall()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        for row in rows:
            try:
                response = self.client.post(self.url, json={"id": str(row["id"]), "priority": row["priority"],
                                                            "event_seq": row["event_seq"], "payload": row["payload"]}, headers=headers)
                response.raise_for_status()
                error = None
            except httpx.HTTPError as exc:
                error = str(exc)[:300] or type(exc).__name__
            with self.ctx.unit_of_work() as uow:
                if error is None:
                    uow.cur.execute("UPDATE notifications SET state = 'SENT', delivered_at = now(), attempts = attempts + 1, "
                                    "last_error = NULL WHERE id = %s", (row["id"],))
                    stats["sent"] += 1
                else:
                    attempts = int(row["attempts"]) + 1
                    uow.cur.execute("UPDATE notifications SET attempts = %s, last_error = %s, next_attempt_at = now() + %s "
                                    "WHERE id = %s", (attempts, error, timedelta(seconds=backoff_seconds(attempts)), row["id"]))
                    stats["failed"] += 1
            if error is not None:
                self.health.record("hermes", False, error)
                break  # Hermes is down: keep the order, try again after the backoff
            self.health.record("hermes", True)
        return stats


# ------------------------------------------------------------------ recovery controller


class Recovery:
    def __init__(self, ctx: Context, tasks: Tasks, executions: Executions, git: GitChanges, verifications: Verifications,
                 health: Health, orchestration: Any = None) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.executions = executions
        self.git = git
        self.verifications = verifications
        self.health = health
        self.orchestration = orchestration
        self._passes = 0
        tasks.on_transition.append(self._on_transition)
        executions.on_finished.append(lambda uow, row, state, failure: self._checkpoint_task(uow, row["task_id"],
                                                                                              f"execution {state.lower()}"))

    # ---------------------------------------------------------------- hooks

    def _checkpoint_task(self, uow: UnitOfWork, task_id: UUID, reason: str) -> None:
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
        task = uow.cur.fetchone()
        if task is not None:
            checkpoint(uow, task, reason)

    def _on_transition(self, uow: UnitOfWork, task: Row, previous: str) -> None:
        checkpoint(uow, task, f"{previous} -> {task['state']}")
        if task["state"] in TERMINAL_STATES:
            self._wind_down(uow, task)

    def _wind_down(self, uow: UnitOfWork, task: Row) -> None:
        """A task ended (cancelled, failed, done): stop its work, drop queued launches and its lease.
        Workspaces are retained once their executions have stopped (see `_retain`)."""
        uow.cur.execute("SELECT id FROM executions WHERE task_id = %s AND state = ANY(%s)", (task["id"], list(ACTIVE)))
        for row in uow.cur.fetchall():
            try:
                with uow.cur.connection.transaction():
                    self.executions.stop(uow, row["id"], actor="control-plane", reason=f"task {task['state'].lower()}")
            except ApiError as exc:
                log.warning("could not stop %s: %s", row["id"], exc)
        uow.cur.execute("DELETE FROM pending_launches WHERE task_id = %s", (task["id"],))
        uow.cur.execute("DELETE FROM task_leases WHERE task_id = %s", (task["id"],))

    # ---------------------------------------------------------------- passes

    def startup(self) -> dict[str, Any]:
        return self.run("STARTUP")

    def periodic(self) -> None:
        self.health.check()
        self._passes += 1
        if self._passes % PERIODIC_EVERY == 0:
            self.run("PERIODIC")

    def run(self, trigger: str) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        report: dict[str, Any] = {"health": self.health.check()}
        steps = [("executions", self._reconcile_executions), ("orphans", self._remove_orphans),
                 ("intents", self._reconcile_intents), ("verifications", self._relaunch_verifications),
                 ("ended_tasks", self._ended_tasks), ("retained", self._retain)]
        if trigger in ("STARTUP", "OPERATOR"):
            steps.append(("workspaces", self._check_workspaces))
        if self.orchestration is not None:
            steps.append(("leases_adopted", self.orchestration.adopt_orphans))
        for name, step in steps:
            try:
                report[name] = step()
            except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
                log.exception("recovery step %s failed", name)
                report[name] = {"error": str(exc)[:300]}
        self.ctx.coordinator.wake_scheduler("recovery")  # Redis keeps no state: a wake-up is all it needs
        report["seconds"] = round(time.monotonic() - t0, 2)
        changed = any(isinstance(v, dict) and any(isinstance(n, int) and n for n in v.values()) or (isinstance(v, int) and v)
                      for k, v in report.items() if k not in ("health", "seconds"))
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("INSERT INTO recovery_runs (id, trigger, holder, report, started_at) VALUES (%s, %s, %s, %s, %s)",
                            (uuid7(), trigger, self.ctx.instance_id, jsonb(report), started))
            if trigger != "PERIODIC" or changed:
                record_event(uow.cur, "RECOVERY_COMPLETED", actor="control-plane",
                             summary=f"{trigger.lower()} reconciliation: " + _describe(report), data=report, pending=uow.events)
        return report

    def _reconcile_executions(self) -> dict[str, int]:
        """Active executions vs Agent Manager: finished or vanished containers are finalized (LOST when absent);
        REQUESTED ones are dispatched again (Agent Manager creation is idempotent per execution)."""
        stats = {"finalized": 0, "dispatched": 0}
        if self.ctx.agents is None:
            return stats
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT id, state FROM executions WHERE state = ANY(%s)", (list(ACTIVE),))
            rows = uow.cur.fetchall()
        for row in rows:
            try:
                if row["state"] == "REQUESTED":
                    self.executions.dispatch(row["id"])
                    stats["dispatched"] += 1
                    continue
                status = self.ctx.agents.status(str(row["id"]))
                if status["state"] in ("absent", "exited"):
                    self.executions._finalize(row["id"], status)
                    stats["finalized"] += 1
            except AgentManagerError as exc:
                log.warning("recovery of execution %s deferred: %s", row["id"], exc)
        return stats

    def _remove_orphans(self) -> dict[str, int]:
        """Resources labelled for executions the database does not know, or knows as finished. A finished execution's
        output was collected before it was marked terminal (finalize collects first), so removing is safe."""
        stats = {"removed": 0}
        if self.ctx.agents is None:
            return stats
        managed = self.ctx.agents.managed()
        ids = {c["labels"].get("ho.execution") for c in managed.get("containers", []) if c["labels"].get("ho.execution")}
        if not ids:
            return stats
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT id::text AS id, state FROM executions WHERE id::text = ANY(%s)", (sorted(ids),))
            known = {r["id"]: r["state"] for r in uow.cur.fetchall()}
        for execution in sorted(ids):
            if execution in known and known[execution] not in TERMINAL:
                continue
            try:
                self.ctx.agents.remove(execution)
                stats["removed"] += 1
            except AgentManagerError as exc:
                log.warning("could not remove orphan %s: %s", execution, exc)
        return stats

    def _reconcile_intents(self) -> dict[str, int]:
        """Intents left PENDING/SENT (a crash between the call and recording its outcome) are resolved from the
        state the call would have produced."""
        stats = {"confirmed": 0, "abandoned": 0}
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT * FROM operation_intents WHERE state IN ('PENDING', 'SENT') AND updated_at < now() - %s "
                            "FOR UPDATE SKIP LOCKED", (INTENT_GRACE,))
            for intent in uow.cur.fetchall():
                outcome = None
                if intent["kind"] == "CREATE_EXECUTION":
                    uow.cur.execute("SELECT state, started_at FROM executions WHERE id::text = %s", (intent["target"],))
                    row = uow.cur.fetchone()
                    if row is None or (row["state"] in TERMINAL and row["started_at"] is None):
                        outcome = "ABANDONED"  # never started (cancelled or refused before dispatch)
                    elif row["started_at"] is not None:
                        outcome = "CONFIRMED"  # the worker was created
                    # still REQUESTED: Executions.sync dispatches it again (creation is idempotent per execution)
                elif intent["kind"] == "MERGE":
                    uow.cur.execute("SELECT t.state, g.merge_commit_sha FROM tasks t LEFT JOIN git_changes g ON g.task_id = t.id "
                                    "WHERE t.id = %s", (intent["task_id"],))
                    row = uow.cur.fetchone()
                    if row and row["merge_commit_sha"]:
                        outcome = "CONFIRMED"
                    elif row and row["state"] != "MERGING":
                        outcome = "ABANDONED"
                    # still MERGING: the merge is re-sent by GitChanges.sync (idempotent per approval)
                if outcome:
                    uow.cur.execute("UPDATE operation_intents SET state = %s, last_error = %s, updated_at = now() WHERE id = %s",
                                    (outcome, "reconciled by recovery", intent["id"]))
                    stats["confirmed" if outcome == "CONFIRMED" else "abandoned"] += 1
        return stats

    def _relaunch_verifications(self) -> dict[str, int]:
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT v.id FROM verifications v JOIN tasks t ON t.id = v.task_id WHERE v.state = 'PREPARING' "
                            "AND cardinality(v.execution_ids) = 0 AND v.created_at < now() - %s AND t.state IN "
                            "('TESTING', 'VERIFYING')", (INTENT_GRACE,))
            ids = [r["id"] for r in uow.cur.fetchall()]
        for verification_id in ids:
            self.verifications._launch(verification_id)
        return {"relaunched": len(ids)}

    def _ended_tasks(self) -> dict[str, int]:
        """Ended tasks keep no lease, queued launches, or running work (wind-down missed by a crash)."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT t.* FROM tasks t WHERE t.state IN ('DONE', 'CANCELLED', 'FAILED') AND (EXISTS "
                            "(SELECT 1 FROM task_leases l WHERE l.task_id = t.id) OR EXISTS (SELECT 1 FROM pending_launches p "
                            "WHERE p.task_id = t.id) OR EXISTS (SELECT 1 FROM executions e WHERE e.task_id = t.id AND "
                            "e.state = ANY(%s))) FOR UPDATE OF t", (list(ACTIVE),))
            tasks = uow.cur.fetchall()
            for task in tasks:
                self._wind_down(uow, task)
        return {"wound_down": len(tasks)}

    def _retain(self) -> dict[str, int]:
        """Cancelled or failed work is kept: once a task's executions stopped, its workspaces are collected (their
        commits saved under platform refs) and marked RETAINED instead of being deleted."""
        retained = 0
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT DISTINCT t.key FROM tasks t JOIN workspaces w ON w.task_id = t.id WHERE t.state IN "
                            "('CANCELLED', 'FAILED') AND w.status = 'ACTIVE' AND NOT EXISTS (SELECT 1 FROM executions e "
                            "WHERE e.task_id = t.id AND e.state = ANY(%s))", (list(ACTIVE),))
            keys = [r["key"] for r in uow.cur.fetchall()]
        for key in keys:
            with self.ctx.unit_of_work() as uow:
                try:
                    with uow.cur.connection.transaction():
                        self.git.collect(uow, key)
                except ApiError as exc:
                    log.warning("could not collect %s before retaining it: %s", key, exc)
                uow.cur.execute("UPDATE workspaces SET status = 'RETAINED' WHERE task_id = (SELECT id FROM tasks WHERE key = %s) "
                                "AND status = 'ACTIVE' RETURNING id", (key,))
                retained += len(uow.cur.fetchall())
        return {"workspaces": retained}

    def _check_workspaces(self) -> dict[str, int]:
        """Active development workspaces whose clone vanished (deleted by hand, disk restored) are marked REMOVED;
        the subtask working in one is sent back so the orchestrator starts it again from a fresh clone."""
        missing = 0
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT w.*, p.relative_path, t.key AS task_key FROM workspaces w JOIN tasks t ON t.id = w.task_id "
                            "JOIN projects p ON p.id = w.project_id WHERE w.status = 'ACTIVE' AND w.kind = 'DEVELOPMENT' "
                            "AND t.state NOT IN ('DONE', 'CANCELLED', 'FAILED')")
            rows = uow.cur.fetchall()
        for ws in rows:
            try:
                self.ctx.git.collect(ws["relative_path"], ws["name"], ws["branch"], ws["base_sha"])
                continue
            except ApiError as exc:
                if "is missing" not in str(getattr(exc, "message", exc)):
                    continue
            with self.ctx.unit_of_work() as uow:
                uow.cur.execute("UPDATE workspaces SET status = 'REMOVED' WHERE id = %s", (ws["id"],))
                uow.cur.execute("UPDATE subtasks SET state = 'FIX_REQUIRED', state_reason = 'its workspace disappeared; "
                                "start it again', workspace_id = NULL, updated_at = now() WHERE workspace_id = %s "
                                "AND state IN ('IN_PROGRESS', 'IN_REVIEW', 'FIX_REQUIRED') RETURNING id", (ws["id"],))
                if uow.cur.fetchone() is not None:
                    uow.cur.execute("UPDATE tasks SET step_requested = true WHERE id = %s", (ws["task_id"],))
                record_event(uow.cur, "WORKSPACE_MISSING", actor="control-plane", project_id=ws["project_id"],
                             task_id=ws["task_id"], summary=f"workspace {ws['name']} of {ws['task_key']} is missing",
                             data={"workspace": ws["name"]}, pending=uow.events)
            missing += 1
        return {"missing": missing}

    # ---------------------------------------------------------------- views

    def status(self, uow: UnitOfWork) -> dict[str, Any]:
        uow.cur.execute("SELECT * FROM recovery_runs ORDER BY finished_at DESC LIMIT 5")
        runs = uow.cur.fetchall()
        uow.cur.execute("SELECT count(*) AS n FROM notifications WHERE state = 'PENDING'")
        pending = int(uow.cur.fetchone()["n"])  # type: ignore[index]
        uow.cur.execute("SELECT count(*) AS n FROM operation_intents WHERE state IN ('PENDING', 'SENT')")
        intents = int(uow.cur.fetchone()["n"])  # type: ignore[index]
        return {"health": self.health.summary(uow), "pending_notifications": pending, "open_intents": intents,
                "recent_runs": runs}


def _describe(report: dict[str, Any]) -> str:
    parts = []
    for key, value in report.items():
        if key in ("health", "seconds") or not isinstance(value, (dict, int)):
            continue
        if isinstance(value, int):
            if value:
                parts.append(f"{key} {value}")
            continue
        if "error" in value:
            parts.append(f"{key} failed")
            continue
        changed = ", ".join(f"{k} {v}" for k, v in value.items() if v)
        if changed:
            parts.append(f"{key}: {changed}")
    return "; ".join(parts) or "nothing to repair"
