"""Waiting for the merge decision (READY_FOR_MERGE) uses no runtime budget either.

Revision ID: 0019
Revises: 0018
Unfinished tasks get the time they already spent in READY_FOR_MERGE added to their waited seconds, from their
state-change history, as 0013 did for the waiting states.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    WITH changes AS (
        SELECT e.task_id, e.occurred_at, e.data->>'to' AS to_state, t.started_at,
               lead(e.occurred_at) OVER (PARTITION BY e.task_id ORDER BY e.seq) AS next_at
        FROM events e JOIN tasks t ON t.id = e.task_id
        WHERE e.type = 'TASK_STATE_CHANGED' AND t.started_at IS NOT NULL AND t.completed_at IS NULL
    ), waits AS (
        SELECT task_id,
               COALESCE(sum(greatest(0, extract(epoch FROM next_at - greatest(occurred_at, started_at))))
                        FILTER (WHERE next_at IS NOT NULL), 0) AS waited,
               max(occurred_at) FILTER (WHERE next_at IS NULL) AS open_since
        FROM changes WHERE to_state = 'READY_FOR_MERGE' GROUP BY task_id
    )
    UPDATE tasks t SET waited_seconds = t.waited_seconds + w.waited::bigint,
                       waiting_since = CASE WHEN t.state = 'READY_FOR_MERGE' THEN greatest(w.open_since, t.started_at)
                                            ELSE t.waiting_since END
    FROM waits w WHERE w.task_id = t.id;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
