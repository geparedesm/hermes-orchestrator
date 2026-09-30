"""Layered configuration (ARCHITECTURE.md section 9).

Platform configuration:  config/defaults.yaml  +  config/<machine>.yaml
Project configuration:   platform project_defaults
                         -> .hermes/project.yaml         (may override freely, within hard policy)
                         -> .hermes.local.yaml           (may only tighten security-relevant fields)
                         -> task override                (may only tighten security-relevant fields)
                         -> hard policy clamp            (ho_core.policy.hard)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import schemas
from .hashing import hash_value
from .policy import hard

Json = dict[str, Any]


class ConfigError(ValueError):
    pass


def load_yaml(path: Path) -> Json:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def deep_merge(base: Json, override: Json) -> Json:
    """Return base with override applied; mappings merge, everything else replaces."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# --------------------------------------------------------------------------
# Platform configuration
# --------------------------------------------------------------------------


LOCAL_PLATFORM_FILE = "local.yaml"  # untracked, machine-specific (for example projects_root_host)


def load_platform_config(config_dir: Path, machine_profile: str) -> Json:
    """defaults.yaml + <machine_profile>.yaml + optional untracked local.yaml, validated."""
    if machine_profile in ("defaults", "local"):
        raise ConfigError(f"{machine_profile!r} is not a machine profile")
    defaults = load_yaml(config_dir / "defaults.yaml")
    machine_file = config_dir / f"{machine_profile}.yaml"
    if not machine_file.is_file():
        raise ConfigError(f"unknown machine profile {machine_profile!r} (no {machine_file.name})")
    merged = deep_merge(defaults, load_yaml(machine_file))
    local_file = config_dir / LOCAL_PLATFORM_FILE
    if local_file.is_file():
        merged = deep_merge(merged, load_yaml(local_file))
    schemas.validate("platform", merged)
    return merged


# --------------------------------------------------------------------------
# Project configuration
# --------------------------------------------------------------------------

_AUTONOMY = {"AUTONOMOUS": 0, "BALANCED": 1, "SUPERVISED": 2}
_DEV_NETWORK = {"standard": 0, "restricted": 1, "provider_only": 2}
_TEST_NETWORK = {"allowlist": 0, "isolated": 1}
_BUDGET = {"UNLIMITED": 0, "LARGE": 1, "NORMAL": 2, "SMALL": 3}
_RESOURCE = {"HEAVY": 0, "NORMAL": 1, "LIGHT": 2}


def _stricter(order: dict[str, int]) -> Callable[[Any, Any], Any]:
    def combine(current: Any, new: Any) -> Any:
        if current is None:
            return new
        return new if order[new] > order[current] else current

    return combine


def _all_true(current: Any, new: Any) -> Any:  # permissive booleans: tightening means AND
    return new if current is None else bool(current) and bool(new)


def _any_true(current: Any, new: Any) -> Any:  # requirement booleans: tightening means OR
    return new if current is None else bool(current) or bool(new)


def _union(current: Any, new: Any) -> Any:
    return sorted(set(current or []) | set(new))


def _intersection(current: Any, new: Any) -> Any:
    return sorted(set(new) if current is None else set(current) & set(new))


def _minimum(current: Any, new: Any) -> Any:
    return new if current is None else min(current, new)


# Fields that layers after .hermes/project.yaml may only make stricter.
TIGHTEN_ONLY: dict[tuple[str, ...], Callable[[Any, Any], Any]] = {
    ("autonomy",): _stricter(_AUTONOMY),
    ("network", "development"): _stricter(_DEV_NETWORK),
    ("network", "testing"): _stricter(_TEST_NETWORK),
    ("network", "allowed_domains"): _intersection,
    ("network", "test_allowed_domains"): _intersection,
    ("network", "research", "official_docs"): _all_true,
    ("network", "research", "package_registries"): _all_true,
    ("network", "research", "public_github"): _all_true,
    ("quality_gate", "tests"): _any_true,
    ("quality_gate", "full_suite"): _any_true,
    ("quality_gate", "build"): _any_true,
    ("quality_gate", "lint"): _any_true,
    ("quality_gate", "typecheck"): _any_true,
    ("quality_gate", "security_checks"): _any_true,
    ("quality_gate", "browser_tests"): _any_true,
    ("quality_gate", "ci_checks"): _any_true,
    ("quality_gate", "docs_updated"): _any_true,
    ("quality_gate", "block_on_findings"): _union,
    ("budget", "profile"): _stricter(_BUDGET),
    ("budget", "limits", "runtime_minutes"): _minimum,
    ("budget", "limits", "agent_launches"): _minimum,
    ("budget", "limits", "retries"): _minimum,
    ("budget", "limits", "review_cycles"): _minimum,
    ("budget", "limits", "provider_usage_units"): _minimum,
    ("budget", "limits", "subtasks"): _minimum,
    ("expansion", "profile"): _stricter(_BUDGET),
    ("git", "protected_branches"): _union,
    ("git", "require_pull_request"): _any_true,
    ("resources", "max_profile"): _stricter(_RESOURCE),
    ("resources", "max_agent_workers"): _minimum,
    ("config_drift", "auto_update_trivial"): _all_true,
}

# Fields only the version-controlled project file (or platform defaults) may set.
PROJECT_ONLY_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("version",),
    ("project",),
    ("environments",),
    ("secrets",),
    ("agents", "allowed_providers"),
)


def _leaves(data: Json, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    items: list[tuple[tuple[str, ...], Any]] = []
    for key, value in data.items():
        path = prefix + (key,)
        if isinstance(value, dict) and value and path not in TIGHTEN_ONLY:
            items.extend(_leaves(value, path))
        else:
            items.append((path, value))
    return items


def _get(data: Json, path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set(data: Json, path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = copy.deepcopy(value)


@dataclass
class EffectiveProjectConfig:
    data: Json
    hash: str
    policy_version: str
    # Layer keys that were ignored because they would have weakened policy.
    rejected: list[str] = field(default_factory=list)
    # Hard-policy adjustments applied on top of the layers.
    clamped: list[str] = field(default_factory=list)


def _apply_restricted_layer(effective: Json, layer: Json, layer_name: str, rejected: list[str]) -> None:
    for path, value in _leaves(layer):
        dotted = ".".join(path)
        if any(path[: len(p)] == p for p in PROJECT_ONLY_PREFIXES):
            rejected.append(f"{layer_name}: {dotted} can only be set in .hermes/project.yaml")
            continue
        combiner = TIGHTEN_ONLY.get(path)
        if combiner is None:
            _set(effective, path, value)
            continue
        current = _get(effective, path)
        combined = combiner(current, value)
        if combined != value:
            rejected.append(f"{layer_name}: {dotted}={value!r} would weaken {current!r}; kept {combined!r}")
        _set(effective, path, combined)


def build_project_config(
    platform: Json,
    project_yaml: Json,
    *,
    local_yaml: Json | None = None,
    task_override: Json | None = None,
    default_branch: str | None = None,
) -> EffectiveProjectConfig:
    """Merge, tighten, clamp, and validate a project's effective configuration."""
    project_errors = schemas.errors_for("project", project_yaml)
    if project_errors:
        raise schemas.SchemaValidationError("project", project_errors)

    rejected: list[str] = []
    effective = deep_merge(platform.get("project_defaults", {}), project_yaml)
    if local_yaml:
        _apply_restricted_layer(effective, local_yaml, ".hermes.local.yaml", rejected)
    if task_override:
        _apply_restricted_layer(effective, task_override, "task override", rejected)

    clamped = hard.clamp_project_config(effective, platform=platform, default_branch=default_branch)
    schemas.validate("project", effective)
    policy_version = str(platform["platform"]["policy_version"])
    digest = hash_value({"config": effective, "policy_version": policy_version})
    return EffectiveProjectConfig(effective, digest, policy_version, rejected, clamped)


def stricter_autonomy(current: str | None, requested: str | None) -> str | None:
    """The more restrictive of two autonomy profiles (task overrides may only tighten)."""
    if requested is None:
        return current
    return _stricter(_AUTONOMY)(current, requested)
