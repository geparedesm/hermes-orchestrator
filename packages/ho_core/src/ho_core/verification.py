"""Risk-adaptive verification and the Test Gap Policy (MASTER_SPEC sections 50, 56-58).

Pure functions: the control plane feeds them the changed files of an integrated change and the
project's effective configuration, and gets back the change's risk, the checks that must pass,
the test gaps, and the runner steps to execute.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .enums import Risk

# Sensitive areas (section 57). Patterns match repository-relative paths.
SENSITIVE_AREAS: dict[str, tuple[str, ...]] = {
    "authentication": (r"(^|/)(auth|authentication|login|logout|oauth|oidc|saml|sso|session|sessions|jwt|password)[^/]*(/|\.|$)",),
    "authorization": (r"(^|/)(permission|permissions|acl|rbac|abac|roles?|policy|policies|authz)(/|\.|_|$)",),
    "payments": (r"(^|/)(payment|payments|billing|checkout|invoice|invoices|subscription|stripe|paypal)[^/]*(/|\.|$)",),
    "db_migrations": (r"(^|/)(migrations?|alembic|flyway|liquibase)/", r"(^|/)[^/]*migration[^/]*$", r"\.sql$"),
    "docker": (r"(^|/)(Dockerfile|dockerfile)[^/]*$", r"\.dockerfile$", r"(^|/)(docker-)?compose[^/]*\.ya?ml$", r"(^|/)\.dockerignore$"),
    "infrastructure": (r"(^|/)(terraform|infra|infrastructure|k8s|kubernetes|helm|charts|ansible|pulumi|cdk)/", r"\.(tf|tfvars|hcl)$"),
    "ci_cd": (r"^\.github/workflows/", r"^\.gitlab-ci\.ya?ml$", r"(^|/)Jenkinsfile$", r"^\.circleci/", r"(^|/)azure-pipelines\.ya?ml$",
              r"^\.buildkite/"),
    "secrets": (r"(^|/)\.env(\.[^/]*)?$", r"(^|/)(secrets?|credentials?|keys?)(/|\.|_)", r"\.(pem|key|p12|pfx|jks|keystore)$"),
    "permissions": (r"(^|/)(sudoers|\.htaccess|\.htpasswd)$", r"(^|/)(iam|security)[^/]*\.(ya?ml|json|tf)$"),
    "dependencies": (r"(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|requirements[^/]*\.txt|"
                     r"pyproject\.toml|poetry\.lock|uv\.lock|Pipfile(\.lock)?|setup\.(py|cfg)|go\.(mod|sum)|Cargo\.(toml|lock)|pom\.xml|"
                     r"build\.gradle(\.kts)?|gradle\.properties|composer\.(json|lock)|pubspec\.(yaml|lock)|Gemfile(\.lock)?)$",),
    "network": (r"(^|/)(nginx|haproxy|traefik|envoy|caddy|Caddyfile)[^/]*", r"(^|/)[^/]*(cors|firewall|proxy|ingress)[^/]*\.(ya?ml|json|conf)$"),
}
_COMPILED = {area: tuple(re.compile(p, re.IGNORECASE) for p in patterns) for area, patterns in SENSITIVE_AREAS.items()}
_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing)/|(^|/)test_[^/]+\.py$|_test\.(py|go|rb)$|\.(test|spec)\.[jt]sx?$|"
                        r"(Test|Tests|IT)\.(java|kt)$|_spec\.rb$|_test\.dart$", re.IGNORECASE)
_DOC_FILE = re.compile(r"\.(md|mdx|rst|txt|adoc)$|(^|/)(docs?|documentation)/|(^|/)(README|CHANGELOG|LICENSE|NOTICE)[^/]*$", re.IGNORECASE)
SMALL_FILES, SMALL_LINES = 5, 150

# Order in which runner steps execute.
STEP_ORDER = ("install", "build", "lint", "typecheck", "test", "security")
_ORDER = [Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL]


def _at_least(risk: Risk, minimum: Risk) -> bool:
    return _ORDER.index(risk) >= _ORDER.index(minimum)


def is_test_file(path: str) -> bool:
    return bool(_TEST_FILE.search(path))


def is_doc_file(path: str) -> bool:
    return bool(_DOC_FILE.search(path))


@dataclass
class RiskAssessment:
    risk: Risk
    reasons: list[str]
    areas: dict[str, list[str]]  # sensitive area -> matching paths

    def as_json(self) -> dict[str, Any]:
        return {"risk": self.risk.value, "reasons": self.reasons, "areas": self.areas}


def assess_risk(paths: Iterable[str], *, insertions: int = 0, deletions: int = 0,
                sensitive_paths: Iterable[str] = (), critical_paths: Iterable[str] = ()) -> RiskAssessment:
    """Classify an integrated change (section 57)."""
    paths = sorted(set(paths))
    areas: dict[str, list[str]] = {}
    for path in paths:
        for area, patterns in _COMPILED.items():
            if any(p.search(path) for p in patterns):
                areas.setdefault(area, []).append(path)
    critical = [p for p in paths if any(fnmatch.fnmatch(p, g) for g in critical_paths)]
    sensitive = [p for p in paths if any(fnmatch.fnmatch(p, g) for g in sensitive_paths)]
    code = [p for p in paths if not is_doc_file(p)]
    reasons: list[str] = []
    if critical:
        risk = Risk.CRITICAL
        reasons.append(f"critical paths changed: {', '.join(critical[:5])}")
    elif areas or sensitive:
        risk = Risk.HIGH
        reasons += [f"{area}: {', '.join(files[:3])}" for area, files in sorted(areas.items())]
        if sensitive:
            reasons.append(f"project sensitive paths: {', '.join(sensitive[:5])}")
    elif not code:
        risk = Risk.LOW
        reasons.append("documentation only")
    elif len(code) <= SMALL_FILES and insertions + deletions <= SMALL_LINES:
        risk = Risk.LOW
        reasons.append(f"small change: {len(code)} file(s), {insertions + deletions} line(s)")
    else:
        risk = Risk.MEDIUM
        reasons.append(f"{len(code)} file(s), {insertions + deletions} line(s) changed")
    return RiskAssessment(risk, reasons, areas)


# ------------------------------------------------------------------- requirements


@dataclass
class Requirement:
    name: str  # tests, build, lint, typecheck, security, browser, cross_review, requirements, ...
    source: str  # "project", "risk", or "hard policy"
    command: str | None = None  # the runner step's command, when it is one

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "has_command": self.command is not None}


@dataclass
class VerificationPlan:
    risk: RiskAssessment
    requirements: list[Requirement]
    steps: list[tuple[str, str]]  # (name, command) for the test runner, in order
    browser: bool
    gaps: list[dict[str, str]] = field(default_factory=list)
    needs_approval: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {"risk": self.risk.as_json(), "requirements": [r.as_json() for r in self.requirements],
                "steps": [s[0] for s in self.steps], "browser": self.browser, "gaps": self.gaps,
                "needs_approval": self.needs_approval}


_STEP_COMMAND = {"build": "build", "lint": "lint", "typecheck": "typecheck", "test": "test", "security": "security"}


def plan_verification(config: dict[str, Any], paths: list[str], *, insertions: int = 0, deletions: int = 0,
                      github: bool = False) -> VerificationPlan:
    """Which checks an integrated change needs and how the runners execute them (sections 56-58)."""
    commands = config.get("commands") or {}
    gate = config.get("quality_gate") or {}
    verification = config.get("verification") or {}
    risk = assess_risk(paths, insertions=insertions, deletions=deletions,
                       sensitive_paths=verification.get("sensitive_paths", []),
                       critical_paths=verification.get("critical_paths", []))
    level = risk.risk
    wanted: dict[str, str] = {}

    def need(name: str, source: str) -> None:
        wanted.setdefault(name, source)

    if gate.get("tests", True) or gate.get("full_suite", True):
        need("tests", "project")
    if gate.get("build", True):
        need("build", "project")
    if gate.get("lint", True):
        need("lint", "project")
    if gate.get("typecheck"):
        need("typecheck", "project")
    if gate.get("security_checks"):
        need("security", "project")
    if _at_least(level, Risk.MEDIUM):
        for name in ("lint", "typecheck"):
            need(name, "risk")
    if _at_least(level, Risk.HIGH):
        need("security", "risk")
    browser_config = config.get("browser_tests") or {}
    browser = bool(gate.get("browser_tests") and browser_config.get("enabled") and browser_config.get("base_url"))
    if browser:
        need("browser", "project")
    # Always required; a project cannot switch these off (section 58, hard policy).
    for name in ("cross_review", "requirements", "no_blocking_findings", "no_conflicts", "no_policy_violations"):
        need(name, "hard policy")
    if gate.get("ci_checks", True) and github:
        need("ci_checks", "project")
    if gate.get("docs_updated"):
        need("docs_updated", "project")

    requirements = []
    for name, source in wanted.items():
        key = "test" if name == "tests" else name
        requirements.append(Requirement(name, source, commands.get(_STEP_COMMAND[key]) if key in _STEP_COMMAND else None))

    steps: list[tuple[str, str]] = []
    if commands.get("install"):
        steps.append(("install", commands["install"]))
    for name in STEP_ORDER[1:]:
        requirement = "tests" if name == "test" else name
        if requirement in wanted and commands.get(name):
            steps.append((name, commands[name]))

    gaps: list[dict[str, str]] = []
    code = [p for p in paths if not is_doc_file(p)]
    if "tests" in wanted and not commands.get("test"):
        gaps.append({"kind": "no_test_command", "detail": "the project has no test command"})
    if code and not any(is_test_file(p) for p in paths):
        gaps.append({"kind": "no_test_changes", "detail": f"{len(code)} code file(s) changed without any test file changing"})
    for requirement in requirements:
        if requirement.name in ("build", "lint", "typecheck", "security") and not requirement.command:
            gaps.append({"kind": f"no_{requirement.name}_command", "detail": f"{requirement.name} required ({requirement.source}) "
                                                                                 "but the project has no command for it"})
    needs_approval = []
    if level == Risk.CRITICAL:
        needs_approval.append("critical change (section 57)")
    if gaps and _at_least(level, Risk.HIGH):
        needs_approval.append("sensitive change with inadequate tests (section 56)")
    return VerificationPlan(risk, requirements, steps, browser, gaps, needs_approval)


def post_merge_steps(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Post-merge verification: the project's post_merge command, or build + test."""
    commands = config.get("commands") or {}
    steps = [("install", commands["install"])] if commands.get("install") else []
    if commands.get("post_merge"):
        return steps + [("post_merge", commands["post_merge"])]
    return steps + [(name, commands[name]) for name in ("build", "test") if commands.get(name)]


def alternatives(step_results: dict[str, str], browser_passed: bool | None) -> list[str]:
    """Verification evidence that stands in for missing tests (section 56)."""
    names = [n for n, status in step_results.items() if status == "PASSED" and n in ("build", "lint", "typecheck", "security", "test")]
    if browser_passed:
        names.append("browser")
    return names
