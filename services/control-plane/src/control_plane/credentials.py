"""Credential Broker, control-plane side (MASTER_SPEC section 20, SECURITY_MODEL.md section 7.1).

Provider sessions live in Docker volumes created by `make auth-<provider>`
(scripts/auth-login.sh). The control plane never sees their contents: it keeps
only a status per provider identity, moves tasks to AUTH_REQUIRED when an
execution reports an authentication failure, and resumes them when the
operator confirms a new login.
"""

from __future__ import annotations

import re
from typing import Any

from ho_core.adapters import ADAPTERS
from ho_core.enums import TaskState
from ho_core.statemachine import Trigger

from .agentmgr import AgentManagerError
from .auth import Principal
from .context import Context, UnitOfWork
from .errors import BadRequest, Conflict, UpstreamError
from .events import record_event
from .executions import Executions
from .tasks import Tasks

_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class Credentials:
    def __init__(self, ctx: Context, tasks: Tasks, executions: Executions) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.executions = executions

    def _volumes(self) -> list[dict[str, Any]]:
        if self.ctx.agents is None:
            raise UpstreamError("agent-manager is not configured")
        try:
            return self.ctx.agents.credentials()["credentials"]
        except AgentManagerError as exc:
            raise UpstreamError(str(exc)) from exc

    def status(self, uow: UnitOfWork) -> dict[str, Any]:
        volumes = {(v["provider"], v["identity"]): v for v in self._volumes()}
        uow.cur.execute("SELECT * FROM credential_refs ORDER BY provider, identity")
        refs = {(r["provider"], r["identity"]): r for r in uow.cur.fetchall()}
        try:
            pinned = self.ctx.agents.images()["images"] if self.ctx.agents else []
        except AgentManagerError:
            pinned = []
        identities = []
        for key in sorted(set(volumes) | set(refs)):
            ref = refs.get(key)
            identities.append({
                "provider": key[0],
                "identity": key[1],
                "volume": volumes.get(key, {}).get("volume"),
                "status": ref["status"] if ref else "UNKNOWN",
                "last_verified_at": ref["last_verified_at"].isoformat() if ref and ref["last_verified_at"] else None,
                "last_error": ref["last_error"] if ref else None,
            })
        providers = []
        for name, adapter in ADAPTERS.items():
            default = next((i for i in identities if i["provider"] == name and i["identity"] == "default"), None)
            health = adapter.health_check(pinned_images=pinned, credential_present=bool(default and default["volume"]),
                                          credential_status=default["status"] if default else "UNKNOWN")
            providers.append(health.__dict__)
        return {"providers": providers, "identities": identities}

    def mark_ready(self, uow: UnitOfWork, provider: str, identity: str, *, principal: Principal) -> dict[str, Any]:
        """Operator confirms a new login; waiting tasks resume and their interrupted work continues."""
        if provider not in ADAPTERS or not _IDENTITY.match(identity):
            raise BadRequest("unknown provider or invalid identity")
        if not any(v["provider"] == provider and v["identity"] == identity for v in self._volumes()):
            raise Conflict(f"no credential volume for {provider}/{identity}; run `make auth-{provider} IDENTITY={identity}` first")
        self.executions._set_credential(uow, provider, identity, "READY", None)
        record_event(uow.cur, "CREDENTIAL_READY", actor=principal.value, summary=f"{provider}/{identity} login renewed",
                     data={"provider": provider, "identity": identity}, pending=uow.events)

        uow.cur.execute("SELECT * FROM tasks WHERE state = 'AUTH_REQUIRED' AND waiting_on_credential = %s FOR UPDATE",
                        (f"{provider}/{identity}",))
        resumed, continued = [], []
        for task in uow.cur.fetchall():
            self.tasks.transition(uow, task, TaskState(task["resume_state"]), trigger=Trigger.SYSTEM,
                                  actor=principal.value, reason=f"{provider}/{identity} login renewed")
            uow.cur.execute("UPDATE tasks SET waiting_on_credential = NULL WHERE id = %s", (task["id"],))
            resumed.append(task["key"])
            # Continue each interrupted agent execution once (resume_of marks it as continued).
            uow.cur.execute(
                """
                SELECT e.* FROM executions e
                WHERE e.task_id = %s AND e.provider = %s AND COALESCE(e.provider_identity, 'default') = %s
                  AND e.state = 'FAILED' AND e.failure_class = 'AUTH' AND e.agent_run
                  AND NOT EXISTS (SELECT 1 FROM executions n WHERE n.resume_of = e.id)
                ORDER BY e.created_at
                """,
                (task["id"], provider, identity),
            )
            for row in uow.cur.fetchall():
                new = self.executions.retry_after_auth(uow, row)
                if new is not None:
                    continued.append({"failed": str(row["id"]), "continued_by": str(new["id"]),
                                      "resumed_session": bool(row["provider_session_id"])})
        return {"provider": provider, "identity": identity, "status": "READY", "tasks_resumed": resumed,
                "executions_continued": continued}
