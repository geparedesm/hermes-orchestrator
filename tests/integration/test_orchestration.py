"""Phase 7 through the control plane: lease, orchestrator steps and actions, the subtask cycle
(develop -> cross-review -> fix), integration -> verification -> integration review -> Quality Gate,
budgets, and the scenarios from the Codex adversarial review of the design."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from ho_core.enums import TaskState as S
from ho_core.statemachine import Trigger

from conftest import git_repo  # type: ignore[import-not-found]
from fake_agents import FakeAgentManager  # type: ignore[import-not-found]
from test_git import APP, PROJECT_YAML, REVIEW, commit, finish_verification, latest_verification  # type: ignore[import-not-found]

pytestmark = [pytest.mark.integration, pytest.mark.orchestration]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
DONE = {"status": "completed", "summary": "Implemented.", "changed_files": ["app.py"], "tests": {"ran": True, "passed": True, "command": "python -m unittest", "summary": "ok"},
        "commits": [], "follow_ups": [], "blocked_reason": None}


@pytest.fixture
def agents(services) -> FakeAgentManager:
    fake = FakeAgentManager()
    services.ctx.agents = fake
    return fake


@pytest.fixture
def repo(projects_root):
    return git_repo(projects_root / "demo", {".hermes/project.yaml": PROJECT_YAML, "app.py": APP, "README.md": "# demo\n"})


@pytest.fixture
def task(api, services, repo, agents) -> str:
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    key = api.post("/v1/tasks", {"project": "demo", "request": "Add a greeting endpoint"}).json()["key"]
    services.scheduler.run_once()  # READY, then dispatched: lease + PLANNING + first step
    return key


# ------------------------------------------------------------------ helpers


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def state(api, task) -> str:
    return api.get(f"/v1/tasks/{task}").json()["state"]


def active(services, task, role) -> list[dict]:
    return q(services, "SELECT e.* FROM executions e JOIN tasks t ON t.id = e.task_id WHERE t.key = %s AND e.role = %s "
             "AND e.state IN ('REQUESTED', 'STARTING', 'RUNNING') ORDER BY e.created_at", task, role)


def executions(services, task, role) -> list[dict]:
    return q(services, "SELECT e.* FROM executions e JOIN tasks t ON t.id = e.task_id WHERE t.key = %s AND e.role = %s "
             "ORDER BY e.created_at", task, role)


def answer(services, agents, execution, structured, *, ok=True) -> None:
    """Finish an agent execution with a structured answer in its provider's output format."""
    execution = str(execution)
    if agents.specs[execution]["image"].startswith("claude"):
        final = {"type": "result", "subtype": "success", "is_error": not ok, "session_id": "s", "num_turns": 2,
                 "structured_output": structured, "usage": {"input_tokens": 1000, "output_tokens": 500}}
        agents.finish_agent(execution, json.dumps(final).encode(), exit_code=0 if ok else 1)
    else:
        agents.finish_agent(execution, (FIXTURES / "codex-success.jsonl").read_bytes(),
                            extra={"ho/last_message.json": json.dumps(structured).encode()})
    services.scheduler.run_once()


def step(services, agents, task, *actions, summary="next step") -> dict:
    [execution] = active(services, task, "ORCHESTRATOR")
    full = [{"type": None, "text": None, "subtasks": None, "subtask": None, "role": None, "provider": None, "prompt": None,
             "level": None, "reversible": None, "category": None, "anchors": None, "questions": None, **a} for a in actions]
    answer(services, agents, execution["id"], {"summary": summary, "actions": full})
    return execution


def plan_item(key, **kw):
    return {"key": key, "title": f"subtask {key}", "kind": "IMPLEMENT", "description": f"do {key}", "depends_on": [],
            "files": [f"{key}.py"], "risk": "LOW", "preferred_provider": None, **kw}


def actions(services, task) -> list[tuple[str, str, str | None]]:
    rows = q(services, "SELECT a.type, a.outcome, a.reason FROM orchestrator_actions a JOIN tasks t ON t.id = a.task_id "
             "WHERE t.key = %s ORDER BY a.created_at, a.seq", task)
    return [(r["type"], r["outcome"], r["reason"]) for r in rows]


def subtasks(services, task) -> dict[str, dict]:
    rows = q(services, "SELECT s.* FROM subtasks s JOIN tasks t ON t.id = s.task_id WHERE t.key = %s", task)
    return {r["local_key"]: r for r in rows}


def develop(services, agents, repo, execution, files) -> None:
    """The developer commits in its workspace and reports completion."""
    commit(repo.parent / execution["workspace"], "subtask work", files)
    answer(services, agents, execution["id"], DONE)


def review_verdict(services, agents, execution, verdict=REVIEW) -> None:
    answer(services, agents, execution["id"], verdict)


def plan_and_start(services, agents, task, *items, start="a", provider="codex"):
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Greeting endpoint returns hello."},
         {"type": "SET_PLAN", "subtasks": list(items or [plan_item("a")])},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": start, "provider": provider})


# ------------------------------------------------------------------ lease and steps


def test_dispatch_takes_the_lease_and_plans(api, services, agents, task):
    assert state(api, task) == "PLANNING"
    [lease] = q(services, "SELECT * FROM task_leases")
    assert lease["epoch"] == 1 and lease["holder"] == services.ctx.instance_id
    [execution] = active(services, task, "ORCHESTRATOR")
    spec = agents.specs[str(execution["id"])]
    assert "Add a greeting endpoint" in spec["inputs"]["prompt.md"]
    assert "workspace" not in spec
    assert execution["lease_epoch"] == 1 and execution["spec"]["purpose"]["fenced"]


def test_plan_runs_the_subtask_cycle_to_ready_for_merge(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    assert [a[:2] for a in actions(services, task)] == [("SET_REQUIREMENTS", "ACCEPTED"), ("SET_PLAN", "ACCEPTED"),
                                                       ("REQUEST_EXECUTION", "ACCEPTED")]
    assert state(api, task) == "RUNNING"
    [dev] = active(services, task, "DEVELOPER")
    assert dev["provider"] == "codex" and subtasks(services, task)["a"]["state"] == "IN_PROGRESS"

    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    [rev] = active(services, task, "REVIEWER")
    assert rev["provider"] == "claude"  # never the developer's provider
    assert subtasks(services, task)["a"]["state"] == "IN_REVIEW"

    review_verdict(services, agents, rev)
    assert subtasks(services, task)["a"]["state"] == "ACCEPTED"
    assert state(api, task) == "TESTING"  # integrated automatically
    finish_verification(services, agents, task)
    assert state(api, task) == "REVIEW"
    [integration_review] = active(services, task, "REVIEWER")
    assert integration_review["provider"] == "claude" and integration_review["subtask_id"] is None

    review_verdict(services, agents, integration_review)
    services.scheduler.run_once()
    assert state(api, task) == "READY_FOR_MERGE", q(services, "SELECT type, summary FROM events ORDER BY seq DESC LIMIT 8")
    assert len(executions(services, task, "ORCHESTRATOR")) == 1  # nothing asked the orchestrator to think again
    [manifest] = q(services, "SELECT m.kind, a.path FROM manifests m JOIN artifacts a ON a.id = m.artifact_id")
    assert manifest["kind"] == "READY_FOR_MERGE"
    body = json.loads(services.ctx.artifacts.read(manifest["path"]))
    assert [s["state"] for s in body["dag"]["subtasks"]] == ["ACCEPTED"]
    assert {(r["subject"], r["reviewer_provider"]) for r in body["reviews"]} == {(f"{task}-1", "claude"), (task, "claude")}
    inspect = api.get(f"/v1/tasks/{task}/orchestration").json()
    assert inspect["lease"]["epoch"] == 1 and inspect["subtasks"][0]["state"] == "ACCEPTED"
    # Phase 10: the Dashboard's task view shows the whole story from records.
    view = api.get(f"/v1/dashboard/tasks/{task}").json()
    assert view["task"]["state"] == "READY_FOR_MERGE" and view["quality_gate"]["outcome"] == "PASS"
    assert [s["state"] for s in view["dag"]["subtasks"]] == ["ACCEPTED"] and view["dag"]["edges"] == []
    assert {r["reviewer_provider"] for r in view["reviews"]} == {"claude"} and view["tests"][0]["state"] == "PASSED"
    assert {e["role"] for e in view["executions"]} >= {"ORCHESTRATOR", "DEVELOPER", "REVIEWER", "TESTER"}
    assert "READY_FOR_MERGE" in [e["type"] for e in view["timeline"]] and view["checkpoints"]
    assert view["manifests"][0]["kind"] == "READY_FOR_MERGE" and view["orchestration"]["lease"]["epoch"] == 1
    manifest = api.get(f"/v1/tasks/{task}/manifests/{view['manifests'][0]['id']}").json()
    assert manifest["kind"] == "READY_FOR_MERGE" and manifest["task"]["key"] == task


def test_wait_while_work_runs_does_not_loop(api, services, agents, task):
    plan_and_start(services, agents, task)  # a developer is running
    q(services, "UPDATE tasks SET step_requested = true")
    services.scheduler.run_once()
    step(services, agents, task, {"type": "WAIT"})
    for _ in range(3):
        services.scheduler.run_once()
    assert len(executions(services, task, "ORCHESTRATOR")) == 2 and state(api, task) == "RUNNING"


def test_steps_that_leave_nothing_running_are_bounded(api, services, agents, task):
    for _ in range(3):
        step(services, agents, task, {"type": "WAIT"})
    assert state(api, task) == "BLOCKED"
    assert "STEP_STALLED" in [r["type"] for r in q(services, "SELECT type FROM events")]


def test_actions_from_a_stale_epoch_are_rejected(api, services, agents, task):
    q(services, "UPDATE task_leases SET epoch = epoch + 1")  # another leader took over
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a")]})
    assert actions(services, task) == [("SET_PLAN", "REJECTED", "stale lease epoch")]
    assert subtasks(services, task) == {}


def test_fenced_launch_from_an_older_epoch_is_cancelled(api, services, agents, task):
    [execution] = q(services, "SELECT * FROM executions WHERE role = 'ORCHESTRATOR'")
    q(services, "UPDATE executions SET state = 'REQUESTED' WHERE id = %s", execution["id"])
    q(services, "UPDATE task_leases SET epoch = epoch + 1")
    services.executions.dispatch(execution["id"])
    [row] = q(services, "SELECT state, failure_class FROM executions WHERE id = %s", execution["id"])
    assert (row["state"], row["failure_class"]) == ("CANCELLED", "POLICY")


def test_invalid_actions_are_rejected_and_fed_back(api, services, agents, task):
    step(services, agents, task,
         {"type": "SET_PLAN", "subtasks": [plan_item("a", depends_on=["b"]), plan_item("b", depends_on=["a"])]},
         {"type": "REQUEST_EXECUTION", "role": "TESTER", "subtask": "a"},
         {"type": "ACCEPT_SUBTASK", "subtask": "a"})
    outcomes = actions(services, task)
    assert [o[1] for o in outcomes] == ["REJECTED"] * 3
    assert "cycle" in outcomes[0][2] and "unknown subtask" in outcomes[1][2]
    api.post(f"/v1/tasks/{task}/revise", {"text": "Same request, retry."})
    services.scheduler.run_once()
    [execution] = active(services, task, "ORCHESTRATOR")
    assert "dependency cycle" in agents.specs[str(execution["id"])]["inputs"]["prompt.md"]


def test_dependencies_gate_readiness(api, services, agents, task):
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a"), plan_item("b", depends_on=["a"])]},
         {"type": "REQUEST_EXECUTION", "subtask": "b", "provider": "codex"})
    assert subtasks(services, task)["b"]["state"] == "PENDING"
    assert actions(services, task)[-1][1] == "REJECTED"


def test_high_impact_assumption_needs_a_human(api, services, agents, task):
    step(services, agents, task, {"type": "RECORD_ASSUMPTION", "text": "Drop the legacy endpoint", "level": "HIGH",
                                  "reversible": False})
    assert state(api, task) == "APPROVAL_REQUIRED"
    [approval] = q(services, "SELECT * FROM approvals WHERE action = 'ASSUMPTION'")
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert state(api, task) == "PLANNING"
    assert q(services, "SELECT status FROM assumptions")[0]["status"] == "APPROVED"
    services.scheduler.run_once()
    assert len(executions(services, task, "ORCHESTRATOR")) == 2  # the decision triggers a step


# ------------------------------------------------------------------ review cycle and authorship


CHANGES = {"verdict": "changes_requested", "summary": "Missing test.", "requirements_met": False,
           "unmet_requirements": ["tests"], "findings": [{"severity": "HIGH", "category": "tests", "path": "a.py", "line": 1,
                                                          "description": "No test covers the greeting."}]}


def test_changes_requested_go_back_to_the_developer_then_an_alternate(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "x = 1\n"})
    for cycle in (1, 2):
        [rev] = active(services, task, "REVIEWER")
        review_verdict(services, agents, rev, CHANGES)
        [fix] = active(services, task, "DEVELOPER")
        assert fix["provider"] == "codex" and fix["workspace"] == dev["workspace"]
        assert "No test covers the greeting" in agents.specs[str(fix["id"])]["inputs"]["prompt.md"]
        develop(services, agents, repo, fix, {"a.py": f"x = {cycle + 1}\n"})
    [rev] = active(services, task, "REVIEWER")
    review_verdict(services, agents, rev, CHANGES)  # the limit (2 cycles) is reached
    [alternate] = active(services, task, "DEVELOPER")
    assert alternate["provider"] == "claude" and alternate["workspace"] != dev["workspace"]  # fresh attempt
    [old] = q(services, "SELECT status FROM workspaces WHERE path LIKE %s", f"%{Path(dev['workspace']).name}")
    assert old["status"] == "RETAINED"  # the earlier attempt's commits are not carried over
    develop(services, agents, repo, alternate, {"a.py": "x = 9\n"})
    [rev] = active(services, task, "REVIEWER")
    assert rev["provider"] == "codex"  # reviewed by the provider that did not write this attempt


def test_both_providers_as_developers_need_both_integration_reviews(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("a"), plan_item("b"))
    [dev_a] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev_a, {"a.py": "a = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    assert len(active(services, task, "ORCHESTRATOR")) == 1  # accepting a and leaving b READY asks for a step
    q(services, "INSERT INTO credential_refs (provider, identity, status) VALUES ('codex', 'default', 'AUTH_REQUIRED') "
      "ON CONFLICT (provider, identity) DO UPDATE SET status = 'AUTH_REQUIRED'")
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "subtask": "b", "provider": "claude"})
    q(services, "UPDATE credential_refs SET status = 'READY' WHERE provider = 'codex'")
    [dev_b] = active(services, task, "DEVELOPER")
    assert dev_b["provider"] == "claude"  # codex is unavailable, so the router picks claude
    develop(services, agents, repo, dev_b, {"b.py": "b = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    finish_verification(services, agents, task)
    assert state(api, task) == "REVIEW"
    reviewers = sorted(r["provider"] for r in active(services, task, "REVIEWER"))
    assert reviewers == ["claude", "codex"]


# ------------------------------------------------------------------ failures and budgets


def test_orchestrator_failures_fail_over_then_block(api, services, agents, task):
    for expected in ("claude", "claude", "codex"):
        [execution] = active(services, task, "ORCHESTRATOR")
        assert execution["provider"] == expected
        agents.finish_agent(str(execution["id"]), b'{"type":"result","is_error":true,"result":"boom"}', exit_code=1)
        services.scheduler.run_once()
        services.scheduler.run_once()
    assert state(api, task) == "BLOCKED"
    assert "FAILOVER_COMPLETED" in [r["type"] for r in q(services, "SELECT type FROM events")]


def test_concurrent_launches_cannot_overshoot_the_budget(api, services, agents, task):
    from control_plane import budgets

    with services.ctx.unit_of_work() as uow:
        uow.cur.execute("SELECT * FROM tasks WHERE key = %s", (task,))
        row = uow.cur.fetchone()
        uow.cur.execute("UPDATE budgets SET limits = limits || '{\"provider_usage_units\": 450000}' WHERE task_id = %s", (row["id"],))
    with services.ctx.unit_of_work() as uow:  # the planning step already reserved 200k
        budgets.reserve(uow, row, agent=True, timeout_minutes=10)
    with services.ctx.unit_of_work() as uow, pytest.raises(budgets.BudgetExhausted):
        budgets.reserve(uow, row, agent=True, timeout_minutes=10)


def test_budget_increase_needs_an_approval(api, services, agents, task):
    response = api.post(f"/v1/tasks/{task}/budget", {"add": {"agent_launches": 5}})
    assert response.status_code == 201, response.text
    api.post(f"/v1/approvals/{response.json()['id']}/decision", {"decision": "APPROVE"})
    [budget] = q(services, "SELECT limits FROM budgets")
    assert budget["limits"]["agent_launches"] >= 5
    assert "BUDGET_RAISED" in [r["type"] for r in q(services, "SELECT type FROM events")]


def test_suspended_task_ages_until_it_outranks_the_preemptor(api, services, agents, task):
    orchestration = services.orchestration
    [row] = q(services, "SELECT * FROM tasks WHERE key = %s", task)
    low = {**row, "priority": "LOW"}
    assert orchestration._rank(low, None) == 3
    aging = services.ctx.platform["scheduler"]["aging_minutes"]
    since = datetime.now(timezone.utc) - timedelta(minutes=aging * 3 + 1)
    assert orchestration._rank(low, since) == 0  # as urgent as CRITICAL: it cannot be starved forever


def test_scope_conflicts_queue_the_launch(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("a", files=["shared.py"]), plan_item("b", files=["shared.py"]))
    q(services, "UPDATE tasks SET step_requested = true")
    services.scheduler.run_once()
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "subtask": "b", "provider": "codex"})
    assert len(active(services, task, "DEVELOPER")) == 1
    [pending] = q(services, "SELECT * FROM pending_launches")
    assert "scope overlaps" in pending["reason"]
    for _ in range(3):  # retried every pass while the overlap lasts: the wait is recorded once (T-1, T-56)
        services.orchestration.process_pending()
    [row] = q(services, "SELECT count(*) AS n FROM events WHERE type = 'LAUNCH_WAITING' AND summary LIKE '%%scope overlaps%%'")
    assert row["n"] == 1


# ------------------------------------------------------------------ relationships and knowledge


def test_duplicate_requests_wait_for_the_user(api, services, agents, task):
    duplicate = api.post("/v1/tasks", {"project": "demo", "request": "Add a greeting endpoint"}).json()["key"]
    related = api.post("/v1/tasks", {"project": "demo", "request": "Add a greeting endpoint with tests and docs"}).json()["key"]
    services.scheduler.run_once()
    assert state(api, duplicate) == "BLOCKED"
    assert state(api, related) == "PLANNING"
    kinds = {(r["f"], r["t"], r["kind"]) for r in q(services, "SELECT f.key AS f, t.key AS t, r.kind FROM task_relationships r "
                                                             "JOIN tasks f ON f.id = r.from_task_id JOIN tasks t ON t.id = r.to_task_id")}
    assert (duplicate, task, "DUPLICATE") in kinds and (related, task, "RELATED") in kinds
    response = api.post(f"/v1/tasks/{duplicate}/manifest")
    assert response.status_code == 201, response.text
    assert response.json()["dag"]["task_relationships"][0]["kind"] == "DUPLICATE"


def test_knowledge_is_proposed_confirmed_retrieved_and_goes_stale(api, services, agents, repo, task):
    step(services, agents, task, {"type": "PROPOSE_KNOWLEDGE", "text": "Greeting lives in a.py\nKeep it pure.",
                                  "category": "CONVENTION", "anchors": ["a.py"]})
    [item] = api.get("/v1/projects/demo/knowledge").json()["items"]
    assert item["trust"] == "HYPOTHESIS"
    assert api.post(f"/v1/knowledge/{item['id']}/decision", {"decision": "CONFIRM"}).status_code == 200
    api.post(f"/v1/tasks/{task}/revise", {"text": "Greeting endpoint returns hello."})
    services.scheduler.run_once()
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    assert "Keep it pure" in agents.specs[str(dev["id"])]["inputs"]["prompt.md"]
    develop(services, agents, repo, dev, {"a.py": "x = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    assert q(services, "SELECT trust FROM knowledge_items")[0]["trust"] == "STALE"


def test_budget_is_visible(api, services, agents, task):
    budget = api.get(f"/v1/tasks/{task}/budget").json()
    assert budget["consumed"]["agent_launches"] == 1 and budget["reserved"]["provider_usage_units"] > 0


def test_orchestrator_login_failure_fails_over_without_waiting(api, services, agents, task):
    [execution] = active(services, task, "ORCHESTRATOR")
    agents.finish_agent(str(execution["id"]), (FIXTURES / "claude-auth-failure.jsonl").read_bytes(), exit_code=1)
    services.scheduler.run_once()
    assert state(api, task) == "PLANNING"  # not AUTH_REQUIRED: codex leads now
    [lease] = q(services, "SELECT * FROM task_leases")
    assert (lease["provider"], lease["epoch"]) == ("codex", 2)
    [step] = active(services, task, "ORCHESTRATOR")
    assert step["provider"] == "codex" and step["lease_epoch"] == 2


def test_orchestrator_retries_are_charged_to_the_retries_budget(api, services, agents, task):
    [execution] = active(services, task, "ORCHESTRATOR")
    agents.finish_agent(str(execution["id"]), b'{"type":"result","is_error":true,"result":"boom"}', exit_code=1)
    services.scheduler.run_once()
    [retry] = active(services, task, "ORCHESTRATOR")
    assert retry["spec"]["purpose"]["retry"] is True
    assert api.get(f"/v1/tasks/{task}/budget").json()["reserved"]["retries"] == 1


def test_plan_expansion_needs_an_approval_bound_to_that_plan(api, services, agents, task):
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a")]})
    q(services, "UPDATE tasks SET step_requested = true")
    services.scheduler.run_once()
    bigger = [plan_item(k) for k in ("a", "b", "c", "d", "e")]
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": bigger})
    assert state(api, task) == "APPROVAL_REQUIRED" and len(subtasks(services, task)) == 1
    [approval] = q(services, "SELECT * FROM approvals WHERE action = 'SCOPE_EXPANSION'")
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    services.scheduler.run_once()
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": bigger})  # the same plan, now authorized
    assert len(subtasks(services, task)) == 5
    assert q(services, "SELECT state FROM approvals WHERE id = %s", approval["id"])[0]["state"] == "CONSUMED"


def test_results_arriving_while_paused_are_not_applied(api, services, agents, task):
    api.post(f"/v1/tasks/{task}/pause")
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a")]})
    assert actions(services, task) == [("SET_PLAN", "REJECTED", "task is PAUSED")]
    assert subtasks(services, task) == {} and len(executions(services, task, "ORCHESTRATOR")) == 1
    api.post(f"/v1/tasks/{task}/resume")
    services.scheduler.run_once()
    assert len(active(services, task, "ORCHESTRATOR")) == 1  # it decides again once resumed


def test_test_authoring_runs_as_development_work(api, services, agents, task):
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("t", kind="TEST_AUTHORING")]},
         {"type": "REQUEST_EXECUTION", "role": "TESTER", "subtask": "t", "provider": "codex"})
    [dev] = active(services, task, "DEVELOPER")
    assert dev["subtask_id"] == subtasks(services, task)["t"]["id"]


def test_rejected_steps_retry_with_feedback_then_block(api, services, agents, task):
    for _ in range(3):
        step(services, agents, task, {"type": "ACCEPT_SUBTASK", "subtask": "missing"})
    assert state(api, task) == "BLOCKED"
    assert [a[1] for a in actions(services, task)] == ["REJECTED"] * 3


def test_dependent_subtasks_start_from_the_accepted_work(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("a"), plan_item("b", depends_on=["a"]))
    [dev_a] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev_a, {"a.py": "a = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "subtask": "b", "provider": "codex"})
    [dev_b] = active(services, task, "DEVELOPER")
    assert (repo.parent / dev_b["workspace"] / "a.py").read_text() == "a = 1\n"  # a's work is there
    develop(services, agents, repo, dev_b, {"b.py": "b = 1\n"})
    assert len(active(services, task, "REVIEWER")) == 1
    ws = {r["name"]: r for r in q(services, "SELECT name, base_sha, head_sha FROM workspaces")}
    a_ws, b_ws = (ws[Path(d["workspace"]).name] for d in (dev_a, dev_b))
    assert b_ws["base_sha"] == a_ws["head_sha"]  # b is reviewed from a's accepted head: only its own changes


def test_orphaned_tasks_are_adopted(api, services, agents, task):
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a")]})
    q(services, "DELETE FROM task_leases")  # the leader vanished
    services.scheduler.run_once()
    [lease] = q(services, "SELECT * FROM task_leases")
    assert lease["holder"] == services.ctx.instance_id and lease["epoch"] == 2  # never reuses an epoch
    assert len(active(services, task, "ORCHESTRATOR")) == 1
    assert "LEASE_ADOPTED" in [r["type"] for r in q(services, "SELECT type FROM events")]


def test_research_subtasks_are_accepted_without_code(api, services, agents, task):
    plan_and_start(services, agents, task, plan_item("r", kind="RESEARCH"), plan_item("a", depends_on=["r"]), start="r")
    [dev] = active(services, task, "DEVELOPER")
    answer(services, agents, dev["id"], {**DONE, "summary": "The package exposes greet in hello/__init__.py.",
                                         "changed_files": []})
    assert subtasks(services, task)["r"]["state"] == "ACCEPTED"
    assert subtasks(services, task)["a"]["state"] == "READY"
    [execution] = active(services, task, "ORCHESTRATOR")
    assert "exposes greet" in agents.specs[str(execution["id"])]["inputs"]["prompt.md"]


def test_exhausted_budget_during_verification_pauses_and_resumes(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "x = 1\n"})
    [launches] = q(services, "SELECT (consumed->>'agent_launches')::int AS n FROM budgets")
    q(services, "UPDATE budgets SET limits = limits || jsonb_build_object('agent_launches', %s)", launches["n"])
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])  # accepted -> integrate -> runner needs a launch
    assert state(api, task) == "PAUSED_BUDGET"
    assert latest_verification(services, task)["state"] == "PREPARING"
    approval = api.post(f"/v1/tasks/{task}/budget", {"add": {"agent_launches": 5}}).json()
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    assert state(api, task) == "TESTING"
    assert latest_verification(services, task)["state"] == "RUNNING"


def test_lost_subtask_reviews_are_retried_then_handed_back(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "x = 1\n"})
    for _ in range(3):
        [rev] = active(services, task, "REVIEWER")
        agents.vanish(str(rev["id"]))  # the worker disappeared (another stack, a crash)
        services.scheduler.run_once()
    assert len(executions(services, task, "REVIEWER")) == 3
    assert subtasks(services, task)["a"]["state"] == "FIX_REQUIRED"
    assert any("cross-review could not be completed" in r["summary"]
               for r in q(services, "SELECT summary FROM events WHERE type = 'ORCHESTRATOR_INPUT'"))


def test_reviews_lost_before_the_control_plane_reacted_are_healed(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "x = 1\n"})
    [rev] = active(services, task, "REVIEWER")
    q(services, "UPDATE executions SET state = 'LOST', ended_at = now() WHERE id = %s", rev["id"])  # no hook ran
    q(services, "UPDATE subtasks SET updated_at = now() - interval '5 minutes'")
    services.scheduler.run_once()
    assert len(active(services, task, "REVIEWER")) == 1 and subtasks(services, task)["a"]["state"] == "IN_REVIEW"


def test_review_retry_waits_while_git_service_is_down(api, services, agents, repo, task, monkeypatch):
    from control_plane.errors import UpstreamError

    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "x = 1\n"})
    [rev] = active(services, task, "REVIEWER")

    def down(*_args, **_kwargs):
        raise UpstreamError("git-service unavailable: connection refused")

    monkeypatch.setattr(services.ctx.git, "_post", down)
    agents.vanish(str(rev["id"]))
    services.scheduler.run_once()  # the lost review is still recorded; its retry waits
    assert q(services, "SELECT state FROM executions WHERE id = %s", rev["id"])[0]["state"] == "LOST"
    assert subtasks(services, task)["a"]["state"] == "IN_REVIEW"
    monkeypatch.undo()
    services.scheduler.run_once()
    assert len(active(services, task, "REVIEWER")) == 1


def test_orchestrator_reads_a_view_of_tracked_files_that_maintenance_prunes(api, services, agents, repo, task):
    [orchestrator] = executions(services, task, "ORCHESTRATOR")
    [item] = orchestrator["spec"]["project_read"]
    view = repo / item["path"].split("/", 1)[1]
    assert item["path"].split("/")[-3:-1] == [".hermes", "read"] and view.name == item["sha"]
    assert sorted(str(p.relative_to(view)) for p in view.rglob("*") if p.is_file()) == [".hermes/project.yaml", "README.md", "app.py"]
    q(services, "UPDATE executions SET state = 'SUCCEEDED', ended_at = now() WHERE id = %s", orchestrator["id"])
    os.utime(view, (0, 0))  # unused for long
    assert services.maintenance.run()["read_views"]["removed"] == 1 and not view.exists()


def test_a_plan_that_drops_its_last_open_subtask_is_integrated(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("a"), plan_item("b"))
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    [rev] = active(services, task, "REVIEWER")
    review_verdict(services, agents, rev)
    assert subtasks(services, task)["a"]["state"] == "ACCEPTED" and state(api, task) == "RUNNING"
    services.scheduler.run_once()  # "a accepted; b ready": the orchestrator is asked
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a")]})  # b is not needed after all
    assert subtasks(services, task)["b"]["state"] == "CANCELLED"
    assert state(api, task) == "TESTING"  # integrated without another step


def test_a_plan_that_replaces_an_open_subtask_is_not_integrated(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("a"), plan_item("b"))
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    [rev] = active(services, task, "REVIEWER")
    review_verdict(services, agents, rev)
    services.scheduler.run_once()
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("a"), plan_item("c")]})
    assert subtasks(services, task)["c"]["state"] in ("READY", "PENDING") and state(api, task) == "RUNNING"


def test_submitting_an_accepted_plan_while_planning_integrates_it(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    [rev] = active(services, task, "REVIEWER")
    q(services, "UPDATE tasks SET orchestrator_failures = 3 WHERE key = %s", task)
    with services.ctx.unit_of_work() as uow:  # stand-in: blocked before integrating, then retried
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.BLOCKED, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason="stalled")
    api.post(f"/v1/tasks/{task}/retry")
    assert q(services, "SELECT orchestrator_failures FROM tasks WHERE key = %s", task)[0]["orchestrator_failures"] == 0
    review_verdict(services, agents, rev)  # the subtask is accepted while the task waits to be planned again
    services.scheduler.run_once()
    if state(api, task) == "PLANNING":
        step(services, agents, task, {"type": "SUBMIT_FOR_QUALITY_GATE"})
    assert state(api, task) in ("TESTING", "REVIEW")


def test_a_preemptive_block_does_not_cancel_work_started_in_the_same_step(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("a"), plan_item("b"))
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    [rev] = active(services, task, "REVIEWER")
    review_verdict(services, agents, rev)
    services.scheduler.run_once()
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "b", "provider": "codex"},
         {"type": "REPORT_BLOCKED", "text": "if b fails again we are out of budget"})
    assert state(api, task) == "RUNNING" and active(services, task, "DEVELOPER")
    assert actions(services, task)[-1][1] == "REJECTED"


def test_a_retried_task_with_its_plan_accepted_is_integrated_without_asking(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    [rev] = active(services, task, "REVIEWER")
    with services.ctx.unit_of_work() as uow:  # stand-in: blocked before the last acceptance, then retried
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.BLOCKED, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason="stalled")
    review_verdict(services, agents, rev)
    api.post(f"/v1/tasks/{task}/retry")
    steps_before = len(executions(services, task, "ORCHESTRATOR"))
    services.scheduler.run_once()
    services.scheduler.run_once()
    assert state(api, task) in ("TESTING", "REVIEW")
    assert len(executions(services, task, "ORCHESTRATOR")) == steps_before  # no step was needed
    [row] = q(services, "SELECT step_requested FROM tasks WHERE key = %s", task)
    assert row["step_requested"] is False


def test_a_revision_is_planned_before_an_accepted_plan_is_integrated(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    [rev] = active(services, task, "REVIEWER")
    with services.ctx.unit_of_work() as uow:
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.BLOCKED, trigger=Trigger.SYSTEM,
                                  actor="control-plane", reason="stalled")
    review_verdict(services, agents, rev)
    assert api.post(f"/v1/tasks/{task}/revise", {"text": "Also greet in Spanish."}).status_code in (200, 201)
    api.post(f"/v1/tasks/{task}/retry")
    services.scheduler.run_once()
    assert state(api, task) == "PLANNING" and active(services, task, "ORCHESTRATOR")  # the revision is planned first


def test_each_conflicting_workspace_gets_one_resolution_attempt(api, services, agents, repo, task, monkeypatch):
    orchestration = services.orchestration
    [step] = active(services, task, "ORCHESTRATOR")
    handed = []

    def resolve(uow, task_key, *, provider, principal):
        handed.append(task_key)
        return {"execution": str(step["id"])}  # stand-in for the DEVELOPER execution Git Service would start

    monkeypatch.setattr(orchestration.git, "resolve_conflicts", resolve)

    def conflict(ref):
        with services.ctx.unit_of_work() as uow:
            orchestration._resolve_conflict(uow, services.tasks.get(uow, task, lock=True),
                                            {"conflicting_ref": f"refs/hermes/workspaces/{ref}", "conflicts": ["tests/test_x.py"]})

    def inputs():
        return q(services, "SELECT count(*) AS n FROM events e JOIN tasks t ON t.id = e.task_id WHERE t.key = %s "
                 "AND e.type = 'ORCHESTRATOR_INPUT' AND e.summary LIKE '%%after a resolution attempt'", task)[0]["n"]

    conflict("t-1-7a1")
    assert len(handed) == 1 and inputs() == 0
    q(services, "UPDATE executions SET spec = jsonb_set(spec, '{purpose}', %s::jsonb) WHERE id = %s",
      json.dumps({"kind": "RESOLVE", "ref": "refs/hermes/workspaces/t-1-7a1"}), step["id"])
    conflict("t-1-7a1")  # the same workspace again: the orchestrator decides
    assert len(handed) == 1 and inputs() == 1
    conflict("t-1-8a1")  # another subtask's workspace conflicting too gets its own attempt
    assert len(handed) == 2 and inputs() == 1
    # T-1: a resolution that never started (its image was missing) is no attempt; the conflict is handed over again.
    q(services, "UPDATE executions SET state = 'FAILED', started_at = NULL, ended_at = now() WHERE id = %s", step["id"])
    conflict("t-1-7a1")
    assert len(handed) == 3 and inputs() == 1


def test_a_subtask_whose_dependencies_conflict_reconciles_them_first(api, services, agents, repo, task):
    """T-50: parallel subtasks both registered their model in the same file; the one depending on both must not stall."""
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Two models and a wizard."},
         {"type": "SET_PLAN", "subtasks": [plan_item("a"), plan_item("b"), plan_item("c", depends_on=["a", "b"])]},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "b", "provider": "claude"})
    dev_a, dev_b = active(services, task, "DEVELOPER")  # in creation order: a, then b
    develop(services, agents, repo, dev_a, {"models.py": "import a\n"})
    develop(services, agents, repo, dev_b, {"models.py": "import b\n"})
    for reviewer in active(services, task, "REVIEWER"):
        review_verdict(services, agents, reviewer)
    assert {k: v["state"] for k, v in subtasks(services, task).items()} == {"a": "ACCEPTED", "b": "ACCEPTED", "c": "READY"}
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "subtask": "c", "provider": "codex"})
    assert actions(services, task)[-1][:2] == ("REQUEST_EXECUTION", "ACCEPTED")
    [dev_c] = active(services, task, "DEVELOPER")
    assert "<<<<<<<" in (repo.parent / dev_c["workspace"] / "models.py").read_text()  # the conflict is there to reconcile
    [row] = q(services, "SELECT spec->'assignment'->>'prompt' AS prompt FROM executions WHERE id = %s", dev_c["id"])
    assert row["prompt"].startswith("Subtask") and "FIRST, before your assignment" in row["prompt"] and "models.py" in row["prompt"]
    assert q(services, "SELECT 1 FROM events WHERE type = 'WORKSPACE_CONFLICTS'")


def test_a_conflict_workspace_left_by_a_rolled_back_launch_is_reclaimed(api, services, agents, repo, task):
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Two models and a wizard."},
         {"type": "SET_PLAN", "subtasks": [plan_item("a"), plan_item("b"), plan_item("c", depends_on=["a", "b"])]},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "b", "provider": "claude"})
    dev_a, dev_b = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev_a, {"models.py": "import a\n"})
    develop(services, agents, repo, dev_b, {"models.py": "import b\n"})
    for reviewer in active(services, task, "REVIEWER"):
        review_verdict(services, agents, reviewer)
    # A launch that prepared the clone and then rolled back leaves it on disk without a record.
    orphan = repo / ".hermes" / "worktrees" / f"{task.lower()}-3a1"
    services.ctx.git.conflict_workspace("demo", orphan.name, f"hermes/{task.lower()}/3a1", "main",
                                        f"refs/hermes/workspaces/{Path(dev_b['workspace']).name}",
                                        base_ref=f"refs/hermes/workspaces/{Path(dev_a['workspace']).name}")
    assert orphan.is_dir()
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "subtask": "c", "provider": "codex"})
    assert actions(services, task)[-1][:2] == ("REQUEST_EXECUTION", "ACCEPTED")
    assert q(services, "SELECT 1 FROM events WHERE type = 'WORKSPACE_RECLAIMED'")


def _to_integration_review(services, agents, repo, task) -> dict:
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('hello')\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    finish_verification(services, agents, task)
    [integration_review] = active(services, task, "REVIEWER")
    assert integration_review["subtask_id"] is None
    return integration_review


def test_a_failed_integration_review_is_retried_then_blocks_with_the_reason(api, services, agents, repo, task):
    """T-53: the integration reviewer failed (structured output) and the task waited in REVIEW for hours."""
    review = _to_integration_review(services, agents, repo, task)
    for _ in range(2):
        answer(services, agents, review["id"], {}, ok=False)
        assert state(api, task) == "REVIEW"
        [review] = active(services, task, "REVIEWER")  # requested again
        assert review["subtask_id"] is None
    answer(services, agents, review["id"], {}, ok=False)
    assert state(api, task) == "BLOCKED"
    [row] = q(services, "SELECT state_reason FROM tasks WHERE key = %s", task)
    assert "integration review by claude failed" in row["state_reason"] and "failed 3 times" in row["state_reason"]


def test_a_retry_gives_the_integration_review_fresh_attempts(api, services, agents, repo, task):
    """T-8: after a retry, one more max-turns failure blocked the task at once ("failed 5 times"): the count included
    the failures that had already blocked it."""
    review = _to_integration_review(services, agents, repo, task)
    assert review["spec"]["assignment"]["max_turns"] == 120  # reviewers get more turns than a development step
    for _ in range(3):
        answer(services, agents, review["id"], {}, ok=False)
        if state(api, task) == "REVIEW":
            [review] = active(services, task, "REVIEWER")
    assert state(api, task) == "BLOCKED"
    assert api.post(f"/v1/tasks/{task}/retry").status_code == 200
    with services.ctx.db.transaction() as cur:  # stand-in: the retried task reached the integration review again
        cur.execute("UPDATE tasks SET state = 'REVIEW' WHERE key = %s", (task,))
    services.orchestration._integration_review_later(task, "claude")
    [review] = active(services, task, "REVIEWER")
    answer(services, agents, review["id"], {}, ok=False)
    assert state(api, task) == "REVIEW"  # reviewed again, not blocked by the failures before the retry
    assert len(active(services, task, "REVIEWER")) == 1


def test_a_lost_integration_review_is_requested_again(api, services, agents, repo, task):
    review = _to_integration_review(services, agents, repo, task)
    with services.ctx.db.transaction() as cur:  # stand-in: the review was lost (the platform never saw it finish)
        cur.execute("UPDATE executions SET state = 'FAILED', ended_at = now() WHERE id = %s", (review["id"],))
        cur.execute("UPDATE tasks SET updated_at = now() - interval '5 minutes' WHERE key = %s", (task,))
    services.orchestration.heal_reviews()
    [again] = active(services, task, "REVIEWER")
    assert again["id"] != review["id"] and again["subtask_id"] is None and state(api, task) == "REVIEW"
    services.orchestration._integration_review_later(task, again["provider"])  # a concurrent path asks again
    services.orchestration.heal_reviews()
    assert [r["id"] for r in active(services, task, "REVIEWER")] == [again["id"]]  # still one reviewer


def test_accepted_work_still_integrates_after_a_resolution_that_could_not_start(api, services, agents, repo, task):
    """T-53: the resolution of a conflict was refused for budget, yet its workspace swap was kept; once the budget was
    raised, integration found "no committed work" although both subtasks were accepted."""
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('task')\n"})
    commit(repo, "the person writes the same file", {"a.py": "print('person')\n"})
    q(services, "UPDATE budgets SET limits = jsonb_set(limits, '{agent_launches}', to_jsonb((consumed->>'agent_launches')::int)) "
      "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)  # no room left: the review already started
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])  # accepted -> integrate -> conflict
    assert "INTEGRATION_CONFLICT" in [r["type"] for r in q(services, "SELECT type FROM events e JOIN tasks t ON t.id = e.task_id "
                                                           "WHERE t.key = %s", task)]
    rows = q(services, "SELECT kind, status FROM workspaces w JOIN tasks t ON t.id = w.task_id WHERE t.key = %s "
             "AND w.kind <> 'VERIFICATION'", task)
    assert rows == [{"kind": "DEVELOPMENT", "status": "ACTIVE"}]  # the refused resolution swapped nothing
    q(services, "UPDATE budgets SET limits = jsonb_set(limits, '{agent_launches}', '40') "
      "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", task)  # stand-in: the person raises the budget
    with services.ctx.unit_of_work() as uow:
        services.tasks.transition(uow, services.tasks.get(uow, task, lock=True), S.RUNNING, trigger=Trigger.USER,
                                  actor="test", reason="budget raised")
    with services.ctx.unit_of_work() as uow:  # the next attempt reclaims the clone the refused one left behind
        services.orchestration._resolve_conflict(uow, services.tasks.get(uow, task, lock=True),
                                                 {"conflicting_ref": "refs/hermes/workspaces/t-1-1a1", "conflicts": ["a.py"]})
    assert [r["status"] for r in q(services, "SELECT status FROM workspaces WHERE kind = 'CONFLICT'")] == ["ACTIVE"]
    assert "WORKSPACE_RECLAIMED" in [r["type"] for r in q(services, "SELECT type FROM events")]
    [resolver] = active(services, task, "DEVELOPER")
    with services.ctx.unit_of_work() as uow:  # the resolver commits nothing: the accepted work is integrated anyway
        uow.cur.execute("UPDATE executions SET state = 'SUCCEEDED', ended_at = now() WHERE id = %s", (resolver["id"],))
        result = services.git.integrate(uow, task, retest=False)
    assert not result["ok"] and result["conflicts"] == ["a.py"]  # the real conflict, not "no committed work"


@pytest.mark.parametrize(("blocked", "planning"), [(True, False), (False, False), (True, True)])
def test_a_resolution_without_a_merge_stops_instead_of_looping(api, services, agents, repo, task, blocked, planning):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "print('task')\n"})
    commit(repo, "the person writes the same file", {"a.py": "print('person')\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    [resolver] = active(services, task, "DEVELOPER")
    with services.ctx.unit_of_work() as uow:
        assert services.orchestration._integrate_if_ready(uow, services.tasks.get(uow, task, lock=True), explicit=True) == "conflict resolution running"
        assert services.orchestration.request_step(uow, services.tasks.get(uow, task, lock=True)) is None
    if planning:  # stand-in: integration started from the accepted-plan recovery path, in PLANNING (Codex review)
        q(services, "UPDATE tasks SET state = 'PLANNING' WHERE key = %s", task)
    result = {**DONE, "status": "blocked" if blocked else "completed",
              "blocked_reason": "PostgreSQL unavailable" if blocked else None}
    answer(services, agents, resolver["id"], result)
    assert state(api, task) == "BLOCKED"
    assert not active(services, task, "DEVELOPER")
    assert not active(services, task, "ORCHESTRATOR")


def test_a_paused_agent_gets_no_new_work(api, services, agents, task):
    from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

    assert api.post("/v1/agents/codex/pause", {}, token=PLUGIN_TOKEN, principal="dashboard:operator").status_code == 200
    with services.ctx.unit_of_work() as uow:
        assert services.orchestration._available(uow) == ["claude"]


def test_a_review_that_cannot_start_for_budget_asks_the_person_once(api, services, agents, repo, task):
    """T-56: the integration review could not start (budget used up); the heal retried every few seconds for hours and
    the person was never asked to raise the budget."""
    review = _to_integration_review(services, agents, repo, task)
    with services.ctx.db.transaction() as cur:  # stand-in: the review was lost and the budget is used up
        cur.execute("UPDATE executions SET state = 'FAILED', ended_at = now() WHERE id = %s", (review["id"],))
        cur.execute("UPDATE tasks SET updated_at = now() - interval '5 minutes' WHERE key = %s", (task,))
        cur.execute("UPDATE budgets SET limits = jsonb_set(limits, '{agent_launches}', to_jsonb((consumed->>'agent_launches')::int)) "
                    "WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", (task,))
    for _ in range(3):
        services.orchestration.heal_reviews()
    assert state(api, task) == "PAUSED_BUDGET"  # the person is asked (notification, Raise budget)
    [row] = q(services, "SELECT count(*) AS n FROM events e JOIN tasks t ON t.id = e.task_id WHERE t.key = %s "
                        "AND e.type = 'RETRY_SCHEDULED'", task)
    assert row["n"] == 1


def test_a_plan_refused_while_an_assumption_waits_is_shown_to_the_next_step(api, services, agents, task):
    step(services, agents, task, {"type": "RECORD_ASSUMPTION", "text": "Only public fields are exposed", "level": "HIGH",
                                  "reversible": False},
         {"type": "SET_PLAN", "text": "Harden the public API", "subtasks": [plan_item("harden")]})
    assert actions(services, task)[-1][:2] == ("SET_PLAN", "REJECTED")
    [approval] = q(services, "SELECT * FROM approvals WHERE action = 'ASSUMPTION'")
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"})
    services.scheduler.run_once()
    [execution] = active(services, task, "ORCHESTRATOR")
    prompt = agents.specs[str(execution["id"])]["inputs"]["prompt.md"]
    assert "it proposed: Harden the public API; harden: subtask harden" in prompt


def test_quality_gate_reasons_stay_in_view_until_fixed(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "a = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    # Stand-in for a failed gate whose announcement an earlier step already read (T-6).
    q(services, "INSERT INTO quality_gate_evaluations (id, task_id, commit_sha, config_hash, policy_version, risk, requirements, "
      "residual_risk, outcome) SELECT gen_random_uuid(), id, 'abc', 'h', 'p', 'MEDIUM', %s::jsonb, 'r', 'FAIL' FROM tasks "
      "WHERE key = %s", json.dumps([{"name": "requirements", "detail": "unmet: README.md was not updated", "status": "FAIL"},
                                    {"name": "exception_approval", "detail": "risk HIGH needs an exception", "status": "APPROVAL_REQUIRED"},
                                    {"name": "tests", "detail": "test passed", "status": "PASS"}]), task)
    q(services, "UPDATE tasks SET state = 'FIX_REQUIRED', step_requested = true, orchestrator_cursor_seq = "
      "(SELECT max(seq) FROM events) WHERE key = %s", task)
    for execution in active(services, task, "TESTER") + active(services, task, "REVIEWER"):
        agents.finish_agent(str(execution["id"]), b"", exit_code=1)
    q(services, "UPDATE verifications SET state = 'ERROR' WHERE state IN ('PREPARING', 'RUNNING')")
    services.scheduler.run_once()
    [execution] = active(services, task, "ORCHESTRATOR")
    prompt = agents.specs[str(execution["id"])]["inputs"]["prompt.md"]
    assert "## What the last Quality Gate evaluation requires" in prompt
    assert "- requirements: unmet: README.md was not updated" in prompt and "test passed" not in prompt
    assert "- exception_approval: risk HIGH needs an exception" in prompt
    assert "Every subtask is accepted" in prompt


def test_an_invalid_plan_key_is_named_in_the_rejection(api, services, agents, task):
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("portfolio-final-tests"), plan_item("a"), plan_item("a")]})
    [(kind, outcome, reason)] = actions(services, task)
    assert (kind, outcome) == ("SET_PLAN", "REJECTED")
    assert "invalid: 'portfolio-final-tests'" in reason and "repeated: a" in reason and "1 to 20 characters" in reason


def test_a_new_subtask_may_depend_on_an_accepted_one_the_plan_leaves_out(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("a"), plan_item("b"))
    [dev] = active(services, task, "DEVELOPER")
    develop(services, agents, repo, dev, {"a.py": "a = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    assert subtasks(services, task)["a"]["state"] == "ACCEPTED"
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("b"), plan_item("fix", depends_on=["a", "nope"])]})
    assert "accepted subtasks you can depend on: a" in actions(services, task)[-1][2]
    [execution] = active(services, task, "ORCHESTRATOR")
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("b"), plan_item("fix", depends_on=["a"])]})
    assert actions(services, task)[-1][:2] == ("SET_PLAN", "ACCEPTED")
    plan = subtasks(services, task)
    assert plan["a"]["state"] == "ACCEPTED" and plan["fix"]["state"] == "READY"
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("b"), plan_item("fix", depends_on=["a"])]})
    assert actions(services, task)[-1][2].startswith("the plan is unchanged")  # the same short plan is not a new version


def test_a_kept_accepted_subtask_still_counts_for_dependency_cycles(api, services, agents, repo, task):
    plan_and_start(services, agents, task, plan_item("b"), plan_item("a", depends_on=["b"]), start="b")
    develop(services, agents, repo, active(services, task, "DEVELOPER")[0], {"b.py": "b = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "subtask": "a", "provider": "codex"})
    develop(services, agents, repo, active(services, task, "DEVELOPER")[0], {"a.py": "a = 1\n"})
    review_verdict(services, agents, active(services, task, "REVIEWER")[0])
    q(services, "UPDATE tasks SET state = 'RUNNING', step_requested = true WHERE key = %s", task)  # stand-in: before integration
    q(services, "UPDATE subtasks SET state = 'FIX_REQUIRED' WHERE local_key = 'b'")
    services.scheduler.run_once()
    step(services, agents, task, {"type": "SET_PLAN", "subtasks": [plan_item("b", depends_on=["a"])]})
    assert "dependency cycle" in actions(services, task)[-1][2]


# ------------------------------------------------------------------ agents out of credit

QUOTA_RESULT = b'{"type":"result","subtype":"error","is_error":true,"result":"You have hit your usage limit"}'


def out_of_credit(services, provider, hours=2):
    """Stand-in for the usage an execution of `provider` reported: its session window is used up."""
    q(services, "INSERT INTO provider_usage (provider, identity, window_kind, used_percent, resets_at, observed_at) "
      "VALUES (%s, 'default', 'session', 100, now() + make_interval(hours => %s), now()) ON CONFLICT (provider, identity, window_kind) "
      "DO UPDATE SET used_percent = 100, resets_at = EXCLUDED.resets_at, observed_at = now()", provider, hours)


def credit_back(services, provider):
    q(services, "UPDATE provider_usage SET resets_at = now() - interval '1 minute' WHERE provider = %s", provider)


def test_a_review_waits_for_its_reviewer_credit_then_starts_by_itself(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    [dev] = active(services, task, "DEVELOPER")
    assert dev["provider"] == "codex"
    out_of_credit(services, "claude")
    develop(services, agents, repo, dev, {"a.py": "a = 1\n"})
    assert active(services, task, "REVIEWER") == []  # never launched only to fail
    [pending] = q(services, "SELECT * FROM pending_launches WHERE kind = 'SUBTASK_REVIEW'")
    assert "claude is out of credit until" in pending["reason"]
    services.scheduler.run_once()
    services.scheduler.run_once()
    notices = q(services, "SELECT summary FROM events WHERE type = 'WAITING_FOR_CREDIT'")
    assert len(notices) == 1 and notices[0]["summary"].startswith("claude is out of credit until")
    assert state(api, task) == "RUNNING" and subtasks(services, task)["a"]["state"] == "IN_REVIEW"
    credit_back(services, "claude")
    services.scheduler.run_once()
    [rev] = active(services, task, "REVIEWER")
    assert rev["provider"] == "claude"


def test_a_review_that_hits_the_quota_waits_instead_of_counting_as_a_failure(api, services, agents, repo, task):
    plan_and_start(services, agents, task)
    develop(services, agents, repo, active(services, task, "DEVELOPER")[0], {"a.py": "a = 1\n"})
    for _ in range(3):  # more than the review retries a real failure gets
        [rev] = active(services, task, "REVIEWER")
        agents.finish_agent(str(rev["id"]), QUOTA_RESULT, exit_code=1)
        services.scheduler.run_once()
        assert subtasks(services, task)["a"]["state"] == "IN_REVIEW"
        assert "claude is out of credit until" in q(services, "SELECT reason FROM pending_launches")[0]["reason"]
        q(services, "UPDATE executions SET ended_at = ended_at - interval '31 minutes' WHERE failure_class = 'QUOTA'")
        services.scheduler.run_once()  # the fallback wait (no usage window) has passed: it is tried again
    assert state(api, task) == "RUNNING"


def test_the_orchestrator_moves_to_the_other_agent_before_a_step_when_its_own_is_out_of_credit(api, services, agents, task):
    plan_and_start(services, agents, task)
    out_of_credit(services, "claude")
    q(services, "UPDATE tasks SET step_requested = true WHERE key = %s", task)
    services.scheduler.run_once()
    [step_execution] = active(services, task, "ORCHESTRATOR")
    assert step_execution["provider"] == "codex"
    assert "FAILOVER_COMPLETED" in [r["type"] for r in q(services, "SELECT type FROM events")]


def test_when_both_agents_are_out_of_credit_steps_wait_without_blocking_the_task(api, services, agents, task):
    out_of_credit(services, "codex")
    [execution] = active(services, task, "ORCHESTRATOR")
    agents.finish_agent(str(execution["id"]), QUOTA_RESULT, exit_code=1)
    services.scheduler.run_once()
    services.scheduler.run_once()
    assert state(api, task) == "PLANNING"
    assert q(services, "SELECT orchestrator_failures FROM tasks WHERE key = %s", task)[0]["orchestrator_failures"] == 0
    assert active(services, task, "ORCHESTRATOR") == []
    assert "is out of credit until" in q(services, "SELECT reason FROM pending_launches WHERE kind = 'STEP'")[0]["reason"]
