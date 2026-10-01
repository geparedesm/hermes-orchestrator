"""Notification delivery to Hermes (AD-12; MASTER_SPEC sections 64 and 74; docs/design/phase-9.md).

The outbox (`notifications`) is written in the same transaction as its event. This deliverer posts it to a
Hermes webhook route (`deliver_only`: the text is the message, no agent turn), signed with Hermes's
generic HMAC V2: `X-Webhook-Signature-V2` = hex HMAC-SHA256 of `"<timestamp>.<body>"` with
`X-Webhook-Timestamp`, verified by Hermes within a 300 s window. Hermes caches each `X-Request-ID`
even when its delivery fails and answers a repeat with `duplicate` (200), so the ID carries the attempt
number: a retry after a failure is delivered again (at least once), while a resend of the same attempt
is still deduplicated.

Attention notifications go out one by one, in order. Routine ones are aggregated into one digest at most
every ROUTINE_DIGEST. The oldest pending notification gates delivery, so order survives an outage, and
its backoff (10 s doubling to 15 min) is the outbox's backoff while Hermes is unreachable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .context import Context
from .db import Row

log = logging.getLogger(__name__)
BATCH = 50
MAX_DELAY = 900  # seconds
ROUTINE_DIGEST = timedelta(minutes=5)
DIGEST_MAX = 20

# What the person should do next, by event (the commands are the plugin's /orch verbs).
_HINTS = {
    "APPROVAL_REQUIRED": "Approve: /orch approve {approval_id}\nReject: /orch reject {approval_id}",
    "READY_FOR_MERGE": "Review it (/orch status {task}); approving the merge request is a separate approval.",
    "AUTH_REQUIRED": "Log in again on the host (make auth-claude or make auth-codex), then confirm with `ho auth ready`.",
    "PAUSED_BUDGET": "Raise the budget: /orch budget {task} agent_launches=10",
    "BLOCKED": "Inspect it: /orch status {task}; retry with /orch retry {task} or cancel with /orch cancel {task}.",
}


def backoff_seconds(attempts: int) -> int:
    """10 s, 20 s, 40 s, ... capped at 15 minutes: notifications are retried until Hermes returns."""
    return min(10 * 2 ** max(0, attempts - 1), MAX_DELAY)


def sign(secret: str, body: bytes, timestamp: int) -> dict[str, str]:
    """Hermes generic HMAC V2 headers (gateway/platforms/webhook.py at the pinned release)."""
    signature = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Webhook-Signature-V2": signature, "X-Webhook-Timestamp": str(timestamp)}


def render(payload: dict[str, Any], task: str | None) -> str:
    """One notification as chat text."""
    event = payload.get("type", "")
    data = payload.get("data") or {}
    head = f"[{task}] " if task else ""
    text = f"{head}{payload.get('summary', event)}"
    hint = _HINTS.get(event)
    if hint:
        try:
            text += "\n" + hint.format(task=task or "", **{k: v for k, v in data.items() if isinstance(v, str)})
        except (KeyError, IndexError):
            pass
    return text


def render_digest(items: list[tuple[dict[str, Any], str | None]]) -> str:
    lines = [f"Progress ({len(items)} update{'s' if len(items) != 1 else ''}):"]
    lines += [f"- {render(payload, task).splitlines()[0]}" for payload, task in items]
    return "\n".join(lines)


class Outbox:
    def __init__(self, ctx: Context, health: Any, url: str | None, secret: str | None,
                 client: httpx.Client | None = None) -> None:
        self.ctx = ctx
        self.health = health
        self.url = url
        self.secret = secret
        self.client = client or httpx.Client(timeout=5)

    def _post(self, request_id: str, body: dict[str, Any]) -> str | None:
        """Send one webhook; returns an error string or None."""
        raw = json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-Request-ID": request_id}
        if self.secret:
            headers |= sign(self.secret, raw, int(time.time()))
        try:
            response = self.client.post(self.url, content=raw, headers=headers)  # type: ignore[arg-type]
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return (str(exc) or type(exc).__name__)[:300]
        return None

    def _keys(self, rows: list[Row]) -> dict[str, tuple[str, str]]:
        ids = sorted({r["payload"].get("task_id") for r in rows if r["payload"].get("task_id")})
        if not ids:
            return {}
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT t.id::text AS id, t.key, p.slug FROM tasks t JOIN projects p ON p.id = t.project_id "
                            "WHERE t.id::text = ANY(%s)", (ids,))
            return {r["id"]: (r["key"], r["slug"]) for r in uow.cur.fetchall()}

    def _mark(self, rows: list[Row], error: str | None) -> None:
        with self.ctx.unit_of_work() as uow:
            if error is None:
                uow.cur.execute("UPDATE notifications SET state = 'SENT', delivered_at = now(), attempts = attempts + 1, "
                                "last_error = NULL WHERE id = ANY(%s)", ([r["id"] for r in rows],))
            else:
                head = rows[0]
                attempts = int(head["attempts"]) + 1
                uow.cur.execute("UPDATE notifications SET attempts = %s, last_error = %s, next_attempt_at = now() + %s "
                                "WHERE id = %s", (attempts, error, timedelta(seconds=backoff_seconds(attempts)), head["id"]))
        if self.health is not None:
            self.health.record("hermes", error is None, error)

    def deliver(self) -> dict[str, int]:
        stats = {"sent": 0, "failed": 0, "digests": 0}
        if not self.url:
            return stats  # Hermes not configured: notifications wait in the outbox
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT *, next_attempt_at <= now() AS due FROM notifications WHERE state = 'PENDING' "
                            "ORDER BY priority, created_at LIMIT %s", (BATCH,))
            rows = uow.cur.fetchall()
        if not rows or not rows[0]["due"]:
            return stats
        keys = self._keys(rows)

        def task_of(row: Row) -> tuple[str | None, str | None]:
            return keys.get(row["payload"].get("task_id") or "", (None, None))

        for row in [r for r in rows if r["priority"] == "ATTENTION"]:
            task, project = task_of(row)
            payload = row["payload"]
            error = self._post(f"{row['id']}:{int(row['attempts']) + 1}", {"text": render(payload, task), "event": payload.get("type"), "task": task,
                                                "project": project, "priority": "ATTENTION", "id": str(row["id"])})
            self._mark([row], error)
            if error:
                stats["failed"] += 1
                return stats
            stats["sent"] += 1

        routine = [r for r in rows if r["priority"] == "ROUTINE"]
        now = datetime.now(timezone.utc)
        if routine and (len(routine) >= DIGEST_MAX or routine[0]["created_at"] <= now - ROUTINE_DIGEST):
            if not routine[0]["due"]:
                return stats
            batch = routine[:DIGEST_MAX]
            items = [(r["payload"], task_of(r)[0]) for r in batch]
            error = self._post(f"digest-{batch[0]['id']}:{int(batch[0]['attempts']) + 1}", {"text": render_digest(items), "event": "DIGEST", "task": None,
                                                            "project": None, "priority": "ROUTINE",
                                                            "id": f"digest-{batch[0]['id']}"})
            self._mark(batch, error)
            if error:
                stats["failed"] += 1
            else:
                stats["sent"] += len(batch)
                stats["digests"] += 1
        return stats
