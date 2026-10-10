"""An expired MERGE approval is requested again through the normal merge request."""

from __future__ import annotations

import pytest

import test_git  # type: ignore[import-not-found]
from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration
agents = pytest.fixture(test_git.agents.__wrapped__)
repo = pytest.fixture(test_git.repo.__wrapped__)
task = pytest.fixture(test_git.task.__wrapped__)
DASHBOARD = {"token": PLUGIN_TOKEN, "principal": "dashboard:operator"}


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def expire(services, approval_id):
    q(services, "UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s", approval_id)
    with services.ctx.unit_of_work() as uow:
        services.approvals.expire_due(uow)


def test_an_expired_merge_is_rebuilt_from_the_current_change(api, services, repo, agents, task):
    first = test_git.ready_for_merge(api, services, repo, agents, task)
    expire(services, first["id"])
    response = api.post(f"/v1/approvals/{first['id']}/renew", **DASHBOARD)
    assert response.status_code == 201, response.text
    renewed = response.json()
    assert renewed["action"] == "MERGE" and renewed["renewed_from"] == first["id"]
    assert renewed["subject"]["head_sha"] == first["subject"]["head_sha"]  # same integrated commit, re-checked
    assert str(q(services, "SELECT merge_approval_id FROM git_changes")[0]["merge_approval_id"]) == renewed["id"]


def test_an_expired_merge_is_not_renewed_when_the_task_moved_on(api, services, repo, agents, task):
    first = test_git.ready_for_merge(api, services, repo, agents, task)
    expire(services, first["id"])
    q(services, "UPDATE tasks SET state = 'RUNNING'")  # stand-in: the change is being reworked
    response = api.post(f"/v1/approvals/{first['id']}/renew", **DASHBOARD)
    assert response.status_code == 409 and "READY_FOR_MERGE" in response.text
