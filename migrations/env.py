"""Alembic environment: plain SQL migrations, no ORM metadata."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool


def database_url() -> str:
    url = os.environ.get("HO_MIGRATION_DATABASE_URL")
    if url:
        return url
    password = Path(os.environ["HO_DB_OWNER_PASSWORD_FILE"]).read_text().strip()
    host = os.environ.get("HO_DB_HOST", "postgres")
    user = os.environ.get("HO_DB_OWNER_USER", "ho_owner")
    name = os.environ.get("HO_DB_NAME", "ho")
    return f"postgresql+psycopg://{user}:{password}@{host}:5432/{name}"


def run() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported")
run()
