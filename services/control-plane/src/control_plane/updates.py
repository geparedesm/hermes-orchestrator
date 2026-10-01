"""Approval-controlled platform updates (MASTER_SPEC section 79; docs/operations.md).

An update is requested for a target version and becomes an UPDATE approval bound to the running version
and the target. scripts/update.sh starts it only by consuming that approval (so the running version must
not have changed since the decision), then snapshots, updates, checks health, and rolls back on failure;
each update is recorded in `platform_updates`.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from ho_core.enums import ApprovalAction, Risk
from ho_core.ids import uuid7

from .approvals import Approvals
from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import BadRequest, Conflict, NotFound
from .events import record_event

_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")


class Updates:
    def __init__(self, ctx: Context, approvals: Approvals) -> None:
        self.ctx = ctx
        self.approvals = approvals

    @property
    def current(self) -> str:
        return str(self.ctx.platform["platform"].get("version") or "dev")

    def _subject(self, to_version: str) -> dict[str, Any]:
        return {"kind": "platform_update", "from_version": self.current, "to_version": to_version}

    def request(self, uow: UnitOfWork, to_version: str, *, principal: Principal) -> Row:
        if not _VERSION.match(to_version):
            raise BadRequest("versions are image tags like 2026.10.1 or 1.4.0")
        if to_version == self.current:
            raise Conflict(f"the platform already runs {to_version}")
        return self.approvals.request(uow, action=ApprovalAction.UPDATE, project_id=None, subject=self._subject(to_version),
                                      config_hash="0" * 64, summary=f"update the platform from {self.current} to {to_version}",
                                      requested_by=principal.value, risk=Risk.HIGH)

    def start(self, uow: UnitOfWork, approval_id: UUID, *, backup: str | None) -> Row:
        approval = self.approvals.get(uow, approval_id, lock=True)
        if approval["action"] != ApprovalAction.UPDATE:
            raise Conflict("that approval is not a platform update")
        to_version = approval["subject"].get("to_version", "")
        if not self.approvals.consume(uow, approval, subject=self._subject(to_version), config_hash="0" * 64):
            raise Conflict("the update approval is not usable: it is not approved, expired, or the running version changed")
        uow.cur.execute("INSERT INTO platform_updates (id, approval_id, from_version, to_version, state, backup) "
                        "VALUES (%s, %s, %s, %s, 'STARTED', %s) RETURNING *",
                        (uuid7(), approval_id, self.current, to_version, backup))
        row = uow.cur.fetchone()
        record_event(uow.cur, "UPDATE_STARTED", actor="control-plane", summary=f"platform update {self.current} -> {to_version}",
                     data={"update": str(row["id"]), "backup": backup}, pending=uow.events)  # type: ignore[index]
        assert row is not None
        return row

    def finish(self, uow: UnitOfWork, update_id: UUID, *, state: str, report: dict[str, Any]) -> Row:
        if state not in ("SUCCEEDED", "ROLLED_BACK", "FAILED"):
            raise BadRequest("state is SUCCEEDED, ROLLED_BACK, or FAILED")
        uow.cur.execute("UPDATE platform_updates SET state = %s, report = %s, finished_at = now() WHERE id = %s "
                        "AND state = 'STARTED' RETURNING *", (state, jsonb(report), update_id))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound("no update in progress with that id")
        record_event(uow.cur, "UPDATE_COMPLETED" if state == "SUCCEEDED" else "UPDATE_FAILED", actor="control-plane",
                     summary=f"platform update {row['from_version']} -> {row['to_version']}: {state.lower()}",
                     data={"update": str(update_id), **report}, pending=uow.events)
        return row

    def list(self, uow: UnitOfWork) -> list[Row]:
        uow.cur.execute("SELECT * FROM platform_updates ORDER BY started_at DESC LIMIT 50")
        return uow.cur.fetchall()
