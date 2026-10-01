"""Phase 11 operations: artifact retention, maintenance runs, and approval-controlled platform updates.

Revision ID: 0008
Revises: 0007
See docs/operations.md.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Retention removes an artifact's file but keeps its metadata and digest (audit trail, manifests).
    ALTER TABLE artifacts ADD COLUMN purged_at timestamptz;

    ALTER TABLE recovery_runs DROP CONSTRAINT IF EXISTS recovery_runs_trigger_check;
    ALTER TABLE recovery_runs ADD CONSTRAINT recovery_runs_trigger_check
        CHECK (trigger IN ('STARTUP', 'PERIODIC', 'OPERATOR', 'MAINTENANCE'));

    -- Platform updates are approved like everything else but belong to no project.
    ALTER TABLE approvals ALTER COLUMN project_id DROP NOT NULL;
    ALTER TABLE approvals ADD CONSTRAINT approvals_project_scope CHECK (project_id IS NOT NULL OR action = 'UPDATE');

    -- One row per platform update, bound to the UPDATE approval that authorized it.
    CREATE TABLE platform_updates (
        id            uuid PRIMARY KEY,
        approval_id   uuid NOT NULL UNIQUE REFERENCES approvals (id),
        from_version  text NOT NULL,
        to_version    text NOT NULL,
        state         text NOT NULL CHECK (state IN ('STARTED', 'SUCCEEDED', 'ROLLED_BACK', 'FAILED')),
        backup        text,
        report        jsonb NOT NULL DEFAULT '{}',
        started_at    timestamptz NOT NULL DEFAULT now(),
        finished_at   timestamptz
    );

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON platform_updates TO ho_app;
            GRANT DELETE ON notifications, recovery_runs TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
