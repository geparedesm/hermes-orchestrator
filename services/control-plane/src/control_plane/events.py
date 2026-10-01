"""Event log and notification outbox (DATA_MODEL.md sections 3.8 and 7).

Events are written in the same transaction as the state change they describe.
Attention events get an outbox row delivered through Hermes immediately; curated routine events
are aggregated into a digest; other events are audit only (no notification).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ho_core.ids import uuid7
from psycopg import Cursor

from .db import Row, jsonb

# Delivered through Hermes immediately, one message each (MASTER_SPEC section 74; PHASES.md Phase 9).
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
        "TASK_FAILED",
        "PLATFORM_DEGRADED",
    }
)
# Aggregated into a periodic digest. Everything else (grants, workers, routing, steps) is audit only.
ROUTINE_EVENTS = frozenset(
    {
        "TASK_CREATED",
        "TASK_CANCELLED",
        "PROJECT_READY",
        "PLAN_VERSIONED",
        "SUBTASK_ACCEPTED",
        "INTEGRATION_COMPLETED",
        "TEST_PASSED",
        "REVIEW_PASSED",
        "REVIEW_FAILED",
        "PR_CREATED",
        "MERGE_COMPLETED",
        "BUDGET_THRESHOLD",
        "FAILOVER_COMPLETED",
        "ORCHESTRATOR_FAILBACK",
        "PLATFORM_RECOVERED",
        "REQUIREMENTS_REVISED",
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
    if event_type in ATTENTION_EVENTS or event_type in ROUTINE_EVENTS:
        priority = "ATTENTION" if event_type in ATTENTION_EVENTS else "ROUTINE"
        cur.execute(
            "INSERT INTO notifications (id, event_seq, priority, payload, state) VALUES (%s, %s, %s, %s, 'PENDING')",
            (uuid7(), row["seq"], priority, jsonb({**payload, "data": data or {}})),
        )
    if pending is not None:
        pending.append(payload)
    return int(row["seq"])
