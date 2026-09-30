"""Phase 4 Claude/Codex workers: credential references, usage, and adapter results.

Revision ID: 0003
Revises: 0002
See DATA_MODEL.md sections 3.3 and 3.9. Credential references hold status only:
tokens and credential files never reach PostgreSQL (SECURITY_MODEL.md section 7.1).
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE credential_refs (
        provider          text NOT NULL CHECK (provider IN ('claude', 'codex')),
        identity          text NOT NULL CHECK (identity ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
        status            text NOT NULL CHECK (status IN ('UNKNOWN', 'READY', 'AUTH_REQUIRED')),
        last_verified_at  timestamptz,
        last_error        text,
        updated_at        timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (provider, identity)
    );

    ALTER TABLE executions
        ADD COLUMN agent_run           boolean NOT NULL DEFAULT false,
        ADD COLUMN resume_of           uuid REFERENCES executions (id),
        ADD COLUMN provider_session_id text,
        ADD COLUMN result              jsonb;
    CREATE UNIQUE INDEX executions_one_resume ON executions (resume_of) WHERE resume_of IS NOT NULL;

    CREATE TABLE usage_records (
        id               uuid PRIMARY KEY,
        execution_id     uuid NOT NULL UNIQUE REFERENCES executions (id),
        task_id          uuid NOT NULL REFERENCES tasks (id),
        project_id       uuid NOT NULL REFERENCES projects (id),
        provider         text NOT NULL CHECK (provider IN ('claude', 'codex')),
        units            jsonb NOT NULL,
        wall_seconds     integer,
        created_at       timestamptz NOT NULL DEFAULT now()
    );

    -- Which provider identity an AUTH_REQUIRED task waits for; cleared when it resumes.
    ALTER TABLE tasks ADD COLUMN waiting_on_credential text;
    -- Set when the task's networks and session volumes were removed after it ended.
    ALTER TABLE tasks ADD COLUMN environment_released_at timestamptz;

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON credential_refs TO ho_app;
            GRANT SELECT, INSERT ON usage_records TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
