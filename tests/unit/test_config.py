from pathlib import Path

import pytest
import yaml

from ho_core.config import ConfigError, build_project_config, load_platform_config
from ho_core.schemas import SchemaValidationError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def platform():
    return load_platform_config(ROOT / "config", "mac-m2-pro")


def project(**extra):
    return {"version": 1, "project": {"name": "demo"}, **extra}


@pytest.mark.parametrize("profile", ["mac-m2-pro", "linux"])
def test_machine_profiles_are_valid(profile):
    config = load_platform_config(ROOT / "config", profile)
    assert config["machine"]["name"] == profile


def test_mac_profile_caps_three_workers(platform):
    assert platform["machine"]["max_agent_workers"] == 3


def test_linux_does_not_inherit_mac_values():
    linux = load_platform_config(ROOT / "config", "linux")
    assert linux["machine"]["max_agent_workers"] == 2
    assert linux["machine"]["platform"] == "linux/amd64"


def test_unknown_profile_rejected():
    with pytest.raises(ConfigError):
        load_platform_config(ROOT / "config", "defaults")
    with pytest.raises(ConfigError):
        load_platform_config(ROOT / "config", "missing")


def test_example_project_builds(platform):
    example = yaml.safe_load((ROOT / "schemas/examples/project.yaml").read_text())
    result = build_project_config(platform, example, default_branch="main")
    assert result.data["autonomy"] == "BALANCED"
    assert {"main", "master", "release"} <= set(result.data["git"]["protected_branches"])
    assert len(result.hash) == 64


def test_hard_policy_clamps_protected_branches_and_findings(platform):
    cfg = project(git={"protected_branches": ["develop"]}, quality_gate={"block_on_findings": ["MEDIUM"]})
    result = build_project_config(platform, cfg, default_branch="trunk")
    assert set(result.data["git"]["protected_branches"]) == {"develop", "main", "master", "trunk"}
    assert result.data["quality_gate"]["block_on_findings"] == ["MEDIUM", "HIGH", "CRITICAL"]
    assert result.clamped


def test_project_worker_cap_limited_by_machine(platform):
    result = build_project_config(platform, project(resources={"max_agent_workers": 10}))
    assert result.data["resources"]["max_agent_workers"] == 3


def test_project_may_relax_defaults_within_hard_policy(platform):
    result = build_project_config(platform, project(quality_gate={"lint": False}))
    assert result.data["quality_gate"]["lint"] is False


def test_local_layer_cannot_weaken(platform):
    cfg = project(autonomy="SUPERVISED", network={"development": "restricted", "allowed_domains": ["docs.example.com"]},
                  quality_gate={"lint": True})
    local = {"autonomy": "AUTONOMOUS", "network": {"development": "standard", "allowed_domains": ["evil.example.net"]},
             "quality_gate": {"lint": False}}
    result = build_project_config(platform, cfg, local_yaml=local)
    assert result.data["autonomy"] == "SUPERVISED"
    assert result.data["network"]["development"] == "restricted"
    assert result.data["network"]["allowed_domains"] == []
    assert result.data["quality_gate"]["lint"] is True
    assert len(result.rejected) == 4


def test_local_layer_can_tighten_and_set_neutral_fields(platform):
    local = {"autonomy": "SUPERVISED", "retention": {"artifacts_days": 7}}
    result = build_project_config(platform, project(), local_yaml=local)
    assert result.data["autonomy"] == "SUPERVISED"
    assert result.data["retention"]["artifacts_days"] == 7
    assert result.rejected == []


def test_local_layer_cannot_add_secrets_or_environments(platform):
    local = {"secrets": [{"name": "PROD_KEY", "environment": "production"}]}
    result = build_project_config(platform, project(), local_yaml=local)
    assert "secrets" not in result.data
    assert result.rejected


def test_task_override_cannot_raise_budget(platform):
    result = build_project_config(platform, project(budget={"profile": "SMALL"}), task_override={"budget": {"profile": "LARGE"}})
    assert result.data["budget"]["profile"] == "SMALL"


def test_invalid_project_yaml_rejected(platform):
    with pytest.raises(SchemaValidationError):
        build_project_config(platform, project(docker="WRITE"))


def test_hash_changes_with_config(platform):
    a = build_project_config(platform, project())
    b = build_project_config(platform, project(autonomy="SUPERVISED"))
    assert a.hash != b.hash
    assert a.hash == build_project_config(platform, project()).hash
