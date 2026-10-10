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


def test_the_expired_list_holds_only_approvals_that_can_be_requested_again(api, services, agents, task):
    first = api.post(f"/v1/tasks/{task}/budget", {"add": {"retries": 1}}).json()
    expire(services, first["id"])
    listed = [a["id"] for a in api.get("/v1/approvals?state=EXPIRED", **DASHBOARD).json()["approvals"]]
    assert listed == [first["id"]]
    renewed = api.post(f"/v1/approvals/{first['id']}/renew", **DASHBOARD).json()
    expire(services, renewed["id"])
    listed = [a["id"] for a in api.get("/v1/approvals?state=EXPIRED", **DASHBOARD).json()["approvals"]]
    assert listed == [renewed["id"]]  # the renewed one dropped out; the newest expired is reachable
    api.post(f"/v1/tasks/{task}/cancel")
    assert api.get("/v1/approvals?state=EXPIRED", **DASHBOARD).json()["approvals"] == []


def test_an_applied_budget_raise_is_never_asked_again(api, services, agents, task):
    raised = api.post(f"/v1/tasks/{task}/budget", {"add": {"agent_launches": 5}}).json()
    api.post(f"/v1/approvals/{raised['id']}/decision", {"decision": "APPROVE"})  # applied and recorded as used
    orch.q(services, "UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s", raised["id"])
    with services.ctx.unit_of_work() as uow:
        services.approvals.expire_due(uow)
    response = api.post(f"/v1/approvals/{raised['id']}/renew", **DASHBOARD)
    assert response.status_code == 409 and "CONSUMED" in response.text
    # An approved one that expired anyway (rows from before approvals were recorded as used) is refused too.
    orch.q(services, "UPDATE approvals SET state = 'EXPIRED' WHERE id = %s", raised["id"])
    response = api.post(f"/v1/approvals/{raised['id']}/renew", **DASHBOARD)
    assert response.status_code == 409 and "already decided" in response.text
    assert api.get("/v1/approvals?state=EXPIRED", **DASHBOARD).json()["approvals"] == []


def test_approvals_applied_when_decided_are_recorded_as_used(api, services, agents, task):
    raised = api.post(f"/v1/tasks/{task}/budget", {"add": {"retries": 2}}).json()
    api.post(f"/v1/approvals/{raised['id']}/decision", {"decision": "APPROVE"})
    orch.step(services, agents, task, {"type": "RECORD_ASSUMPTION", "text": "Keep the API", "level": "HIGH", "reversible": False})
    [assumption] = orch.q(services, "SELECT id FROM approvals WHERE action = 'ASSUMPTION'")
    api.post(f"/v1/approvals/{assumption['id']}/decision", {"decision": "APPROVE"})
    states = {r["action"]: r["state"] for r in orch.q(services, "SELECT action, state FROM approvals WHERE task_id IS NOT NULL")}
    assert states == {"BUDGET_INCREASE": "CONSUMED", "ASSUMPTION": "CONSUMED"}
    orch.q(services, "UPDATE approvals SET expires_at = now() - interval '1 minute'")
    with services.ctx.unit_of_work() as uow:
        assert services.approvals.expire_due(uow) == 0  # used approvals never turn into "expired"


def test_rows_applied_before_the_fix_are_corrected(api, services, agents, task):
    import importlib.util
    import sys
    import types
    from pathlib import Path

    raised = api.post(f"/v1/tasks/{task}/budget", {"add": {"retries": 1}}).json()
    api.post(f"/v1/approvals/{raised['id']}/decision", {"decision": "APPROVE"})
    orch.q(services, "UPDATE approvals SET state = 'EXPIRED', consumed_at = NULL, invalidated_reason = 'approval window elapsed' "
           "WHERE id = %s", raised["id"])  # how such a row looked before the fix
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0011_applied_approvals.py"
    spec = importlib.util.spec_from_file_location("m0011", path)
    module = importlib.util.module_from_spec(spec)
    statements: list[str] = []
    sys.modules["alembic"] = types.SimpleNamespace(op=types.SimpleNamespace(execute=statements.append))
    try:
        spec.loader.exec_module(module)
        module.upgrade()
    finally:
        del sys.modules["alembic"]
    orch.q(services, statements[0])
    [row] = orch.q(services, "SELECT state, consumed_at IS NOT NULL AS used FROM approvals WHERE id = %s", raised["id"])
    assert row["state"] == "CONSUMED" and row["used"]
