"""PostgreSQL access: a connection pool and explicit transactions."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

Row = dict[str, Any]


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=False,
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=30)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def transaction(self) -> Iterator[Cursor[Row]]:
        """One transaction; commits on success, rolls back on any exception."""
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    yield cur

    @contextmanager
    def connection(self) -> Iterator[Connection[Row]]:
        with self.pool.connection() as conn:
            yield conn

    def ping(self) -> bool:
        try:
            with self.transaction() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - health check reports failure only
            return False


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)
