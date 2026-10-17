"""New projects at the projects root from a platform template (docs/design/new-projects.md).

Revision ID: 0020
Revises: 0019

A creation is now either a CHILD (from a parent's template, as before) or a PROJECT (from a platform template, no
parent, GitHub optional). A live PROJECT creation reserves its name: it is the folder and the project slug.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

APPROVAL_ACTIONS = (
    "MERGE", "SCOPE_EXPANSION", "BUDGET_INCREASE", "BUDGET_UNLIMITED", "HIGH_RISK_OPERATION",
    "ENVIRONMENT_ACCESS", "PROJECT_CONFIG_CHANGE", "ASSUMPTION", "UPDATE", "PROJECT_READY", "CREATE_CHILD", "PLAN",
    "CREATE_PROJECT",
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN {_in(APPROVAL_ACTIONS)});
    -- A project being created has no project row yet, like a platform update.
    ALTER TABLE approvals DROP CONSTRAINT approvals_project_scope;
    ALTER TABLE approvals ADD CONSTRAINT approvals_project_scope
        CHECK (project_id IS NOT NULL OR action IN ('UPDATE', 'CREATE_PROJECT'));

    -- At most one open request per action and scope; project creations have no project yet, so each one is its own
    -- scope (its operation), or only one project could be requested at a time.
    DROP INDEX approvals_open;
    CREATE UNIQUE INDEX approvals_open ON approvals (project_id, task_id, action)
        NULLS NOT DISTINCT WHERE state IN ('PENDING', 'APPROVED') AND action <> 'CREATE_PROJECT';
    CREATE UNIQUE INDEX approvals_open_project_creation ON approvals ((subject->>'operation_id'))
        WHERE state IN ('PENDING', 'APPROVED') AND action = 'CREATE_PROJECT';

    ALTER TABLE child_creations ADD COLUMN kind text NOT NULL DEFAULT 'CHILD' CHECK (kind IN ('CHILD', 'PROJECT'));
    ALTER TABLE child_creations ALTER COLUMN parent_id DROP NOT NULL;
    ALTER TABLE child_creations ALTER COLUMN github_repo DROP NOT NULL;
    ALTER TABLE child_creations ADD CONSTRAINT child_creations_kind_parent
        CHECK ((kind = 'CHILD') = (parent_id IS NOT NULL) AND (kind = 'PROJECT' OR github_repo IS NOT NULL));
    -- A live project creation reserves its name (folder and slug); a rejected, invalidated, or abandoned one frees it.
    CREATE UNIQUE INDEX child_creations_project_name ON child_creations (name)
        WHERE kind = 'PROJECT' AND state NOT IN ('REJECTED', 'INVALIDATED', 'FAILED');
    """)


def downgrade() -> None:
    previous = tuple(a for a in APPROVAL_ACTIONS if a != "CREATE_PROJECT")
    op.execute(f"""
    DROP INDEX child_creations_project_name;
    DELETE FROM child_creations WHERE kind = 'PROJECT';
    ALTER TABLE child_creations DROP CONSTRAINT child_creations_kind_parent;
    ALTER TABLE child_creations ALTER COLUMN github_repo SET NOT NULL;
    ALTER TABLE child_creations ALTER COLUMN parent_id SET NOT NULL;
    ALTER TABLE child_creations DROP COLUMN kind;
    DELETE FROM approvals WHERE action = 'CREATE_PROJECT';
    DROP INDEX approvals_open_project_creation;
    DROP INDEX approvals_open;
    CREATE UNIQUE INDEX approvals_open ON approvals (project_id, task_id, action)
        NULLS NOT DISTINCT WHERE state IN ('PENDING', 'APPROVED');
    ALTER TABLE approvals DROP CONSTRAINT approvals_project_scope;
    ALTER TABLE approvals ADD CONSTRAINT approvals_project_scope CHECK (project_id IS NOT NULL OR action = 'UPDATE');
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN {_in(previous)});
    """)
