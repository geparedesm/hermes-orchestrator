"""Versioned pen.dev designs and mandatory visual approval.

Revision ID: 0022
Revises: 0021
"""
from alembic import op
revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None
ACTIONS = "'MERGE', 'SCOPE_EXPANSION', 'BUDGET_INCREASE', 'BUDGET_UNLIMITED', 'HIGH_RISK_OPERATION', 'ENVIRONMENT_ACCESS', 'PROJECT_CONFIG_CHANGE', 'ASSUMPTION', 'UPDATE', 'PROJECT_READY', 'CREATE_CHILD', 'PLAN', 'CREATE_PROJECT'"


def upgrade():
    op.execute(f"""
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN ({ACTIONS}, 'UI_DESIGN'));
    ALTER TABLE tasks ADD COLUMN ui_design_required boolean NOT NULL DEFAULT false;
    CREATE TABLE ui_designs (
        id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(id),
        version integer NOT NULL CHECK (version > 0),
        requirements_version integer NOT NULL,
        plan_version integer NOT NULL,
        config_id uuid NOT NULL REFERENCES project_configs(id),
        state text NOT NULL CHECK (state IN ('PREPARING', 'PENDING', 'APPROVED', 'REJECTED', 'FAILED', 'SUPERSEDED')),
        execution_id uuid REFERENCES executions(id),
        approval_id uuid REFERENCES approvals(id),
        manifest jsonb NOT NULL DEFAULT '{{}}',
        delivered_images text[] NOT NULL DEFAULT '{{}}',
        feedback text,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(task_id, version)
    );
    CREATE UNIQUE INDEX ui_design_one_current ON ui_designs(task_id) WHERE state IN ('PREPARING', 'PENDING', 'APPROVED');
    GRANT SELECT, INSERT, UPDATE ON ui_designs TO ho_app;
    """)


def downgrade():
    op.execute(f"""
    DROP TABLE ui_designs;
    ALTER TABLE tasks DROP COLUMN ui_design_required;
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN ({ACTIONS}));
    """)
