"""The project's test services for agents that run tests (docs/design/child-projects.md, T-50).

Verification always had them; developers and reviewers, whose project rules may require running the tests before
they finish (an Odoo module's `ho-odoo-test` needs its PostgreSQL), get the same task environment: started before
their worker is dispatched, and joined through the `test_services` capability the policy already allows them.
"""

from __future__ import annotations

from typing import Any

from .context import Context
from .db import Row


def configured(config: dict[str, Any]) -> bool:
    test_env = config.get("test_environment") or {}
    return bool(test_env.get("compose_files") or test_env.get("services"))


def request_body(project: Row, config: dict[str, Any], workspace: str) -> dict[str, Any] | None:
    """What to start for an execution in `workspace`, stored in its spec so dispatch (also after a restart) starts the
    services before the worker; None when the project has no test services."""
    if not configured(config):
        return None
    test_env = config.get("test_environment") or {}
    return {"project": project["slug"], "project_path": project["relative_path"],
            "workspace": f"{project['relative_path']}/{workspace}", "compose_files": test_env.get("compose_files", []),
            "services": test_env.get("services", []), "startup_timeout": int(test_env.get("startup_timeout_seconds", 180)),
            "reuse": True}


def start_for_dispatch(ctx: Context, task_key: str, body: dict[str, Any]) -> None:
    """Called by dispatch before the worker is created; raises AgentManagerError (dispatch retries it)."""
    ctx.agents.start_environment(task_key, body)  # type: ignore[union-attr]
