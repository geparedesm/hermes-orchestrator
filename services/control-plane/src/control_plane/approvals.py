"""Approval Service (MASTER_SPEC section 25; SECURITY_MODEL.md section 6).

Approvals are action-specific and bound to a state hash computed from the
action, its subject, the effective configuration hash, and the policy version.
The component performing the action recomputes the hash immediately before
acting (`consume`); any difference invalidates the approval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from uuid import UUID

from ho_core.enums import ApprovalAction, ApprovalState, Risk
from ho_core.hashing import hash_value
from ho_core.ids import uuid7

from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import Conflict, Forbidden, NotFound
from .events import record_event


class DecisionHandler(Protocol):
    def __call__(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None: ...


def state_hash(action: str, subject: dict[str, Any], config_hash: str, policy_version: str) -> str:
    return hash_value({"action": action, "subject": subject, "config_hash": config_hash, "policy_version": policy_version})


class Renewer(Protocol):
    def __call__(self, uow: UnitOfWork, expired: Row, principal: Principal) -> Row: ...


_TERMINAL_TASK_STATES = ("DONE", "CANCELLED", "FAILED")
# Renewed by rebuilding them from the current state, so even one decided before can be asked again safely.
_REBUILT_FROM_STATE = (ApprovalAction.MERGE.value,)


class Approvals:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._handlers: dict[str, list[DecisionHandler]] = {}
        self._renewers: dict[str, Renewer] = {}

    def register_handler(self, action: ApprovalAction, handler: Callable[..., None]) -> None:
        """Handlers run in order on every decision of the action; each ignores subjects it does not own."""
        self._handlers.setdefault(action.value, []).append(handler)

    def register_renewer(self, action: ApprovalAction, renewer: Callable[..., Row]) -> None:
        """How an expired approval of `action` is requested again: the owner rebuilds it from the current state
        (and refuses with Conflict when it no longer applies). Actions without one are requested again as they were."""
        self._renewers[action.value] = renewer

    # ------------------------------------------------------------------ queries

    def get(self, uow: UnitOfWork, approval_id: UUID | str, *, lock: bool = False) -> Row:
        uow.cur.execute(f"SELECT * FROM approvals WHERE id = %s{' FOR UPDATE' if lock else ''}", (str(approval_id),))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound(f"approval {approval_id} not found")
        return row

    def list(self, uow: UnitOfWork, *, state: str | None = None, limit: int = 100) -> list[Row]:
        if state == ApprovalState.EXPIRED:
            # Only the ones that can still be requested again, newest first: no newer approval of the same action for
            # the same project and task, and the task (if any) has not ended. Renewed and outdated ones drop out.
            uow.cur.execute(
                "SELECT a.* FROM approvals a LEFT JOIN tasks t ON t.id = a.task_id WHERE a.state = 'EXPIRED' "
                "AND (a.decided_at IS NULL OR a.action = ANY(%s)) AND (t.id IS NULL OR t.state <> ALL(%s)) AND NOT EXISTS (SELECT 1 FROM approvals n WHERE n.action = a.action "
                "AND n.project_id IS NOT DISTINCT FROM a.project_id AND n.task_id IS NOT DISTINCT FROM a.task_id "
                "AND n.requested_at > a.requested_at) ORDER BY a.requested_at DESC LIMIT %s",
                (list(_REBUILT_FROM_STATE), list(_TERMINAL_TASK_STATES), limit))
            return uow.cur.fetchall()
        if state:
            uow.cur.execute("SELECT * FROM approvals WHERE state = %s ORDER BY requested_at LIMIT %s", (state, limit))
        else:
            uow.cur.execute("SELECT * FROM approvals ORDER BY requested_at DESC LIMIT %s", (limit,))
        return uow.cur.fetchall()

    # ----------------------------------------------------------------- commands

    def request(
        self,
        uow: UnitOfWork,
        *,
        action: ApprovalAction,
        project_id: UUID | None,
        subject: dict[str, Any],
        config_hash: str,
        summary: str,
        requested_by: str,
        task_id: UUID | None = None,
        risk: Risk = Risk.MEDIUM,
        from_task_state: str | None = None,
    ) -> Row:
        ttl = self.ctx.platform["platform"]["approval_ttl_hours"]
        hours = int(ttl.get(action.value, ttl["default"]))
        now = datetime.now(timezone.utc)
        digest = state_hash(action.value, subject, config_hash, self.ctx.policy_version)
        approval_id = uuid7()
        uow.cur.execute(
            """
            INSERT INTO approvals (id, action, project_id, task_id, risk, subject, config_hash, policy_version,
                                   state_hash, from_task_state, state, summary, requested_by, requested_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, %s)
            RETURNING *
            """,
            (approval_id, action.value, project_id, task_id, risk.value, jsonb(subject), config_hash,
             self.ctx.policy_version, digest, from_task_state, summary, requested_by, now, now + timedelta(hours=hours)),
        )
        row = uow.cur.fetchone()
        assert row is not None
        record_event(
            uow.cur, "APPROVAL_REQUIRED", actor="control-plane", project_id=project_id, task_id=task_id,
            summary=f"{action.value}: {summary}",
            data={"approval_id": str(approval_id), "action": action.value, "risk": risk.value, "expires_at": row["expires_at"].isoformat()},
            pending=uow.events,
        )
        return row

    def decide(self, uow: UnitOfWork, approval_id: UUID | str, *, principal: Principal, approve: bool, note: str | None) -> Row:
        if principal.value not in self.ctx.platform["platform"]["approvers"]:
            raise Forbidden(f"{principal.value} is not an allowed approver")
        row = self.get(uow, approval_id, lock=True)
        if row["state"] != ApprovalState.PENDING:
            raise Conflict(f"approval is {row['state']}, not PENDING")
        now = datetime.now(timezone.utc)
        if row["expires_at"] <= now:
            return self._set_terminal(uow, row, ApprovalState.EXPIRED, "expired before decision")
        state = ApprovalState.APPROVED if approve else ApprovalState.REJECTED
        uow.cur.execute(
            """
            UPDATE approvals SET state = %s, decided_by = %s, decided_at = %s, decision_note = %s, version = version + 1
            WHERE id = %s RETURNING *
            """,
            (state.value, principal.value, now, note, row["id"]),
        )
        decided = uow.cur.fetchone()
        assert decided is not None
        record_event(
            uow.cur, "APPROVAL_DECIDED", actor=principal.value, project_id=row["project_id"], task_id=row["task_id"],
            summary=f"{row['action']} {state.value.lower()} by {principal.value}",
            data={"approval_id": str(row["id"]), "decision": state.value, "note": note}, pending=uow.events,
        )
        for handler in self._handlers.get(row["action"], []):
            handler(uow, decided, approve, principal)
        return self.get(uow, row["id"])

    def renew(self, uow: UnitOfWork, approval_id: UUID | str, *, principal: Principal) -> Row:
        """Request an expired approval again (a person's action, never an agent's). The expired one stays EXPIRED for
        the record and the new one points to it. Still bound the same way: whoever acts on it checks the current state
        at use time, so a renewed approval for something that changed is invalidated, never applied."""
        if principal.value not in self.ctx.platform["platform"]["approvers"]:
            raise Forbidden(f"{principal.value} is not an allowed approver")
        row = self.get(uow, approval_id, lock=True)
        if row["state"] != ApprovalState.EXPIRED:
            raise Conflict(f"only expired approvals can be requested again; this one is {row['state']}")
        if row["decided_at"] is not None and row["action"] not in _REBUILT_FROM_STATE:
            # Approved, possibly already applied (a budget raise), and expired unused: asking again could apply it twice.
            raise Conflict(f"this approval was already decided by {row['decided_by']}: there is nothing to ask again")
        uow.cur.execute("SELECT id, state FROM approvals WHERE action = %s AND project_id IS NOT DISTINCT FROM %s "
                        "AND task_id IS NOT DISTINCT FROM %s AND requested_at > %s ORDER BY requested_at DESC LIMIT 1",
                        (row["action"], row["project_id"], row["task_id"], row["requested_at"]))
        newer = uow.cur.fetchone()
        if newer is not None:
            raise Conflict(f"a newer {row['action']} approval exists ({newer['state']}): use that one")
        if row["task_id"] is not None:
            uow.cur.execute("SELECT key, state FROM tasks WHERE id = %s", (row["task_id"],))
            task = uow.cur.fetchone()
            if task is not None and task["state"] in _TERMINAL_TASK_STATES:
                raise Conflict(f"{task['key']} is {task['state']}: there is nothing left to approve")
        renewer = self._renewers.get(row["action"])
        if renewer is not None:
            renewed = renewer(uow, row, principal)
        else:
            renewed = self.request(uow, action=ApprovalAction(row["action"]), project_id=row["project_id"], subject=row["subject"],
                                   config_hash=row["config_hash"], summary=row["summary"], requested_by=principal.value,
                                   task_id=row["task_id"], risk=Risk(row["risk"]), from_task_state=row["from_task_state"])
        uow.cur.execute("UPDATE approvals SET renewed_from = %s WHERE id = %s RETURNING *", (row["id"], renewed["id"]))
        renewed = uow.cur.fetchone()
        assert renewed is not None
        record_event(uow.cur, "APPROVAL_RENEWED", actor=principal.value, project_id=row["project_id"], task_id=row["task_id"],
                     summary=f"{row['action']} approval requested again", pending=uow.events,
                     data={"approval_id": str(renewed["id"]), "renewed_from": str(row["id"])})
        return renewed

    def consume(self, uow: UnitOfWork, approval: Row, *, subject: dict[str, Any], config_hash: str) -> bool:
        """Validate an APPROVED approval against the current state and mark it CONSUMED.

        Returns False (and invalidates it) when anything bound to it changed.
        """
        row = self.get(uow, approval["id"], lock=True)
        now = datetime.now(timezone.utc)
        if row["state"] != ApprovalState.APPROVED:
            return False
        if row["expires_at"] <= now:
            self._set_terminal(uow, row, ApprovalState.EXPIRED, "expired before use")
            return False
        current = state_hash(row["action"], subject, config_hash, self.ctx.policy_version)
        if current != row["state_hash"]:
            self._set_terminal(uow, row, ApprovalState.INVALIDATED, "approved state changed before use")
            return False
        uow.cur.execute(
            "UPDATE approvals SET state = 'CONSUMED', consumed_at = %s, version = version + 1 WHERE id = %s",
            (now, row["id"]),
        )
        record_event(
            uow.cur, "APPROVAL_CONSUMED", actor="control-plane", project_id=row["project_id"], task_id=row["task_id"],
            summary=f"{row['action']} approval used", data={"approval_id": str(row["id"])}, pending=uow.events,
        )
        return True

    def mark_applied(self, uow: UnitOfWork, approval: Row) -> None:
        """For approvals applied when they are decided (an assumption confirmed, a budget raised): record them as used,
        so they never later read as expired or get asked again."""
        uow.cur.execute("UPDATE approvals SET state = 'CONSUMED', consumed_at = now(), version = version + 1 "
                        "WHERE id = %s AND state = 'APPROVED' RETURNING id", (approval["id"],))
        if uow.cur.fetchone() is not None:
            record_event(uow.cur, "APPROVAL_CONSUMED", actor="control-plane", project_id=approval["project_id"],
                         task_id=approval["task_id"], summary=f"{approval['action']} approval applied",
                         data={"approval_id": str(approval["id"])}, pending=uow.events)

    def invalidate_open(self, uow: UnitOfWork, *, project_id: UUID, task_id: UUID | None, action: ApprovalAction | None, reason: str) -> int:
        query = "SELECT * FROM approvals WHERE project_id = %s AND task_id IS NOT DISTINCT FROM %s AND state IN ('PENDING', 'APPROVED')"
        params: list[Any] = [project_id, task_id]
        if action:
            query += " AND action = %s"
            params.append(action.value)
        uow.cur.execute(query + " FOR UPDATE", params)
        rows = uow.cur.fetchall()
        for row in rows:
            self._set_terminal(uow, row, ApprovalState.INVALIDATED, reason)
        return len(rows)

    def expire_due(self, uow: UnitOfWork) -> int:
        uow.cur.execute(
            "SELECT * FROM approvals WHERE state IN ('PENDING', 'APPROVED') AND expires_at <= now() FOR UPDATE SKIP LOCKED"
        )
        rows = uow.cur.fetchall()
        for row in rows:
            self._set_terminal(uow, row, ApprovalState.EXPIRED, "approval window elapsed")
        return len(rows)

    def _set_terminal(self, uow: UnitOfWork, row: Row, state: ApprovalState, reason: str) -> Row:
        uow.cur.execute(
            "UPDATE approvals SET state = %s, invalidated_reason = %s, version = version + 1 WHERE id = %s RETURNING *",
            (state.value, reason, row["id"]),
        )
        updated = uow.cur.fetchone()
        assert updated is not None
        event = "APPROVAL_EXPIRED" if state == ApprovalState.EXPIRED else "APPROVAL_INVALIDATED"
        record_event(
            uow.cur, event, actor="control-plane", project_id=row["project_id"], task_id=row["task_id"],
            summary=f"{row['action']} approval {state.value.lower()}: {reason}",
            data={"approval_id": str(row["id"]), "reason": reason}, pending=uow.events,
        )
        return updated
