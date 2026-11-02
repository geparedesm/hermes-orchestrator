"""Approval details: what each pending decision changes, why it needs a person, and what yes and no do."""

from __future__ import annotations

import json

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
    """Recorded before the commands were kept: listed as recorded, and never taken as blocked."""
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
    d = approval["details"]
    assert d["headline"] == "Before this change can go into main, the safety rules need your OK on 1 point."
    assert d["reasons"] == [{"kind": "commands", "title": "An agent ran 2 command(s) the safety check flags",
                             "detail": "They look like an attempt to run a risky command. The check only warns, so they may "
                                       "have run: look at what it matched.", "items": []}]
    assert d["recommendation"]["action"] == "look" and d["blockers"] == [] and d["passed"] == []
    assert d["if_no"] == "The task goes back to the agents to fix it."


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


@pytest.mark.parametrize(("rule", "blocked"), [("CMD-H09", True), ("CMD-H11", False)])
def test_a_gate_exception_says_in_plain_words_what_the_agents_tried(api, services, projects_root, rule, blocked):
    """T-55: four near-identical technical lines with execution ids; a person with little development background
    needs one sentence, whether it matters, and the commands only on request."""
    from ho_core.enums import ApprovalAction, Risk

    key, row = _task(api, services, projects_root)
    data = {"execution_id": "01a1", "role": "REVIEWER", "provider": "claude", "count": 1, "rules": [rule],
            "unproven": 0 if blocked else 1, "commands": [{"command": "sudo -u postgres psql -c '\\l'", "rule": rule}]}
    with services.ctx.db.transaction() as cur:
        cur.execute("INSERT INTO events (task_id, project_id, type, actor, summary, data, audit) VALUES "
                    "(%s, %s, 'COMMAND_HIGH_RISK', 'agent-manager', '1 high-risk command(s) observed in execution 01a1 (advisory)', %s, false)",
                    (row["id"], row["project_id"], json.dumps(data)))
    with services.ctx.unit_of_work() as uow:
        services.approvals.request(uow, action=ApprovalAction.HIGH_RISK_OPERATION, project_id=row["project_id"], task_id=row["id"],
                                   subject={"kind": "quality_gate_exception", "commit": "a" * 40,
                                            "reasons": ["policy violations were observed (section 58)"]},
                                   config_hash="0" * 64, summary=f"{key}: Quality Gate exception needed", requested_by="quality-gate",
                                   risk=Risk.CRITICAL)
    [approval] = pending(api, "HIGH_RISK_OPERATION")
    d = approval["details"]
    assert d["commands"] == [{"who": "reviewer claude", "command": "sudo -u postgres psql -c '\\l'", "rule": rule,
                              "meaning": "use administrator permissions or change permissions" if blocked
                              else "read passwords or keys"}]
    assert "01a1" not in d["headline"] and texts(approval) == []  # the commands are listed once, not repeated as lines
    assert d["headline"] == "Before this change can go into main, the safety rules need your OK on 1 point."
    assert d["reasons"][0]["items"] == ["Reviewer claude: sudo -u postgres psql -c '\\l'"]
    if blocked:
        assert d["reasons"][0]["detail"] == ("They look like an attempt to use administrator permissions or change permissions. "
                                             "They were blocked and changed nothing.")
        assert d["risk"] == "low" and d["recommendation"]["action"] == "look"  # no passing tests or review recorded here
    else:
        assert d["risk"] == "critical" and d["recommendation"]["action"] == "look"
        assert d["reasons"][0]["detail"].startswith("They look like an attempt to read passwords or keys. The check only warns")


def test_a_gate_exception_explains_each_reason_what_passed_and_what_still_blocks(api, services, projects_root):
    """T-13 (#138): "a sensitive change has too few tests" while every test passed. The card says which sensitive area
    changed and which check is missing, what passed, the text the command check matched, and that the reviewer's
    open problems block the change whatever the person decides."""
    from ho_core.enums import ApprovalAction, Risk

    key, row = _task(api, services, projects_root)
    commit = "c" * 40
    plan = {"risk": {"risk": "HIGH", "areas": {"payments": ["models/payment.py", "models/payment_notice.py"]},
                     "reasons": ["payments: models/payment.py", "project sensitive paths: security/ir.model.access.csv"]},
            "gaps": [{"kind": "no_typecheck_command", "detail": "typecheck required (risk) but the project has no command for it"}]}
    requirements = [{"name": "tests", "status": "PASS"}, {"name": "build", "status": "PASS"}, {"name": "lint", "status": "PASS"},
                    {"name": "security", "status": "PASS"}, {"name": "typecheck", "status": "UNAVAILABLE"},
                    {"name": "cross_review", "status": "PASS"}, {"name": "requirements", "status": "PASS"},
                    {"name": "no_blocking_findings", "status": "FAIL", "detail": "1 open finding(s) of severity CRITICAL, HIGH, MEDIUM"},
                    {"name": "no_conflicts", "status": "PASS"}, {"name": "no_policy_violations", "status": "APPROVAL_REQUIRED"}]
    command = {"command": "/bin/bash -lc \"cat > models/payment_notice.py <<'PY'", "rule": "CMD-H09", "blocked": False,
               "match": "…body = 'Recordatorio: su pago vence mañana'…"}
    data = {"role": "DEVELOPER", "provider": "codex", "count": 1, "rules": ["CMD-H09"], "unproven": 1, "commands": [command]}
    with services.ctx.db.transaction() as cur:
        cur.execute("INSERT INTO events (task_id, project_id, type, actor, summary, data, audit) VALUES (%s, %s, 'COMMAND_HIGH_RISK', "
                    "'agent-manager', '1 high-risk command(s) observed in execution 01a1 (advisory)', %s, false)",
                    (row["id"], row["project_id"], json.dumps(data)))
        cur.execute("INSERT INTO verifications (id, task_id, project_id, purpose, commit_sha, plan, state) "
                    "VALUES (gen_random_uuid(), %s, %s, 'INTEGRATION', %s, %s, 'PASSED')",
                    (row["id"], row["project_id"], commit, json.dumps(plan)))
        cur.execute("INSERT INTO quality_gate_evaluations (id, task_id, commit_sha, config_hash, policy_version, risk, requirements, "
                    "residual_risk, outcome) VALUES (gen_random_uuid(), %s, %s, 'h', 'p', 'HIGH', %s, 'r', 'FAIL')",
                    (row["id"], commit, json.dumps(requirements)))
        for provider in ("claude", "codex"):
            cur.execute("INSERT INTO executions (id, task_id, project_id, role, provider, image, command, resource_profile, state, "
                        "spec, requested_by) VALUES (gen_random_uuid(), %s, %s, 'REVIEWER', %s, 'i', '[]', 'LIGHT', 'SUCCEEDED', "
                        "'{}', 'test') RETURNING id", (row["id"], row["project_id"], provider))
            cur.execute("INSERT INTO reviews (id, task_id, execution_id, commit_sha, reviewer_provider, outcome, requirements_met, "
                        "summary) VALUES (gen_random_uuid(), %s, %s, %s, %s, 'APPROVED', true, 's') RETURNING id",
                        (row["id"], cur.fetchone()["id"], commit, provider))
            review = cur.fetchone()["id"]
            if provider == "claude":
                cur.execute("INSERT INTO review_findings (id, review_id, severity, category, path, description) VALUES "
                            "(gen_random_uuid(), %s, 'MEDIUM', 'security', 'models/whatsapp_bot.py', %s), "
                            "(gen_random_uuid(), %s, 'LOW', 'ux', 'data/whatsapp_data.xml', 'Misleading wording.')",
                            (review, "Replying BAJA again and again makes the bot send unlimited messages. It bypasses the "
                                     "hourly limit.", review))
    with services.ctx.unit_of_work() as uow:
        services.approvals.request(uow, action=ApprovalAction.HIGH_RISK_OPERATION, project_id=row["project_id"], task_id=row["id"],
                                   subject={"kind": "quality_gate_exception", "commit": commit,
                                            "reasons": ["policy violations were observed (section 58)",
                                                        "sensitive change with inadequate tests (section 56)"]},
                                   config_hash="0" * 64, summary=f"{key}: Quality Gate exception needed", requested_by="quality-gate",
                                   risk=Risk.CRITICAL)
    [approval] = pending(api, "HIGH_RISK_OPERATION")
    d = approval["details"]
    assert d["headline"] == ("Before this change can go into main, the safety rules need your OK on 2 points. "
                             "The reviewer also found 1 problem that must be fixed first.")
    assert d["passed"] == ["tests", "build", "code style (lint)", "security scan", "does what was asked",
                           "up to date with main", "approved by Claude and Codex"]
    commands, sensitive = d["reasons"]
    assert commands["items"] == ["Developer codex: the check matched \"…body = 'Recordatorio: su pago vence mañana'…\""]
    assert sensitive == {"kind": "rule", "title": "A sensitive change is missing some required checks",
                         "detail": "It changes payments (2 files) and files the project marks as sensitive "
                                   "(security/ir.model.access.csv), so the rules ask for extra checks. Every check that "
                                   "could run passed.",
                         "items": ["This project has no type check (a tool that catches code using data the wrong way), "
                                   "so that check was skipped."]}
    assert d["blockers"] == [{"severity": "Medium", "category": "security", "path": "models/whatsapp_bot.py",
                              "text": "Replying BAJA again and again makes the bot send unlimited messages."}]
    assert d["minor_findings"] == 1
    assert d["recommendation"] == {"action": "reject", "text": "The reviewer's 1 problem(s) block this change whatever you "
                                                               "decide, so approving now would not add it."}
    assert d["if_yes"] == ("You accept these points for this exact version only (commit ccccccc). "
                           "It still cannot go in until the reviewer's problems are fixed.")
    # The task is not waiting for this decision (it is being worked on again): the card says so.
    assert d["note"] is not None and "asked again only if something still needs your OK" in d["note"]
