"""The runtime budget counts working time, not time spent waiting for people.

Revision ID: 0013
Revises: 0012
A task accumulates the seconds it spends in a waiting state (APPROVAL_REQUIRED, AUTH_REQUIRED, PAUSED,
PAUSED_BUDGET, BLOCKED); runtime is the time since it started minus that. Existing tasks are backfilled from their
state-change history.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

WAITING = "('APPROVAL_REQUIRED', 'AUTH_REQUIRED', 'PAUSED', 'PAUSED_BUDGET', 'BLOCKED')"


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE tasks ADD COLUMN waited_seconds bigint NOT NULL DEFAULT 0 CHECK (waited_seconds >= 0);
    ALTER TABLE tasks ADD COLUMN waiting_since timestamptz;

    WITH changes AS (
        SELECT e.task_id, e.occurred_at, e.data->>'to' AS to_state, t.started_at,
               lead(e.occurred_at) OVER (PARTITION BY e.task_id ORDER BY e.seq) AS next_at
        FROM events e JOIN tasks t ON t.id = e.task_id
        WHERE e.type = 'TASK_STATE_CHANGED' AND t.started_at IS NOT NULL
    ), waits AS (
        -- Only the part of each wait after the task started (runtime is measured from started_at).
        SELECT task_id,
               COALESCE(sum(greatest(0, extract(epoch FROM next_at - greatest(occurred_at, started_at))))
                        FILTER (WHERE next_at IS NOT NULL), 0) AS waited,
               max(occurred_at) FILTER (WHERE next_at IS NULL) AS open_since
        FROM changes WHERE to_state IN {WAITING} GROUP BY task_id
    )
    UPDATE tasks t SET waited_seconds = w.waited::bigint,
                       waiting_since = CASE WHEN t.state IN {WAITING} THEN w.open_since END
    FROM waits w WHERE w.task_id = t.id;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
