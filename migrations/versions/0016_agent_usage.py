"""Agents in the Dashboard: each plan's usage windows and pausing a provider (docs/design/agents-dashboard.md).

Revision ID: 0016
Revises: 0015

`provider_usage` keeps the latest usage window a provider's CLI reported (session or week): only a percentage and a
reset time (invariant 4). `provider_settings` lets a person pause a provider: routing skips it, running work finishes.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE provider_usage (
        provider      text NOT NULL,
        identity      text NOT NULL,
        window_kind   text NOT NULL CHECK (window_kind IN ('session', 'week')),
        used_percent  numeric(5, 1) NOT NULL CHECK (used_percent >= 0 AND used_percent <= 100),
        resets_at     timestamptz NOT NULL,
        observed_at   timestamptz NOT NULL DEFAULT now(),
        execution_id  uuid REFERENCES executions (id) ON DELETE SET NULL,
        PRIMARY KEY (provider, identity, window_kind)
    );
    CREATE TABLE provider_settings (
        provider    text NOT NULL,
        identity    text NOT NULL,
        enabled     boolean NOT NULL DEFAULT true,
        updated_by  text NOT NULL,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (provider, identity)
    );
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON provider_usage, provider_settings TO ho_app;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE provider_settings; DROP TABLE provider_usage;")
