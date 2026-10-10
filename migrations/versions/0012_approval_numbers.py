"""A short number per approval, to answer from chat ("si 12").

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE approvals ADD COLUMN number bigserial;
    CREATE UNIQUE INDEX approvals_number ON approvals (number);
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT USAGE ON SEQUENCE approvals_number_seq TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
