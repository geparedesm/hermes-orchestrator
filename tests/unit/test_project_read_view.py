"""The orchestrator reads a project only through a Git Service read view, never the live directory."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_manager.plan import Rejected, build_plan
from ho_core.config import build_project_config, load_platform_config
from ho_core.enums import Role
from ho_core.policy.engine import GrantRequest, evaluate_grant

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40


@pytest.fixture
def platform():
    return load_platform_config(ROOT / "config", "mac-m2-pro")


def orchestrator(platform, path):
    execution = str(uuid.uuid4())
    config = build_project_config(platform, {"version": 1, "project": {"name": "demo"}}).data
    grant, _ = evaluate_grant(GrantRequest(grant_id=f"G-{execution[:8]}", project="demo", task="T-1", execution=execution,
                                           worker="w-1-a", role=Role.ORCHESTRATOR, provider="claude", project_read=["demo"]),
                              config, platform, now=datetime.now(timezone.utc))
    return {"execution_id": execution, "task": "T-1", "project": "demo", "role": "ORCHESTRATOR", "project_path": "demo",
            "image": "claude-generic", "command": ["true"], "grant": grant, "project_read": [{"slug": "demo", "path": path}]}


def repo(root: Path, relative: str) -> Path:
    project = root / relative
    (project / ".git").mkdir(parents=True)
    (project / ".hermes" / "read" / SHA).mkdir(parents=True)
    return project


def mounts(platform, root, path):
    result = build_plan(orchestrator(platform, path), platform=platform, projects_root=root, projects_root_host="/host/projects")
    return [m for m in result.mounts if m.target == "/projects/demo"]


def test_only_the_read_view_is_mounted_read_only(platform, tmp_path):
    repo(tmp_path, "demo")
    [mount] = mounts(platform, tmp_path, f"demo/.hermes/read/{SHA}")
    assert mount.source == f"/host/projects/demo/.hermes/read/{SHA}" and mount.read_only


def test_nested_projects_are_readable_through_their_view(platform, tmp_path):
    repo(tmp_path, "odoo/custom-addons/job")
    [mount] = mounts(platform, tmp_path, f"odoo/custom-addons/job/.hermes/read/{SHA}")
    assert mount.source.endswith(f"odoo/custom-addons/job/.hermes/read/{SHA}")


@pytest.mark.parametrize("path", ["demo", "demo/.hermes/read", "demo/.hermes/worktrees/" + SHA, f"demo/.hermes/read/{SHA[:-1]}",
                                  f"../demo/.hermes/read/{SHA}", f"/demo/.hermes/read/{SHA}", f"other/.hermes/read/{SHA}",
                                  f"demo/../demo/.hermes/read/{SHA}"])
def test_anything_but_a_read_view_is_refused(platform, tmp_path, path):
    repo(tmp_path, "demo")
    with pytest.raises(Rejected):
        mounts(platform, tmp_path, path)


def test_a_symbolic_link_on_the_way_is_refused(platform, tmp_path):
    project = repo(tmp_path, "demo")
    (tmp_path / "elsewhere" / SHA).mkdir(parents=True)
    other = tmp_path / "demo2"
    (other / ".git").mkdir(parents=True)
    (other / ".hermes").symlink_to(tmp_path / "elsewhere-hermes")
    (tmp_path / "elsewhere-hermes" / "read").mkdir(parents=True)
    (tmp_path / "elsewhere-hermes" / "read" / SHA).mkdir()
    with pytest.raises(Rejected):
        mounts(platform, tmp_path, f"demo2/.hermes/read/{SHA}")
    assert project.exists()
