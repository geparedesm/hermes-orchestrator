"""Phase 5 Git isolation: workspaces, per-task Git state, and merge records.

Revision ID: 0004
Revises: 0003
See DATA_MODEL.md sections 3.2, 3.4 and 3.7 (`workspaces`, `git_changes`).
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE tasks
        ADD COLUMN base_commit   text CHECK (base_commit ~ '^[0-9a-f]{40}$'),
        ADD COLUMN target_branch text;

    CREATE TABLE workspaces (
        id            uuid PRIMARY KEY,
        project_id    uuid NOT NULL REFERENCES projects (id),
        task_id       uuid NOT NULL REFERENCES tasks (id),
        name          text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
        kind          text NOT NULL CHECK (kind IN ('DEVELOPMENT', 'CONFLICT', 'VERIFICATION')),
        path          text NOT NULL,
        branch        text NOT NULL,
        base_sha      text NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
        head_sha      text,
        status        text NOT NULL CHECK (status IN ('ACTIVE', 'RETAINED', 'REMOVED')),
        created_by    text NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        collected_at  timestamptz,
        retain_until  timestamptz,
        removed_at    timestamptz,
        UNIQUE (project_id, name)
    );
    CREATE INDEX workspaces_task ON workspaces (task_id, status);

    CREATE TABLE git_changes (
        task_id                   uuid PRIMARY KEY REFERENCES tasks (id),
        project_id                uuid NOT NULL REFERENCES projects (id),
        target_branch             text NOT NULL,
        base_sha                  text NOT NULL,
        integration_ref           text,
        integration_sha           text,
        integration_target_sha    text,
        integration_conflicts     jsonb,
        retest_execution_id       uuid REFERENCES executions (id),
        retest_status             text CHECK (retest_status IN ('RUNNING', 'PASSED', 'FAILED', 'NOT_CONFIGURED')),
        divergence                jsonb,
        divergence_level          text CHECK (divergence_level IN ('NONE', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        divergence_target_sha     text,
        reconcile_required        boolean NOT NULL DEFAULT false,
        remote_branch             text,
        pushed_sha                text,
        pr_number                 integer,
        pr_url                    text,
        ci_status                 jsonb,
        merge_approval_id         uuid REFERENCES approvals (id),
        merge_commit_sha          text,
        merged_at                 timestamptz,
        merged_by_approval_id     uuid REFERENCES approvals (id),
        verification_execution_id uuid REFERENCES executions (id),
        post_merge_status         text CHECK (post_merge_status IN ('RUNNING', 'PASSED', 'FAILED', 'NOT_CONFIGURED')),
        updated_at                timestamptz NOT NULL DEFAULT now()
    );

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON workspaces, git_changes TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
