"""Dependency caches in Agent Manager plans (MASTER_SPEC section 71): per project and ecosystem, only for
executions that write a workspace, pointed to by the package manager's own cache variable."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_manager.plan import build_plan
from ho_core.config import build_project_config, load_platform_config
from ho_core.enums import Role
from ho_core.policy.engine import GrantRequest, evaluate_grant

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def platform():
    return load_platform_config(ROOT / "config", "mac-m2-pro")


def plan_for(platform, tmp_path, *, image, role=Role.DEVELOPER, provider="codex", workspace="WRITE", project="demo"):
    execution = str(uuid.uuid4())
    config = build_project_config(platform, {"version": 1, "project": {"name": project}}).data
    grant, _ = evaluate_grant(GrantRequest(grant_id=f"G-{execution[:8]}", project=project, task="T-1", execution=execution,
                                           worker="w-1-a", role=role, provider=provider, workspace=workspace),
                              config, platform, now=datetime.now(timezone.utc))
    (tmp_path / project / ".hermes" / "worktrees" / "t-1-w1").mkdir(parents=True, exist_ok=True)
    body = {"execution_id": execution, "task": "T-1", "project": project, "role": role.value, "project_path": project,
            "image": image, "command": ["true"], "grant": grant}
    if workspace != "NONE":
        body["workspace"] = f"{project}/.hermes/worktrees/t-1-w1"
    return build_plan(body, platform=platform, projects_root=tmp_path, projects_root_host=str(tmp_path))


def test_caches_follow_the_toolchains_of_the_image(platform, tmp_path):
    plan = plan_for(platform, tmp_path, image="codex-node-python")
    assert sorted(plan.caches) == [("ho-cache-demo-npm", "npm"), ("ho-cache-demo-pip", "pip")]
    assert plan.env["PIP_CACHE_DIR"] == "/cache/pip" and plan.env["npm_config_cache"] == "/cache/npm"
    mounts = {m.target: m for m in plan.mounts}
    assert mounts["/cache/pip"].source == "ho-cache-demo-pip" and not mounts["/cache/pip"].read_only


def test_caches_are_never_shared_between_projects(platform, tmp_path):
    other = plan_for(platform, tmp_path, image="codex-python", project="other")
    assert other.caches == [("ho-cache-other-pip", "pip")]


def test_no_cache_without_workspace_write_or_toolchain(platform, tmp_path):
    assert plan_for(platform, tmp_path, image="codex-generic").caches == []
    assert plan_for(platform, tmp_path, image="claude-python", role=Role.REVIEWER, provider="claude",
                    workspace="READ").caches == []


def test_caches_can_be_disabled(platform, tmp_path):
    disabled = copy.deepcopy(platform)
    disabled["machine"]["dependency_cache"] = {"enabled": False, "max_gb_per_cache": 1}
    assert plan_for(disabled, tmp_path, image="codex-python").caches == []
