"""Expired approvals can be requested again; the new approval points to the one it renews.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE approvals ADD COLUMN renewed_from uuid REFERENCES approvals (id);
    CREATE UNIQUE INDEX approvals_renewed_once ON approvals (renewed_from) WHERE renewed_from IS NOT NULL;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
