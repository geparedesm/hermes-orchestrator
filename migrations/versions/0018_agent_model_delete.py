"""Allow the application role to restore a provider model to the CLI default.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON provider_models TO ho_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON provider_models FROM ho_app")
