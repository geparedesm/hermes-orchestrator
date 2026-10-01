"""Phase 7 through the control plane: lease, orchestrator steps and actions, the subtask cycle
(develop -> cross-review -> fix), integration -> verification -> integration review -> Quality Gate,
budgets, and the scenarios from the Codex adversarial review of the design."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import git_repo  # type: ignore[import-not-found]
from fake_agents import FakeAgentManager  # type: ignore[import-not-found]
from test_git import APP, PROJECT_YAML, REVIEW, commit, finish_verification  # type: ignore[import-not-found]

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
             "level": None, "reversible": None, "category": None, "anchors": None, **a} for a in actions]
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


def test_wait_without_new_events_does_not_loop(api, services, agents, task):
    step(services, agents, task, {"type": "WAIT"})
    for _ in range(3):
        services.scheduler.run_once()
    assert len(executions(services, task, "ORCHESTRATOR")) == 1
    api.post(f"/v1/tasks/{task}/revise", {"text": "Also greet in Spanish."})
    services.scheduler.run_once()
    assert len(executions(services, task, "ORCHESTRATOR")) == 2  # an external event triggers the next step


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
