"""Phase 6 testing: verifications, test results, cross-reviews, and Quality Gate evaluations.

Revision ID: 0005
Revises: 0004
See DATA_MODEL.md section 3.4.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE verifications (
        id             uuid PRIMARY KEY,
        task_id        uuid NOT NULL REFERENCES tasks (id),
        project_id     uuid NOT NULL REFERENCES projects (id),
        purpose        text NOT NULL CHECK (purpose IN ('INTEGRATION', 'POST_MERGE')),
        commit_sha     text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
        plan           jsonb NOT NULL,
        state          text NOT NULL CHECK (state IN ('PREPARING', 'RUNNING', 'PASSED', 'FAILED', 'ERROR')),
        workspace      text,
        environment    jsonb,
        execution_ids  uuid[] NOT NULL DEFAULT '{}',
        error          text,
        created_at     timestamptz NOT NULL DEFAULT now(),
        finished_at    timestamptz
    );
    CREATE INDEX verifications_task ON verifications (task_id, created_at);

    CREATE TABLE test_runs (
        id                 uuid PRIMARY KEY,
        task_id            uuid NOT NULL REFERENCES tasks (id),
        verification_id    uuid NOT NULL REFERENCES verifications (id),
        execution_id       uuid NOT NULL REFERENCES executions (id),
        scope              text NOT NULL CHECK (scope IN ('RELEVANT', 'FULL_SUITE', 'BROWSER', 'POST_MERGE')),
        kind               text NOT NULL,
        commit_sha         text NOT NULL,
        status             text NOT NULL CHECK (status IN ('PASSED', 'FAILED', 'ERROR', 'SKIPPED')),
        attempts           integer NOT NULL DEFAULT 1,
        duration_ms        bigint,
        definitive         boolean NOT NULL DEFAULT true,
        report_artifact_id uuid REFERENCES artifacts (id),
        log_artifact_id    uuid REFERENCES artifacts (id),
        created_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX test_runs_verification ON test_runs (verification_id);

    CREATE TABLE reviews (
        id                  uuid PRIMARY KEY,
        task_id             uuid NOT NULL REFERENCES tasks (id),
        execution_id        uuid NOT NULL UNIQUE REFERENCES executions (id),
        commit_sha          text NOT NULL,
        reviewer_provider   text NOT NULL,
        developer_providers text[] NOT NULL DEFAULT '{}',
        outcome             text NOT NULL CHECK (outcome IN ('APPROVED', 'CHANGES_REQUESTED', 'BLOCKED')),
        requirements_met    boolean NOT NULL,
        unmet_requirements  jsonb NOT NULL DEFAULT '[]',
        summary             text NOT NULL,
        created_at          timestamptz NOT NULL DEFAULT now(),
        CHECK (NOT (reviewer_provider = ANY (developer_providers)))
    );

    CREATE TABLE review_findings (
        id          uuid PRIMARY KEY,
        review_id   uuid NOT NULL REFERENCES reviews (id),
        severity    text NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        category    text NOT NULL,
        path        text,
        line        integer,
        description text NOT NULL,
        status      text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'FIXED', 'WONT_FIX_APPROVED', 'INVALID'))
    );

    CREATE TABLE quality_gate_evaluations (
        id              uuid PRIMARY KEY,
        task_id         uuid NOT NULL REFERENCES tasks (id),
        commit_sha      text NOT NULL,
        config_hash     text NOT NULL,
        policy_version  text NOT NULL,
        verification_id uuid REFERENCES verifications (id),
        review_id       uuid REFERENCES reviews (id),
        risk            text NOT NULL CHECK (risk IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        requirements    jsonb NOT NULL,
        test_gaps       jsonb NOT NULL DEFAULT '[]',
        residual_risk   text NOT NULL,
        outcome         text NOT NULL CHECK (outcome IN ('PASS', 'FAIL')),
        evaluated_at    timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX quality_gate_task ON quality_gate_evaluations (task_id, evaluated_at);

    ALTER TABLE git_changes ADD COLUMN quality_gate_id uuid REFERENCES quality_gate_evaluations (id);

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON verifications, test_runs, reviews, review_findings, quality_gate_evaluations TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
