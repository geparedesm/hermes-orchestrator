"""The project's test services for agents that run tests (docs/design/child-projects.md, T-50).

Verification always had them; developers and reviewers, whose project rules may require running the tests before
they finish (an Odoo module's `ho-odoo-test` needs its PostgreSQL), get the same task environment: started before
their worker is dispatched, and joined through the `test_services` capability the policy already allows them.
"""

from __future__ import annotations

import logging
from typing import Any

from .agentmgr import AgentManagerError
from .context import Context, UnitOfWork
from .db import Row
from .events import record_event

log = logging.getLogger(__name__)


def configured(config: dict[str, Any]) -> bool:
    test_env = config.get("test_environment") or {}
    return bool(test_env.get("compose_files") or test_env.get("services"))


def ensure_before_dispatch(ctx: Context, uow: UnitOfWork, task: Row, project: Row, config: dict[str, Any], workspace: str) -> bool:
    """Start the task's test services after this transaction commits, before the execution it requests is dispatched.
    Returns whether the project has test services (the caller then asks for the `test_services` capability)."""
    if not configured(config) or ctx.agents is None:
        return False
    test_env = config.get("test_environment") or {}
    body = {"project": project["slug"], "project_path": project["relative_path"],
            "workspace": f"{project['relative_path']}/{workspace}", "compose_files": test_env.get("compose_files", []),
            "services": test_env.get("services", []), "startup_timeout": int(test_env.get("startup_timeout_seconds", 180))}

    def start(task_key: str = task["key"]) -> None:
        try:
            ctx.agents.start_environment(task_key, body)  # type: ignore[union-attr]
        except AgentManagerError as exc:
            log.warning("test services of %s did not start: %s", task_key, exc.message)
            with ctx.unit_of_work() as later:
                record_event(later.cur, "TEST_SERVICES_UNAVAILABLE", actor="control-plane", project_id=task["project_id"],
                             task_id=task["id"], summary=f"test services did not start: {exc.message}"[:300], pending=later.events)
    uow.after_commit.insert(0, start)  # before the dispatch the execution request registers
    return True
