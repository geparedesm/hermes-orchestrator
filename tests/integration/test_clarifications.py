"""Clarifying questions during planning and supervised plans (docs/design/clarifications.md), against the real state
machine, with the orchestrator's answers played by fake agent results."""

from __future__ import annotations

import pytest

from conftest import PLUGIN_TOKEN, git_repo  # type: ignore[import-not-found]
from test_git import APP, PROJECT_YAML  # type: ignore[import-not-found]
import test_orchestration  # type: ignore[import-not-found]
from test_orchestration import active, actions, plan_item, q, state, step, subtasks  # type: ignore[import-not-found]

pytestmark = [pytest.mark.integration, pytest.mark.orchestration]
agents = pytest.fixture(test_orchestration.agents.__wrapped__)
QUESTIONS = [
    {"text": "How often should alerts be sent?", "why": "changes the cron and the template",
     "options": [{"label": "Daily digest", "recommended": True}, {"label": "Immediately", "recommended": False}], "allow_other": False},
    {"text": "Which jobs trigger an alert?", "why": "changes the filter",
     "options": [{"label": "Score at least 70", "recommended": True}, {"label": "Any new job", "recommended": False}],
     "allow_other": True},
]


def make_task(api, services, projects_root, *, yaml=PROJECT_YAML, approve_plan=False) -> str:
    git_repo(projects_root / "demo", {".hermes/project.yaml": yaml, "app.py": APP, "README.md": "# demo\n"})
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    body = {"project": "demo", "request": "Send job alerts"}
    if approve_plan:
        body["approve_plan"] = True
    key = api.post("/v1/tasks", body).json()["key"]
    services.scheduler.run_once()  # lease + PLANNING + first step
    return key


@pytest.fixture
def task(api, services, agents, projects_root) -> str:
    return make_task(api, services, projects_root)


def ask(services, agents, task, questions=QUESTIONS):
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Send job alerts to users."},
         {"type": "ASK_QUESTIONS", "questions": questions})


def pending_round(services, task) -> dict:
    [row] = q(services, "SELECT c.* FROM clarifications c JOIN tasks t ON t.id = c.task_id WHERE t.key = %s AND c.state = 'PENDING'", task)
    return row


def answer(api, task, number, **body):
    return api.post(f"/v1/tasks/{task}/clarifications/{number}/answer", body)


def bundle(services, task) -> str:
    """What the orchestrator's next step reads."""
    with services.ctx.unit_of_work() as uow:
        return services.orchestration.context_bundle(uow, services.tasks.get(uow, task))


def requirements(services, task) -> list[dict]:
    return q(services, "SELECT r.version, r.source FROM requirement_versions r JOIN tasks t ON t.id = r.task_id WHERE t.key = %s "
             "ORDER BY r.version", task)


def test_questions_wait_for_the_person_then_planning_resumes_with_the_answers(api, services, agents, task):
    ask(services, agents, task)
    assert state(api, task) == "APPROVAL_REQUIRED"
    round_ = pending_round(services, task)
    summary = api.get("/v1/dashboard/summary", token=PLUGIN_TOKEN, principal="dashboard:operator").json()
    assert [c["number"] for c in summary["required_actions"]["clarifications"]] == [round_["number"]]
    response = answer(api, task, round_["number"], answers={"q1": {"option": "b"}, "q2": {"text": "only remote jobs"}})
    assert response.status_code == 200, response.text
    assert state(api, task) == "PLANNING"
    assert [r["source"] for r in requirements(services, task)] == ["ORCHESTRATOR", "CLARIFICATION"]
    services.scheduler.run_once()  # the answer triggers the next step
    assert active(services, task, "ORCHESTRATOR")
    prompt = bundle(services, task)
    assert "How often should alerts be sent? — Immediately" in prompt and "only remote jobs" in prompt
    assert "No more questions in this planning episode" in prompt


def test_the_recommended_answers_in_one_click(api, services, agents, task):
    ask(services, agents, task)
    round_ = pending_round(services, task)
    assert answer(api, task, round_["number"], use_recommended=True).status_code == 200
    [row] = q(services, "SELECT answers FROM clarifications WHERE id = %s", round_["id"])
    assert row["answers"] == {"q1": {"option": "a", "label": "Daily digest"}, "q2": {"option": "a", "label": "Score at least 70"}}


def test_answering_does_not_rearm_another_round(api, services, agents, task):
    ask(services, agents, task)
    answer(api, task, pending_round(services, task)["number"], use_recommended=True)
    services.scheduler.run_once()
    step(services, agents, task, {"type": "ASK_QUESTIONS", "questions": QUESTIONS})
    assert actions(services, task)[-1][:2] == ("ASK_QUESTIONS", "REJECTED")
    assert "no more questions" in actions(services, task)[-1][2]
    assert state(api, task) == "PLANNING"


def test_a_revision_cancels_pending_questions_and_opens_a_new_episode(api, services, agents, task):
    ask(services, agents, task)
    round_ = pending_round(services, task)
    assert api.post(f"/v1/tasks/{task}/revise", {"text": "Alerts by email only."}).status_code in (200, 201)
    assert state(api, task) == "PLANNING"
    [row] = q(services, "SELECT state, cancel_reason FROM clarifications WHERE id = %s", round_["id"])
    assert row["state"] == "CANCELLED" and row["cancel_reason"] == "requirements revised"
    assert answer(api, task, round_["number"], use_recommended=True).status_code == 409
    services.scheduler.run_once()
    step(services, agents, task, {"type": "ASK_QUESTIONS", "questions": QUESTIONS[:1]})  # a new episode: one more round
    assert state(api, task) == "APPROVAL_REQUIRED"


def test_answers_are_checked_and_only_people_answer(api, services, agents, task):
    ask(services, agents, task)
    number = pending_round(services, task)["number"]
    assert answer(api, task, number, answers={"q1": {"option": "z"}, "q2": {"option": "a"}}).status_code == 400
    assert answer(api, task, number, answers={"q1": {"text": "weekly"}, "q2": {"option": "a"}}).status_code == 400  # q1: options only
    assert answer(api, task, number, answers={"q1": {"option": "a"}}).status_code == 400  # q2 unanswered
    agent = api.post(f"/v1/tasks/{task}/clarifications/{number}/answer", {"use_recommended": True}, token=PLUGIN_TOKEN,
                     principal="hermes-agent:orchestration")
    assert agent.status_code in (400, 401, 403)
    assert state(api, task) == "APPROVAL_REQUIRED"


def test_a_cancelled_task_takes_no_answers(api, services, agents, task):
    ask(services, agents, task)
    number = pending_round(services, task)["number"]
    api.post(f"/v1/tasks/{task}/cancel")
    assert answer(api, task, number, use_recommended=True).status_code == 409


def test_later_actions_of_the_asking_step_are_not_applied(api, services, agents, task):
    step(services, agents, task, {"type": "ASK_QUESTIONS", "questions": QUESTIONS},
         {"type": "SET_PLAN", "subtasks": [plan_item("a")]})
    assert actions(services, task)[-1][:2] == ("SET_PLAN", "REJECTED") and not subtasks(services, task)


# ------------------------------------------------------------------ supervised plans


SUPERVISED = PROJECT_YAML + "planning: {approve_plan: true}\n"


def plan(services, agents, task, *items):
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "Greeting endpoint."},
         {"type": "SET_PLAN", "subtasks": list(items or [plan_item("a")])},
         {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"})


def plan_approval(api) -> dict:
    [approval] = [a for a in api.get("/v1/approvals").json()["approvals"] if a["action"] == "PLAN"]
    return approval


def test_a_supervised_plan_starts_nothing_until_approved(api, services, agents, projects_root):
    task = make_task(api, services, projects_root, yaml=SUPERVISED)
    plan(services, agents, task)
    assert state(api, task) == "APPROVAL_REQUIRED" and not active(services, task, "DEVELOPER")
    assert actions(services, task)[-1][:2] == ("REQUEST_EXECUTION", "REJECTED")
    approval = plan_approval(api)
    summary = api.get("/v1/dashboard/summary", token=PLUGIN_TOKEN, principal="dashboard:operator").json()
    [card] = [a for a in summary["required_actions"]["approvals"] if a["action"] == "PLAN"]
    assert card["details"]["changes"][0]["text"].startswith("a: subtask a — implement, risk low")
    assert api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"}).status_code == 200
    assert state(api, task) == "QUEUED"
    services.scheduler.run_once()
    step(services, agents, task, {"type": "REQUEST_EXECUTION", "role": "DEVELOPER", "subtask": "a", "provider": "codex"})
    assert active(services, task, "DEVELOPER")


def test_a_rejected_plan_is_planned_again_with_the_note(api, services, agents, projects_root):
    task = make_task(api, services, projects_root, yaml=SUPERVISED)
    plan(services, agents, task)
    approval = plan_approval(api)
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "REJECT", "note": "split the API from the UI"})
    assert state(api, task) == "PLANNING"
    services.scheduler.run_once()
    assert active(services, task, "ORCHESTRATOR")
    assert q(services, "SELECT 1 FROM events e JOIN tasks t ON t.id = e.task_id WHERE t.key = %s AND e.type = 'ORCHESTRATOR_INPUT' "
             "AND e.summary LIKE %s", task, "%split the API from the UI%")
    plan(services, agents, task, plan_item("a"), plan_item("b"))
    assert plan_approval(api)["id"] != approval["id"]  # a new plan, a new decision


def test_a_revision_invalidates_a_pending_plan_approval(api, services, agents, projects_root):
    task = make_task(api, services, projects_root, yaml=SUPERVISED)
    plan(services, agents, task)
    approval = plan_approval(api)
    api.post(f"/v1/tasks/{task}/revise", {"text": "Also a goodbye endpoint."})
    assert state(api, task) == "PLANNING"
    assert api.get(f"/v1/approvals/{approval['id']}").json()["state"] == "INVALIDATED"


def test_a_task_can_ask_for_plan_review_when_the_project_does_not(api, services, agents, projects_root):
    task = make_task(api, services, projects_root, approve_plan=True)
    assert api.get(f"/v1/tasks/{task}").json()["approve_plan"] is True
    plan(services, agents, task)
    assert state(api, task) == "APPROVAL_REQUIRED" and plan_approval(api)


def test_answers_stay_in_context_past_long_requirements_and_oversized_ones_are_refused(api, services, agents, projects_root):
    task = make_task(api, services, projects_root)
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "x" * 20_000},
         {"type": "ASK_QUESTIONS", "questions": QUESTIONS})
    assert answer(api, task, pending_round(services, task)["number"], use_recommended=True).status_code == 200
    assert "How often should alerts be sent? — Daily digest" in bundle(services, task)  # beyond the 5,000-character cut

    with services.ctx.db.transaction() as cur:  # a second round on a near-limit task (stand-in for a long history)
        cur.execute("UPDATE clarifications SET state = 'CANCELLED' WHERE task_id = (SELECT id FROM tasks WHERE key = %s)", (task,))
        cur.execute("UPDATE tasks SET state = 'PLANNING', resume_state = NULL WHERE key = %s", (task,))
    services.scheduler.run_once()
    step(services, agents, task, {"type": "SET_REQUIREMENTS", "text": "y" * 49_990},
         {"type": "ASK_QUESTIONS", "questions": QUESTIONS[:1]})
    response = answer(api, task, pending_round(services, task)["number"], use_recommended=True)
    assert response.status_code == 409 and "size limit" in response.json()["message"]
    assert state(api, task) == "APPROVAL_REQUIRED"  # nothing applied: still waiting, answers not lost
