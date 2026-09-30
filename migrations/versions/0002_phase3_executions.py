"""Phase 3 Agent Manager: executions and capability grants.

Revision ID: 0002
Revises: 0001
See DATA_MODEL.md section 3.3. Subtask references are added with the DAG in Phase 7.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE executions (
        id                  uuid PRIMARY KEY,
        task_id             uuid NOT NULL REFERENCES tasks (id),
        project_id          uuid NOT NULL REFERENCES projects (id),
        role                text NOT NULL CHECK (role IN ('ORCHESTRATOR', 'DEVELOPER', 'REVIEWER', 'TESTER', 'BROWSER')),
        provider            text NOT NULL CHECK (provider IN ('claude', 'codex', 'none')),
        provider_identity   text,
        image               text NOT NULL,
        command             jsonb NOT NULL,
        workspace           text,
        resource_profile    text NOT NULL CHECK (resource_profile IN ('LIGHT', 'NORMAL', 'HEAVY')),
        state               text NOT NULL CHECK (state IN ('REQUESTED', 'STARTING', 'RUNNING', 'STOPPING',
                                                           'SUCCEEDED', 'FAILED', 'CANCELLED', 'LOST')),
        lease_epoch         bigint NOT NULL DEFAULT 1,
        spec                jsonb NOT NULL,
        exit_code           integer,
        failure_class       text CHECK (failure_class IN ('TRANSIENT', 'AUTH', 'QUOTA', 'TASK', 'CAPACITY',
                                                          'POLICY', 'TIMEOUT', 'LOST', 'CANCELLED', 'UNKNOWN')),
        failure_reason      text,
        result_artifact_ids uuid[] NOT NULL DEFAULT '{}',
        requested_by        text NOT NULL,
        dispatch_attempts   integer NOT NULL DEFAULT 0,
        created_at          timestamptz NOT NULL DEFAULT now(),
        started_at          timestamptz,
        ended_at            timestamptz,
        updated_at          timestamptz NOT NULL DEFAULT now(),
        version             integer NOT NULL DEFAULT 1
    );
    CREATE INDEX executions_task ON executions (task_id, created_at);
    CREATE INDEX executions_active ON executions (state)
        WHERE state IN ('REQUESTED', 'STARTING', 'RUNNING', 'STOPPING');

    CREATE TABLE capability_grants (
        id             uuid PRIMARY KEY,
        grant_key      text NOT NULL UNIQUE CHECK (grant_key ~ '^G-[0-9A-Za-z-]{6,64}$'),
        execution_id   uuid NOT NULL UNIQUE REFERENCES executions (id),
        project_id     uuid NOT NULL REFERENCES projects (id),
        task_id        uuid NOT NULL REFERENCES tasks (id),
        grant_doc      jsonb NOT NULL,
        requested      jsonb NOT NULL,
        reductions     jsonb NOT NULL DEFAULT '[]',
        issued_at      timestamptz NOT NULL,
        expires_at     timestamptz NOT NULL,
        revoked_at     timestamptz,
        revoked_reason text
    );

    ALTER TABLE policy_decisions ADD COLUMN execution_id uuid REFERENCES executions (id);

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON executions, capability_grants TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
