"""Basic scheduler (MASTER_SPEC sections 29, 31, 61, 63).

Each pass:
1. expires approvals whose window elapsed;
2. promotes BACKLOG tasks to READY when their project is PROJECT_READY and all
   DEPENDENCY tasks are DONE;
3. offers READY tasks to the dispatcher in priority order with aging, so
   low-priority work cannot starve.

PostgreSQL is the authority: a session advisory lock makes one scheduler the
leader, and Redis only shortens the wait between passes.

Phase 2 has no workers. The default dispatcher declines every task, so READY
tasks wait in the queue. Phase 3/4 replace it with one that starts
orchestrator executions through Agent Manager.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from ho_core.enums import Priority, TaskState
from ho_core.statemachine import Trigger

from .approvals import Approvals
from .context import Context, UnitOfWork
from .db import Row
from .tasks import Tasks

log = logging.getLogger(__name__)

SCHEDULER_LOCK_KEY = 0x484F5343  # "HOSC"


class Dispatcher(Protocol):
    def dispatch(self, uow: UnitOfWork, task: Row) -> bool:
        """Try to start work for a READY task. Return False when there is no capacity."""
        ...


class NoWorkersDispatcher:
    """Phase 2 placeholder: no Agent Manager exists yet, so nothing is dispatched."""

    def dispatch(self, uow: UnitOfWork, task: Row) -> bool:
        return False


@dataclass
class QueueEntry:
    key: str
    project: str
    priority: str
    effective_rank: int
    ready_at: datetime | None
    waiting_minutes: float

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "project": self.project,
            "priority": self.priority,
            "effective_rank": self.effective_rank,
            "ready_at": self.ready_at.isoformat() if self.ready_at else None,
            "waiting_minutes": round(self.waiting_minutes, 1),
        }


def effective_rank(priority: str, waiting_minutes: float, aging_minutes: int) -> int:
    """Priority rank lowered by one level per `aging_minutes` of waiting (never below CRITICAL)."""
    return max(0, Priority(priority).rank - int(waiting_minutes // aging_minutes))


class Scheduler:
    def __init__(self, ctx: Context, tasks: Tasks, approvals: Approvals, dispatcher: Dispatcher | None = None,
                 executions: Any = None) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.approvals = approvals
        self.executions = executions
        self.dispatcher = dispatcher or NoWorkersDispatcher()
        # Extra reconciliation passes run on every tick (for example Git merges and verification).
        self.hooks: list[Any] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ queue

    def queue(self, uow: UnitOfWork, *, now: datetime | None = None) -> list[QueueEntry]:
        now = now or datetime.now(timezone.utc)
        aging = int(self.ctx.platform["scheduler"]["aging_minutes"])
        uow.cur.execute(
            """
            SELECT t.key, t.priority, t.ready_at, t.created_at, p.slug FROM tasks t
            JOIN projects p ON p.id = t.project_id WHERE t.state = 'READY'
            """
        )
        entries = []
        for row in uow.cur.fetchall():
            since = row["ready_at"] or row["created_at"]
            waiting = max(0.0, (now - since).total_seconds() / 60)
            entries.append(QueueEntry(row["key"], row["slug"], row["priority"], effective_rank(row["priority"], waiting, aging),
                                      row["ready_at"], waiting))
        # Rank first, then longest wait, then creation order via key number.
        entries.sort(key=lambda e: (e.effective_rank, -e.waiting_minutes, int(e.key[2:])))
        return entries

    # ------------------------------------------------------------------ passes

    def run_once(self) -> dict[str, int]:
        stats = {"expired": 0, "promoted": 0, "dispatched": 0, "executions_finished": 0}
        if self.executions is not None:
            stats["executions_finished"] = self.executions.sync()["finished"]
        for hook in self.hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001 - one failing hook must not stop scheduling
                log.exception("scheduler hook %s failed", getattr(hook, "__qualname__", hook))
        with self.ctx.unit_of_work() as uow:
            stats["expired"] = self.approvals.expire_due(uow)
        with self.ctx.unit_of_work() as uow:
            stats["promoted"] = self._promote_backlog(uow)
        with self.ctx.unit_of_work() as uow:
            for entry in self.queue(uow):
                task = self.tasks.get(uow, entry.key, lock=True)
                if task["state"] != TaskState.READY:
                    continue
                if not self.dispatcher.dispatch(uow, task):
                    break
                stats["dispatched"] += 1
        return stats

    def _promote_backlog(self, uow: UnitOfWork) -> int:
        uow.cur.execute(
            """
            SELECT t.* FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE t.state = 'BACKLOG' AND p.status = 'PROJECT_READY'
              AND NOT EXISTS (
                  SELECT 1 FROM task_relationships r JOIN tasks d ON d.id = r.to_task_id
                  WHERE r.from_task_id = t.id AND r.kind = 'DEPENDENCY' AND d.state <> 'DONE')
            ORDER BY t.created_at
            FOR UPDATE OF t SKIP LOCKED
            """
        )
        rows = uow.cur.fetchall()
        for task in rows:
            # Pin the configuration the task will run under.
            uow.cur.execute(
                "UPDATE tasks SET config_id = (SELECT id FROM project_configs WHERE project_id = %s AND status = 'ACTIVE') "
                "WHERE id = %s RETURNING *",
                (task["project_id"], task["id"]),
            )
            pinned = uow.cur.fetchone()
            assert pinned is not None
            self.tasks.transition(uow, pinned, TaskState.READY, trigger=Trigger.SCHEDULER, actor="scheduler",
                                  reason="project ready and dependencies met")
        return len(rows)

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        poll = float(self.ctx.platform["scheduler"]["poll_seconds"])
        backoff = poll
        with self.ctx.db.connection() as lock_conn:
            lock_conn.autocommit = True
            while not self._stop.is_set():
                row = lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (SCHEDULER_LOCK_KEY,)).fetchone()
                if row and row["ok"]:
                    break
                log.info("another scheduler holds the lock; standing by")
                self._stop.wait(poll * 3)
            log.info("scheduler started", extra={"event": "SCHEDULER_STARTED"})
            while not self._stop.is_set():
                try:
                    stats = self.run_once()
                    if any(stats.values()):
                        log.info("scheduler pass %s", stats)
                    backoff = poll
                except Exception:  # noqa: BLE001 - keep the loop alive; state is in PostgreSQL
                    log.exception("scheduler pass failed")
                    backoff = min(backoff * 2, 60.0)
                    self._stop.wait(backoff)
                    continue
                started = time.monotonic()
                if not self.ctx.coordinator.wait_for_wake(poll):
                    # Timed out, Redis disabled, or Redis failing: poll PostgreSQL on schedule.
                    remaining = poll - (time.monotonic() - started)
                    if remaining > 0:
                        self._stop.wait(remaining)
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (SCHEDULER_LOCK_KEY,))

