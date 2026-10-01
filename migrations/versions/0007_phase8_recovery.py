"""Phase 8 recovery: task checkpoints, component health, recovery runs, and outbox delivery details.

Revision ID: 0007
Revises: 0006
See DATA_MODEL.md section 5 and docs/design/phase-8.md.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE task_checkpoints (
        id          uuid PRIMARY KEY,
        task_id     uuid NOT NULL REFERENCES tasks (id),
        seq         integer NOT NULL,
        reason      text NOT NULL,
        state       text NOT NULL,
        epoch       bigint,
        snapshot    jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now(),
        UNIQUE (task_id, seq)
    );

    -- Health of the services the control plane depends on (self-healing, DEGRADED reporting).
    CREATE TABLE component_health (
        component            text PRIMARY KEY CHECK (component IN ('agent_manager', 'git_service', 'redis', 'hermes')),
        state                text NOT NULL CHECK (state IN ('HEALTHY', 'DEGRADED')),
        consecutive_failures integer NOT NULL DEFAULT 0,
        last_error           text,
        since                timestamptz NOT NULL DEFAULT now(),
        checked_at           timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE recovery_runs (
        id           uuid PRIMARY KEY,
        trigger      text NOT NULL CHECK (trigger IN ('STARTUP', 'PERIODIC', 'OPERATOR')),
        holder       text NOT NULL,
        report       jsonb NOT NULL,
        started_at   timestamptz NOT NULL,
        finished_at  timestamptz NOT NULL DEFAULT now()
    );

    ALTER TABLE notifications ADD COLUMN last_error text;

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT ON task_checkpoints, recovery_runs TO ho_app;
            GRANT SELECT, INSERT, UPDATE ON component_health TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
