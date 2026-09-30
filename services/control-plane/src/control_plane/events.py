"""Event log and notification outbox (DATA_MODEL.md sections 3.8 and 7).

Events are written in the same transaction as the state change they describe.
Attention events also get an outbox row for immediate delivery through Hermes
(Phase 9); routine events are aggregated later.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ho_core.ids import uuid7
from psycopg import Cursor

from .db import Row, jsonb

ATTENTION_EVENTS = frozenset(
    {
        "APPROVAL_REQUIRED",
        "AUTH_REQUIRED",
        "BLOCKED",
        "PAUSED_BUDGET",
        "TEST_FAILED",
        "READY_FOR_MERGE",
        "RECOVERY_FAILED",
        "TASK_COMPLETED",
        "DEGRADED",
    }
)
AUDIT_EVENTS = frozenset(
    {
        "PROJECT_REGISTERED",
        "PROJECT_UNREGISTERED",
        "PROJECT_READY",
        "APPROVAL_REQUIRED",
        "APPROVAL_DECIDED",
        "APPROVAL_INVALIDATED",
        "APPROVAL_EXPIRED",
        "APPROVAL_CONSUMED",
        "POLICY_DECISION",
        "TASK_CANCELLED",
        "GRANT_ISSUED",
        "GRANT_REVOKED",
        "WORKER_CREATED",
        "WORKER_STOPPED",
    }
)


def record_event(
    cur: Cursor[Row],
    event_type: str,
    *,
    actor: str,
    summary: str,
    project_id: UUID | None = None,
    task_id: UUID | None = None,
    data: dict[str, Any] | None = None,
    pending: list[dict[str, Any]] | None = None,
) -> int:
    """Insert an event (and an outbox row for routine or attention delivery).

    `pending` collects events to publish on Redis after commit.
    """
    audit = event_type in AUDIT_EVENTS
    cur.execute(
        """
        INSERT INTO events (project_id, task_id, type, actor, summary, data, audit)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING seq, occurred_at
        """,
        (project_id, task_id, event_type, actor, summary, jsonb(data or {}), audit),
    )
    row = cur.fetchone()
    assert row is not None
    payload = {
        "seq": row["seq"],
        "type": event_type,
        "summary": summary,
        "project_id": str(project_id) if project_id else None,
        "task_id": str(task_id) if task_id else None,
        "occurred_at": row["occurred_at"].isoformat(),
    }
    priority = "ATTENTION" if event_type in ATTENTION_EVENTS else "ROUTINE"
    cur.execute(
        "INSERT INTO notifications (id, event_seq, priority, payload, state) VALUES (%s, %s, %s, %s, 'PENDING')",
        (uuid7(), row["seq"], priority, jsonb(payload)),
    )
    if pending is not None:
        pending.append(payload)
    return int(row["seq"])
