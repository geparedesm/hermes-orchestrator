"""Policy Engine decisions (MASTER_SPEC sections 12, 22-26; SECURITY_MODEL.md sections 6-8, 11).

Pure functions: callers persist each decision as a `policy_decisions` row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from .. import schemas
from ..enums import (
    ApprovalAction,
    Autonomy,
    CommandClass,
    Decision,
    Environment,
    EnvironmentAccess,
    Risk,
    Role,
)
from . import hard
from .commands import classify

Json = dict[str, Any]


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    rule_ids: tuple[str, ...]
    summary: str
    command_class: CommandClass | None = None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

# High-risk commands that no approval can unlock from inside an execution:
# container control, credential access, and merges belong to trusted services.
_HARD_DENIED_RULES = frozenset({"CMD-H04", "CMD-H07", "CMD-H11"})
# Pushing is a Git Service action, never an execution command.
_DENIED_CONTROLLED_RULES = frozenset({"CMD-C05"})


def decide_command(command: str, *, autonomy: Autonomy) -> PolicyDecision:
    result = classify(command)
    rules = (result.rule_id,)
    if result.command_class == CommandClass.HIGH_RISK:
        if result.rule_id in _HARD_DENIED_RULES:
            return PolicyDecision(Decision.DENY, rules + ("HP-EXEC",), result.summary, result.command_class)
        return PolicyDecision(Decision.REQUIRE_APPROVAL, rules, result.summary, result.command_class)
    if result.command_class == CommandClass.CONTROLLED:
        if result.rule_id in _DENIED_CONTROLLED_RULES:
            return PolicyDecision(Decision.DENY, rules + ("HP-GIT",), result.summary, result.command_class)
        if autonomy == Autonomy.SUPERVISED:
            return PolicyDecision(Decision.REQUIRE_APPROVAL, rules + ("AUT-SUP",), result.summary, result.command_class)
        return PolicyDecision(Decision.ALLOW, rules, result.summary, result.command_class)
    return PolicyDecision(Decision.ALLOW, rules, result.summary, result.command_class)


# --------------------------------------------------------------------------
# Control-plane actions
# --------------------------------------------------------------------------


def decide_action(action: ApprovalAction, *, autonomy: Autonomy, risk: Risk = Risk.LOW) -> PolicyDecision:
    if action in hard.ALWAYS_REQUIRE_APPROVAL:
        return PolicyDecision(Decision.REQUIRE_APPROVAL, ("HP-APPROVAL",), f"{action} always requires approval")
    if action == ApprovalAction.ASSUMPTION:
        if risk in (Risk.HIGH, Risk.CRITICAL):
            return PolicyDecision(Decision.REQUIRE_APPROVAL, ("AMB-HIGH",), "High or irreversible ambiguity")
        return PolicyDecision(Decision.ALLOW, ("AMB-LOW",), "Record assumption and continue")
    if action in (ApprovalAction.SCOPE_EXPANSION, ApprovalAction.BUDGET_INCREASE, ApprovalAction.PROJECT_CONFIG_CHANGE):
        if autonomy == Autonomy.AUTONOMOUS and risk == Risk.LOW and action != ApprovalAction.PROJECT_CONFIG_CHANGE:
            return PolicyDecision(Decision.ALLOW, ("AUT-AUTO",), "Low-risk change within autonomous profile")
        return PolicyDecision(Decision.REQUIRE_APPROVAL, ("APR-SCOPE",), f"{action} requires approval")
    return PolicyDecision(Decision.REQUIRE_APPROVAL, ("APR-DEFAULT",), f"{action} requires approval")


# --------------------------------------------------------------------------
# Environments
# --------------------------------------------------------------------------


def decide_environment_access(
    environment: Environment, access: EnvironmentAccess, *, approved: bool
) -> PolicyDecision:
    if environment in (Environment.LOCAL, Environment.TEST):
        return PolicyDecision(Decision.ALLOW, ("ENV-LOCAL",), "Local and test access within grant")
    if environment == Environment.STAGING and access == EnvironmentAccess.READ:
        return PolicyDecision(Decision.ALLOW, ("ENV-STG-R",), "Staging is read-only by default")
    rule = "ENV-STG-W" if environment == Environment.STAGING else f"ENV-PROD-{access.value[0]}"
    if approved:
        return PolicyDecision(Decision.ALLOW, (rule, "APR-OK"), "Approved, time-limited, audited")
    return PolicyDecision(Decision.REQUIRE_APPROVAL, (rule,), f"{environment} {access} requires approval")


# --------------------------------------------------------------------------
# Capability grants
# --------------------------------------------------------------------------

_LEVELS = {
    "workspace": ["NONE", "READ", "WRITE"],
    "git": ["NONE", "READ", "LOCAL_COMMIT"],
    "egress": ["NONE", "PROVIDER_ONLY", "ALLOWLIST", "STANDARD"],
    "tests": ["NONE", "EXECUTE"],
    "artifacts": ["NONE", "READ", "WRITE"],
    "production": ["NONE", "PROD_READ", "PROD_WRITE"],
    "resources": ["LIGHT", "NORMAL", "HEAVY"],
}

# Maximum capabilities per role (SECURITY_MODEL.md section 8.2).
ROLE_MAXIMUM: dict[Role, Json] = {
    Role.ORCHESTRATOR: {"workspace": "NONE", "git": "READ", "egress": "STANDARD", "test_services": False,
                        "tests": "NONE", "artifacts": "READ", "secrets": False, "production": "NONE"},
    Role.DEVELOPER: {"workspace": "WRITE", "git": "LOCAL_COMMIT", "egress": "STANDARD", "test_services": True,
                     "tests": "EXECUTE", "artifacts": "WRITE", "secrets": True, "production": "PROD_WRITE"},
    Role.REVIEWER: {"workspace": "READ", "git": "READ", "egress": "ALLOWLIST", "test_services": True,
                    "tests": "EXECUTE", "artifacts": "READ", "secrets": False, "production": "NONE"},
    Role.TESTER: {"workspace": "WRITE", "git": "NONE", "egress": "NONE", "test_services": True,
                  "tests": "EXECUTE", "artifacts": "WRITE", "secrets": True, "production": "NONE"},
    Role.BROWSER: {"workspace": "NONE", "git": "NONE", "egress": "NONE", "test_services": True,
                   "tests": "EXECUTE", "artifacts": "WRITE", "secrets": True, "production": "NONE"},
}

_PROJECT_EGRESS = {"standard": "STANDARD", "restricted": "ALLOWLIST", "provider_only": "PROVIDER_ONLY"}
_AGENT_ROLES = (Role.ORCHESTRATOR, Role.DEVELOPER, Role.REVIEWER)


def _min(kind: str, *values: str) -> str:
    return min(values, key=_LEVELS[kind].index)


@dataclass(frozen=True)
class GrantApproval:
    """An approval already validated by the Approval Service for this task."""

    approval_id: str
    production: str = "NONE"  # PROD_READ or PROD_WRITE
    staging_write: bool = False


@dataclass
class GrantRequest:
    grant_id: str
    project: str
    task: str
    execution: str
    worker: str
    role: Role
    provider: str | None
    provider_identity: str = "default"
    subtask: str | None = None
    workspace: str = "NONE"
    git: str = "NONE"
    egress: str = "NONE"
    test_services: bool = False
    allowed_domains: Sequence[str] = ()
    secrets: Sequence[str] = ()  # NAME entries from project configuration
    environments: Sequence[str] = ()
    production: str = "NONE"
    tests: str = "NONE"
    artifacts: str = "NONE"
    project_read: Sequence[str] = ()
    resource_profile: str = "NORMAL"
    timeout_minutes: int = 60
    lease_epoch: int = 1
    approvals: Sequence[GrantApproval] = field(default_factory=tuple)


def evaluate_grant(request: GrantRequest, config: Json, platform: Json, *, now: datetime) -> tuple[Json, list[str]]:
    """Grant the intersection of the request, role maximum, project policy, and hard policy.

    Returns the grant (validated against capability.schema.json) and a list of
    reductions applied, for the policy decision record.
    """
    notes: list[str] = []
    role_max = ROLE_MAXIMUM[request.role]

    def reduce(kind: str, key: str, requested: str, *limits: str) -> str:
        granted = _min(kind, requested, *limits)
        if granted != requested:
            notes.append(f"{key}: {requested} reduced to {granted}")
        return granted

    network = config.get("network", {})
    project_egress = _PROJECT_EGRESS[network.get("development", "standard")]
    egress_limits = [role_max["egress"]]
    if request.role in _AGENT_ROLES:
        egress_limits.append(project_egress)
    egress = reduce("egress", "network.egress", request.egress, *egress_limits)
    if request.role in _AGENT_ROLES and egress == "NONE":
        egress = "PROVIDER_ONLY"  # agent CLIs cannot work without their provider API
        notes.append("network.egress: raised to PROVIDER_ONLY for provider access")

    domains: list[str] = []
    if egress == "ALLOWLIST":
        allowed = set(network.get("allowed_domains", []))
        domains = sorted(set(request.allowed_domains) & allowed)
        dropped = set(request.allowed_domains) - allowed
        if dropped:
            notes.append(f"network.allowed_domains: dropped {sorted(dropped)} not in project policy")

    staging_write = any(a.staging_write for a in request.approvals)
    environments = []
    for env in request.environments:
        if env in ("LOCAL", "TEST", "STAGING_READ") or (env == "STAGING_WRITE" and staging_write):
            environments.append(env)
        else:
            notes.append(f"environments: {env} denied without approval")

    approved_production = "NONE"
    for approval in request.approvals:
        approved_production = max(approved_production, approval.production, key=_LEVELS["production"].index)
    production = reduce("production", "production", request.production, role_max["production"], approved_production)

    secrets: list[str] = []
    project_secrets = {s["name"]: s["environment"] for s in config.get("secrets", [])}
    allowed_secret_envs = {"local", "test"}
    if "STAGING_READ" in environments or "STAGING_WRITE" in environments:
        allowed_secret_envs.add("staging")
    if production != "NONE":
        allowed_secret_envs.add("production")
    for name in request.secrets:
        env = project_secrets.get(name)
        if not role_max["secrets"] or env is None or env not in allowed_secret_envs:
            notes.append(f"secrets: {name} denied")
            continue
        secrets.append(f"{request.project}/{env}/{name}")

    resources_cfg = config.get("resources", {})
    profile = reduce("resources", "resources.profile", request.resource_profile, resources_cfg.get("max_profile", "HEAVY"))
    max_timeout = int(platform["machine"].get("max_execution_minutes", 240))
    timeout = min(request.timeout_minutes, max_timeout)
    if timeout != request.timeout_minutes:
        notes.append(f"resources.timeout_minutes: capped at {max_timeout}")

    project_read = sorted(set(request.project_read)) if request.role == Role.ORCHESTRATOR else []
    if request.project_read and request.role != Role.ORCHESTRATOR:
        notes.append("project_read: only the orchestrator may read projects")

    approval_ids = sorted({a.approval_id for a in request.approvals})
    grant: Json = {
        "schema_version": 1,
        "grant_id": request.grant_id,
        "project": request.project,
        "task": request.task,
        "execution": request.execution,
        "worker": request.worker,
        "role": request.role.value,
        "provider_credential": (
            {"provider": request.provider, "identity": request.provider_identity}
            if request.role in _AGENT_ROLES and request.provider
            else None
        ),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=timeout)).isoformat(),
        "lease_epoch": request.lease_epoch,
        "capabilities": {
            "workspace": reduce("workspace", "workspace", request.workspace, role_max["workspace"]),
            "git": reduce("git", "git", request.git, role_max["git"]),
            "network": {
                "egress": egress,
                "test_services": bool(request.test_services and role_max["test_services"]),
                **({"allowed_domains": domains} if domains else {}),
            },
            "secrets": secrets,
            "environments": environments,
            "docker": hard.FORBIDDEN_DOCKER_ACCESS,
            "production": production,
            "tests": reduce("tests", "tests", request.tests, role_max["tests"]),
            "artifacts": reduce("artifacts", "artifacts", request.artifacts, role_max["artifacts"]),
            "project_read": project_read,
        },
        "resources": {"profile": profile, "timeout_minutes": timeout},
    }
    if request.subtask:
        grant["subtask"] = request.subtask
    if approval_ids:
        grant["approvals"] = approval_ids
    schemas.validate("capability", grant)
    return grant, notes
