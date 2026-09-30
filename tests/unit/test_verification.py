from __future__ import annotations

import pytest

from ho_core.enums import Risk
from ho_core.verification import assess_risk, is_test_file, plan_verification, post_merge_steps

COMMANDS = {"install": "npm ci", "build": "npm run build", "lint": "npm run lint", "typecheck": "tsc --noEmit",
            "test": "npm test", "security": "npm audit"}


@pytest.mark.parametrize("path,area", [
    ("src/auth/login.ts", "authentication"), ("app/permissions.py", "authorization"), ("billing/stripe.py", "payments"),
    ("db/migrations/0003_add.sql", "db_migrations"), ("Dockerfile", "docker"), ("compose.prod.yaml", "docker"),
    ("infra/main.tf", "infrastructure"), (".github/workflows/ci.yml", "ci_cd"), (".env.production", "secrets"),
    ("package.json", "dependencies"), ("requirements-dev.txt", "dependencies"), ("deploy/nginx.conf", "network"),
])
def test_sensitive_areas(path, area):
    assessment = assess_risk([path])
    assert assessment.risk == Risk.HIGH and area in assessment.areas


def test_risk_levels():
    assert assess_risk(["README.md", "docs/guide.md"]).risk == Risk.LOW
    assert assess_risk(["src/a.py"], insertions=20).risk == Risk.LOW
    assert assess_risk([f"src/m{i}.py" for i in range(8)], insertions=300).risk == Risk.MEDIUM
    assert assess_risk(["src/a.py"], sensitive_paths=["src/*"]).risk == Risk.HIGH
    assert assess_risk(["src/a.py", "README.md"], critical_paths=["src/a.py"]).risk == Risk.CRITICAL


def test_test_file_detection():
    for path in ("tests/test_a.py", "src/a.test.ts", "pkg/a_test.go", "src/test/java/AppTest.java", "spec/a_spec.rb"):
        assert is_test_file(path), path
    assert not is_test_file("src/app.py")


def config(**gate):
    return {"commands": COMMANDS, "quality_gate": {"tests": True, "build": True, "lint": False, **gate}}


def names(plan):
    return {r.name for r in plan.requirements}


def test_requirements_grow_with_risk():
    low = plan_verification(config(), ["src/a.py", "tests/test_a.py"], insertions=10)
    assert low.risk.risk == Risk.LOW and {"tests", "build", "cross_review", "requirements", "no_conflicts"} <= names(low)
    assert "lint" not in names(low) and "security" not in names(low)
    assert [s[0] for s in low.steps] == ["install", "build", "test"]

    medium = plan_verification(config(), [f"src/m{i}.py" for i in range(9)] + ["tests/t.py"], insertions=500)
    assert {"lint", "typecheck"} <= names(medium) and "security" not in names(medium)
    high = plan_verification(config(), ["src/auth/login.py", "tests/test_login.py"])
    assert {"lint", "typecheck", "security"} <= names(high)
    assert [s[0] for s in high.steps] == ["install", "build", "lint", "typecheck", "test", "security"]
    assert not high.needs_approval
    critical = plan_verification({**config(), "verification": {"critical_paths": ["src/core/*"]}}, ["src/core/x.py", "tests/t.py"])
    assert critical.needs_approval == ["critical change (section 57)"]


def test_hard_policy_requirements_cannot_be_disabled():
    plan = plan_verification({"commands": COMMANDS, "quality_gate": {"tests": False, "full_suite": False, "build": False,
                                                                      "lint": False, "ci_checks": False}}, ["README.md"])
    assert {"cross_review", "requirements", "no_blocking_findings", "no_conflicts", "no_policy_violations"} <= names(plan)
    assert "tests" not in names(plan)


def test_test_gaps():
    no_tests = plan_verification({"commands": {}, "quality_gate": {}}, ["src/a.py"])
    assert {g["kind"] for g in no_tests.gaps} >= {"no_test_command", "no_test_changes"}
    sensitive = plan_verification(config(), ["src/auth/login.py"])  # no test changed on a HIGH-risk change
    assert sensitive.needs_approval == ["sensitive change with inadequate tests (section 56)"]
    docs = plan_verification(config(), ["README.md"])
    assert docs.gaps == []  # documentation-only changes need no tests


def test_ci_and_browser_requirements():
    browser = plan_verification({**config(browser_tests=True), "browser_tests": {"enabled": True, "base_url": "http://app:3000"}},
                                ["src/a.py", "tests/a.py"], github=True)
    assert browser.browser and {"browser", "ci_checks"} <= names(browser)
    assert not plan_verification(config(browser_tests=True), ["src/a.py"]).browser  # needs browser_tests.enabled + base_url


def test_post_merge_steps():
    assert post_merge_steps({"commands": COMMANDS}) == [("install", "npm ci"), ("build", "npm run build"), ("test", "npm test")]
    assert post_merge_steps({"commands": {"post_merge": "make verify"}}) == [("post_merge", "make verify")]
    assert post_merge_steps({}) == []
