"""A UI design launch that waits for capacity can be queued.

Revision ID: 0024
Revises: 0023
"""
from alembic import op
revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None
KINDS = "'DEVELOP', 'FIX', 'SUBTASK_REVIEW', 'INTEGRATION_REVIEW', 'RESOLVE', 'STEP'"


def upgrade():
    op.execute(f"""
    ALTER TABLE pending_launches DROP CONSTRAINT pending_launches_kind_check;
    ALTER TABLE pending_launches ADD CONSTRAINT pending_launches_kind_check CHECK (kind IN ({KINDS}, 'UI_DESIGN'));
    """)


def downgrade():
    op.execute(f"""
    DELETE FROM pending_launches WHERE kind = 'UI_DESIGN';
    ALTER TABLE pending_launches DROP CONSTRAINT pending_launches_kind_check;
    ALTER TABLE pending_launches ADD CONSTRAINT pending_launches_kind_check CHECK (kind IN ({KINDS}));
    """)
