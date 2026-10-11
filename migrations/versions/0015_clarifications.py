"""Clarifying questions during planning and supervised plans (docs/design/clarifications.md).

Revision ID: 0015
Revises: 0014

The orchestrator may ask the person one round of multiple-choice questions per planning episode; the answers become a
requirements version (source CLARIFICATION). With `approve_plan`, a plan waits for a PLAN approval bound to the plan and
requirements versions before any subtask starts.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

APPROVAL_ACTIONS = (
    "MERGE", "SCOPE_EXPANSION", "BUDGET_INCREASE", "BUDGET_UNLIMITED", "HIGH_RISK_OPERATION",
    "ENVIRONMENT_ACCESS", "PROJECT_CONFIG_CHANGE", "ASSUMPTION", "UPDATE", "PROJECT_READY", "CREATE_CHILD", "PLAN",
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN {_in(APPROVAL_ACTIONS)});

    ALTER TABLE requirement_versions DROP CONSTRAINT requirement_versions_source_check;
    ALTER TABLE requirement_versions ADD CONSTRAINT requirement_versions_source_check
        CHECK (source IN ('USER', 'ORCHESTRATOR', 'CLARIFICATION'));

    -- A task created with "review the plan before work starts" (tighten-only over the project's setting).
    ALTER TABLE tasks ADD COLUMN approve_plan boolean NOT NULL DEFAULT false;
    -- The supervised plan: which plan version waits for (or got) its PLAN approval.
    ALTER TABLE tasks ADD COLUMN plan_approval_version integer;
    ALTER TABLE tasks ADD COLUMN plan_approval_state text CHECK (plan_approval_state IN ('PENDING', 'APPROVED', 'REJECTED'));

    CREATE TABLE clarifications (
        id                   uuid PRIMARY KEY,
        task_id              uuid NOT NULL REFERENCES tasks (id),
        number               bigint NOT NULL DEFAULT nextval('approvals_number_seq'),
        episode              integer NOT NULL,   -- the person's requirements version that opened the planning episode
        requirements_version integer,            -- the version the questions were asked against
        questions            jsonb NOT NULL,
        answers              jsonb,
        state                text NOT NULL CHECK (state IN ('PENDING', 'ANSWERED', 'CANCELLED')),
        cancel_reason        text,
        answered_by          text,
        asked_at             timestamptz NOT NULL DEFAULT now(),
        answered_at          timestamptz
    );
    CREATE UNIQUE INDEX clarifications_pending ON clarifications (task_id) WHERE state = 'PENDING';
    CREATE UNIQUE INDEX clarifications_number ON clarifications (number);
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON clarifications TO ho_app;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute(f"""
    DROP TABLE clarifications;
    ALTER TABLE tasks DROP COLUMN plan_approval_state, DROP COLUMN plan_approval_version, DROP COLUMN approve_plan;
    DELETE FROM requirement_versions WHERE source = 'CLARIFICATION';
    ALTER TABLE requirement_versions DROP CONSTRAINT requirement_versions_source_check;
    ALTER TABLE requirement_versions ADD CONSTRAINT requirement_versions_source_check CHECK (source IN ('USER', 'ORCHESTRATOR'));
    DELETE FROM approvals WHERE action = 'PLAN';
    ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_action_check CHECK (action IN {_in(APPROVAL_ACTIONS[:-1])});
    """)
