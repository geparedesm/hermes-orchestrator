from datetime import datetime, timezone
from pathlib import Path

import pytest

from ho_core.config import build_project_config, load_platform_config
from ho_core.enums import (
    ApprovalAction,
    Autonomy,
    CommandClass,
    Decision,
    Environment,
    EnvironmentAccess,
    Risk,
    Role,
)
from ho_core.policy.commands import classify
from ho_core.policy.engine import (
    GrantApproval,
    GrantRequest,
    decide_action,
    decide_command,
    decide_environment_access,
    evaluate_grant,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "git diff HEAD~1",
        "git commit -m 'fix'",
        "npm test",
        "npm run lint",
        "pytest -q tests/unit",
        "cargo test",
        "grep -rn TODO src | wc -l",
        "CI=1 npm test",
    ],
)
def test_safe_commands(command):
    assert classify(command).command_class == CommandClass.SAFE


@pytest.mark.parametrize(
    "command",
    ["npm install left-pad", "pip install requests", "curl https://example.com", "git pull", "some-unknown-tool --x",
     "ls $(cat file)", "rm -r build", "export API_URL=http://x"],
)
def test_controlled_commands(command):
    assert classify(command).command_class == CommandClass.CONTROLLED


@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin main",
        "git push origin +main",
        "git push origin --delete main",
        "rm -rf /",
        "rm -rf ~",
        "psql -c 'DROP DATABASE app'",
        "docker run --privileged alpine",
        "cat /var/run/docker.sock",
        "terraform apply -auto-approve",
        "sudo apt-get install x",
        "cat ~/.ssh/id_ed25519",
        "npm test && git push -f",
        "gh pr merge 42 --admin",
    ],
)
def test_high_risk_commands(command):
    assert classify(command).command_class == CommandClass.HIGH_RISK


def test_riskiest_segment_wins():
    assert classify("ls && docker ps").command_class == CommandClass.HIGH_RISK
    assert classify("ls; npm install x").command_class == CommandClass.CONTROLLED


def test_command_decisions_by_autonomy():
    assert decide_command("npm test", autonomy=Autonomy.SUPERVISED).decision == Decision.ALLOW
    assert decide_command("npm install x", autonomy=Autonomy.BALANCED).decision == Decision.ALLOW
    assert decide_command("npm install x", autonomy=Autonomy.SUPERVISED).decision == Decision.REQUIRE_APPROVAL
    assert decide_command("terraform destroy", autonomy=Autonomy.AUTONOMOUS).decision == Decision.REQUIRE_APPROVAL
    assert decide_command("docker ps", autonomy=Autonomy.AUTONOMOUS).decision == Decision.DENY
    assert decide_command("git push origin feature", autonomy=Autonomy.AUTONOMOUS).decision == Decision.DENY


@pytest.mark.parametrize("autonomy", list(Autonomy))
def test_hard_approvals_cannot_be_removed_by_autonomy(autonomy):
    for action in (ApprovalAction.MERGE, ApprovalAction.BUDGET_UNLIMITED, ApprovalAction.HIGH_RISK_OPERATION,
                   ApprovalAction.ENVIRONMENT_ACCESS, ApprovalAction.PROJECT_READY):
        assert decide_action(action, autonomy=autonomy, risk=Risk.LOW).decision == Decision.REQUIRE_APPROVAL


def test_assumption_policy():
    assert decide_action(ApprovalAction.ASSUMPTION, autonomy=Autonomy.BALANCED, risk=Risk.MEDIUM).decision == Decision.ALLOW
    assert decide_action(ApprovalAction.ASSUMPTION, autonomy=Autonomy.AUTONOMOUS, risk=Risk.HIGH).decision == Decision.REQUIRE_APPROVAL


def test_environment_access():
    assert decide_environment_access(Environment.TEST, EnvironmentAccess.WRITE, approved=False).decision == Decision.ALLOW
    assert decide_environment_access(Environment.STAGING, EnvironmentAccess.READ, approved=False).decision == Decision.ALLOW
    assert decide_environment_access(Environment.STAGING, EnvironmentAccess.WRITE, approved=False).decision == Decision.REQUIRE_APPROVAL
    assert decide_environment_access(Environment.PRODUCTION, EnvironmentAccess.READ, approved=False).decision == Decision.REQUIRE_APPROVAL
    assert decide_environment_access(Environment.PRODUCTION, EnvironmentAccess.READ, approved=True).decision == Decision.ALLOW


@pytest.fixture(scope="module")
def platform():
    return load_platform_config(ROOT / "config", "mac-m2-pro")


def config(platform, **extra):
    base = {"version": 1, "project": {"name": "demo"},
            "secrets": [{"name": "TEST_DB", "environment": "test"}, {"name": "PROD_KEY", "environment": "production"}],
            **extra}
    return build_project_config(platform, base).data


def request(role, **kw):
    defaults = dict(grant_id="G-000001-test", project="demo", task="T-1", execution="0192f3a1-7c4e-7a10-9b2d-4f5e6a7b8c9d",
                    worker="w-1-1", role=role, provider="codex")
    return GrantRequest(**{**defaults, **kw})


def test_developer_grant_is_intersection(platform):
    req = request(Role.DEVELOPER, workspace="WRITE", git="LOCAL_COMMIT", egress="STANDARD", test_services=True,
                  secrets=["TEST_DB", "PROD_KEY"], environments=["TEST", "STAGING_WRITE"], production="PROD_WRITE",
                  tests="EXECUTE", artifacts="WRITE", resource_profile="HEAVY", timeout_minutes=9999)
    grant, notes = evaluate_grant(req, config(platform), platform, now=NOW)
    caps = grant["capabilities"]
    assert caps["docker"] == "NONE"
    assert caps["production"] == "NONE"
    assert caps["secrets"] == ["demo/test/TEST_DB"]
    assert caps["environments"] == ["TEST"]
    assert grant["resources"]["timeout_minutes"] == 240
    assert any("PROD_KEY" in n for n in notes)


def test_reviewer_cannot_write(platform):
    req = request(Role.REVIEWER, provider="claude", workspace="WRITE", git="LOCAL_COMMIT", egress="STANDARD", secrets=["TEST_DB"])
    grant, _ = evaluate_grant(req, config(platform), platform, now=NOW)
    caps = grant["capabilities"]
    assert caps["workspace"] == "READ"
    assert caps["git"] == "READ"
    assert caps["network"]["egress"] == "ALLOWLIST"
    assert caps["secrets"] == []


def test_runner_has_no_egress_or_provider(platform):
    req = request(Role.TESTER, provider=None, workspace="WRITE", egress="STANDARD", test_services=True, tests="EXECUTE")
    grant, _ = evaluate_grant(req, config(platform), platform, now=NOW)
    assert grant["provider_credential"] is None
    assert grant["capabilities"]["network"] == {"egress": "NONE", "test_services": True}


def test_project_network_mode_limits_agents(platform):
    cfg = config(platform, network={"development": "provider_only"})
    grant, _ = evaluate_grant(request(Role.DEVELOPER, egress="STANDARD"), cfg, platform, now=NOW)
    assert grant["capabilities"]["network"]["egress"] == "PROVIDER_ONLY"
    grant, _ = evaluate_grant(request(Role.DEVELOPER, egress="NONE"), cfg, platform, now=NOW)
    assert grant["capabilities"]["network"]["egress"] == "PROVIDER_ONLY"


def test_production_needs_matching_approval(platform):
    approval = GrantApproval(approval_id="0192f3a1-7c4e-7a10-9b2d-000000000001", production="PROD_READ")
    req = request(Role.DEVELOPER, production="PROD_WRITE", secrets=["PROD_KEY"], approvals=[approval])
    grant, _ = evaluate_grant(req, config(platform), platform, now=NOW)
    assert grant["capabilities"]["production"] == "PROD_READ"
    assert grant["capabilities"]["secrets"] == ["demo/production/PROD_KEY"]
    assert grant["approvals"] == [approval.approval_id]


def test_only_orchestrator_reads_projects(platform):
    grant, _ = evaluate_grant(request(Role.ORCHESTRATOR, provider="claude", project_read=["demo"], workspace="WRITE"),
                              config(platform), platform, now=NOW)
    assert grant["capabilities"]["project_read"] == ["demo"]
    assert grant["capabilities"]["workspace"] == "NONE"
    grant, notes = evaluate_grant(request(Role.DEVELOPER, project_read=["demo"]), config(platform), platform, now=NOW)
    assert grant["capabilities"]["project_read"] == []
