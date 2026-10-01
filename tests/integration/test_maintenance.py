"""Phase 11 operations: daily maintenance (caches, artifact and workspace retention, history) and the merge
approval requested automatically when the Quality Gate passes."""

from __future__ import annotations

import pytest

import test_git  # type: ignore[import-not-found]
from test_git import clone, commit, integrate_and_pass, pass_gate, workspace  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration
agents = pytest.fixture(test_git.agents.__wrapped__)
repo = pytest.fixture(test_git.repo.__wrapped__)
task = pytest.fixture(test_git.task.__wrapped__)


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def finished_execution(api, services, agents, task):
    execution = api.post(f"/v1/tasks/{task}/executions", {"role": "TESTER", "command": ["true"]}).json()["id"]
    agents.finish(execution, 0, files={"log.txt": b"output"})
    services.scheduler.run_once()
    return execution


def test_old_execution_outputs_are_purged_and_audit_artifacts_kept(api, services, agents, task):
    finished_execution(api, services, agents, task)
    outputs = q(services, "SELECT * FROM artifacts WHERE kind LIKE 'executions/%%'")
    assert outputs
    q(services, "UPDATE tasks SET state = 'DONE', completed_at = now() - interval '40 days' WHERE key = %s", task)
    report = services.maintenance.run()
    assert report["artifacts"]["purged"] == len(outputs)
    for output in outputs:
        [row] = q(services, "SELECT purged_at, sha256 FROM artifacts WHERE id = %s", output["id"])
        assert row["purged_at"] is not None and row["sha256"] == output["sha256"]  # the digest stays for the audit trail
        assert not (services.ctx.artifacts.root / output["path"]).exists()
    kept = q(services, "SELECT kind FROM artifacts WHERE purged_at IS NULL")
    assert {"request"} <= {k["kind"] for k in kept}


def test_recent_and_failed_task_outputs_are_kept_longer(api, services, agents, task):
    finished_execution(api, services, agents, task)
    q(services, "UPDATE tasks SET state = 'FAILED', updated_at = now() - interval '40 days' WHERE key = %s", task)
    assert services.maintenance.run()["artifacts"]["purged"] == 0  # failed tasks: 90 days by default


def test_retained_workspaces_are_removed_after_their_retention(api, services, agents, repo, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "unfinished", {"app.py": "x = 2\n"})
    api.post(f"/v1/tasks/{task}/cancel")
    services.recovery.run("PERIODIC")
    assert q(services, "SELECT status FROM workspaces")[0]["status"] == "RETAINED"
    q(services, "UPDATE tasks SET updated_at = now() - interval '20 days' WHERE key = %s", task)
    assert services.maintenance.run()["workspaces"]["removed"] == 1
    assert q(services, "SELECT status FROM workspaces")[0]["status"] == "REMOVED"
    assert not clone(repo, ws).exists()


def test_history_is_pruned_and_maintenance_runs_daily(api, services, agents, task):
    q(services, "UPDATE notifications SET state = 'SENT', delivered_at = now() - interval '40 days'")
    before = getattr(agents, "cache_maintenance", 0)  # the first scheduler pass already ran the daily maintenance
    report = services.maintenance.run()
    assert report["history"]["notifications"] >= 1 and report["caches"]["trimmed"] == 0
    assert agents.cache_maintenance == before + 1
    assert not services.maintenance.due()  # once a day
    services.maintenance.tick()
    assert agents.cache_maintenance == before + 1
    assert "MAINTENANCE_COMPLETED" in [r["type"] for r in q(services, "SELECT type FROM events")]


def test_maintenance_and_caches_are_operator_actions(api, services, agents, task):
    from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

    assert api.post("/v1/maintenance/run").status_code == 200
    assert api.post("/v1/maintenance/run", token=PLUGIN_TOKEN, principal="x:y").status_code == 403
    assert api.get("/v1/caches").json()["max_bytes_per_cache"] == 2 * 1024**3
    assert api.delete("/v1/caches/demo?ecosystem=pip").json() == {"removed": ["ho-cache-demo-pip"]}


def test_passing_gate_requests_the_merge_approval(api, services, agents, repo, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), "feature", {"app.py": "x = 3\n"})
    integrate_and_pass(api, services, agents, task)
    pass_gate(api, services, agents, task)
    [approval] = q(services, "SELECT * FROM approvals WHERE action = 'MERGE' AND state = 'PENDING'")
    assert approval["requested_by"] == "control-plane:quality-gate"
