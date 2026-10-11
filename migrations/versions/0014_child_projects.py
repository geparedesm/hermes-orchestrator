"""Child projects created from a parent's template (docs/design/child-projects.md).

Revision ID: 0014
Revises: 0013

A parent project (odoo-base) creates child projects (Odoo modules) from a template it versions. The creation is
requested by a person, approved (CREATE_CHILD) with everything it will use pinned, and recorded as a durable
operation before any effect, so an interrupted creation resumes from its last recorded step.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

APPROVAL_ACTIONS = (
    "MERGE", "SCOPE_EXPANSION", "BUDGET_INCREASE", "BUDGET_UNLIMITED", "HIGH_RISK_OPERATION",
    "ENVIRONMENT_ACCESS", "PROJECT_CONFIG_CHANGE", "ASSUMPTION", "UPDATE", "PROJECT_READY", "CREATE_CHILD",
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN {_in(APPROVAL_ACTIONS)});

    ALTER TABLE projects ADD COLUMN parent_project_id uuid REFERENCES projects (id);
    CREATE INDEX projects_parent ON projects (parent_project_id) WHERE parent_project_id IS NOT NULL;

    CREATE TABLE child_creations (
        id               uuid PRIMARY KEY,
        parent_id        uuid NOT NULL REFERENCES projects (id),
        name             text NOT NULL,
        subject          jsonb NOT NULL,
        destination      text NOT NULL,
        github_repo      text NOT NULL,
        approval_id      uuid REFERENCES approvals (id),
        state            text NOT NULL CHECK (state IN ('AWAITING_APPROVAL', 'RUNNING', 'DONE', 'NEEDS_RECOVERY', 'FAILED',
                                                       'REJECTED', 'INVALIDATED')),
        step             text NOT NULL DEFAULT 'APPROVED' CHECK (step IN ('APPROVED', 'PUBLISHED', 'PUSHED',
                                                                         'REGISTERED')),
        evidence         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        child_project_id uuid REFERENCES projects (id),
        error            text,
        requested_by     text NOT NULL,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now()
    );
    -- One live creation per destination and per GitHub repository; finished refusals free the name again.
    CREATE UNIQUE INDEX child_creations_destination ON child_creations (parent_id, destination)
        WHERE state NOT IN ('REJECTED', 'INVALIDATED', 'FAILED');
    CREATE UNIQUE INDEX child_creations_repo ON child_creations (github_repo)
        WHERE state NOT IN ('REJECTED', 'INVALIDATED', 'FAILED');
    CREATE INDEX child_creations_parent ON child_creations (parent_id, created_at DESC);
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON child_creations TO ho_app;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute(f"""
    DROP TABLE child_creations;
    DROP INDEX projects_parent;
    ALTER TABLE projects DROP COLUMN parent_project_id;
    DELETE FROM approvals WHERE action = 'CREATE_CHILD';
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN {_in(APPROVAL_ACTIONS[:-1])});
    """)
