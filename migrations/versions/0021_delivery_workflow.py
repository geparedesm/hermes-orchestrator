"""Role-specific models and durable human-test candidates.

Revision ID: 0021
Revises: 0020
"""
from alembic import op
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE role_models (
        provider text NOT NULL CHECK (provider IN ('claude', 'codex')),
        role text NOT NULL CHECK (role IN ('ORCHESTRATOR', 'DEVELOPER', 'REVIEWER')),
        model text NOT NULL,
        updated_by text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (provider, role)
    );
    GRANT SELECT, INSERT, UPDATE, DELETE ON role_models TO ho_app;
    ALTER TABLE tasks ADD COLUMN delivery_mode text NOT NULL DEFAULT 'SUBTASKS'
        CHECK (delivery_mode IN ('SUBTASKS', 'FEATURE'));
    ALTER TABLE tasks ADD COLUMN human_test_required boolean NOT NULL DEFAULT false;
    ALTER TABLE tasks ADD COLUMN model_overrides jsonb NOT NULL DEFAULT '{}';
    CREATE TABLE feature_plans (
        task_id uuid NOT NULL REFERENCES tasks(id),
        version integer NOT NULL,
        document_artifact_id uuid NOT NULL REFERENCES artifacts(id),
        PRIMARY KEY (task_id, version)
    );
    GRANT SELECT, INSERT ON feature_plans TO ho_app;
    CREATE TABLE preview_candidates (
        id uuid PRIMARY KEY,
        environment text NOT NULL,
        version integer NOT NULL CHECK (version > 0),
        url text NOT NULL,
        state text NOT NULL CHECK (state IN ('PREPARING', 'READY', 'TESTING', 'RETIRED', 'FAILED')),
        manifest jsonb NOT NULL,
        manifest_hash text NOT NULL,
        created_by text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        ready_at timestamptz,
        expires_at timestamptz NOT NULL,
        UNIQUE (environment, version)
    );
    CREATE UNIQUE INDEX preview_one_active ON preview_candidates(environment)
        WHERE state IN ('PREPARING', 'READY', 'TESTING');
    GRANT SELECT, INSERT, UPDATE ON preview_candidates TO ho_app;
    """)


def downgrade():
    op.execute("""
    DROP TABLE preview_candidates;
    DROP TABLE feature_plans;
    ALTER TABLE tasks DROP COLUMN model_overrides;
    ALTER TABLE tasks DROP COLUMN human_test_required;
    ALTER TABLE tasks DROP COLUMN delivery_mode;
    DROP TABLE role_models;
    """)
