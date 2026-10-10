"""Expired approvals requested again (Dashboard "Request again", /orch renew)."""

from __future__ import annotations

import pytest

import test_orchestration as orch  # type: ignore[import-not-found]
from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

pytestmark = [pytest.mark.integration, pytest.mark.orchestration]
agents = pytest.fixture(orch.agents.__wrapped__)
repo = pytest.fixture(orch.repo.__wrapped__)
task = pytest.fixture(orch.task.__wrapped__)
DASHBOARD = {"token": PLUGIN_TOKEN, "principal": "dashboard:operator"}


def expire(services, approval_id):
    orch.q(services, "UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s", approval_id)
    with services.ctx.unit_of_work() as uow:
        services.approvals.expire_due(uow)
    assert orch.q(services, "SELECT state FROM approvals WHERE id = %s", approval_id)[0]["state"] == "EXPIRED"


def test_an_expired_budget_increase_is_requested_again_and_still_works(api, services, agents, task):
    first = api.post(f"/v1/tasks/{task}/budget", {"add": {"agent_launches": 5}}).json()
    expire(services, first["id"])
    response = api.post(f"/v1/approvals/{first['id']}/renew", **DASHBOARD)
    assert response.status_code == 201, response.text
    renewed = response.json()
    assert renewed["state"] == "PENDING" and renewed["renewed_from"] == first["id"] and renewed["action"] == "BUDGET_INCREASE"
    assert renewed["requested_by"] == "dashboard:operator"
    [event] = orch.q(services, "SELECT actor, data FROM events WHERE type = 'APPROVAL_RENEWED'")
    assert event["actor"] == "dashboard:operator" and event["data"]["renewed_from"] == first["id"]
    before = orch.q(services, "SELECT (limits->>'agent_launches')::int AS n FROM budgets")[0]["n"]
    assert api.post(f"/v1/approvals/{renewed['id']}/decision", {"decision": "APPROVE"}).status_code == 200
    assert orch.q(services, "SELECT (limits->>'agent_launches')::int AS n FROM budgets")[0]["n"] == before + 5
    # Once is enough: the expired one now has a newer approval.
    again = api.post(f"/v1/approvals/{first['id']}/renew", **DASHBOARD)
    assert again.status_code == 409 and "newer" in again.text


def test_only_expired_approvals_by_an_approver_for_live_tasks(api, services, agents, task):
    pending = api.post(f"/v1/tasks/{task}/budget", {"add": {"retries": 1}}).json()
    assert api.post(f"/v1/approvals/{pending['id']}/renew", **DASHBOARD).status_code == 409  # not expired
    expire(services, pending["id"])
    assert api.post(f"/v1/approvals/{pending['id']}/renew", token=PLUGIN_TOKEN, principal="whatsapp:stranger").status_code == 403
    api.post(f"/v1/tasks/{task}/cancel")
    response = api.post(f"/v1/approvals/{pending['id']}/renew", **DASHBOARD)
    assert response.status_code == 409 and "CANCELLED" in response.text


def test_an_expired_assumption_is_asked_again_while_it_is_open(api, services, agents, task):
    orch.step(services, agents, task, {"type": "RECORD_ASSUMPTION", "text": "Drop the legacy endpoint", "level": "HIGH",
                                       "reversible": False})
    [first] = orch.q(services, "SELECT * FROM approvals WHERE action = 'ASSUMPTION'")
    expire(services, first["id"])
    renewed = api.post(f"/v1/approvals/{first['id']}/renew", **DASHBOARD).json()
    assert str(orch.q(services, "SELECT approval_id FROM assumptions")[0]["approval_id"]) == renewed["id"]
    api.post(f"/v1/approvals/{renewed['id']}/decision", {"decision": "APPROVE"})
    assert orch.q(services, "SELECT status FROM assumptions")[0]["status"] == "APPROVED"
    assert orch.state(api, task) == "PLANNING"


def test_an_assumption_resolved_since_is_not_asked_again(api, services, agents, task):
    orch.step(services, agents, task, {"type": "RECORD_ASSUMPTION", "text": "Drop the legacy endpoint", "level": "HIGH",
                                       "reversible": False})
    [first] = orch.q(services, "SELECT * FROM approvals WHERE action = 'ASSUMPTION'")
    expire(services, first["id"])
    orch.q(services, "UPDATE assumptions SET status = 'REJECTED'")  # stand-in: replanned without it
    response = api.post(f"/v1/approvals/{first['id']}/renew", **DASHBOARD)
    assert response.status_code == 409 and "no longer open" in response.text
