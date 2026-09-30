"""Phase 2 control plane: projects, configuration, tasks, approvals, audit, outbox.

Revision ID: 0001
Revises:
See DATA_MODEL.md section 3. Later phases add subtasks, executions, grants,
leases, checkpoints, reviews, tests, and knowledge tables.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TASK_STATES = (
    "BACKLOG", "READY", "PLANNING", "QUEUED", "RUNNING", "TESTING", "REVIEW", "FIX_REQUIRED",
    "QUALITY_GATE", "APPROVAL_REQUIRED", "AUTH_REQUIRED", "PAUSED", "PAUSED_BUDGET", "BLOCKED",
    "FAILED", "READY_FOR_MERGE", "MERGING", "VERIFYING", "DONE", "CANCELLED",
)
APPROVAL_ACTIONS = (
    "MERGE", "SCOPE_EXPANSION", "BUDGET_INCREASE", "BUDGET_UNLIMITED", "HIGH_RISK_OPERATION",
    "ENVIRONMENT_ACCESS", "PROJECT_CONFIG_CHANGE", "ASSUMPTION", "UPDATE", "PROJECT_READY",
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE projects (
        id                uuid PRIMARY KEY,
        slug              text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{{0,62}}$'),
        name              text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
        relative_path     text NOT NULL CHECK (relative_path !~ '(^|/)\\.\\.(/|$)' AND left(relative_path, 1) <> '/'),
        host_path         text NOT NULL,
        git_remote        text,
        default_branch    text,
        head_commit       text,
        status            text NOT NULL CHECK (status IN ('REGISTERED', 'SCANNING', 'PROPOSED', 'PROJECT_READY',
                                                          'DRIFT_DETECTED', 'SUSPENDED', 'UNREGISTERED')),
        config_path       text NOT NULL DEFAULT '.hermes/project.yaml',
        knowledge_path    text NOT NULL DEFAULT '.hermes/',
        hermes_project_ref text,
        registered_by     text NOT NULL,
        registered_at     timestamptz NOT NULL DEFAULT now(),
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        version           integer NOT NULL DEFAULT 1
    );
    -- One registration per path; unregistering frees the path but keeps history.
    CREATE UNIQUE INDEX projects_active_path ON projects (relative_path) WHERE status <> 'UNREGISTERED';

    CREATE TABLE artifacts (
        id            uuid PRIMARY KEY,
        project_id    uuid NOT NULL REFERENCES projects (id),
        task_id       uuid,
        kind          text NOT NULL,
        path          text NOT NULL UNIQUE,
        sha256        text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{{64}}$'),
        size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
        media_type    text NOT NULL,
        redacted      boolean NOT NULL DEFAULT false,
        retain_until  timestamptz,
        created_at    timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE onboarding_scans (
        id           uuid PRIMARY KEY,
        project_id   uuid NOT NULL REFERENCES projects (id),
        head_commit  text,
        report       jsonb NOT NULL,
        artifact_id  uuid NOT NULL REFERENCES artifacts (id),
        created_at   timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE approvals (
        id                 uuid PRIMARY KEY,
        action             text NOT NULL CHECK (action IN {_in(APPROVAL_ACTIONS)}),
        project_id         uuid NOT NULL REFERENCES projects (id),
        task_id            uuid,
        risk               text NOT NULL CHECK (risk IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        subject            jsonb NOT NULL,
        config_hash        text NOT NULL,
        policy_version     text NOT NULL,
        state_hash         text NOT NULL CHECK (state_hash ~ '^[a-f0-9]{{64}}$'),
        from_task_state    text,
        state              text NOT NULL CHECK (state IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED',
                                                          'INVALIDATED', 'CONSUMED')),
        summary            text NOT NULL,
        requested_by       text NOT NULL,
        requested_at       timestamptz NOT NULL DEFAULT now(),
        expires_at         timestamptz NOT NULL,
        decided_by         text,
        decided_at         timestamptz,
        decision_note      text,
        consumed_at        timestamptz,
        invalidated_reason text,
        evidence_refs      jsonb NOT NULL DEFAULT '[]',
        version            integer NOT NULL DEFAULT 1,
        CHECK (state NOT IN ('APPROVED', 'REJECTED', 'CONSUMED') OR decided_by IS NOT NULL)
    );
    -- At most one open request per action and scope.
    CREATE UNIQUE INDEX approvals_open ON approvals (project_id, task_id, action)
        NULLS NOT DISTINCT WHERE state IN ('PENDING', 'APPROVED');
    CREATE INDEX approvals_state ON approvals (state, expires_at);

    CREATE TABLE project_configs (
        id                  uuid PRIMARY KEY,
        project_id          uuid NOT NULL REFERENCES projects (id),
        source              text NOT NULL CHECK (source IN ('REPOSITORY', 'PROPOSAL')),
        source_commit       text,
        project_yaml        jsonb NOT NULL,
        project_yaml_sha256 text NOT NULL,
        local_yaml          jsonb,
        effective_config    jsonb NOT NULL,
        effective_hash      text NOT NULL CHECK (effective_hash ~ '^[a-f0-9]{{64}}$'),
        policy_version      text NOT NULL,
        rejected_layers     jsonb NOT NULL DEFAULT '[]',
        clamped             jsonb NOT NULL DEFAULT '[]',
        scan_id             uuid REFERENCES onboarding_scans (id),
        status              text NOT NULL CHECK (status IN ('PROPOSED', 'ACTIVE', 'SUPERSEDED', 'REJECTED')),
        approval_id         uuid REFERENCES approvals (id),
        created_at          timestamptz NOT NULL DEFAULT now(),
        updated_at          timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX project_configs_one_active ON project_configs (project_id) WHERE status = 'ACTIVE';

    CREATE SEQUENCE task_key_seq;

    CREATE TABLE tasks (
        id                          uuid PRIMARY KEY,
        key                         text NOT NULL UNIQUE CHECK (key ~ '^T-[0-9]+$'),
        project_id                  uuid NOT NULL REFERENCES projects (id),
        title                       text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
        original_request_artifact_id uuid NOT NULL REFERENCES artifacts (id),
        requested_by                text NOT NULL,
        priority                    text NOT NULL CHECK (priority IN ('CRITICAL', 'HIGH', 'NORMAL', 'LOW')),
        state                       text NOT NULL CHECK (state IN {_in(TASK_STATES)}),
        resume_state                text CHECK (resume_state IN {_in(TASK_STATES)}),
        state_reason                text,
        autonomy                    text CHECK (autonomy IN ('SUPERVISED', 'BALANCED', 'AUTONOMOUS')),
        budget_profile              text NOT NULL CHECK (budget_profile IN ('SMALL', 'NORMAL', 'LARGE', 'UNLIMITED')),
        expansion_profile           text CHECK (expansion_profile IN ('SMALL', 'NORMAL', 'LARGE', 'UNLIMITED')),
        config_id                   uuid REFERENCES project_configs (id),
        risk                        text CHECK (risk IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        labels                      text[] NOT NULL DEFAULT '{{}}',
        idempotency_key             text NOT NULL,
        ready_at                    timestamptz,
        started_at                  timestamptz,
        completed_at                timestamptz,
        created_at                  timestamptz NOT NULL DEFAULT now(),
        updated_at                  timestamptz NOT NULL DEFAULT now(),
        version                     integer NOT NULL DEFAULT 1,
        UNIQUE (requested_by, idempotency_key)
    );
    CREATE INDEX tasks_state ON tasks (state, priority, ready_at);
    CREATE INDEX tasks_project ON tasks (project_id, state);
    -- Deferred: a task and its original request artifact are created in one transaction.
    ALTER TABLE artifacts ADD CONSTRAINT artifacts_task FOREIGN KEY (task_id) REFERENCES tasks (id)
        DEFERRABLE INITIALLY DEFERRED;
    ALTER TABLE approvals ADD CONSTRAINT approvals_task FOREIGN KEY (task_id) REFERENCES tasks (id);

    CREATE TABLE task_relationships (
        id            uuid PRIMARY KEY,
        from_task_id  uuid NOT NULL REFERENCES tasks (id),
        to_task_id    uuid NOT NULL REFERENCES tasks (id),
        kind          text NOT NULL CHECK (kind IN ('DUPLICATE', 'RELATED', 'DEPENDENCY', 'CONFLICTING', 'INDEPENDENT')),
        classified_by text NOT NULL,
        evidence      text,
        created_at    timestamptz NOT NULL DEFAULT now(),
        CHECK (from_task_id <> to_task_id),
        UNIQUE (from_task_id, to_task_id, kind)
    );

    CREATE TABLE budgets (
        task_id               uuid PRIMARY KEY REFERENCES tasks (id),
        profile               text NOT NULL CHECK (profile IN ('SMALL', 'NORMAL', 'LARGE', 'UNLIMITED')),
        limits                jsonb NOT NULL,
        consumed              jsonb NOT NULL,
        thresholds            jsonb NOT NULL,
        state                 text NOT NULL CHECK (state IN ('OK', 'WARNING', 'OPTIMIZE', 'EXHAUSTED')),
        unlimited_approval_id uuid REFERENCES approvals (id),
        updated_at            timestamptz NOT NULL DEFAULT now(),
        CHECK (profile <> 'UNLIMITED' OR unlimited_approval_id IS NOT NULL)
    );

    CREATE TABLE policy_decisions (
        id          uuid PRIMARY KEY,
        project_id  uuid REFERENCES projects (id),
        task_id     uuid REFERENCES tasks (id),
        subject     text NOT NULL,
        class       text CHECK (class IN ('SAFE', 'CONTROLLED', 'HIGH_RISK')),
        decision    text NOT NULL CHECK (decision IN ('ALLOW', 'DENY', 'REQUIRE_APPROVAL')),
        rule_ids    text[] NOT NULL,
        summary     text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE events (
        seq          bigserial PRIMARY KEY,
        occurred_at  timestamptz NOT NULL DEFAULT now(),
        project_id   uuid REFERENCES projects (id),
        task_id      uuid REFERENCES tasks (id),
        type         text NOT NULL CHECK (type ~ '^[A-Z][A-Z_]+$'),
        actor        text NOT NULL,
        summary      text NOT NULL,
        data         jsonb NOT NULL DEFAULT '{{}}',
        audit        boolean NOT NULL DEFAULT false
    );
    CREATE INDEX events_task ON events (task_id, seq);
    CREATE INDEX events_project ON events (project_id, seq);

    CREATE TABLE notifications (
        id              uuid PRIMARY KEY,
        event_seq       bigint NOT NULL REFERENCES events (seq),
        priority        text NOT NULL CHECK (priority IN ('ATTENTION', 'ROUTINE')),
        payload         jsonb NOT NULL,
        state           text NOT NULL CHECK (state IN ('PENDING', 'SENT', 'FAILED', 'SUPPRESSED')),
        attempts        integer NOT NULL DEFAULT 0,
        next_attempt_at timestamptz NOT NULL DEFAULT now(),
        delivered_at    timestamptz,
        created_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX notifications_pending ON notifications (state, next_attempt_at);

    CREATE TABLE idempotency_keys (
        principal     text NOT NULL,
        key           text NOT NULL,
        request_hash  text NOT NULL,
        status_code   integer,
        response      jsonb,
        created_at    timestamptz NOT NULL DEFAULT now(),
        expires_at    timestamptz NOT NULL DEFAULT now() + interval '7 days',
        PRIMARY KEY (principal, key)
    );

    CREATE TABLE operation_intents (
        id          uuid PRIMARY KEY,
        project_id  uuid REFERENCES projects (id),
        task_id     uuid REFERENCES tasks (id),
        kind        text NOT NULL,
        target      text NOT NULL,
        request     jsonb NOT NULL,
        state       text NOT NULL CHECK (state IN ('PENDING', 'SENT', 'CONFIRMED', 'FAILED', 'ABANDONED')),
        attempts    integer NOT NULL DEFAULT 0,
        last_error  text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    );

    -- Least-privilege application role (created by scripts/postgres-init.sh).
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ho_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ho_app;
            -- Audit records are append-only for the application.
            REVOKE UPDATE, DELETE ON events, policy_decisions FROM ho_app;
            REVOKE ALL ON alembic_version FROM ho_app;
            GRANT SELECT ON alembic_version TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
