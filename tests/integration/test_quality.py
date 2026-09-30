"""Phase 6 through the control plane: verification plans, test environments, test evidence,
risk-adaptive requirements, test gaps, cross-review, and the Quality Gate."""

from __future__ import annotations

import json

import pytest

from conftest import git_repo  # type: ignore[import-not-found]
from fake_agents import FakeAgentManager  # type: ignore[import-not-found]
from test_git import (  # type: ignore[import-not-found]
    REVIEW,
    clone,
    commit,
    events,
    finish_verification,
    latest_verification,
    new_task,
    set_state,
    workspace,
)

pytestmark = pytest.mark.integration

PROJECT_YAML = """\
version: 1
project: {name: demo}
agents: {allowed_providers: [claude, codex]}
commands:
  install: "pip install -r requirements.txt"
  build: "python -m compileall -q ."
  lint: "ruff check ."
  test: "python -m unittest"
  security: "pip-audit"
quality_gate: {typecheck: false, browser_tests: true, docs_updated: false}
browser_tests: {enabled: true, base_url: "http://app:8000"}
test_environment: {compose_files: [compose.yaml], services: [db, app]}
network: {testing: allowlist, test_allowed_domains: [pypi.org, files.pythonhosted.org], development: restricted}
verification: {sensitive_paths: ["billing/*"]}
"""


@pytest.fixture
def agents(services) -> FakeAgentManager:
    fake = FakeAgentManager()
    services.ctx.agents = fake
    return fake


@pytest.fixture
def repo(projects_root):
    return git_repo(projects_root / "demo", {".hermes/project.yaml": PROJECT_YAML, "app.py": "x = 1\n",
                                             "compose.yaml": "services: {db: {image: postgres}}\n", "requirements.txt": "\n"})


@pytest.fixture
def task(api, services, repo, agents) -> str:
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    return new_task(api, services)


def integrate(api, services, repo, task, files, developer=None, agents=None):
    ws = workspace(api, task)
    if developer:  # a real DEVELOPER agent run marks the provider as a developer of the change
        response = api.post(f"/v1/tasks/{task}/executions", {"role": "DEVELOPER", "provider": developer, "prompt": "do it",
                                                             "workspace": ws["path"], "capabilities": {"workspace": "WRITE"}})
        agents.finish_agent(response.json()["id"], b'{"type":"result","is_error":true,"result":"x"}', exit_code=1)
        services.scheduler.run_once()
    commit(clone(repo, ws), "task change", files)
    result = api.post(f"/v1/tasks/{task}/git/integrate").json()
    assert result.get("ok"), result
    services.scheduler.run_once()  # launches the verification (environment + runners)
    return result


def review(api, services, agents, task, verdict=REVIEW, provider="claude"):
    response = api.post(f"/v1/tasks/{task}/reviews", {"provider": provider})
    assert response.status_code == 201, response.text
    final = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "num_turns": 2,
             "structured_output": verdict, "usage": {}}
    agents.finish_agent(response.json()["id"], json.dumps(final).encode())
    services.scheduler.run_once()
    return response.json()


def gate(api, services, task):
    set_state(services, task, "QUALITY_GATE")
    return api.post(f"/v1/tasks/{task}/quality-gate").json()


def requirement(evaluation, name):
    return next(r for r in evaluation["requirements"] if r["name"] == name)


# ------------------------------------------------------------------ verification


def test_verification_runs_in_isolated_test_environment(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n", "tests/test_app.py": "import unittest\n"})
    verification = latest_verification(services, task)
    assert verification["state"] == "RUNNING" and verification["plan"]["risk"]["risk"] == "LOW"
    assert agents.environments[task]["compose_files"] == ["compose.yaml"] and agents.environments[task]["services"] == ["db", "app"]
    tester, browser = (agents.specs[str(e)] for e in verification["execution_ids"])
    assert [n for n in sorted(tester["inputs"]) if n.endswith(".sh")] == [
        "step-10-install.sh", "step-20-build.sh", "step-30-lint.sh", "step-40-test.sh"]  # security only from HIGH risk
    network = tester["grant"]["capabilities"]["network"]
    assert network["test_services"] and network["egress"] == "ALLOWLIST"
    assert network["allowed_domains"] == ["files.pythonhosted.org", "pypi.org"]  # test allowlist only, no research presets
    assert browser["image"] == "browser-runner" and browser["role"] == "BROWSER"
    assert json.loads(browser["inputs"]["browser.json"])["base_url"] == "http://app:8000"
    assert browser["grant"]["capabilities"]["workspace"] == "READ"

    done = finish_verification(services, agents, task)
    assert done["state"] == "PASSED" and agents.services_stopped == [task]  # environment removed after testing
    results = api.get(f"/v1/tasks/{task}/tests").json()["verifications"][0]
    assert {(r["scope"], r["kind"], r["status"]) for r in results["runs"]} >= {("FULL_SUITE", "test", "PASSED"),
                                                                              ("BROWSER", "browser", "PASSED")}
    assert all(r["log_artifact_id"] for r in results["runs"])


def test_developer_research_access_is_separate_from_test_access(api, services, repo, agents, task):
    ws = workspace(api, task)
    response = api.post(f"/v1/tasks/{task}/executions", {"role": "DEVELOPER", "provider": "codex", "prompt": "x",
                                                         "workspace": ws["path"], "capabilities": {"egress": "ALLOWLIST"}})
    domains = response.json() and agents.specs[response.json()["id"]]["grant"]["capabilities"]["network"]["allowed_domains"]
    assert "registry.npmjs.org" in domains and "github.com" in domains and "pypi.org" in domains


def test_environment_failure_fails_the_verification(api, services, repo, agents, task):
    from control_plane.agentmgr import AgentManagerError

    agents.environment_error = AgentManagerError(502, "environment_failed", "db did not become healthy")
    integrate(api, services, repo, task, {"app.py": "x = 3\n"})
    verification = latest_verification(services, task)
    assert verification["state"] == "ERROR" and "healthy" in verification["error"]
    assert "TEST_FAILED" in events(api, task)


# ---------------------------------------------------------------- Quality Gate


def test_gate_passes_with_evidence_and_review(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n", "tests/test_app.py": "import unittest\n"})
    finish_verification(services, agents, task)
    review(api, services, agents, task)
    evaluation = gate(api, services, task)
    assert evaluation["outcome"] == "PASS", evaluation["requirements"]
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "READY_FOR_MERGE"
    names = {r["name"]: r["status"] for r in evaluation["requirements"]}
    assert names["tests"] == names["build"] == names["lint"] == names["browser"] == names["cross_review"] == "PASS"
    assert evaluation["risk"] == "LOW" and evaluation["test_gaps"] == []
    assert "QUALITY_GATE_EVALUATED" in events(api, task)


def test_failing_tests_block_ready_for_merge(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n", "tests/test_app.py": "x\n"})
    assert finish_verification(services, agents, task, "FAILED")["state"] == "FAILED"
    review(api, services, agents, task)
    evaluation = gate(api, services, task)
    assert evaluation["outcome"] == "FAIL" and requirement(evaluation, "tests")["status"] == "FAIL"
    view = api.get(f"/v1/tasks/{task}").json()
    assert view["state"] == "FIX_REQUIRED" and "tests" in view["state_reason"]


def test_missing_review_and_blocking_findings_fail_the_gate(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n", "tests/test_app.py": "x\n"})
    finish_verification(services, agents, task)
    evaluation = gate(api, services, task)
    assert requirement(evaluation, "cross_review")["status"] == "FAIL"
    set_state(services, task, "RUNNING")
    finding = {"severity": "HIGH", "category": "security", "path": "app.py", "line": 1, "description": "SQL injection"}
    review(api, services, agents, task, {**REVIEW, "verdict": "changes_requested", "findings": [finding],
                                         "requirements_met": False, "unmet_requirements": ["validate input"]})
    evaluation = gate(api, services, task)
    assert requirement(evaluation, "no_blocking_findings")["status"] == "FAIL"
    assert requirement(evaluation, "requirements")["status"] == "FAIL"
    assert "REVIEW_FAILED" in events(api, task)
    findings = api.get(f"/v1/tasks/{task}/reviews").json()["reviews"][0]["findings"]
    assert findings[0]["description"] == "SQL injection"


def test_cross_review_must_come_from_another_provider(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n"}, developer="claude", agents=agents)
    response = api.post(f"/v1/tasks/{task}/reviews", {"provider": "claude"})
    assert response.status_code == 400 and "other than the developers" in response.json()["message"]
    assert api.post(f"/v1/tasks/{task}/reviews", {"provider": "codex"}).status_code == 201


def test_sensitive_change_with_test_gaps_needs_an_explicit_approval(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"billing/charge.py": "def charge(): pass\n", "requirements.txt": "stripe\n"})
    verification = latest_verification(services, task)
    plan = verification["plan"]
    assert plan["risk"]["risk"] == "HIGH" and "security" in plan["steps"]
    # No test file changed, and HIGH risk requires typecheck, for which the project has no command.
    assert {g["kind"] for g in plan["gaps"]} == {"no_test_changes", "no_typecheck_command"}
    finish_verification(services, agents, task)
    review(api, services, agents, task)
    evaluation = gate(api, services, task)
    assert evaluation["outcome"] == "FAIL" and requirement(evaluation, "exception_approval")["status"] == "APPROVAL_REQUIRED"
    assert evaluation["test_gaps"][0]["alternatives"]  # alternative evidence recorded (section 56)
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "APPROVAL_REQUIRED"
    approval = next(a for a in api.get("/v1/approvals").json()["approvals"] if a["action"] == "HIGH_RISK_OPERATION")
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "READY_FOR_MERGE"  # re-evaluated after approval
    latest = api.get(f"/v1/tasks/{task}/quality-gate").json()["evaluation"]
    assert latest["outcome"] == "PASS" and "gaps" in latest["residual_risk"]


def test_policy_violations_need_an_approval(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n", "tests/test_app.py": "x\n"})
    finish_verification(services, agents, task)
    review(api, services, agents, task)
    with services.ctx.db.transaction() as cur:
        cur.execute("INSERT INTO events (task_id, project_id, type, actor, summary, data, audit) "
                    "SELECT id, project_id, 'COMMAND_HIGH_RISK', 'agent-manager', '1 high-risk command(s) observed', '{}', false "
                    "FROM tasks WHERE key = %s", (task,))
    evaluation = gate(api, services, task)
    assert requirement(evaluation, "no_policy_violations")["status"] == "APPROVAL_REQUIRED"
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "APPROVAL_REQUIRED"


def test_flaky_tests_are_reported(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n", "tests/test_app.py": "x\n"})
    services.scheduler.run_once()
    verification = latest_verification(services, task)
    report = {"schema_version": 1, "steps": [{"name": "test", "status": "PASSED", "exit_code": 0, "duration_ms": 5,
                                              "attempts": 2, "log": "steps/test.log"}]}
    for execution in verification["execution_ids"]:
        agents.finish(str(execution), 0, files={"test_results.json": json.dumps(report).encode()})
    services.scheduler.run_once()
    assert "TEST_FLAKY" in events(api, task)


def test_gate_waits_while_verification_runs(api, services, repo, agents, task):
    integrate(api, services, repo, task, {"app.py": "x = 2\n", "tests/test_app.py": "x\n"})
    review(api, services, agents, task)
    evaluation = gate(api, services, task)
    assert requirement(evaluation, "verification")["status"] == "PENDING"
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "QUALITY_GATE"  # waits instead of failing
