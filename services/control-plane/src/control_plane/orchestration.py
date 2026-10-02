"""Multi-agent orchestration (MASTER_SPEC sections 5-9, 29-33, 43-49; docs/design/phase-7.md).

The model plans, the control plane drives:

Leases        one leader per task; acquiring bumps the epoch, renewing does not.
Launches      every automatic execution goes through `launch`: capacity, budget, scope conflicts,
              and higher-priority work make it wait in `pending_launches`, retried with aging.
Orchestrator  bounded steps (ORCHESTRATOR executions) triggered only by new external events;
              each returns actions from a closed vocabulary that are validated, applied, and audited.
Pipeline      deterministic cycle the model cannot skip: developer -> cross-review by the other
              provider -> fixes up to the review-cycle limit -> alternate developer or BLOCKED;
              all subtasks accepted -> integration -> verification -> integration review(s) ->
              Quality Gate -> READY_FOR_MERGE or FIX_REQUIRED.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from ho_core.adapters.base import FailureClass
from ho_core.enums import ApprovalAction, Priority, Risk, TaskState
from ho_core.ids import uuid7
from ho_core.routing import ProviderStats, route
from ho_core.statemachine import WAITING_STATES, Trigger

from . import budgets, manifests, recovery
from .approvals import Approvals
from .auth import Principal
from .budgets import BudgetExhausted
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import ApiError, Conflict, NotFound, UpstreamError
from .events import record_event
from .executions import ACTIVE, ExecutionRequest, Executions
from .gitops import CONTROL_PLANE, GitChanges
from .tasks import Tasks
from .verification import QualityGate, Reviews, Verifications, required_reviewer_set

log = logging.getLogger(__name__)
S = TaskState
ORCHESTRATOR = Principal("control-plane", "orchestrator")
LEASE_MINUTES = 30
STEP_TIMEOUT_MINUTES = 20
MAX_STEP_FAILURES = 3
MAX_ACTIONS = 30
FAILBACK_HOLD = timedelta(minutes=10)
MAX_REVIEW_RETRIES = 2
# Only these events make the orchestrator think again (design change 2).
TRIGGERS = ("ORCHESTRATOR_INPUT", "REQUIREMENTS_REVISED", "APPROVAL_DECIDED", "BUDGET_RAISED")
STEP_STATES = (S.PLANNING, S.QUEUED, S.RUNNING, S.FIX_REQUIRED)
OTHER = {"claude": "codex", "codex": "claude"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------ leases


class Leases:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def get(self, uow: UnitOfWork, task_id: UUID, *, lock: bool = False) -> Row | None:
        uow.cur.execute(f"SELECT * FROM task_leases WHERE task_id = %s{' FOR UPDATE' if lock else ''}", (task_id,))
        return uow.cur.fetchone()

    def acquire(self, uow: UnitOfWork, task_id: UUID, provider: str) -> int:
        """Take the lead (new epoch). Fails if another holder's lease has not expired."""
        uow.cur.execute(
            """
            INSERT INTO task_leases (task_id, holder, provider, epoch, expires_at)
            VALUES (%s, %s, %s, (SELECT COALESCE(max(lease_epoch), 0) + 1 FROM executions WHERE task_id = %s), now() + %s)
            ON CONFLICT (task_id) DO UPDATE SET epoch = task_leases.epoch + 1, holder = EXCLUDED.holder,
                provider = EXCLUDED.provider, acquired_at = now(), renewed_at = now(), expires_at = EXCLUDED.expires_at
            WHERE task_leases.expires_at < now() OR task_leases.holder = EXCLUDED.holder
            RETURNING epoch
            """,
            (task_id, self.ctx.instance_id, provider, task_id, timedelta(minutes=LEASE_MINUTES)),
        )
        row = uow.cur.fetchone()
        if row is None:
            raise Conflict("another control plane holds this task's lease")
        return int(row["epoch"])

    def renew(self, uow: UnitOfWork, task_id: UUID) -> bool:
        """Extend our lease without changing its epoch."""
        uow.cur.execute("UPDATE task_leases SET renewed_at = now(), expires_at = now() + %s WHERE task_id = %s AND holder = %s",
                        (timedelta(minutes=LEASE_MINUTES), task_id, self.ctx.instance_id))
        return uow.cur.rowcount == 1

    def release(self, uow: UnitOfWork, task_id: UUID) -> None:
        uow.cur.execute("DELETE FROM task_leases WHERE task_id = %s", (task_id,))


# ---------------------------------------------------------------------- the module


class Orchestration:
    def __init__(self, ctx: Context, tasks: Tasks, approvals: Approvals, executions: Executions, git: GitChanges,
                 verifications: Verifications, reviews: Reviews, gate: QualityGate) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.approvals = approvals
        self.executions = executions
        self.git = git
        self.verifications = verifications
        self.reviews = reviews
        self.gate = gate
        self.leases = Leases(ctx)
        executions.on_agent_result.append(self._on_agent_result)
        executions.on_finished.append(self._on_execution_finished)
        reviews.on_recorded.append(self._on_review)
        verifications.on_finished.append(self._on_verification)
        gate.on_evaluated.append(self._on_gate)
        approvals.register_handler(ApprovalAction.ASSUMPTION, self._on_waiting_approval)
        approvals.register_handler(ApprovalAction.SCOPE_EXPANSION, self._on_waiting_approval)
        approvals.register_handler(ApprovalAction.BUDGET_INCREASE, self._on_budget_increase)

    # ------------------------------------------------------------- helpers

    def _task(self, uow: UnitOfWork, task_id: UUID, *, lock: bool = True) -> Row:
        uow.cur.execute(f"SELECT * FROM tasks WHERE id = %s{' FOR UPDATE' if lock else ''}", (task_id,))
        task = uow.cur.fetchone()
        assert task is not None
        return task

    def _event(self, uow: UnitOfWork, task: Row, event: str, summary: str, data: dict[str, Any] | None = None,
               actor: str = "orchestration") -> None:
        record_event(uow.cur, event, actor=actor, project_id=task["project_id"], task_id=task["id"], summary=summary[:300],
                     data=data or {}, pending=uow.events)

    def _input(self, uow: UnitOfWork, task: Row, summary: str, data: dict[str, Any] | None = None) -> None:
        """Something only the orchestrator can decide happened: it triggers the next step."""
        self._event(uow, task, "ORCHESTRATOR_INPUT", summary, data)

    def _move(self, uow: UnitOfWork, task: Row, *targets: S, reason: str) -> Row:
        """Walk the task through `targets` in order (skipping the ones it is already past)."""
        for target in targets:
            if task["state"] == target:
                continue
            task = self.tasks.transition(uow, task, target, trigger=Trigger.SYSTEM, actor="orchestration", reason=reason)
        return task

    def _slug(self, uow: UnitOfWork, task: Row) -> str:
        uow.cur.execute("SELECT slug FROM projects WHERE id = %s", (task["project_id"],))
        return uow.cur.fetchone()["slug"]  # type: ignore[index]

    def _subtasks(self, uow: UnitOfWork, task: Row) -> list[Row]:
        uow.cur.execute("SELECT * FROM subtasks WHERE task_id = %s AND plan_version = %s ORDER BY key",
                        (task["id"], task["current_plan_version"] or 0))
        return uow.cur.fetchall()

    def _subtask(self, uow: UnitOfWork, task: Row, key: str | None, *, lock: bool = True) -> Row | None:
        if not key:
            return None
        uow.cur.execute(f"SELECT * FROM subtasks WHERE task_id = %s AND (key = %s OR (local_key = %s AND plan_version = %s))"
                        f"{' FOR UPDATE' if lock else ''}",
                        (task["id"], key, key, task["current_plan_version"] or 0))
        return uow.cur.fetchone()

    def _set_subtask(self, uow: UnitOfWork, subtask: Row, state: str, reason: str | None = None, **fields: Any) -> None:
        sets = ", ".join(f"{k} = %s" for k in fields)
        uow.cur.execute(f"UPDATE subtasks SET state = %s, state_reason = %s, updated_at = now(){', ' + sets if sets else ''} "
                        f"WHERE id = %s", (state, reason, *fields.values(), subtask["id"]))

    def _config(self, uow: UnitOfWork, task: Row) -> dict[str, Any]:
        return self.git._context(uow, task["key"])[2]

    def _available(self, uow: UnitOfWork) -> list[str]:
        uow.cur.execute("SELECT provider, status FROM credential_refs WHERE identity = %s", (self.ctx.provider_identity,))
        blocked = {r["provider"] for r in uow.cur.fetchall() if r["status"] == "AUTH_REQUIRED"}
        uow.cur.execute("SELECT DISTINCT provider FROM executions WHERE failure_class = 'QUOTA' AND ended_at > now() - interval '30 minutes'")
        blocked |= {r["provider"] for r in uow.cur.fetchall()}
        return [p for p in ("claude", "codex") if p not in blocked]

    def _route(self, uow: UnitOfWork, task: Row, kind: str, *, suggested: str | None = None, exclude: tuple[str, ...] = ()) -> str | None:
        config = self._config(uow, task)
        uow.cur.execute("SELECT provider, count(*) AS n FROM executions WHERE state = ANY(%s) AND role IN ('DEVELOPER', 'REVIEWER') "
                        "GROUP BY provider",
                        (list(ACTIVE),))
        running = {r["provider"]: int(r["n"]) for r in uow.cur.fetchall()}
        uow.cur.execute(
            "SELECT e.provider, count(*) AS runs, count(*) FILTER (WHERE e.state = 'SUCCEEDED') AS ok, "
            "avg(extract(epoch FROM e.ended_at - e.started_at) / 60) AS minutes "
            "FROM executions e WHERE e.role = 'DEVELOPER' AND e.agent_run AND e.ended_at > now() - interval '30 days' "
            "GROUP BY e.provider")
        stats = {r["provider"]: ProviderStats(int(r["runs"]), int(r["ok"]), float(r["minutes"] or 0)) for r in uow.cur.fetchall()}
        decision = route(kind, available=self._available(uow), project_preferred=(config.get("agents") or {}).get("preferred"),
                         suggested=suggested, running=running, mix=self.ctx.platform["machine"].get("starting_mix"),
                         stats=stats, exclude=exclude)
        self._event(uow, task, "AGENT_ROUTED", f"{kind.lower()} -> {decision.provider or 'no provider available'} "
                    f"{decision.scores}", decision.as_json())
        return decision.provider

    # ------------------------------------------------------------ launches

    def launch(self, uow: UnitOfWork, task: Row, kind: str, request: dict[str, Any], *, subtask: Row | None = None,
               queue: bool = True) -> Row | None:
        """Start an execution now, or queue it when capacity, budget, a scope conflict, or a
        higher-priority task stands in the way (design changes 3 and 6)."""
        blocker = self._blocker(uow, task, kind, subtask)
        if blocker is None:
            try:
                with uow.cur.connection.transaction():
                    if kind == "SUBTASK_REVIEW" and subtask is not None:  # a queued cross-review
                        return self._launch_review(uow, task, subtask, request)
                    return self._start(uow, task, request)
            except BudgetExhausted as exc:
                blocker = f"budget: {exc}"
                uow.after_commit.append(lambda key=task["key"], reason=str(exc): self.executions.pause_for_budget(key, reason))
            except Conflict as exc:
                if "stale orchestration decision" in str(exc):
                    raise
                blocker = str(exc)
            except UpstreamError as exc:  # a platform service is down: wait, never fail the caller's transaction
                blocker = f"service unavailable: {exc}"
        if queue:
            uow.cur.execute("INSERT INTO pending_launches (id, task_id, subtask_id, kind, request, reason) VALUES (%s, %s, %s, %s, %s, %s)",
                            (uuid7(), task["id"], subtask["id"] if subtask else None, kind, jsonb(request), blocker[:300]))
            self._event(uow, task, "LAUNCH_WAITING", f"{kind.lower()} waits: {blocker}", {"kind": kind})
        return None

    def _start(self, uow: UnitOfWork, task: Row, request: dict[str, Any]) -> Row:
        fields = dict(request)
        if fields.get("subtask_id"):
            fields["subtask_id"] = UUID(str(fields["subtask_id"]))
        principal = Principal.parse(fields.pop("principal", ORCHESTRATOR.value))
        return self.executions.request(uow, principal=principal, task_key=task["key"], req=ExecutionRequest(**fields))

    def _rank(self, task: Row, since: datetime | None) -> int:
        aging = int(self.ctx.platform["scheduler"]["aging_minutes"])
        waited = 0.0 if since is None else (_now() - since).total_seconds() / 60
        return max(0, Priority(task["priority"]).rank - int(waited // aging))

    def _blocker(self, uow: UnitOfWork, task: Row, kind: str, subtask: Row | None) -> str | None:
        """Why a launch must wait, or None."""
        mine = self._rank(task, task["launch_suspended_at"])
        uow.cur.execute(
            "SELECT t.*, min(p.requested_at) AS since FROM pending_launches p JOIN tasks t ON t.id = p.task_id "
            "WHERE t.id <> %s GROUP BY t.id", (task["id"],))
        for other in uow.cur.fetchall():
            if self._rank(other, other["launch_suspended_at"] or other["since"]) < mine:
                if task["launch_suspended_at"] is None:
                    uow.cur.execute("UPDATE tasks SET launch_suspended_at = now() WHERE id = %s", (task["id"],))
                return f"{other['key']} has higher priority and is waiting for capacity"
        if subtask and kind in ("DEVELOP", "FIX"):
            files = set((subtask["estimated_scope"] or {}).get("files") or [])
            if files:
                uow.cur.execute(
                    "SELECT s.key, s.estimated_scope, t.priority, t.key AS task_key FROM subtasks s JOIN tasks t ON t.id = s.task_id "
                    "WHERE t.project_id = %s AND s.id <> %s AND s.state IN ('IN_PROGRESS', 'IN_REVIEW')",
                    (task["project_id"], subtask["id"]))
                for other in uow.cur.fetchall():
                    overlap = files & set((other["estimated_scope"] or {}).get("files") or [])
                    if overlap and (other["task_key"] == task["key"] or Priority(other["priority"]).rank <= Priority(task["priority"]).rank):
                        return f"scope overlaps {other['key']} ({', '.join(sorted(overlap)[:3])})"
        return None

    def process_pending(self) -> int:
        """Retry waiting launches, best effective priority first (aging prevents starvation)."""
        started = 0
        with self.ctx.unit_of_work() as uow:
            # Launches of waiting tasks (paused, budget, approval, login) stay queued until the task resumes.
            uow.cur.execute("SELECT p.*, t.priority, t.launch_suspended_at FROM pending_launches p JOIN tasks t ON t.id = p.task_id "
                            "WHERE t.state <> ALL(%s) ORDER BY p.requested_at", ([w.value for w in WAITING_STATES],))
            rows = uow.cur.fetchall()
        rows.sort(key=lambda r: (self._rank(r, r["launch_suspended_at"] or r["requested_at"]), r["requested_at"]))
        for row in rows:
            with self.ctx.unit_of_work() as uow:
                uow.cur.execute("DELETE FROM pending_launches WHERE id = %s RETURNING id", (row["id"],))
                if uow.cur.fetchone() is None:
                    continue
                task = self._task(uow, row["task_id"])
                if S(task["state"]) not in (*STEP_STATES, S.TESTING, S.REVIEW, S.QUALITY_GATE):
                    continue  # the task left active work; its pending launches are dropped
                epoch = row["request"].get("lease_epoch")
                lease = self.leases.get(uow, task["id"])
                if epoch is not None and (lease is None or int(lease["epoch"]) != int(epoch)):
                    self._event(uow, task, "LAUNCH_DROPPED", f"{row['kind'].lower()} decided under an older lease epoch")
                    continue
                subtask = None
                if row["subtask_id"]:
                    uow.cur.execute("SELECT * FROM subtasks WHERE id = %s", (row["subtask_id"],))
                    subtask = uow.cur.fetchone()
                execution = self.launch(uow, task, row["kind"], row["request"], subtask=subtask)
                if execution is not None:
                    started += 1
                    uow.cur.execute("UPDATE tasks SET launch_suspended_at = NULL WHERE id = %s AND NOT EXISTS "
                                    "(SELECT 1 FROM pending_launches WHERE task_id = %s)", (task["id"], task["id"]))
                    self._after_launch(uow, task, row["kind"], subtask, execution)
        return started

    def _after_launch(self, uow: UnitOfWork, task: Row, kind: str, subtask: Row | None, execution: Row) -> None:
        if subtask is not None and kind in ("DEVELOP", "FIX"):
            self._set_subtask(uow, subtask, "IN_PROGRESS", None, developer_provider=execution["provider"])
        elif subtask is not None and kind == "SUBTASK_REVIEW":
            self._set_subtask(uow, subtask, "IN_REVIEW", None)

    # --------------------------------------------------------- dispatching

    def dispatch(self, uow: UnitOfWork, task: Row) -> bool:
        """Scheduler dispatcher: take a READY task, lead it, and plan it."""
        cap = int(self.ctx.platform["machine"]["max_agent_workers"])
        uow.cur.execute("SELECT count(*) AS n FROM executions WHERE state = ANY(%s) AND role IN ('ORCHESTRATOR', 'DEVELOPER', 'REVIEWER')",
                        (list(ACTIVE),))
        if int(uow.cur.fetchone()["n"]) >= cap:  # type: ignore[index]
            return False
        if self._classify(uow, task):
            return True  # a duplicate waits for the user instead of being planned
        provider = self._route(uow, task, "PLANNING")
        if provider is None:
            return False
        try:
            with uow.cur.connection.transaction():
                self.leases.acquire(uow, task["id"], provider)
        except Conflict:
            log.warning("%s is led by another control plane instance; not dispatched", task["key"])
            return True  # keep dispatching the rest of the queue
        uow.cur.execute("UPDATE tasks SET started_at = COALESCE(started_at, now()), step_requested = true WHERE id = %s", (task["id"],))
        task = self._move(uow, self._task(uow, task["id"]), S.PLANNING, reason=f"orchestrated by {provider}")
        self.request_step(uow, task)
        return True

    def _classify(self, uow: UnitOfWork, task: Row) -> bool:
        """Relate a new task to the project's active tasks (section 7); True when it is a duplicate."""
        uow.cur.execute("SELECT 1 FROM events WHERE task_id = %s AND type = 'TASK_CLASSIFIED'", (task["id"],))
        if uow.cur.fetchone() is not None:
            return False  # already classified (a released duplicate goes ahead)
        mine = _words(self.reviews._request_text(uow, task))
        uow.cur.execute("SELECT * FROM tasks WHERE project_id = %s AND id <> %s AND state NOT IN ('DONE', 'CANCELLED', 'FAILED', 'BACKLOG')",
                        (task["project_id"], task["id"]))
        duplicate = None
        for other in uow.cur.fetchall():
            similarity = _similarity(mine, _words(self.reviews._request_text(uow, other)))
            kind = "DUPLICATE" if similarity >= DUPLICATE_SIMILARITY else "RELATED" if similarity >= RELATED_SIMILARITY else None
            if kind is None:
                continue
            uow.cur.execute("INSERT INTO task_relationships (id, from_task_id, to_task_id, kind, classified_by, evidence) "
                            "VALUES (%s, %s, %s, %s, 'control-plane', %s) ON CONFLICT DO NOTHING",
                            (uuid7(), task["id"], other["id"], kind, f"request similarity {similarity:.2f}"))
            self._event(uow, task, "TASK_RELATED", f"{task['key']} is {kind.lower()} to {other['key']} ({similarity:.0%} similar)",
                        {"task": other["key"], "kind": kind})
            if kind == "DUPLICATE" and duplicate is None:
                duplicate = other
        self._event(uow, task, "TASK_CLASSIFIED", f"{task['key']} is " + (f"a duplicate of {duplicate['key']}" if duplicate
                    else "independent of the active tasks or related to them"))
        if duplicate is not None:
            self.tasks.transition(uow, task, S.BLOCKED, trigger=Trigger.SCHEDULER, actor="orchestration",
                                  reason=f"duplicate of {duplicate['key']}: release it (ho task retry) or cancel it")
            return True
        return False

    def _knowledge_for(self, uow: UnitOfWork, task: Row, files: list[str]) -> list[Row]:
        """Trusted project knowledge whose anchors overlap the given paths."""
        if not files:
            return []
        uow.cur.execute("SELECT category, trust, title, body FROM knowledge_items WHERE project_id = %s AND trust IN "
                        "('CONFIRMED', 'OBSERVED') AND anchors && %s ORDER BY updated_at DESC LIMIT 8", (task["project_id"], files))
        return uow.cur.fetchall()

    # ------------------------------------------------------------- steps

    def request_step(self, uow: UnitOfWork, task: Row) -> Row | None:
        lease = self.leases.get(uow, task["id"], lock=True)
        if lease is None:
            return None
        uow.cur.execute("SELECT 1 FROM executions WHERE task_id = %s AND role = 'ORCHESTRATOR' AND state = ANY(%s)",
                        (task["id"], list(ACTIVE)))
        if uow.cur.fetchone() is not None:
            return None
        uow.cur.execute("SELECT 1 FROM pending_launches WHERE task_id = %s AND kind = 'STEP'", (task["id"],))
        if uow.cur.fetchone() is not None:
            return None
        uow.cur.execute("SELECT coalesce(max(seq), 0) AS seq FROM events WHERE task_id = %s", (task["id"],))
        cursor = int(uow.cur.fetchone()["seq"])  # type: ignore[index]
        uow.cur.execute("UPDATE tasks SET step_requested = false WHERE id = %s", (task["id"],))
        request = {"role": "ORCHESTRATOR", "provider": lease["provider"], "prompt": self.context_bundle(uow, task),
                   "result_schema": "orchestrator-step", "capabilities": {"project_read": True, "egress": "ALLOWLIST"},
                   "max_turns": 40, "timeout_minutes": STEP_TIMEOUT_MINUTES, "lease_epoch": int(lease["epoch"]),
                   "purpose": {"orchestrator_step": True, "fenced": True, "epoch": int(lease["epoch"]), "cursor": cursor,
                               "retry": int(task["orchestrator_failures"]) > 0}}
        return self.launch(uow, task, "STEP", request)

    def context_bundle(self, uow: UnitOfWork, task: Row) -> str:
        """Everything the orchestrator needs for one step, from durable state (section 47); bounded size."""
        lines = [f"# Orchestrator step: task {task['key']}", "", f"Current task state: {task['state']} (events below are history; "
                 "this state is authoritative).", "",
                 "You lead this task. Return actions; the platform validates and executes them and enforces policy, budgets,",
                 "and approvals. The platform automatically runs cross-review by the other provider after each developer",
                 "execution, fix cycles, integration, verification, integration reviews, and the Quality Gate: do not ask for",
                 "those. Plan small, independent subtasks with accurate `files`; request DEVELOPER executions only for READY",
                 "subtasks; use WAIT when nothing needs deciding. Every subtask kind, TEST_AUTHORING included, runs with role DEVELOPER. Record assumptions instead of guessing silently; HIGH or",
                 "irreversible ambiguity needs RECORD_ASSUMPTION with level HIGH (a human decides).", "",
                 "## Request", "", self.reviews._request_text(uow, task).strip()[:5000], "",
                 "## Project files", "", f"The tracked files of the project's main branch are mounted read-only at /projects/{self._slug(uow, task)} "
                 "(no Git metadata; files the project ignores, such as local secrets, are not there). Read it (structure, code, tests, conventions) before writing requirements or a plan; give each subtask "
                 "accurate `files`.", ""]
        if task["current_requirements_version"]:
            uow.cur.execute("SELECT a.path FROM requirement_versions r JOIN artifacts a ON a.id = r.artifact_id "
                            "WHERE r.task_id = %s ORDER BY r.version DESC LIMIT 1", (task["id"],))
            row = uow.cur.fetchone()
            text = self.ctx.artifacts.read(row["path"]).decode("utf-8", "replace") if row else ""
            lines += [f"## Requirements (version {task['current_requirements_version']})", "", text[:5000], ""]
        else:
            lines += ["## Requirements", "", "None yet: start with SET_REQUIREMENTS, then SET_PLAN.", ""]
        uow.cur.execute("SELECT level, assumption, status FROM assumptions WHERE task_id = %s ORDER BY created_at", (task["id"],))
        assumptions = uow.cur.fetchall()
        if assumptions:
            lines += ["## Assumptions", ""] + [f"- [{a['level']}, {a['status']}] {a['assumption'][:300]}" for a in assumptions] + [""]
        subtasks = self._subtasks(uow, task)
        if subtasks:
            lines += ["## Plan (refer to subtasks by their own key, the first word; keep these keys in a revised plan)", ""]
            for s in subtasks:
                uow.cur.execute("SELECT key FROM subtasks WHERE id IN (SELECT depends_on_subtask_id FROM subtask_dependencies "
                                "WHERE subtask_id = %s)", (s["id"],))
                deps = [d["key"] for d in uow.cur.fetchall()]
                lines.append(f"- {s['local_key']} [{s['key']}] {s['state']} {s['kind']} risk {s['risk']}"
                             f"{' dev ' + s['developer_provider'] if s['developer_provider'] else ''}"
                             f"{' deps ' + ','.join(deps) if deps else ''}: {s['title']}"
                             f"{' — ' + s['state_reason'] if s['state_reason'] else ''}")
            lines.append("")
        uow.cur.execute("SELECT type, summary FROM events WHERE task_id = %s AND seq > %s AND type NOT IN "
                        "('GRANT_ISSUED', 'GRANT_REVOKED', 'WORKER_CREATED', 'AGENT_ASSIGNED') "
                        "ORDER BY seq DESC LIMIT 40", (task["id"], task["orchestrator_cursor_seq"]))
        recent = list(reversed(uow.cur.fetchall()))
        if recent:
            lines += ["## Since your last step", ""] + [f"- {e['type']}: {e['summary'][:240]}" for e in recent] + [""]
        uow.cur.execute("SELECT type, reason FROM orchestrator_actions WHERE task_id = %s AND outcome = 'REJECTED' "
                        "AND created_at > now() - interval '1 day' ORDER BY created_at DESC LIMIT 10", (task["id"],))
        rejected = uow.cur.fetchall()
        if rejected:
            lines += ["## Your rejected actions (fix and retry if still needed)", ""] + \
                     [f"- {r['type']}: {r['reason']}" for r in rejected] + [""]
        budget = budgets.state(uow, task)
        if budget:
            lines += ["## Budget", "", f"state {budget['state']}; consumed {json.dumps(budget['consumed'])}; "
                      f"limits {json.dumps(budget['limits'])}" + ("; work efficiently" if budget["state"] == "OPTIMIZE" else ""), ""]
        uow.cur.execute("SELECT category, trust, title, body FROM knowledge_items WHERE project_id = %s AND trust IN "
                        "('CONFIRMED', 'OBSERVED') ORDER BY updated_at DESC LIMIT 10", (task["project_id"],))
        knowledge = uow.cur.fetchall()
        if knowledge:
            lines += ["## Project knowledge", ""] + [f"- [{k['category']}, {k['trust']}] {k['title']}: {k['body'][:200]}"
                                                     for k in knowledge] + [""]
        return "\n".join(lines)[:20000]

    def _on_agent_result(self, uow: UnitOfWork, execution: Row, result: Any) -> None:
        purpose = (execution["spec"] or {}).get("purpose") or {}
        if not purpose.get("orchestrator_step"):
            return
        task = self._task(uow, execution["task_id"])
        lease = self.leases.get(uow, task["id"], lock=True)
        actions = (result.structured or {}).get("actions") or [] if result.ok else []
        if lease is None or int(lease["epoch"]) != int(purpose["epoch"]):
            for seq, action in enumerate(actions[:MAX_ACTIONS]):
                self._record_action(uow, task, execution, purpose["epoch"], seq, action, "REJECTED", "stale lease epoch")
            return
        if not result.ok:
            return  # handled in _on_execution_finished
        if S(task["state"]) not in STEP_STATES:  # paused or waiting meanwhile: decide again when it resumes
            for seq, action in enumerate(actions[:MAX_ACTIONS]):
                self._record_action(uow, task, execution, purpose["epoch"], seq, action, "REJECTED", f"task is {task['state']}")
            uow.cur.execute("UPDATE tasks SET step_requested = true WHERE id = %s", (task["id"],))
            return
        uow.cur.execute("UPDATE tasks SET orchestrator_cursor_seq = GREATEST(orchestrator_cursor_seq, %s) "
                        "WHERE id = %s", (purpose["cursor"], task["id"]))
        self._event(uow, task, "ORCHESTRATOR_STEP", (result.structured or {}).get("summary", "")[:300] or "step completed",
                    {"execution_id": str(execution["id"]), "actions": [a.get("type") for a in actions[:MAX_ACTIONS]]})
        rejected = False
        for seq, action in enumerate(actions[:MAX_ACTIONS]):
            task = self._task(uow, task["id"])
            try:
                with uow.cur.connection.transaction():
                    reason = self._apply(uow, task, int(purpose["epoch"]), action)
                outcome = "ACCEPTED"
            except (ApiError, ValueError) as exc:
                outcome, reason = "REJECTED", str(getattr(exc, "message", exc))[:300]
            self._record_action(uow, task, execution, purpose["epoch"], seq, action, outcome, reason)
            rejected = rejected or outcome == "REJECTED"
        self._after_step(uow, task["id"], rejected)
        recovery.checkpoint(uow, self._task(uow, task["id"]), "orchestrator step applied")

    def _integration_due(self, uow: UnitOfWork, task: Row) -> bool:
        """All subtasks accepted on a RUNNING task: integration starts after this transaction commits."""
        subtasks = self._subtasks(uow, task)
        return task["state"] == S.RUNNING and bool(subtasks) and all(
            s["state"] in ("ACCEPTED", "INTEGRATED", "CANCELLED") for s in subtasks)

    def _after_step(self, uow: UnitOfWork, task_id: UUID, rejected: bool) -> None:
        """A step whose actions were rejected and that left nothing running would stall the task: decide again
        with the rejections as feedback, a bounded number of times (they count as orchestrator failures)."""
        task = self._task(uow, task_id)
        if S(task["state"]) not in STEP_STATES or self._integration_due(uow, task):
            uow.cur.execute("UPDATE tasks SET orchestrator_failures = 0 WHERE id = %s", (task_id,))
            return
        uow.cur.execute("SELECT 1 FROM executions WHERE task_id = %s AND state = ANY(%s) AND role <> 'ORCHESTRATOR' "
                        "UNION ALL SELECT 1 FROM pending_launches WHERE task_id = %s UNION ALL "
                        "SELECT 1 FROM verifications WHERE task_id = %s AND state IN ('PREPARING', 'RUNNING') LIMIT 1",
                        (task["id"], list(ACTIVE), task["id"], task["id"]))
        if uow.cur.fetchone() is not None:
            uow.cur.execute("UPDATE tasks SET orchestrator_failures = 0 WHERE id = %s", (task_id,))
            return  # something is in flight; its result will trigger the next step
        self._event(uow, task, "STEP_STALLED", "your last step left nothing running" + (" (some actions were rejected)" if rejected
                    else "") + ": start READY subtasks with REQUEST_EXECUTION, fix the plan, or REPORT_BLOCKED")
        uow.cur.execute("UPDATE tasks SET orchestrator_failures = orchestrator_failures + 1 WHERE id = %s "
                        "RETURNING orchestrator_failures", (task["id"],))
        failures = int(uow.cur.fetchone()["orchestrator_failures"])  # type: ignore[index]
        if failures >= MAX_STEP_FAILURES:
            self._move(uow, task, S.BLOCKED, reason=f"{failures} orchestrator steps in a row left nothing running")
        else:
            uow.cur.execute("UPDATE tasks SET step_requested = true WHERE id = %s", (task["id"],))

    def _record_action(self, uow: UnitOfWork, task: Row, execution: Row, epoch: int, seq: int, action: dict[str, Any],
                       outcome: str, reason: str | None) -> None:
        payload = {k: v for k, v in action.items() if v not in (None, [], "")}
        if isinstance(payload.get("prompt"), str):
            payload["prompt"] = payload["prompt"][:2000]
        uow.cur.execute("INSERT INTO orchestrator_actions (id, task_id, execution_id, lease_epoch, seq, type, payload, outcome, reason) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (uuid7(), task["id"], execution["id"], epoch, seq, str(action.get("type"))[:40], jsonb(payload), outcome, reason))

    # ------------------------------------------------------------- actions

    def _apply(self, uow: UnitOfWork, task: Row, epoch: int, action: dict[str, Any]) -> str | None:
        kind = action.get("type")
        if kind == "WAIT":
            return None
        if kind == "SET_REQUIREMENTS":
            return self._set_requirements(uow, task, action.get("text") or "", source="ORCHESTRATOR", reason="orchestrator")
        if kind == "RECORD_ASSUMPTION":
            return self._record_assumption(uow, task, action)
        if kind == "SET_PLAN":
            return self._set_plan(uow, task, action.get("subtasks") or [])
        if kind == "REQUEST_EXECUTION":
            return self._request_execution(uow, task, epoch, action)
        if kind == "ACCEPT_SUBTASK":
            subtask = self._subtask(uow, task, action.get("subtask"))
            if subtask is not None and subtask["kind"] == "RESEARCH" and subtask["state"] in ("FIX_REQUIRED", "IN_REVIEW"):
                uow.cur.execute("SELECT 1 FROM executions WHERE subtask_id = %s AND state = 'SUCCEEDED' "
                                "AND result->>'status' = 'completed'", (subtask["id"],))
                if uow.cur.fetchone() is None:
                    raise Conflict(f"{subtask['key']} has no completed research to accept")
                self._accept_research(uow, task, subtask)
                return "research accepted"
            if subtask is None or subtask["state"] != "ACCEPTED":
                raise Conflict("subtasks are accepted by an approving cross-review, not by the orchestrator")
            return "already accepted"
        if kind == "REJECT_SUBTASK":
            subtask = self._subtask(uow, task, action.get("subtask"))
            if subtask is None or subtask["state"] not in ("ACCEPTED", "IN_REVIEW", "BLOCKED", "FIX_REQUIRED"):
                raise Conflict("unknown subtask or not in a state that can be rejected")
            self._set_subtask(uow, subtask, "FIX_REQUIRED", (action.get("text") or "rejected by the orchestrator")[:300])
            return None
        if kind == "REQUEST_APPROVAL":
            config = self._config(uow, task)
            self.approvals.request(uow, action=ApprovalAction.SCOPE_EXPANSION, project_id=task["project_id"], task_id=task["id"],
                                   subject={"kind": "orchestrator_request", "text": (action.get("text") or "")[:2000]},
                                   config_hash=config["_hash"], summary=f"{task['key']}: {(action.get('text') or '')[:200]}",
                                   requested_by="orchestrator", risk=Risk.HIGH, from_task_state=task["state"])
            self._move(uow, task, S.APPROVAL_REQUIRED, reason="the orchestrator asked for an approval")
            return None
        if kind == "PROPOSE_KNOWLEDGE":
            text = (action.get("text") or "").strip()
            if not text or action.get("category") is None:
                raise ValueError("knowledge needs text and a category")
            title, _, body = text.partition("\n")
            uow.cur.execute("INSERT INTO knowledge_items (id, project_id, category, trust, title, body, provenance, anchors) "
                            "VALUES (%s, %s, %s, 'HYPOTHESIS', %s, %s, %s, %s)",
                            (uuid7(), task["project_id"], action["category"], title[:200], (body or title)[:2000],
                             jsonb({"task": task["key"], "source": "orchestrator"}), list(action.get("anchors") or [])[:20]))
            return None
        if kind == "SUBMIT_FOR_QUALITY_GATE":
            return self._integrate_if_ready(uow, task, explicit=True)
        if kind == "REPORT_BLOCKED":
            self._move(uow, task, S.BLOCKED, reason=(action.get("text") or "reported blocked by the orchestrator")[:300])
            return None
        raise ValueError(f"unknown action {kind!r}")

    def _set_requirements(self, uow: UnitOfWork, task: Row, text: str, *, source: str, reason: str) -> str:
        if not text.strip():
            raise ValueError("requirements text is empty")
        version = int(task["current_requirements_version"] or 0) + 1
        artifact = self.ctx.artifacts.write(uow.cur, project_id=task["project_id"], task_id=task["id"], kind="requirements",
                                            name=f"requirements-v{version}.md", content=text[:50_000].encode(), media_type="text/markdown")
        uow.cur.execute("INSERT INTO requirement_versions (id, task_id, version, artifact_id, source, change_reason) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", (uuid7(), task["id"], version, artifact.id, source, reason[:300]))
        uow.cur.execute("UPDATE tasks SET current_requirements_version = %s WHERE id = %s", (version, task["id"]))
        self._event(uow, task, "REQUIREMENTS_VERSIONED", f"requirements version {version} ({source.lower()})", {"version": version})
        return f"version {version}"

    def _record_assumption(self, uow: UnitOfWork, task: Row, action: dict[str, Any]) -> str | None:
        level, text = action.get("level") or "MEDIUM", (action.get("text") or "").strip()
        if not text:
            raise ValueError("assumption text is empty")
        needs_approval = level == "HIGH" or action.get("reversible") is False
        assumption_id = uuid7()
        approval_id = None
        if needs_approval:
            config = self._config(uow, task)
            approval = self.approvals.request(uow, action=ApprovalAction.ASSUMPTION, project_id=task["project_id"], task_id=task["id"],
                                              subject={"assumption": text[:2000], "level": level}, config_hash=config["_hash"],
                                              summary=f"{task['key']}: confirm assumption: {text[:200]}", requested_by="orchestrator",
                                              risk=Risk.HIGH, from_task_state=task["state"])
            approval_id = approval["id"]
        uow.cur.execute("INSERT INTO assumptions (id, task_id, level, assumption, reason, impact, reversible, status, approval_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (assumption_id, task["id"], level, text[:2000], "", None, action.get("reversible") is not False,
                         "PENDING_APPROVAL" if needs_approval else "RECORDED", approval_id))
        self._event(uow, task, "ASSUMPTION_RECORDED", f"[{level}] {text[:250]}", {"needs_approval": needs_approval})
        if needs_approval:
            self._move(uow, task, S.APPROVAL_REQUIRED, reason="a high-impact assumption needs a human decision")
        return None

    def _set_plan(self, uow: UnitOfWork, task: Row, items: list[dict[str, Any]]) -> str:
        if not items:
            raise ValueError("a plan needs at least one subtask")
        keys = [str(i.get("key") or "").strip() for i in items]
        if len(set(keys)) != len(keys) or not all(re.match(r"^[A-Za-z0-9_-]{1,20}$", k) for k in keys):
            raise ValueError("subtask keys must be unique short identifiers")
        if any(re.match(r"^T-[0-9]+-[0-9]+$", k) for k in keys):
            raise ValueError("subtask keys are your own short names (like 'api'); T-n-m keys are assigned by the platform")
        graph = {k: [d for d in (i.get("depends_on") or [])] for k, i in zip(keys, items, strict=True)}
        for key, deps in graph.items():
            unknown = [d for d in deps if d not in graph]
            if unknown:
                raise ValueError(f"{key} depends on unknown subtasks {unknown}")
        _check_acyclic(graph)
        current = {s["local_key"]: s for s in self._subtasks(uow, task)}
        busy = [s["key"] for s in current.values() if s["state"] in ("IN_PROGRESS", "IN_REVIEW") and s["local_key"] not in graph]
        if busy:
            raise Conflict(f"the new plan drops subtasks that are running ({', '.join(busy)}); revise them instead")
        new_keys = [k for k in keys if k not in current]
        expansion = self._expansion_limit(uow, task)
        if task["current_plan_version"] and expansion is not None and len(new_keys) > expansion:
            if not self._expansion_approved(uow, task, items, len(new_keys), expansion):
                return f"adds {len(new_keys)} subtasks (limit {expansion}): SCOPE_EXPANSION approval requested; resubmit this plan once approved"
        budgets.charge(uow, task, "subtasks", len(new_keys))
        version = int(task["current_plan_version"] or 0) + 1
        uow.cur.execute("SELECT count(*) AS n FROM subtasks WHERE task_id = %s", (task["id"],))
        counter = int(uow.cur.fetchone()["n"])  # type: ignore[index]
        mapping: dict[str, UUID] = {}
        for key, item in zip(keys, items, strict=True):
            existing = current.get(key)
            if existing is not None:
                uow.cur.execute("UPDATE subtasks SET plan_version = %s, title = %s, description = %s, estimated_scope = %s, "
                                "risk = %s, preferred_provider = %s, updated_at = now() WHERE id = %s",
                                (version, item.get("title", existing["title"])[:200], (item.get("description") or existing["description"])[:8000],
                                 jsonb({"files": list(item.get("files") or [])[:100]}), item.get("risk") or existing["risk"],
                                 item.get("preferred_provider"), existing["id"]))
                uow.cur.execute("DELETE FROM subtask_dependencies WHERE subtask_id = %s", (existing["id"],))
                mapping[key] = existing["id"]
                continue
            counter += 1
            subtask_id = uuid7()
            uow.cur.execute(
                "INSERT INTO subtasks (id, task_id, key, local_key, plan_version, kind, title, description, state, estimated_scope, "
                "risk, preferred_provider) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s)",
                (subtask_id, task["id"], f"{task['key']}-{counter}", key, version, item.get("kind") or "IMPLEMENT",
                 str(item.get("title") or key)[:200], str(item.get("description") or "")[:8000],
                 jsonb({"files": list(item.get("files") or [])[:100]}), item.get("risk") or "MEDIUM", item.get("preferred_provider")))
            mapping[key] = subtask_id
        for key, deps in graph.items():
            for dep in deps:
                uow.cur.execute("INSERT INTO subtask_dependencies (subtask_id, depends_on_subtask_id) VALUES (%s, %s)",
                                (mapping[key], mapping[dep]))
        for old_key, old in current.items():
            if old_key not in graph and old["state"] not in ("ACCEPTED", "INTEGRATED"):
                self._set_subtask(uow, old, "CANCELLED", "dropped from the plan")
                self._retire_workspace(uow, old)
            elif old_key not in graph:
                uow.cur.execute("UPDATE subtasks SET plan_version = %s WHERE id = %s", (version, old["id"]))
        uow.cur.execute("UPDATE tasks SET current_plan_version = %s WHERE id = %s", (version, task["id"]))
        self._refresh_ready(uow, task)
        artifact = self.ctx.artifacts.write(uow.cur, project_id=task["project_id"], task_id=task["id"], kind="plan",
                                            name=f"plan-v{version}.json", content=json.dumps(items, indent=2).encode()[:200_000],
                                            media_type="application/json")
        self._event(uow, task, "PLAN_VERSIONED", f"plan version {version}: {len(items)} subtask(s), {len(new_keys)} new",
                    {"version": version, "artifact": str(artifact.id)})
        if task["state"] == S.PLANNING:
            self._move(uow, task, S.QUEUED, reason="plan accepted")
        return f"version {version}"

    def _expansion_approved(self, uow: UnitOfWork, task: Row, items: list[dict[str, Any]], added: int, limit: int) -> bool:
        """A plan beyond the expansion profile needs a human approval bound to that exact plan (design change)."""
        config = self._config(uow, task)
        digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        subject = {"kind": "plan_expansion", "plan": digest, "added": added, "limit": limit}
        uow.cur.execute("SELECT * FROM approvals WHERE task_id = %s AND action = 'SCOPE_EXPANSION' AND state = 'APPROVED' "
                        "AND subject->>'plan' = %s ORDER BY decided_at DESC LIMIT 1", (task["id"], digest))
        approval = uow.cur.fetchone()
        if approval is not None and self.approvals.consume(uow, approval, subject=subject, config_hash=config["_hash"]):
            return True
        uow.cur.execute("SELECT 1 FROM approvals WHERE task_id = %s AND action = 'SCOPE_EXPANSION' AND state = 'PENDING' "
                        "AND subject->>'plan' = %s", (task["id"], digest))
        if uow.cur.fetchone() is None:
            self.approvals.request(uow, action=ApprovalAction.SCOPE_EXPANSION, project_id=task["project_id"], task_id=task["id"],
                                   subject=subject, config_hash=config["_hash"], requested_by="orchestrator", risk=Risk.HIGH,
                                   summary=f"{task['key']}: revised plan adds {added} subtasks (expansion limit {limit})",
                                   from_task_state=task["state"])
            self._move(uow, task, S.APPROVAL_REQUIRED, reason="plan expansion needs approval")
        return False

    def _expansion_limit(self, uow: UnitOfWork, task: Row) -> int | None:
        profile = (self._config(uow, task).get("expansion") or {}).get("profile", "NORMAL")
        return {"SMALL": 1, "NORMAL": 3, "LARGE": 10}.get(profile)

    def _refresh_ready(self, uow: UnitOfWork, task: Row) -> None:
        """PENDING subtasks whose dependencies are all accepted become READY."""
        uow.cur.execute(
            """
            UPDATE subtasks s SET state = 'READY', updated_at = now()
            WHERE s.task_id = %s AND s.state = 'PENDING' AND NOT EXISTS (
                SELECT 1 FROM subtask_dependencies d JOIN subtasks x ON x.id = d.depends_on_subtask_id
                WHERE d.subtask_id = s.id AND x.state NOT IN ('ACCEPTED', 'INTEGRATED'))
            """, (task["id"],))

    def _request_execution(self, uow: UnitOfWork, task: Row, epoch: int, action: dict[str, Any]) -> str:
        subtask = self._subtask(uow, task, action.get("subtask"))
        if subtask is None:
            raise Conflict(f"unknown subtask {action.get('subtask')!r}")
        # Writing tests is development work: a TESTER request for a TEST_AUTHORING subtask runs as DEVELOPER.
        if action.get("role") not in (None, "DEVELOPER") and not (action.get("role") == "TESTER" and subtask["kind"] == "TEST_AUTHORING"):
            raise Conflict("only DEVELOPER executions are requested by the orchestrator (TEST_AUTHORING subtasks included); "
                           "verification and reviews run automatically")
        if subtask["state"] not in ("READY", "FIX_REQUIRED"):
            raise Conflict(f"{subtask['key']} is {subtask['state']}")
        if task["state"] == S.PLANNING and task["current_plan_version"]:
            task = self._move(uow, task, S.QUEUED, reason="plan accepted")  # a plan exists (for example after a release)
        if task["state"] not in (S.QUEUED, S.RUNNING, S.FIX_REQUIRED):
            raise Conflict(f"{task['key']} is {task['state']}")
        provider = action.get("provider") or subtask["preferred_provider"]
        if subtask["state"] == "FIX_REQUIRED" and subtask["developer_provider"]:
            provider = subtask["developer_provider"]  # fixes stay with the developer of the attempt
        else:
            provider = self._route(uow, task, subtask["kind"], suggested=provider)
        if provider is None:
            raise Conflict("no provider is available")
        self._develop(uow, task, subtask, provider, extra=action.get("prompt") or "", epoch=epoch,
                      kind="FIX" if subtask["state"] == "FIX_REQUIRED" else "DEVELOP")
        return f"{subtask['key']} -> {provider}"

    # ------------------------------------------------------------ pipeline

    def _develop(self, uow: UnitOfWork, task: Row, subtask: Row, provider: str, *, extra: str, epoch: int | None,
                 kind: str, fresh: bool = False, retry: bool = False) -> Row | None:
        """Launch a developer execution for a subtask in its workspace (a fresh one for a new attempt)."""
        workspace = None
        if subtask["workspace_id"] and not fresh:
            uow.cur.execute("SELECT * FROM workspaces WHERE id = %s AND status = 'ACTIVE'", (subtask["workspace_id"],))
            workspace = uow.cur.fetchone()
        if workspace is None:
            self._retire_workspace(uow, subtask)
            attempt = int(subtask["attempts"]) + 1
            workspace = self.git.workspace(uow, task["key"], principal=CONTROL_PLANE,
                                           suffix=f"{subtask['key'].split('-')[-1]}a{attempt}".lower(),
                                           base_ref=self._dependency_base(uow, task, subtask))
            uow.cur.execute("UPDATE subtasks SET workspace_id = %s, attempts = %s WHERE id = %s",
                            (workspace["id"], attempt, subtask["id"]))
        knowledge = self._knowledge_for(uow, task, list((subtask["estimated_scope"] or {}).get("files") or []))
        prompt = "\n".join([f"Subtask {subtask['key']}: {subtask['title']}", "", subtask["description"].strip(),
                            *(["", extra.strip()] if extra.strip() else []),
                            *(["", "## Project knowledge for these files"] + [f"- [{k['trust']}] {k['title']}: {k['body'][:300]}"
                                                                             for k in knowledge] if knowledge else []), "",
                            "Work only on this subtask. Run the relevant tests, fix failures, and commit locally when done."])
        request = {"role": "DEVELOPER", "provider": provider, "prompt": prompt, "workspace": workspace["path"],
                   "capabilities": {"workspace": "WRITE", "git": "LOCAL_COMMIT", "tests": "EXECUTE", "egress": "STANDARD"},
                   "subtask_id": str(subtask["id"]), "lease_epoch": epoch,
                   "purpose": {"subtask": str(subtask["id"]), "kind": kind, "fenced": epoch is not None, "retry": retry}}
        if task["state"] == S.QUEUED:
            task = self._move(uow, task, S.RUNNING, reason="subtask work started")
        elif task["state"] == S.FIX_REQUIRED:
            task = self._move(uow, task, S.RUNNING, reason="fixes started")
        uow.cur.execute("UPDATE subtasks SET developer_provider = %s, state = 'IN_PROGRESS', updated_at = now() WHERE id = %s",
                        (provider, subtask["id"]))
        return self.launch(uow, task, kind, request, subtask=subtask)

    def _dependency_base(self, uow: UnitOfWork, task: Row, subtask: Row) -> str | None:
        """A dependent subtask starts from the accepted work it depends on, not from the bare task base."""
        uow.cur.execute("SELECT w.name FROM subtask_dependencies d JOIN subtasks x ON x.id = d.depends_on_subtask_id "
                        "JOIN workspaces w ON w.id = x.workspace_id WHERE d.subtask_id = %s AND w.collected_at IS NOT NULL "
                        "ORDER BY x.key", (subtask["id"],))
        heads = [f"refs/hermes/workspaces/{r['name']}" for r in uow.cur.fetchall()]
        if not heads:
            return None
        if len(heads) == 1:
            return heads[0]
        _, project, _ = self.git._context(uow, task["key"])
        merged = self.ctx.git.integrate(project["relative_path"], task["key"], task["target_branch"] or project["default_branch"]
                                        or "main", heads)
        if not merged.get("ok"):
            raise Conflict(f"the dependencies of {subtask['key']} conflict: {', '.join(merged.get('conflicts') or [])}")
        return merged["integration_ref"]

    def _retire_workspace(self, uow: UnitOfWork, subtask: Row) -> None:
        if subtask["workspace_id"]:
            uow.cur.execute("UPDATE workspaces SET status = 'RETAINED' WHERE id = %s AND status = 'ACTIVE'", (subtask["workspace_id"],))

    def _on_execution_finished(self, uow: UnitOfWork, execution: Row, state: str, failure: str | None) -> None:
        purpose = (execution["spec"] or {}).get("purpose") or {}
        if purpose.get("review") and purpose.get("subtask"):
            if state != "SUCCEEDED":
                self._subtask_review_failed(uow, execution["task_id"], UUID(purpose["subtask"]),
                                            f"review by {execution['provider']} {state.lower()}")
            return
        if not (purpose.get("orchestrator_step") or purpose.get("kind")):
            return
        uow.cur.execute("SELECT * FROM executions WHERE id = %s", (execution["id"],))
        execution = uow.cur.fetchone()  # the row as finished (result, failure reason)
        assert execution is not None
        if purpose.get("orchestrator_step"):
            if state != "SUCCEEDED":
                self._step_failed(uow, execution, failure)
            return
        if purpose.get("kind") in ("DEVELOP", "FIX") and purpose.get("subtask"):
            self._developer_finished(uow, execution, state, failure)
        elif purpose.get("kind") == "RESOLVE":
            task = self._task(uow, execution["task_id"])
            if state == "SUCCEEDED":
                uow.after_commit.append(lambda key=task["key"]: self._integrate_later(key))
            else:
                self._input(uow, task, f"conflict resolution {state.lower()}: {execution['failure_reason'] or ''}")

    def _step_failed(self, uow: UnitOfWork, execution: Row, failure: str | None) -> None:
        task = self._task(uow, execution["task_id"])
        uow.cur.execute("UPDATE tasks SET orchestrator_failures = orchestrator_failures + 1 WHERE id = %s RETURNING orchestrator_failures",
                        (task["id"],))
        failures = int(uow.cur.fetchone()["orchestrator_failures"])  # type: ignore[index]
        lease = self.leases.get(uow, task["id"], lock=True)
        unavailable = failure in (FailureClass.QUOTA.value, FailureClass.AUTH.value)
        if lease and (unavailable or failures >= 2) and OTHER[lease["provider"]] in self._available(uow):
            recovery.checkpoint(uow, task, f"failover from {lease['provider']}")
            epoch = self.leases.acquire(uow, task["id"], OTHER[lease["provider"]])  # failover at a step boundary
            self._event(uow, task, "FAILOVER_COMPLETED", f"orchestration moved to {OTHER[lease['provider']]} (epoch {epoch})",
                        {"from": lease["provider"], "to": OTHER[lease["provider"]], "epoch": epoch})
            if task["state"] == S.AUTH_REQUIRED and failure == FailureClass.AUTH.value:
                # The other provider leads now: the task need not wait for this login.
                task = self.tasks.transition(uow, task, S(task["resume_state"]), trigger=Trigger.SYSTEM, actor="orchestration",
                                             reason=f"orchestration continues with {OTHER[lease['provider']]}")
                uow.cur.execute("UPDATE tasks SET waiting_on_credential = NULL WHERE id = %s", (task["id"],))
        if failures >= MAX_STEP_FAILURES:
            if S(task["state"]) in STEP_STATES:
                self._move(uow, task, S.BLOCKED, reason=f"the orchestrator failed {failures} times in a row")
            return
        if S(task["state"]) in STEP_STATES:
            uow.cur.execute("UPDATE tasks SET step_requested = true WHERE id = %s", (task["id"],))

    def _developer_finished(self, uow: UnitOfWork, execution: Row, state: str, failure: str | None) -> None:
        task = self._task(uow, execution["task_id"])
        uow.cur.execute("SELECT * FROM subtasks WHERE id = %s FOR UPDATE", (execution["subtask_id"],))
        subtask = uow.cur.fetchone()
        if subtask is None or subtask["state"] != "IN_PROGRESS":
            return
        if S(task["state"]) not in (S.RUNNING, S.FIX_REQUIRED, S.QUEUED):
            if S(task["state"]) not in WAITING_STATES or failure == "AUTH":
                return  # AUTH: the Phase 4 flow continues this execution after the login
            self._set_subtask(uow, subtask, "FIX_REQUIRED", f"developer {state.lower()} while the task was {task['state']}")
            uow.cur.execute("UPDATE tasks SET step_requested = true WHERE id = %s", (task["id"],))
            return
        config = self._config(uow, task)
        retries = config.get("retries") or {}
        result = execution["result"] or {}
        if state == "CANCELLED":
            self._set_subtask(uow, subtask, "FIX_REQUIRED", "the developer execution was cancelled")
            return
        if state != "SUCCEEDED":
            if failure == "TRANSIENT" and int(subtask["attempts"]) <= int(retries.get("transient", 2)):
                self._event(uow, task, "RETRY_SCHEDULED", f"{subtask['key']}: transient failure, retrying")
                self._develop(uow, task, subtask, execution["provider"], extra="", epoch=None, kind="DEVELOP", retry=True)
                return
            if failure == "QUOTA" and retries.get("alternate_developer", True) and OTHER[execution["provider"]] in self._available(uow):
                self._event(uow, task, "PROVIDER_FALLBACK", f"{subtask['key']}: {execution['provider']} is out of quota; "
                            f"{OTHER[execution['provider']]} starts a fresh attempt")
                self._develop(uow, task, subtask, OTHER[execution["provider"]], extra="", epoch=None, kind="DEVELOP", fresh=True, retry=True)
                return
            self._set_subtask(uow, subtask, "FIX_REQUIRED", f"developer {state.lower()}: {(execution['failure_reason'] or '')[:200]}")
            self._input(uow, task, f"{subtask['key']} developer execution {state.lower()} ({failure}): "
                        f"{(execution['failure_reason'] or '')[:200]}", {"subtask": subtask["key"]})
            return
        if result.get("status") in ("blocked", "failed"):
            self._set_subtask(uow, subtask, "BLOCKED", (result.get("blocked_reason") or result.get("summary") or "")[:300])
            self._input(uow, task, f"{subtask['key']} reported {result['status']}: "
                        f"{(result.get('blocked_reason') or result.get('summary') or '')[:200]}", {"subtask": subtask["key"]})
            return
        uow.after_commit.append(lambda key=task["key"], sid=subtask["id"], dev=execution["provider"]:
                                self._review_subtask_later(key, sid, dev))

    def _review_subtask_later(self, task_key: str, subtask_id: UUID, developer: str) -> None:
        """Collect the subtask's commits and ask the other provider to review them (section 9)."""
        try:
            with self.ctx.unit_of_work() as uow:
                task = self.tasks.get(uow, task_key, lock=True)
                uow.cur.execute("SELECT * FROM subtasks WHERE id = %s FOR UPDATE", (subtask_id,))
                subtask = uow.cur.fetchone()
                assert subtask is not None
                collected = {c["workspace"]: c for c in self.git.collect(uow, task_key)}
                uow.cur.execute("SELECT * FROM workspaces WHERE id = %s", (subtask["workspace_id"],))
                workspace = uow.cur.fetchone()
                assert workspace is not None
                result = collected.get(workspace["name"])
                if (result is None or not result["commits"]) and subtask["kind"] == "RESEARCH":
                    self._accept_research(uow, task, subtask)
                    return
                if result is None or not result["commits"]:
                    self._set_subtask(uow, subtask, "FIX_REQUIRED", "the developer made no commits")
                    self._input(uow, task, f"{subtask['key']}: the developer finished without committing", {"subtask": subtask["key"]})
                    return
                self._request_subtask_review(uow, task, subtask, developer, workspace, result["head_sha"])
        except ApiError as exc:
            log.warning("review of %s not started: %s", task_key, exc)

    def _accept_research(self, uow: UnitOfWork, task: Row, subtask: Row) -> None:
        """Research produces findings, not code: there is nothing to cross-review, so a completed run is accepted
        and its summary goes to the orchestrator."""
        uow.cur.execute("SELECT result->>'summary' AS summary FROM executions WHERE subtask_id = %s AND state = 'SUCCEEDED' "
                        "ORDER BY ended_at DESC LIMIT 1", (subtask["id"],))
        row = uow.cur.fetchone()
        self._set_subtask(uow, subtask, "ACCEPTED", "research completed (no code to review)")
        self._refresh_ready(uow, task)
        self._input(uow, task, f"{subtask['key']} research findings: {((row or {}).get('summary') or '')[:600]}",
                    {"subtask": subtask["key"]})

    def _request_subtask_review(self, uow: UnitOfWork, task: Row, subtask: Row, developer: str, workspace: Row, head: str) -> None:
        reviewer = OTHER[developer]  # an agent never approves its own work
        ref = f"refs/hermes/workspaces/{workspace['name']}"
        request = {"kind": "SUBTASK_REVIEW", "reviewer": reviewer, "developer": developer, "ref": ref, "head": head,
                   "subtask": str(subtask["id"])}
        self._set_subtask(uow, subtask, "IN_REVIEW", None)
        if self._blocker(uow, task, "SUBTASK_REVIEW", subtask) is None:
            try:
                with uow.cur.connection.transaction():
                    self._launch_review(uow, task, subtask, request)
                return
            except BudgetExhausted as exc:
                uow.after_commit.append(lambda key=task["key"], reason=str(exc): self.executions.pause_for_budget(key, reason))
            except (Conflict, UpstreamError):
                pass  # queued below; a service outage must not undo the finished execution being recorded
        uow.cur.execute("INSERT INTO pending_launches (id, task_id, subtask_id, kind, request, reason) VALUES (%s, %s, %s, %s, %s, %s)",
                        (uuid7(), task["id"], subtask["id"], "SUBTASK_REVIEW", jsonb(request), "waiting for capacity"))

    def _subtask_review_failed(self, uow: UnitOfWork, task_id: UUID, subtask_id: UUID, reason: str) -> None:
        """A subtask review that failed or was lost is retried (bounded); then the orchestrator decides."""
        task = self._task(uow, task_id)
        uow.cur.execute("SELECT * FROM subtasks WHERE id = %s FOR UPDATE", (subtask_id,))
        subtask = uow.cur.fetchone()
        if subtask is None or subtask["state"] != "IN_REVIEW" or S(task["state"]) not in (S.RUNNING, S.FIX_REQUIRED):
            return
        uow.cur.execute("SELECT count(*) AS n FROM executions WHERE subtask_id = %s AND role = 'REVIEWER' AND state <> 'SUCCEEDED' "
                        "AND state <> ALL(%s)", (subtask_id, list(ACTIVE)))
        failed = int(uow.cur.fetchone()["n"])  # type: ignore[index]
        uow.cur.execute("SELECT * FROM workspaces WHERE id = %s", (subtask["workspace_id"],))
        workspace = uow.cur.fetchone()
        if failed > MAX_REVIEW_RETRIES or workspace is None or not workspace["head_sha"] or not subtask["developer_provider"]:
            self._set_subtask(uow, subtask, "FIX_REQUIRED", f"{reason}; review not completed")
            self._input(uow, task, f"{subtask['key']}: the cross-review could not be completed ({reason})", {"subtask": subtask["key"]})
            return
        self._event(uow, task, "RETRY_SCHEDULED", f"{subtask['key']}: {reason}; reviewing again")
        self._request_subtask_review(uow, task, subtask, subtask["developer_provider"], workspace, workspace["head_sha"])

    def heal_reviews(self) -> int:
        """Subtasks waiting for a review that nothing is running or queued for (a review lost before this
        control plane could react) get their review requested again."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                "SELECT s.id, s.task_id FROM subtasks s JOIN tasks t ON t.id = s.task_id WHERE s.state = 'IN_REVIEW' "
                "AND t.state IN ('RUNNING', 'FIX_REQUIRED') AND s.updated_at < now() - interval '2 minutes' "
                "AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.subtask_id = s.id AND e.role = 'REVIEWER' AND e.state = ANY(%s)) "
                "AND NOT EXISTS (SELECT 1 FROM pending_launches p WHERE p.subtask_id = s.id)", (list(ACTIVE),))
            stuck = uow.cur.fetchall()
        for row in stuck:
            with self.ctx.unit_of_work() as uow:
                self._subtask_review_failed(uow, row["task_id"], row["id"], "no review was running")
        return len(stuck)

    def _launch_review(self, uow: UnitOfWork, task: Row, subtask: Row, request: dict[str, Any]) -> Row:
        # Review the subtask's own changes: from where its workspace started (its dependencies' work, if any).
        uow.cur.execute("SELECT base_sha FROM workspaces WHERE id = %s", (subtask["workspace_id"],))
        base = uow.cur.fetchone()["base_sha"]  # type: ignore[index]
        return self.reviews.request_review(uow, task, provider=request["reviewer"], principal=ORCHESTRATOR, ref=request["ref"],
                                           commit=request["head"], base=base, developers=[request["developer"]],
                                           scope=f"subtask {subtask['key']} ({subtask['title']})", subtask=subtask)

    def _on_review(self, uow: UnitOfWork, review: Row, execution: Row) -> None:
        task = self._task(uow, review["task_id"])
        if review["subtask_id"] is None:
            self._integration_review_recorded(uow, task)
            return
        uow.cur.execute("SELECT * FROM subtasks WHERE id = %s FOR UPDATE", (review["subtask_id"],))
        subtask = uow.cur.fetchone()
        if subtask is None or subtask["state"] != "IN_REVIEW":
            return
        if S(task["state"]) in WAITING_STATES:  # no launches now; the orchestrator decides when the task resumes
            approved = review["outcome"] == "APPROVED" and review["requirements_met"]
            self._set_subtask(uow, subtask, "ACCEPTED" if approved else "FIX_REQUIRED",
                              f"review by {review['reviewer_provider']}: {review['outcome'].lower()} (task was {task['state']})")
            if approved:
                self._refresh_ready(uow, task)
            uow.cur.execute("UPDATE tasks SET step_requested = true WHERE id = %s", (task["id"],))
            return
        config = self._config(uow, task)
        if review["outcome"] == "APPROVED" and review["requirements_met"]:
            self._set_subtask(uow, subtask, "ACCEPTED", f"approved by {review['reviewer_provider']}")
            files = list((subtask["estimated_scope"] or {}).get("files") or [])
            if files:  # knowledge anchored to code this subtask changed must be re-confirmed
                uow.cur.execute("UPDATE knowledge_items SET trust = 'STALE', updated_at = now() WHERE project_id = %s "
                                "AND trust IN ('CONFIRMED', 'OBSERVED') AND anchors && %s", (task["project_id"], files))
            self._event(uow, task, "SUBTASK_ACCEPTED", f"{subtask['key']} approved by {review['reviewer_provider']}")
            self._refresh_ready(uow, task)
            if self._integrate_if_ready(uow, task) is None:
                ready = [s["key"] for s in self._subtasks(uow, task) if s["state"] in ("READY", "FIX_REQUIRED")]
                if ready:  # the orchestrator decides what starts next
                    self._input(uow, task, f"{subtask['key']} accepted; ready to start: {', '.join(ready)}",
                                {"subtask": subtask["key"], "ready": ready})
            return
        uow.cur.execute("SELECT severity, path, line, description FROM review_findings WHERE review_id = %s ORDER BY severity",
                        (review["id"],))
        findings = uow.cur.fetchall()
        feedback = "\n".join(["## Review feedback (fix these)", f"Verdict: {review['outcome'].lower()} by {review['reviewer_provider']}",
                              *[f"- [{f['severity']}] {f['path'] or ''}{':' + str(f['line']) if f['line'] else ''} {f['description']}"
                                for f in findings[:40]],
                              *[f"- unmet requirement: {u}" for u in (review["unmet_requirements"] or [])[:10]]])
        cycles = int(subtask["review_cycles"]) + 1
        try:
            budgets.charge(uow, task, "review_cycles")
        except BudgetExhausted as exc:
            self._set_subtask(uow, subtask, "FIX_REQUIRED", "review-cycle budget exhausted", review_cycles=cycles)
            uow.after_commit.append(lambda key=task["key"], reason=str(exc): self.executions.pause_for_budget(key, reason))
            return
        uow.cur.execute("UPDATE subtasks SET review_cycles = %s, state = 'FIX_REQUIRED' WHERE id = %s", (cycles, subtask["id"]))
        subtask = {**subtask, "review_cycles": cycles, "state": "FIX_REQUIRED"}
        limit = int((config.get("review") or {}).get("cycle_limit", 2))
        developer = subtask["developer_provider"]
        if cycles <= limit:
            self._event(uow, task, "REVIEW_FIX_REQUESTED", f"{subtask['key']}: review cycle {cycles}/{limit}, back to {developer}")
            self._develop(uow, task, subtask, developer, extra=feedback, epoch=None, kind="FIX")
            return
        on_limit = (config.get("review") or {}).get("on_limit", "ALTERNATE_DEVELOPER")
        alternate = OTHER[developer]
        if on_limit == "ALTERNATE_DEVELOPER" and int(subtask["attempts"]) < 2 and alternate in self._available(uow):
            self._event(uow, task, "ALTERNATE_DEVELOPER", f"{subtask['key']}: review limit reached; {alternate} starts a fresh attempt")
            uow.cur.execute("UPDATE subtasks SET review_cycles = 0 WHERE id = %s", (subtask["id"],))
            self._develop(uow, task, {**subtask, "review_cycles": 0}, alternate, extra=feedback, epoch=None, kind="DEVELOP", fresh=True, retry=True)
            return
        self._set_subtask(uow, subtask, "BLOCKED", f"review limit reached ({limit} cycles)")
        self._input(uow, task, f"{subtask['key']} is blocked: the review-cycle limit was reached", {"subtask": subtask["key"]})

    # ------------------------------------------------- integration and gate

    def _integrate_if_ready(self, uow: UnitOfWork, task: Row, *, explicit: bool = False) -> str | None:
        subtasks = self._subtasks(uow, task)
        open_ = [s["key"] for s in subtasks if s["state"] not in ("ACCEPTED", "INTEGRATED", "CANCELLED")]
        if not subtasks or open_:
            if explicit:
                raise Conflict(f"not all subtasks are accepted: {', '.join(open_) or 'no plan'}")
            return None
        if task["state"] != S.RUNNING:
            if explicit:
                raise Conflict(f"{task['key']} is {task['state']}")
            return None
        uow.after_commit.append(lambda key=task["key"]: self._integrate_later(key))
        return "integration started"

    def _integrate_later(self, task_key: str) -> None:
        try:
            with self.ctx.unit_of_work() as uow:
                task = self.tasks.get(uow, task_key, lock=True)
                if task["state"] != S.RUNNING:
                    return
                result = self.git.integrate(uow, task_key)
                if not result["ok"]:
                    self._resolve_conflict(uow, self.tasks.get(uow, task_key, lock=True), result)
                    return
                self._move(uow, self.tasks.get(uow, task_key, lock=True), S.TESTING, reason="integrated; verifying")
        except ApiError as exc:
            with self.ctx.unit_of_work() as uow:
                task = self.tasks.get(uow, task_key, lock=True)
                self._input(uow, task, f"integration failed: {exc}")

    def _resolve_conflict(self, uow: UnitOfWork, task: Row, result: dict[str, Any]) -> None:
        """One agent-assisted resolution attempt by the developer of the conflicting work, then the orchestrator decides."""
        uow.cur.execute("SELECT count(*) AS n FROM executions WHERE task_id = %s AND spec->'purpose'->>'kind' = 'RESOLVE'", (task["id"],))
        if int(uow.cur.fetchone()["n"]) >= 1:  # type: ignore[index]
            self._input(uow, task, f"integration conflict in {', '.join(result['conflicts'][:5])} after a resolution attempt")
            return
        name = result["conflicting_ref"].rsplit("/", 1)[-1]
        uow.cur.execute("SELECT s.developer_provider FROM subtasks s JOIN workspaces w ON w.id = s.workspace_id WHERE w.name = %s",
                        (name,))
        row = uow.cur.fetchone()
        provider = (row or {}).get("developer_provider") or self._route(uow, task, "IMPLEMENT")
        try:
            resolved = self.git.resolve_conflicts(uow, task["key"], provider=provider, principal=ORCHESTRATOR)
            uow.cur.execute("UPDATE executions SET spec = jsonb_set(spec, '{purpose}', %s) WHERE id = %s",
                            (jsonb({"kind": "RESOLVE"}), resolved["execution"]))
        except ApiError as exc:
            self._input(uow, task, f"integration conflict could not be handed to an agent: {exc}")

    def _on_verification(self, uow: UnitOfWork, verification: Row, state: str) -> None:
        if verification["purpose"] != "INTEGRATION":
            return
        task = self._task(uow, verification["task_id"])
        if task["state"] != S.TESTING:
            return
        if state != "PASSED":
            self._move(uow, task, S.FIX_REQUIRED, reason=f"verification {state.lower()}")
            self._input(uow, task, f"verification of the integrated change {state.lower()}: {(verification['error'] or '')[:200]}")
            return
        task = self._move(uow, task, S.REVIEW, reason="verification passed; integration review")
        developers = self.reviews.developer_providers(uow, task)
        for provider in sorted(required_reviewer_set(developers) or {self._route(uow, task, "IMPLEMENT") or "claude"}):
            uow.after_commit.append(lambda key=task["key"], p=provider: self._integration_review_later(key, p))

    def _integration_review_later(self, task_key: str, provider: str) -> None:
        try:
            with self.ctx.unit_of_work() as uow:
                self.reviews.request(uow, task_key, provider=provider, principal=ORCHESTRATOR)
        except ApiError as exc:
            with self.ctx.unit_of_work() as uow:
                task = self.tasks.get(uow, task_key, lock=True)
                self._input(uow, task, f"integration review by {provider} could not start: {exc}")

    def _integration_review_recorded(self, uow: UnitOfWork, task: Row) -> None:
        if task["state"] != S.REVIEW:
            return
        uow.cur.execute("SELECT integration_sha FROM git_changes WHERE task_id = %s", (task["id"],))
        commit = uow.cur.fetchone()["integration_sha"]  # type: ignore[index]
        uow.cur.execute("SELECT DISTINCT reviewer_provider FROM reviews WHERE task_id = %s AND commit_sha = %s AND subtask_id IS NULL",
                        (task["id"], commit))
        done = {r["reviewer_provider"] for r in uow.cur.fetchall()}
        needed = required_reviewer_set(self.reviews.developer_providers(uow, task))
        if needed and not needed <= done:
            return
        task = self._move(uow, task, S.QUALITY_GATE, reason="integration reviewed")
        uow.after_commit.append(lambda key=task["key"]: self._evaluate_later(key))

    def _evaluate_later(self, task_key: str) -> None:
        with self.ctx.unit_of_work() as uow:
            task = self.tasks.get(uow, task_key, lock=True)
            if task["state"] != S.QUALITY_GATE:
                return
            uow.cur.execute("SELECT * FROM git_changes WHERE task_id = %s", (task["id"],))
            changes = uow.cur.fetchone()
            remote = self.ctx.git.refs(self.git._context(uow, task_key)[1]["relative_path"], [changes["target_branch"]])["remote"]  # type: ignore[index]
            if remote["kind"] == "github" and changes["pushed_sha"] != changes["integration_sha"]:  # type: ignore[index]
                self.git.pull_request(uow, task_key)  # CI needs the PR (ARCHITECTURE.md section 6.4 step 5)
            self.gate.evaluate(uow, task_key)

    def _on_gate(self, uow: UnitOfWork, task: Row, evaluation: Row) -> None:
        task = self._task(uow, task["id"])
        if evaluation["outcome"] == "PASS" and task["state"] == S.READY_FOR_MERGE:
            try:
                with uow.cur.connection.transaction():
                    manifests.store(uow, self.ctx, task, "READY_FOR_MERGE")
            except Exception:  # noqa: BLE001 - the gate decision stands even if its manifest cannot be built
                log.exception("ready-for-merge manifest for %s failed", task["key"])
        if evaluation["outcome"] == "FAIL" and task["state"] == S.FIX_REQUIRED:
            failing = [f"{r['name']}: {r['detail']}" for r in evaluation["requirements"] if r["status"] == "FAIL"]
            self._input(uow, task, "Quality Gate failed: " + "; ".join(failing)[:250], {"evaluation": str(evaluation["id"])})

    # ----------------------------------------------------------- approvals

    def _on_waiting_approval(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        if approval["task_id"] is None:
            return
        task = self._task(uow, approval["task_id"])
        uow.cur.execute("UPDATE assumptions SET status = %s WHERE approval_id = %s", ("APPROVED" if approved else "REJECTED", approval["id"]))
        if task["state"] != S.APPROVAL_REQUIRED:
            return
        if approved:
            self.tasks.transition(uow, task, S(task["resume_state"]), trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason=f"{approval['action'].lower()} approved")
        else:
            self.tasks.transition(uow, task, S.BLOCKED, trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason=f"{approval['action'].lower()} rejected")

    def _on_budget_increase(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        if approval["task_id"] is None or not approved:
            return
        task = self._task(uow, approval["task_id"])
        uow.cur.execute("SELECT * FROM budgets WHERE task_id = %s FOR UPDATE", (task["id"],))
        budget = uow.cur.fetchone()
        limits = dict(budget["limits"])  # type: ignore[index]
        for counter, amount in (approval["subject"].get("add") or {}).items():
            if counter in budgets.COUNTERS and limits.get(counter) is not None:
                limits[counter] = int(limits[counter]) + int(amount)
        uow.cur.execute("UPDATE budgets SET limits = %s, state = 'OK' WHERE task_id = %s", (jsonb(limits), task["id"]))
        self._event(uow, task, "BUDGET_RAISED", f"budget raised by {principal.value}: {approval['subject'].get('add')}")
        if task["state"] == S.PAUSED_BUDGET:
            task = self.tasks.transition(uow, task, S(task["resume_state"]), trigger=Trigger.APPROVAL, actor=principal.value,
                                         reason="budget raised")
        if S(task["state"]) not in WAITING_STATES:
            self.verifications.relaunch_waiting(uow, task)

    def request_budget_increase(self, uow: UnitOfWork, task_key: str, add: dict[str, int], *, principal: Principal) -> Row:
        task = self.tasks.get(uow, task_key, lock=True)
        unknown = set(add) - set(budgets.COUNTERS)
        if unknown or not add or any(int(v) <= 0 for v in add.values()):
            raise Conflict(f"budget increases name counters from {', '.join(budgets.COUNTERS)} with positive amounts")
        config = self._config(uow, task)
        return self.approvals.request(uow, action=ApprovalAction.BUDGET_INCREASE, project_id=task["project_id"], task_id=task["id"],
                                      subject={"add": {k: int(v) for k, v in add.items()}}, config_hash=config["_hash"],
                                      summary=f"{task_key}: raise budget by {add}", requested_by=principal.value, risk=Risk.MEDIUM,
                                      from_task_state=task["state"])

    # ------------------------------------------------------------ revisions

    def revise(self, uow: UnitOfWork, task_key: str, text: str, *, principal: Principal) -> dict[str, Any]:
        """Live requirement revision (section 43): a new USER version; the orchestrator analyses the impact."""
        task = self.tasks.get(uow, task_key, lock=True)
        if S(task["state"]) in (S.DONE, S.CANCELLED, S.FAILED, S.MERGING, S.VERIFYING):
            raise Conflict(f"{task_key} is {task['state']}")
        version = self._set_requirements(uow, task, text, source="USER", reason=f"revised by {principal.value}")
        self._event(uow, task, "REQUIREMENTS_REVISED", f"requirements revised by {principal.value} ({version}); impact analysis "
                    "follows: keep, replan, cancel, or add subtasks", {"version": version}, actor=principal.value)
        if task["state"] == S.READY_FOR_MERGE:
            self.approvals.invalidate_open(uow, project_id=task["project_id"], task_id=task["id"], action=ApprovalAction.MERGE,
                                           reason="requirements revised")
            self._move(uow, task, S.RUNNING, reason="requirements revised")
        return {"task": task_key, "requirements": version}

    # ------------------------------------------------------------ inspection

    def inspect(self, uow: UnitOfWork, task_key: str) -> dict[str, Any]:
        task = self.tasks.get(uow, task_key)
        lease = self.leases.get(uow, task["id"])
        uow.cur.execute("SELECT type, outcome, reason, lease_epoch, created_at FROM orchestrator_actions WHERE task_id = %s "
                        "ORDER BY created_at DESC, seq DESC LIMIT 50", (task["id"],))
        recent = uow.cur.fetchall()
        uow.cur.execute("SELECT level, assumption, status FROM assumptions WHERE task_id = %s ORDER BY created_at", (task["id"],))
        assumptions = uow.cur.fetchall()
        uow.cur.execute("SELECT kind, reason, requested_at FROM pending_launches WHERE task_id = %s ORDER BY requested_at",
                        (task["id"],))
        pending = uow.cur.fetchall()
        return {"task": task_key, "state": task["state"],
                "lease": {k: lease[k] for k in ("holder", "provider", "epoch", "expires_at")} if lease else None,
                "requirements_version": task["current_requirements_version"], "plan_version": task["current_plan_version"],
                "subtasks": [{k: s[k] for k in ("key", "local_key", "kind", "title", "state", "state_reason", "risk",
                                                "developer_provider", "review_cycles", "attempts")} for s in self._subtasks(uow, task)],
                "assumptions": assumptions, "actions": recent, "pending_launches": pending, "budget": budgets.state(uow, task)}

    def decide_knowledge(self, uow: UnitOfWork, item_id: UUID, *, confirm: bool, principal: Principal) -> Row:
        uow.cur.execute("UPDATE knowledge_items SET trust = %s, updated_at = now(), provenance = provenance || %s "
                        "WHERE id = %s RETURNING *",
                        ("CONFIRMED" if confirm else "REJECTED", jsonb({"decided_by": principal.value}), item_id))
        item = uow.cur.fetchone()
        if item is None:
            raise NotFound("knowledge item not found")
        record_event(uow.cur, "KNOWLEDGE_DECIDED", actor=principal.value, project_id=item["project_id"],
                     summary=f"knowledge {'confirmed' if confirm else 'rejected'}: {item['title'][:200]}",
                     data={"id": str(item_id)}, pending=uow.events)
        return item

    # ----------------------------------------------------------------- sync

    def adopt_orphans(self) -> int:
        """Lead again the orchestrated tasks whose lease is missing or expired (a restarted or lost control plane),
        so they never wait forever; acquiring bumps the epoch, which fences anything decided under the old one."""
        adopted = 0
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                "SELECT t.* FROM tasks t LEFT JOIN task_leases l ON l.task_id = t.id WHERE t.state = ANY(%s) "
                "AND t.started_at IS NOT NULL AND (l.task_id IS NULL OR l.expires_at < now()) "
                "AND EXISTS (SELECT 1 FROM executions e WHERE e.task_id = t.id AND e.role = 'ORCHESTRATOR') FOR UPDATE OF t",
                ([s.value for s in STEP_STATES],))
            for task in uow.cur.fetchall():
                provider = self._route(uow, task, "PLANNING")
                if provider is None:
                    continue
                epoch = self.leases.acquire(uow, task["id"], provider)
                uow.cur.execute("UPDATE tasks SET step_requested = true WHERE id = %s", (task["id"],))
                self._event(uow, task, "LEASE_ADOPTED", f"orchestration resumed by {self.ctx.instance_id} with {provider} "
                            f"(epoch {epoch})", {"provider": provider, "epoch": epoch})
                adopted += 1
        return adopted

    def failback(self) -> int:
        """Return the lead to Claude (the preferred orchestrator) once it is available again, only at a safe
        checkpoint and not within FAILBACK_HOLD of the last failover (no flapping)."""
        if not (self.ctx.platform.get("orchestration") or {}).get("failback", True):
            return 0
        moved = 0
        with self.ctx.unit_of_work() as uow:
            if "claude" not in self._available(uow):
                return 0
            uow.cur.execute(
                "SELECT t.* FROM tasks t JOIN task_leases l ON l.task_id = t.id WHERE l.provider = 'codex' AND t.state = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.task_id = t.id AND e.type = 'FAILOVER_COMPLETED' "
                "AND e.occurred_at > now() - %s) FOR UPDATE OF t", ([s.value for s in STEP_STATES], FAILBACK_HOLD))
            for task in uow.cur.fetchall():
                if not recovery.at_safe_checkpoint(uow, task):
                    continue
                recovery.checkpoint(uow, task, "failback to claude")
                epoch = self.leases.acquire(uow, task["id"], "claude")
                self._event(uow, task, "ORCHESTRATOR_FAILBACK", f"orchestration returned to claude (epoch {epoch})",
                            {"from": "codex", "to": "claude", "epoch": epoch})
                moved += 1
        return moved

    def sync(self) -> dict[str, int]:
        stats = {"steps": 0, "adopted": self.adopt_orphans(), "failback": self.failback(), "reviews_healed": self.heal_reviews(),
                 "launched": self.process_pending()}
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                """
                SELECT t.id FROM tasks t JOIN task_leases l ON l.task_id = t.id
                WHERE t.state = ANY(%s) AND (t.step_requested OR EXISTS (
                    SELECT 1 FROM events e WHERE e.task_id = t.id AND e.seq > t.orchestrator_cursor_seq AND e.type = ANY(%s)))
                """,
                ([s.value for s in STEP_STATES], list(TRIGGERS)),
            )
            candidates = [r["id"] for r in uow.cur.fetchall()]
            uow.cur.execute("SELECT task_id FROM task_leases WHERE holder = %s", (self.ctx.instance_id,))
            for row in uow.cur.fetchall():
                self.leases.renew(uow, row["task_id"])
        with self.ctx.unit_of_work() as uow:  # final manifests for tasks that ended since the last pass
            uow.cur.execute("SELECT t.* FROM tasks t WHERE t.state IN ('DONE', 'CANCELLED', 'FAILED') AND t.started_at IS NOT NULL "
                            "AND NOT EXISTS (SELECT 1 FROM manifests m WHERE m.task_id = t.id AND m.kind = 'FINAL') LIMIT 20")
            for task in uow.cur.fetchall():
                try:
                    with uow.cur.connection.transaction():
                        manifests.store(uow, self.ctx, task, "FINAL")
                except Exception:  # noqa: BLE001 - a broken manifest must not stop orchestration
                    log.exception("final manifest for %s failed", task["key"])
        for task_id in candidates:
            with self.ctx.unit_of_work() as uow:
                task = self._task(uow, task_id)
                if S(task["state"]) in STEP_STATES and self.request_step(uow, task) is not None:
                    stats["steps"] += 1
        return stats


def _check_acyclic(graph: dict[str, list[str]]) -> None:
    seen: dict[str, int] = {}

    def visit(node: str) -> None:
        if seen.get(node) == 1:
            raise ValueError(f"the plan has a dependency cycle through {node}")
        if seen.get(node) == 2:
            return
        seen[node] = 1
        for dep in graph[node]:
            visit(dep)
        seen[node] = 2

    for node in graph:
        visit(node)


DUPLICATE_SIMILARITY = 0.8
RELATED_SIMILARITY = 0.4


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def _similarity(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0
