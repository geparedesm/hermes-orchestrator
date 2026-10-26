"""Disk usage as a platform health component.

Revision ID: 0023
Revises: 0022
"""
from alembic import op
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None
COMPONENTS = "'agent_manager', 'git_service', 'redis', 'hermes'"


def upgrade():
    op.execute(f"""
    ALTER TABLE component_health DROP CONSTRAINT component_health_component_check;
    ALTER TABLE component_health ADD CONSTRAINT component_health_component_check CHECK (component IN ({COMPONENTS}, 'disk'));
    """)


def downgrade():
    op.execute(f"""
    DELETE FROM component_health WHERE component = 'disk';
    ALTER TABLE component_health DROP CONSTRAINT component_health_component_check;
    ALTER TABLE component_health ADD CONSTRAINT component_health_component_check CHECK (component IN ({COMPONENTS}));
    """)
