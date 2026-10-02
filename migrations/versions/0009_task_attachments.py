"""Files attached to a task by the person who asked for it.

Revision ID: 0009
Revises: 0008
Content lives on the artifact volume (kind `attachment`); this table names each file for its task. A retried
task gets rows pointing to the same artifacts. See docs/operations.md.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE task_attachments (
        id            uuid PRIMARY KEY,
        task_id       uuid NOT NULL REFERENCES tasks (id),
        artifact_id   uuid NOT NULL REFERENCES artifacts (id),
        name          text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
        media_type    text NOT NULL,
        size_bytes    bigint NOT NULL CHECK (size_bytes > 0),
        sha256        text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
        position      integer NOT NULL,
        added_by      text NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        UNIQUE (task_id, name)
    );
    CREATE INDEX task_attachments_task ON task_attachments (task_id, position);

    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ho_app') THEN
            GRANT SELECT, INSERT ON task_attachments TO ho_app;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    raise NotImplementedError("Restore from backup instead of downgrading")
