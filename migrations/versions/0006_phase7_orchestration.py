"""Phase 7 orchestration: leases, requirements, assumptions, subtask DAG, orchestrator actions,
knowledge, manifests, and budget reservations.

Revision ID: 0006
Revises: 0005
See DATA_MODEL.md sections 3.2, 3.3, 3.7, 3.8 and docs/design/phase-7.md.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE task_leases (
        task_id      uuid PRIMARY KEY REFERENCES tasks (id),
        holder       text NOT NULL,
        provider     text NOT NULL CHECK (provider IN ('claude', 'codex')),
        epoch        bigint NOT NULL,
        acquired_at  timestamptz NOT NULL DEFAULT now(),
        renewed_at   timestamptz NOT NULL DEFAULT now(),
        expires_at   timestamptz NOT NULL
    );

    ALTER TABLE tasks
        ADD COLUMN orchestrator_cursor_seq     bigint NOT NULL DEFAULT 0,
        ADD COLUMN orchestrator_failures       integer NOT NULL DEFAULT 0,
        ADD COLUMN step_requested              boolean NOT NULL DEFAULT false,
        ADD COLUMN launch_suspended_at         timestamptz,
        ADD COLUMN current_requirements_version integer,
        ADD COLUMN current_plan_version        integer;

    CREATE TABLE requirement_versions (
        id            uuid PRIMARY KEY,
        task_id       uuid NOT NULL REFERENCES tasks (id),
        version       integer NOT NULL,
        artifact_id   uuid NOT NULL REFERENCES artifacts (id),
        source        text NOT NULL CHECK (source IN ('USER', 'ORCHESTRATOR')),
        change_reason text,
        impact        jsonb,
        approval_id   uuid REFERENCES approvals (id),
        created_at    timestamptz NOT NULL DEFAULT now(),
        UNIQUE (task_id, version)
    );

    CREATE TABLE assumptions (
        id          uuid PRIMARY KEY,
        task_id     uuid NOT NULL REFERENCES tasks (id),
        level       text NOT NULL CHECK (level IN ('LOW', 'MEDIUM', 'HIGH')),
        assumption  text NOT NULL,
        reason      text NOT NULL,
        impact      text,
        reversible  boolean NOT NULL,
        status      text NOT NULL CHECK (status IN ('RECORDED', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED')),
        approval_id uuid REFERENCES approvals (id),
        created_at  timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE subtasks (
        id                 uuid PRIMARY KEY,
        task_id            uuid NOT NULL REFERENCES tasks (id),
        key                text NOT NULL UNIQUE CHECK (key ~ '^T-[0-9]+-[0-9]+$'),
        local_key          text NOT NULL,
        plan_version       integer NOT NULL,
        kind               text NOT NULL CHECK (kind IN ('IMPLEMENT', 'TEST_AUTHORING', 'INTEGRATION', 'VERIFICATION', 'RESEARCH')),
        title              text NOT NULL,
        description        text NOT NULL,
        state              text NOT NULL CHECK (state IN ('PENDING', 'READY', 'IN_PROGRESS', 'IN_REVIEW', 'FIX_REQUIRED',
                                                          'ACCEPTED', 'INTEGRATED', 'BLOCKED', 'CANCELLED')),
        estimated_scope    jsonb NOT NULL DEFAULT '{}',
        risk               text NOT NULL DEFAULT 'MEDIUM' CHECK (risk IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        preferred_provider text CHECK (preferred_provider IN ('claude', 'codex')),
        developer_provider text CHECK (developer_provider IN ('claude', 'codex')),
        workspace_id       uuid REFERENCES workspaces (id),
        review_cycles      integer NOT NULL DEFAULT 0,
        attempts           integer NOT NULL DEFAULT 0,
        state_reason       text,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now(),
        UNIQUE (task_id, plan_version, local_key)
    );
    CREATE INDEX subtasks_task ON subtasks (task_id, state);

    CREATE TABLE subtask_dependencies (
        subtask_id            uuid NOT NULL REFERENCES subtasks (id),
        depends_on_subtask_id uuid NOT NULL REFERENCES subtasks (id),
        PRIMARY KEY (subtask_id, depends_on_subtask_id),
        CHECK (subtask_id <> depends_on_subtask_id)
    );

    ALTER TABLE executions ADD COLUMN subtask_id uuid REFERENCES subtasks (id);
    ALTER TABLE reviews ADD COLUMN subtask_id uuid REFERENCES subtasks (id);
    ALTER TABLE reviews DROP CONSTRAINT IF EXISTS reviews_check;
    ALTER TABLE reviews ADD CONSTRAINT reviews_independent CHECK (NOT (reviewer_provider = ANY (developer_providers)));

    CREATE TABLE orchestrator_actions (
        id           uuid PRIMARY KEY,
        task_id      uuid NOT NULL REFERENCES tasks (id),
        execution_id uuid REFERENCES executions (id),
        lease_epoch  bigint NOT NULL,
        seq          integer NOT NULL,
        type         text NOT NULL,
        payload      jsonb NOT NULL,
        outcome      text NOT NULL CHECK (outcome IN ('ACCEPTED', 'REJECTED')),
        reason       text,
        created_at   timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX orchestrator_actions_task ON orchestrator_actions (task_id, created_at);

    CREATE TABLE knowledge_items (
        id                 uuid PRIMARY KEY,
        project_id         uuid NOT NULL REFERENCES projects (id),
        category           text NOT NULL CHECK (category IN ('DISCOVERY', 'CONVENTION', 'DECISION', 'KNOWN_ISSUE',
                                                             'ARCHITECTURE', 'LESSON_LEARNED')),
        trust              text NOT NULL CHECK (trust IN ('CONFIRMED', 'OBSERVED', 'HYPOTHESIS', 'STALE', 'REJECTED')),
        title              text NOT NULL,
        body               text NOT NULL,
        provenance         jsonb NOT NULL DEFAULT '{}',
        anchors            text[] NOT NULL DEFAULT '{}',
        observed_at_commit text,
        repo_path          text,
        superseded_by      uuid REFERENCES knowledge_items (id),
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX knowledge_project ON knowledge_items (project_id, trust);

    CREATE TABLE manifests (
        id           uuid PRIMARY KEY,
        task_id      uuid NOT NULL REFERENCES tasks (id),
        kind         text NOT NULL CHECK (kind IN ('READY_FOR_MERGE', 'FINAL', 'ON_DEMAND')),
        artifact_id  uuid NOT NULL REFERENCES artifacts (id),
        sha256       text NOT NULL,
        generated_at timestamptz NOT NULL DEFAULT now()
    );

    -- Launches the control plane wants to start but that wait for capacity, a scope conflict, or a
    -- higher-priority task (design changes 3 and 6). Retried every scheduler tick with aging.
    CREATE TABLE pending_launches (
        id           uuid PRIMARY KEY,
        task_id      uuid NOT NULL REFERENCES tasks (id),
        subtask_id   uuid REFERENCES subtasks (id),
        kind         text NOT NULL CHECK (kind IN ('DEVELOP', 'FIX', 'SUBTASK_REVIEW', 'INTEGRATION_REVIEW', 'RESOLVE', 'STEP')),
        request      jsonb NOT NULL,
        reason       text,
        attempts     integer NOT NULL DEFAULT 0,
        requested_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX pending_launches_task ON pending_launches (task_id);

    -- Budget reservations (design change 3): reserved units per running execution, replaced by actual usage.
    ALTER TABLE budgets ADD COLUMN reserved jsonb NOT NULL DEFAULT '{}';
    ALTER TABLE executions ADD COLUMN budget_reservation jsonb;

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON task_leases TO ho_app;
            GRANT SELECT, INSERT, UPDATE ON requirement_versions, assumptions, subtasks, knowledge_items, manifests TO ho_app;
            GRANT SELECT, INSERT, DELETE ON subtask_dependencies TO ho_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON pending_launches TO ho_app;
            GRANT SELECT, INSERT ON orchestrator_actions TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
