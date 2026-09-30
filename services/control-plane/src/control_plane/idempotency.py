"""Idempotent command execution (DATA_MODEL.md `idempotency_keys`).

The key row is inserted first in the same transaction as the command. A
concurrent duplicate blocks on that row until the first transaction commits,
then replays the stored response.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ho_core.hashing import hash_value
from psycopg import Cursor

from .db import Row, jsonb
from .errors import BadRequest, Conflict

_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def run_idempotent(
    cur: Cursor[Row],
    *,
    principal: str,
    key: str | None,
    request: Any,
    command: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]]:
    if not key or not _KEY.match(key):
        raise BadRequest("Idempotency-Key header (8-128 characters of [A-Za-z0-9._:-]) is required")
    request_hash = hash_value(request)
    cur.execute(
        """
        INSERT INTO idempotency_keys (principal, key, request_hash) VALUES (%s, %s, %s)
        ON CONFLICT (principal, key) DO NOTHING
        RETURNING key
        """,
        (principal, key, request_hash),
    )
    if cur.fetchone() is None:
        cur.execute(
            "SELECT request_hash, status_code, response FROM idempotency_keys WHERE principal = %s AND key = %s FOR UPDATE",
            (principal, key),
        )
        existing = cur.fetchone()
        assert existing is not None
        if existing["request_hash"] != request_hash:
            raise Conflict("Idempotency-Key was already used with a different request")
        if existing["status_code"] is not None:
            return int(existing["status_code"]), existing["response"]
    status, body = command()
    cur.execute(
        "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE principal = %s AND key = %s",
        (status, jsonb(body), principal, key),
    )
    return status, body
