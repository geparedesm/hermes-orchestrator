"""Dashboard-managed default model per provider.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE provider_models (
        provider text PRIMARY KEY CHECK (provider IN ('claude', 'codex')),
        model text NOT NULL CHECK (model ~ '^[A-Za-z0-9][A-Za-z0-9._:/@\\[\\]-]{0,99}$'),
        updated_by text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    GRANT SELECT, INSERT, UPDATE ON provider_models TO ho_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE provider_models")
