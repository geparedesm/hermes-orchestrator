"""Shared runtime context and the unit-of-work helper."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from psycopg import Cursor

from .agentmgr import AgentManagerClient
from .artifacts import ArtifactStore
from .coordination import Coordinator
from .db import Database, Row
from .gitsvc import GitServiceClient


@dataclass
class UnitOfWork:
    cur: Cursor[Row]
    # Events to publish on Redis after the transaction commits.
    events: list[dict[str, Any]] = field(default_factory=list)
    wake_scheduler: bool = False
    # Side effects on other services, run only after the transaction commits.
    after_commit: list[Callable[[], None]] = field(default_factory=list)


@dataclass
class Context:
    db: Database
    platform: dict[str, Any]
    coordinator: Coordinator
    artifacts: ArtifactStore
    git: GitServiceClient
    agents: AgentManagerClient | None = None
    provider_identity: str = "default"

    @property
    def policy_version(self) -> str:
        return str(self.platform["platform"]["policy_version"])

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        """Transaction plus post-commit side effects (Redis fan-out and wake-ups)."""
        with self.db.transaction() as cur:
            uow = UnitOfWork(cur)
            yield uow
        # Only reached after a successful commit.
        self.coordinator.publish_events(uow.events)
        if uow.wake_scheduler:
            self.coordinator.wake_scheduler("state-change")
        for action in uow.after_commit:
            action()
