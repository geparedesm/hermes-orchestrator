"""Approval details: what each pending decision changes, why it needs a person, and what yes and no do."""

from __future__ import annotations

import pytest

from conftest import PLUGIN_TOKEN, git_repo  # type: ignore[import-not-found]
from test_children import PARENT_YAML, gh_state, parent, request  # type: ignore[import-not-found]  # noqa: F401
from test_git import sh  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration


def pending(api, action):
    summary = api.get("/v1/dashboard/summary", token=PLUGIN_TOKEN, principal="dashboard:operator").json()
    return [a for a in summary["required_actions"]["approvals"] if a["action"] == action]


def texts(approval):
    return [c["text"] for c in approval["details"]["changes"]]


def test_a_first_configuration_lists_what_the_project_sets(api, projects_root):
    git_repo(projects_root / "shop", {".hermes/project.yaml": "version: 1\nproject: {name: shop}\ncommands: {test: \"make check\"}\n",
                                      "README.md": "x\n"})
    api.post("/v1/projects", {"path": "shop"})
    api.post("/v1/projects/shop/scan")
    [approval] = pending(api, "PROJECT_READY")
    details = approval["details"]
    assert any(t.startswith("Command test: ") and "make check" in t for t in texts(approval))
    assert "first configuration" in details["why"] and details["if_yes"].startswith("The project becomes ready")
    assert ["Compared with", "the platform defaults (first configuration)"] in details["facts"]


def test_an_updated_configuration_lists_only_what_changed(api, parent):  # noqa: F811 (fixture)
    yaml = PARENT_YAML.replace('commands: {test: "python -m unittest"}', 'commands: {test: "ho-odoo-test odoo_base"}')
    yaml = yaml.replace("project: {name: odoo-base}", "project: {name: odoo-base}\ntoolchain: {profiles: [odoo]}")
    (parent / ".hermes/project.yaml").write_text(yaml)
    sh(parent, "commit", "-qam", "odoo toolchain")
    api.post("/v1/projects/odoo-base/scan")
    [approval] = pending(api, "PROJECT_READY")
    changes = texts(approval)
    assert changes[0].startswith("Toolchain profiles: ") and "odoo" in changes[0]
    assert "Command test: python -m unittest → ho-odoo-test odoo_base" in changes
    assert not any(t.startswith("Can create") for t in changes)  # unchanged sections are not listed
    assert approval["details"]["if_no"] == "The configuration stays as it was."


def test_a_module_request_says_exactly_what_will_be_created(api, parent):  # noqa: F811 (fixture)
    request(api)
    [approval] = pending(api, "CREATE_CHILD")
    changes = texts(approval)
    assert changes[0] == "Folder custom-addons/job_alerts with the module skeleton and its rules"
    assert "Private GitHub repository geparedesm/odoo-job_alerts" in changes
    assert approval["details"]["why"] == "Creating repositories and folders is a person's decision."


def test_a_budget_raise_shows_usage_and_the_new_limit(api, services, projects_root):
    from ho_core.enums import ApprovalAction

    git_repo(projects_root / "demo", {".hermes/project.yaml": "version: 1\nproject: {name: demo}\n", "README.md": "x\n"})
    api.post("/v1/projects", {"path": "demo"})
    approval_id = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "APPROVE"})
    key = api.post("/v1/tasks", {"project": "demo", "request": "Do it"}).json()["key"]
    with services.ctx.db.transaction() as cur:
        cur.execute("SELECT id, project_id FROM tasks WHERE key = %s", (key,))
        row = cur.fetchone()
    with services.ctx.unit_of_work() as uow:
        services.approvals.request(uow, action=ApprovalAction.BUDGET_INCREASE, project_id=row["project_id"], task_id=row["id"],
                                   subject={"add": {"agent_launches": 10}}, config_hash="0" * 64, summary=f"{key}: raise",
                                   requested_by="test")
    [approval] = pending(api, "BUDGET_INCREASE")
    [change] = texts(approval)
    assert change.startswith("agent runs: 0 used of ") and change.endswith(f"→ limit {int(change.split(' of ')[1].split(' ')[0]) + 10}")
    assert approval["details"]["risk"] == "medium"


def _task(api, services, projects_root):
    git_repo(projects_root / "demo", {".hermes/project.yaml": "version: 1\nproject: {name: demo}\n", "README.md": "x\n"})
    api.post("/v1/projects", {"path": "demo"})
    approval_id = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval_id}/decision", {"decision": "APPROVE"})
    key = api.post("/v1/tasks", {"project": "demo", "request": "Do it"}).json()["key"]
    with services.ctx.db.transaction() as cur:
        cur.execute("SELECT id, project_id FROM tasks WHERE key = %s", (key,))
        return key, cur.fetchone()


def test_a_gate_exception_lists_the_risky_commands(api, services, projects_root):
    from ho_core.enums import ApprovalAction, Risk

    key, row = _task(api, services, projects_root)
    with services.ctx.db.transaction() as cur:
        cur.execute("INSERT INTO events (task_id, project_id, type, actor, summary, data, audit) VALUES "
                    "(%s, %s, 'COMMAND_HIGH_RISK', 'agent-manager', '2 high-risk command(s) observed in execution 01a1 (advisory)', '{}', false)",
                    (row["id"], row["project_id"]))
    with services.ctx.unit_of_work() as uow:
        services.approvals.request(uow, action=ApprovalAction.HIGH_RISK_OPERATION, project_id=row["project_id"], task_id=row["id"],
                                   subject={"kind": "quality_gate_exception", "commit": "a" * 40,
                                            "reasons": ["policy violations were observed (section 58)"]},
                                   config_hash="0" * 64, summary=f"{key}: Quality Gate exception needed", requested_by="quality-gate",
                                   risk=Risk.CRITICAL)
    [approval] = pending(api, "HIGH_RISK_OPERATION")
    assert texts(approval) == ["2 high-risk command(s) observed in execution 01a1 (advisory)"]
    assert approval["details"]["risk"] == "critical" and ["Commit", "a" * 12] in approval["details"]["facts"]
    assert approval["details"]["if_no"] == "The task goes back to the agents to fix it."


def test_a_merge_lists_what_is_included_and_links_the_pull_request(api, services, projects_root):
    from ho_core.enums import ApprovalAction

    key, row = _task(api, services, projects_root)
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE tasks SET current_plan_version = 1 WHERE id = %s", (row["id"],))
        cur.execute("INSERT INTO subtasks (id, task_id, plan_version, key, local_key, kind, title, description, state, risk) "
                    "VALUES (gen_random_uuid(), %s, 1, %s, 'S1', 'IMPLEMENT', 'Search connectors', 'x', 'ACCEPTED', 'LOW')",
                    (row["id"], f"{key}-1"))
        cur.execute("INSERT INTO git_changes (task_id, project_id, target_branch, base_sha, pr_number, pr_url) "
                    "VALUES (%s, %s, 'main', repeat('c', 40), 4, 'https://github.com/x/y/pull/4')", (row["id"], row["project_id"]))
    with services.ctx.unit_of_work() as uow:
        services.approvals.request(uow, action=ApprovalAction.MERGE, project_id=row["project_id"], task_id=row["id"],
                                   subject={"target_branch": "main", "head_sha": "b" * 40, "pr_number": 4, "quality_gate": "x"},
                                   config_hash="0" * 64, summary=f"merge {key}", requested_by="quality-gate")
    [approval] = pending(api, "MERGE")
    assert texts(approval) == ["S1: Search connectors"]
    assert approval["details"]["links"] == [{"label": "Pull request #4", "url": "https://github.com/x/y/pull/4"}]
    assert approval["details"]["if_yes"].startswith("Pull request #4 is merged into main")


def test_budget_details_show_computed_runtime_and_unlimited_outcomes(api, services, projects_root):
    from ho_core.enums import ApprovalAction

    key, row = _task(api, services, projects_root)
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE tasks SET started_at = now() - interval '90 minutes' WHERE id = %s", (row["id"],))
    with services.ctx.unit_of_work() as uow:
        services.approvals.request(uow, action=ApprovalAction.BUDGET_UNLIMITED, project_id=row["project_id"], task_id=row["id"],
                                   subject={}, config_hash="0" * 64, summary=f"{key}: unlimited", requested_by="test")
    [approval] = pending(api, "BUDGET_UNLIMITED")
    runtime = next(t for t in texts(approval) if t.startswith("working time"))
    assert runtime.startswith("working time (min): 90 used of ") or runtime.startswith("working time (min): 89 used of ")
    assert approval["details"]["if_no"] == "The task continues with its current, limited budget."
