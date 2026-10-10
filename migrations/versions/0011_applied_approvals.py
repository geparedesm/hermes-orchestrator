"""Approvals applied when decided (assumptions, budget raises) are recorded as used.

Revision ID: 0011
Revises: 0010
Before this, they stayed APPROVED after being applied and the expiry job later relabelled them EXPIRED,
which read as "nobody answered". Those rows are corrected; from now on they are consumed when applied.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    UPDATE approvals SET state = 'CONSUMED', consumed_at = decided_at, invalidated_reason = NULL
    -- Decided and approved (rejected ones stay REJECTED): these two kinds were applied at the decision. Both the ones
    -- already relabelled EXPIRED and the ones still APPROVED, which the expiry job would relabel later.
    WHERE state IN ('EXPIRED', 'APPROVED') AND decided_at IS NOT NULL AND action IN ('ASSUMPTION', 'BUDGET_INCREASE');
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
