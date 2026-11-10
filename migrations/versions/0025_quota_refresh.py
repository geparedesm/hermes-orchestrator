"""Independent provider quota refresh claims and operational status.

Revision ID: 0025
Revises: 0024
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE provider_quota_refresh (
        provider text NOT NULL CHECK (provider IN ('claude', 'codex')),
        identity text NOT NULL,
        status text NOT NULL DEFAULT 'PENDING',
        claim_id uuid,
        lease_until timestamptz,
        last_attempt_at timestamptz,
        completed_at timestamptz,
        next_attempt_at timestamptz NOT NULL DEFAULT now(),
        failures integer NOT NULL DEFAULT 0,
        PRIMARY KEY (provider, identity)
    );
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT, UPDATE ON provider_quota_refresh TO ho_app;
            GRANT DELETE ON provider_usage TO ho_app;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE provider_quota_refresh;")
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN "
               "REVOKE DELETE ON provider_usage FROM ho_app; END IF; END $$;")
