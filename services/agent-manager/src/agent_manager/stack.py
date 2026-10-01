"""Which platform stack this Agent Manager belongs to (several stacks can share one Docker host).

Every resource gets the label `ho.stack`, and listing, reaping, and orphan cleanup only see their own stack's
resources. Task-scoped names (session volumes, task networks, test-service Compose projects) are derived from
the task key, which repeats across stacks, so other stacks prefix them with their name; the main stack
(`hermes-orchestrator`, or unset) keeps the plain names.
"""

from __future__ import annotations

import os

MAIN = "hermes-orchestrator"
NAME = os.environ.get("HO_STACK", "").strip() or MAIN
LABEL = f"ho.stack={NAME}"


def task_slug(task: str) -> str:
    """The part of a task-scoped resource name that identifies the task within this Docker host."""
    return task.lower() if NAME == MAIN else f"{NAME}-{task.lower()}"
