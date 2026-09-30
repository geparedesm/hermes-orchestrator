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


class Approvals:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._handlers: dict[str, DecisionHandler] = {}

    def register_handler(self, action: ApprovalAction, handler: Callable[..., None]) -> None:
        self._handlers[action.value] = handler

    # ------------------------------------------------------------------ queries

    def get(self, uow: UnitOfWork, approval_id: UUID | str, *, lock: bool = False) -> Row:
        uow.cur.execute(f"SELECT * FROM approvals WHERE id = %s{' FOR UPDATE' if lock else ''}", (str(approval_id),))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound(f"approval {approval_id} not found")
        return row

    def list(self, uow: UnitOfWork, *, state: str | None = None, limit: int = 100) -> list[Row]:
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
        project_id: UUID,
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
        handler = self._handlers.get(row["action"])
        if handler:
            handler(uow, decided, approve, principal)
        return self.get(uow, row["id"])

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
