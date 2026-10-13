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
    for command in ("xargs docker rm", "/usr/bin/docker ps", "sh -c 'docker ps'", "nohup kubectl apply -f x", "docker-compose up",
                    'bash -lc "docker ps"', "env DOCKER_HOST=tcp://host:2375 docker ps", "xargs -I {} docker inspect {}",
                    "timeout 5 docker ps", "sudo -u root docker ps", 'bash -lc "exec docker ps"', 'bash -lc "env X=1 docker ps"',
                    "ls && bash -c 'ls; docker ps'", "if docker ps; then echo yes; fi", "for x in a; do docker ps; done",
                    "env -i docker ps", "timeout 5s docker ps", "bash -c 'X=1 docker ps'", "find . -exec docker ps \\;",
                    "grep docker x; docker ps", "rg --pre=docker pattern .", "git log -p --ext-diff | grep docker"):
        assert classify(command).command_class == CommandClass.HIGH_RISK, command
    for command in ("git log --all --oneline | grep -i docker", "find . -iname 'docker-compose*' | head",
                    'find . -iname "*.cfg" -o -iname "docker-compose*" | head; cat .github/workflows/*.yml 2>/dev/null | head -50; ls -la'):
        assert classify(command).rule_id != "CMD-H07", command  # the word in an argument is not container control
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


def test_only_commands_needing_absent_capabilities_are_provably_blocked():
    """T-55, Codex review: a flagged command is "blocked" only when it needs a tool no worker has."""
    from ho_core.policy.commands import provably_blocked

    for command in ("sudo -u postgres psql -c '\\l'", "ls; sudo service postgresql status 2>&1", "docker ps -a",
                    "docker ps -a 2>/dev/null | head -5"):
        assert provably_blocked(command), command
    for command in ("chmod 777 /workspace/run.sh", "su - postgres", "git push --force", "psql -c 'DROP TABLE x'",
                    "ls -la", "docker ps; chmod 777 x", "docker ps $(chmod 777 /workspace/run.sh)", "sudo ls > /workspace/x",
                    "sudo ls & chmod 777 x", "docker ps `chmod 777 x`", "cat /run/ho-credentials/codex/auth.json /var/run/docker.sock",
                    "chmod 777 /workspace/docker.sock", "pg_isready; sudo -u postgres psql", 'bash -lc "chmod 777 x; docker ps"',
                    "docker ps\nchmod 777 /workspace/run.sh", "docker ps; find /workspace -type f -delete",
                    "docker ps; git restore /workspace/run.sh", "sudo ls; diff --output=/workspace/results a b",
                    "/workspace/docker ps", "sudo ls; file -C x", 'env --split-string="touch /workspace/marker" docker ps',
                    "env X=1 docker ps"):
        assert not provably_blocked(command), command


def test_a_command_is_judged_blocked_on_its_whole_text_before_it_is_cut_short():
    from ho_core.adapters.base import command_event

    event = command_event("COMMAND_STARTED", "docker ps" + " " * 600 + "; chmod 777 /workspace/run.sh")
    assert len(event.data["command"]) == 500 and event.data["blocked"] is False
    assert command_event("COMMAND_STARTED", "sudo -u postgres psql").data["blocked"] is True
    assert command_event("COMMAND_STARTED", "ls -la").data["blocked"] is False  # not flagged at all
