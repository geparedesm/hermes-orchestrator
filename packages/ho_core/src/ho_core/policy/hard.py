"""Immutable hard security policies (MASTER_SPEC sections 23, 26, 41, 91).

Nothing in project configuration, machine-local configuration, task overrides,
or autonomy profiles can relax these rules.
"""

from __future__ import annotations

from typing import Any

from ..enums import ApprovalAction

Json = dict[str, Any]

PROTECTED_BRANCHES = frozenset({"main", "master"})
ALWAYS_BLOCKING_FINDINGS = frozenset({"HIGH", "CRITICAL"})

# Actions that always need an explicit, action-bound human approval
# regardless of autonomy profile (SECURITY_MODEL.md section 6.4).
ALWAYS_REQUIRE_APPROVAL = frozenset(
    {
        ApprovalAction.MERGE,
        ApprovalAction.BUDGET_UNLIMITED,
        ApprovalAction.HIGH_RISK_OPERATION,
        ApprovalAction.ENVIRONMENT_ACCESS,
        ApprovalAction.PROJECT_READY,
        ApprovalAction.UPDATE,
    }
)

# Capability values no grant may ever contain.
FORBIDDEN_DOCKER_ACCESS = "NONE"  # the only permitted value


def clamp_project_config(config: Json, *, platform: Json, default_branch: str | None) -> list[str]:
    """Apply hard policy to a merged project configuration in place.

    Returns human-readable descriptions of every adjustment.
    """
    notes: list[str] = []

    git = config.setdefault("git", {})
    required = set(PROTECTED_BRANCHES)
    if default_branch:
        required.add(default_branch)
    current = set(git.get("protected_branches", []))
    if not required <= current:
        notes.append(f"git.protected_branches: added {sorted(required - current)} (hard policy)")
    git["protected_branches"] = sorted(current | required)

    gate = config.setdefault("quality_gate", {})
    findings = set(gate.get("block_on_findings", []))
    if not ALWAYS_BLOCKING_FINDINGS <= findings:
        notes.append("quality_gate.block_on_findings: HIGH and CRITICAL always block (hard policy)")
    gate["block_on_findings"] = sorted(findings | ALWAYS_BLOCKING_FINDINGS, key=["MEDIUM", "HIGH", "CRITICAL"].index)

    resources = config.setdefault("resources", {})
    machine_cap = int(platform["machine"]["max_agent_workers"])
    project_cap = resources.get("max_agent_workers")
    if project_cap is None or project_cap > machine_cap:
        if project_cap is not None:
            notes.append(f"resources.max_agent_workers: {project_cap} exceeds machine cap {machine_cap}")
        resources["max_agent_workers"] = machine_cap

    order = ["LIGHT", "NORMAL", "HEAVY"]
    max_profile = resources.get("max_profile", "HEAVY")
    default_profile = resources.get("default_profile", "NORMAL")
    if order.index(default_profile) > order.index(max_profile):
        notes.append(f"resources.default_profile: lowered {default_profile} to max_profile {max_profile}")
        resources["default_profile"] = max_profile

    return notes
