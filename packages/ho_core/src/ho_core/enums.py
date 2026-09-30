"""Closed vocabularies shared by services, schemas, and the Hermes plugin.

Values match schemas/*.schema.json and DATA_MODEL.md. Changing a value is a
schema change and needs a migration.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum that serializes as its value (3.11-compatible)."""

    def __str__(self) -> str:
        return self.value


class Priority(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Lower rank is scheduled first."""
        return _PRIORITY_RANK[self]


_PRIORITY_RANK = {Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.NORMAL: 2, Priority.LOW: 3}


class TaskState(StrEnum):
    BACKLOG = "BACKLOG"
    READY = "READY"
    PLANNING = "PLANNING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    TESTING = "TESTING"
    REVIEW = "REVIEW"
    FIX_REQUIRED = "FIX_REQUIRED"
    QUALITY_GATE = "QUALITY_GATE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PAUSED = "PAUSED"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    READY_FOR_MERGE = "READY_FOR_MERGE"
    MERGING = "MERGING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class ProjectStatus(StrEnum):
    REGISTERED = "REGISTERED"
    SCANNING = "SCANNING"
    PROPOSED = "PROPOSED"
    PROJECT_READY = "PROJECT_READY"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    SUSPENDED = "SUSPENDED"
    UNREGISTERED = "UNREGISTERED"


class ConfigStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Autonomy(StrEnum):
    SUPERVISED = "SUPERVISED"
    BALANCED = "BALANCED"
    AUTONOMOUS = "AUTONOMOUS"


class BudgetProfile(StrEnum):
    SMALL = "SMALL"
    NORMAL = "NORMAL"
    LARGE = "LARGE"
    UNLIMITED = "UNLIMITED"


class ApprovalAction(StrEnum):
    MERGE = "MERGE"
    SCOPE_EXPANSION = "SCOPE_EXPANSION"
    BUDGET_INCREASE = "BUDGET_INCREASE"
    BUDGET_UNLIMITED = "BUDGET_UNLIMITED"
    HIGH_RISK_OPERATION = "HIGH_RISK_OPERATION"
    ENVIRONMENT_ACCESS = "ENVIRONMENT_ACCESS"
    PROJECT_CONFIG_CHANGE = "PROJECT_CONFIG_CHANGE"
    ASSUMPTION = "ASSUMPTION"
    UPDATE = "UPDATE"
    PROJECT_READY = "PROJECT_READY"


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    CONSUMED = "CONSUMED"


class CommandClass(StrEnum):
    SAFE = "SAFE"
    CONTROLLED = "CONTROLLED"
    HIGH_RISK = "HIGH_RISK"


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class RelationshipKind(StrEnum):
    DUPLICATE = "DUPLICATE"
    RELATED = "RELATED"
    DEPENDENCY = "DEPENDENCY"
    CONFLICTING = "CONFLICTING"
    INDEPENDENT = "INDEPENDENT"


class Role(StrEnum):
    ORCHESTRATOR = "ORCHESTRATOR"
    DEVELOPER = "DEVELOPER"
    REVIEWER = "REVIEWER"
    TESTER = "TESTER"
    BROWSER = "BROWSER"


class Environment(StrEnum):
    LOCAL = "LOCAL"
    TEST = "TEST"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"


class EnvironmentAccess(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
