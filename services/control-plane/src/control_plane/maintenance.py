"""Daily maintenance (MASTER_SPEC sections 71 and 80; docs/operations.md).

Run by the scheduler leader once a day (and on demand with `ho maintenance run`):

- dependency caches trimmed to their size limit, least recently used first (Agent Manager);
- execution outputs and logs of finished tasks removed after the project's `retention.artifacts_days`,
  of failed or blocked tasks after `platform.retention.failed_artifacts_days`; their metadata and digests
  stay, and requests, requirements, plans, onboarding reports, and manifests are kept;
- workspaces retained for cancelled or failed tasks removed after the project's `failed_workspace_days`;
- delivered notifications and recovery reports pruned.
Task history in PostgreSQL is kept.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ho_core.ids import uuid7

from .agentmgr import AgentManagerError
from .context import Context
from .db import jsonb
from .errors import ApiError
from .events import record_event

log = logging.getLogger(__name__)
INTERVAL = timedelta(hours=24)
PURGEABLE = "executions/%"
FINISHED = ("DONE", "CANCELLED")
UNSUCCESSFUL = ("FAILED", "BLOCKED")


class Maintenance:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def _retention(self) -> dict[str, int]:
        return {"failed_artifacts_days": 90, "notifications_days": 30, "recovery_runs_days": 30,
                **(self.ctx.platform["platform"].get("retention") or {})}

    def due(self) -> bool:
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT max(finished_at) AS last FROM recovery_runs WHERE trigger = 'MAINTENANCE'")
            last = uow.cur.fetchone()["last"]  # type: ignore[index]
        return last is None or last < datetime.now(timezone.utc) - INTERVAL

    def tick(self) -> None:
        if self.due():
            self.run()

    def run(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        report: dict[str, Any] = {}
        for name, step in (("caches", self._caches), ("artifacts", self._artifacts), ("workspaces", self._workspaces),
                           ("history", self._history)):
            try:
                report[name] = step()
            except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
                log.exception("maintenance step %s failed", name)
                report[name] = {"error": str(exc)[:300]}
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("INSERT INTO recovery_runs (id, trigger, holder, report, started_at) VALUES (%s, 'MAINTENANCE', %s, %s, %s)",
                            (uuid7(), self.ctx.instance_id, jsonb(report), started))
            record_event(uow.cur, "MAINTENANCE_COMPLETED", actor="control-plane",
                         summary="daily maintenance: " + ", ".join(f"{k} {v}" for k, v in _counts(report).items()),
                         data=report, pending=uow.events)
        return report

    def _caches(self) -> dict[str, Any]:
        if self.ctx.agents is None:
            return {"trimmed": 0}
        try:
            return {"trimmed": len(self.ctx.agents.maintain_caches().get("trimmed", []))}
        except AgentManagerError as exc:
            return {"error": str(exc)[:300]}

    def _artifacts(self) -> dict[str, int]:
        """Purge execution outputs by task outcome and the project's retention (its pinned configuration)."""
        days = self._retention()
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                """
                SELECT a.id, a.path FROM artifacts a JOIN tasks t ON t.id = a.task_id
                JOIN project_configs c ON c.id = t.config_id
                WHERE a.purged_at IS NULL AND a.kind LIKE %s AND (
                    (t.state = ANY(%s) AND t.completed_at < now() - make_interval(days =>
                        COALESCE((c.effective_config->'retention'->>'artifacts_days')::int, 30)))
                    OR (t.state = ANY(%s) AND t.updated_at < now() - make_interval(days => %s)))
                LIMIT 5000
                """, (PURGEABLE, list(FINISHED), list(UNSUCCESSFUL), int(days["failed_artifacts_days"])))
            rows = uow.cur.fetchall()
            for row in rows:
                self.ctx.artifacts.delete(row["path"])
                uow.cur.execute("UPDATE artifacts SET purged_at = now() WHERE id = %s", (row["id"],))
        return {"purged": len(rows)}

    def _workspaces(self) -> dict[str, int]:
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                """
                SELECT w.id, w.name, p.relative_path FROM workspaces w JOIN tasks t ON t.id = w.task_id
                JOIN projects p ON p.id = w.project_id JOIN project_configs c ON c.id = t.config_id
                WHERE w.status = 'RETAINED' AND t.state IN ('CANCELLED', 'FAILED', 'DONE') AND t.updated_at < now() - make_interval(
                    days => COALESCE((c.effective_config->'retention'->>'failed_workspace_days')::int, 14))
                """)
            rows = uow.cur.fetchall()
        removed = 0
        for row in rows:
            try:
                self.ctx.git.remove_workspace(row["relative_path"], row["name"])
            except ApiError as exc:
                log.warning("could not remove retained workspace %s: %s", row["name"], exc)
                continue
            with self.ctx.unit_of_work() as uow:
                uow.cur.execute("UPDATE workspaces SET status = 'REMOVED', removed_at = now() WHERE id = %s", (row["id"],))
            removed += 1
        return {"removed": removed}

    def _history(self) -> dict[str, int]:
        days = self._retention()
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("DELETE FROM notifications WHERE state = 'SENT' AND delivered_at < now() - make_interval(days => %s)",
                            (int(days["notifications_days"]),))
            notifications = uow.cur.rowcount
            uow.cur.execute("DELETE FROM recovery_runs WHERE trigger <> 'MAINTENANCE' AND finished_at < now() - "
                            "make_interval(days => %s)", (int(days["recovery_runs_days"]),))
            runs = uow.cur.rowcount
        return {"notifications": notifications, "recovery_runs": runs}


def _counts(report: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, value in report.items():
        if isinstance(value, dict):
            out[name] = "failed" if "error" in value else sum(v for v in value.values() if isinstance(v, int))
    return out
