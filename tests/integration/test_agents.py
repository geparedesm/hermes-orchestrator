"""Phase 4: agent executions through the adapters, the Credential Broker, and the Secrets Broker
(control plane side, with the in-memory Agent Manager). tests/docker covers the real containers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import git_repo  # type: ignore[import-not-found]
from control_plane.agentmgr import AgentManagerError
from fake_agents import FakeAgentManager  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
RESULT = {"status": "completed", "summary": "Added OAuth login.", "changed_files": ["src/auth.js"],
          "tests": {"ran": True, "passed": True, "command": "npm test", "summary": "12 passed"},
          "commits": ["3f2a9c1"], "follow_ups": [], "blocked_reason": None}
PROJECT_YAML = """\
version: 1
project: {name: demo}
toolchain: {profiles: [python, node]}
agents: {allowed_providers: [claude, codex]}
environments: {test: {secrets: [TEST_DATABASE_URL]}}
secrets:
  - {name: TEST_DATABASE_URL, environment: test}
  - {name: NPM_TOKEN, environment: test, delivery: env}
  - {name: PROD_API_KEY, environment: production}
"""


@pytest.fixture
def agents(services) -> FakeAgentManager:
    fake = FakeAgentManager()
    services.ctx.agents = fake
    return fake


def ready_task(api, services, projects_root, files=None) -> str:
    git_repo(projects_root / "demo", files)
    assert api.post("/v1/projects", {"path": "demo"}).status_code == 201
    approval = api.post("/v1/projects/demo/scan").json()["approval"]["id"]
    api.post(f"/v1/approvals/{approval}/decision", {"decision": "APPROVE"})
    key = api.post("/v1/tasks", {"project": "demo", "request": "Add OAuth"}).json()["key"]
    services.scheduler.run_once()
    assert api.get(f"/v1/tasks/{key}").json()["state"] == "READY"
    return key


@pytest.fixture
def task(api, services, projects_root, agents) -> str:
    return ready_task(api, services, projects_root)


def workspace(api, task) -> str:
    """The task's registered workspace (created through Git Service on first use)."""
    existing = api.get(f"/v1/tasks/{task}/git").json()["workspaces"]
    if not existing:
        response = api.post(f"/v1/tasks/{task}/git/workspaces", {"suffix": "w1"})
        assert response.status_code == 201, response.text
        return response.json()["path"]
    return existing[0]["path"]


def run_agent(api, task, provider="codex", **body):
    payload = {"role": "DEVELOPER", "provider": provider, "prompt": "Add OAuth login",
               "capabilities": {"workspace": "WRITE", "git": "LOCAL_COMMIT"}, "workspace": workspace(api, task), **body}
    return api.post(f"/v1/tasks/{task}/executions", payload)


def artifacts_of(services, view) -> dict[str, bytes]:
    with services.ctx.db.transaction() as cur:
        cur.execute("SELECT path FROM artifacts WHERE id = ANY(%s)", (list(view["artifacts"]),))
        return {row["path"].rsplit("/", 1)[-1]: services.ctx.artifacts.read(row["path"]) for row in cur.fetchall()}


def events(api, task):
    return [e["type"] for e in api.get(f"/v1/events?task={task}").json()["events"]]


def credential(api, provider, identity="default"):
    return next((i for i in api.get("/v1/credentials").json()["identities"]
                 if i["provider"] == provider and i["identity"] == identity), None)


# ----------------------------------------------------------------------------- runs


def test_agent_run_uses_the_adapter_and_stores_only_filtered_output(api, services, agents, task):
    response = run_agent(api, task)
    assert response.status_code == 201, response.text
    execution = response.json()["id"]
    spec = agents.specs[execution]
    assert spec["image"] == "codex-generic" and spec["command"][:2] == ["/opt/ho/bin/ho-agent-run", "exec"]
    assert "Add OAuth login" in spec["inputs"]["prompt.md"] and "result.schema.json" in spec["inputs"]
    assert spec["session"] is True and spec["grant"]["capabilities"]["network"]["egress"] == "PROVIDER_ONLY"

    agents.finish_agent(execution, (FIXTURES / "codex-success.jsonl").read_bytes(),
                        extra={"ho/last_message.json": json.dumps(RESULT).encode(), "notes.md": b"token=abcdefghijklmnop12345"})
    services.scheduler.run_once()
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "SUCCEEDED", view
    assert view["result"]["status"] == "completed" and view["result"]["commits"] == ["3f2a9c1"]
    assert view["provider_session_id"] == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert view["assignment"]["prompt"] == "Add OAuth login"

    stored = artifacts_of(services, view)
    names = set(stored)
    assert {"result.json", "events.jsonl", "notes.md"} <= {n.rsplit("-", 1)[-1] for n in names}
    assert not any("events.jsonl" in n and "ho__" in n for n in names)  # raw stream is not stored
    everything = b"".join(stored.values())
    assert b"PRIVATE REASONING" not in everything and b"abcdefghijklmnop12345" not in everything

    with services.ctx.db.transaction() as cur:
        cur.execute("SELECT units FROM usage_records WHERE execution_id = %s", (execution,))
        assert cur.fetchone()["units"]["output_tokens"] == 122
    assert credential(api, "codex")["status"] == "READY"


def test_toolchain_profiles_choose_the_image(api, services, projects_root, agents):
    key = ready_task(api, services, projects_root, {".hermes/project.yaml": PROJECT_YAML})
    response = run_agent(api, key, provider="claude")
    assert response.status_code == 201, response.text
    assert agents.specs[response.json()["id"]]["image"] == "claude-node-python"
    runner = api.post(f"/v1/tasks/{key}/executions", {"role": "TESTER", "command": ["npm", "test"]})
    assert agents.specs[runner.json()["id"]]["image"] == "runner-node-python"


def test_invalid_agent_requests(api, agents, task):
    assert run_agent(api, task, role="TESTER", provider=None).status_code == 400  # runners take commands
    assert run_agent(api, task, command=["bash", "-c", "true"]).status_code == 400  # prompt or command, not both
    assert api.post(f"/v1/tasks/{task}/executions", {"role": "DEVELOPER", "provider": "codex"}).status_code == 400


def test_high_risk_commands_raise_an_advisory_event(api, services, agents, task):
    execution = run_agent(api, task, provider="claude").json()["id"]
    agents.finish_agent(execution, (FIXTURES / "claude-success.jsonl").read_bytes())
    services.scheduler.run_once()
    assert api.get(f"/v1/executions/{execution}").json()["state"] == "SUCCEEDED"
    assert "COMMAND_HIGH_RISK" in events(api, task)
    with services.ctx.db.transaction() as cur:  # what the person deciding needs: the command, and that it may have had an effect
        cur.execute("SELECT data FROM events WHERE type = 'COMMAND_HIGH_RISK'")
        data = cur.fetchone()["data"]
    assert data["count"] == 1 and data["rules"] == ["CMD-H11"] and data["unproven"] == 1 and data["role"] == "DEVELOPER"
    assert data["commands"] == [{"command": "cat /run/ho-credentials/claude/oauth_token", "rule": "CMD-H11", "blocked": False}]


def test_resume_continues_the_provider_session(api, services, agents, task):
    first = run_agent(api, task, provider="claude").json()["id"]
    assert api.post(f"/v1/executions/{first}/resume", {"prompt": "Also add tests"}).status_code == 409  # still running
    agents.finish_agent(first, (FIXTURES / "claude-success.jsonl").read_bytes())
    services.scheduler.run_once()
    response = api.post(f"/v1/executions/{first}/resume", {"prompt": "Also add tests"})
    assert response.status_code == 201, response.text
    second = response.json()
    cmd = agents.specs[second["id"]]["command"]
    assert cmd[cmd.index("--resume") + 1] == "5f0c7a9e-1111-4222-8333-944455556666"
    assert second["resume_of"] == first and second["workspace"].endswith(f".hermes/worktrees/{task.lower()}-w1")
    assert "Also add tests" in agents.specs[second["id"]]["inputs"]["prompt.md"]


def test_task_environment_is_released_after_the_task_ends(api, services, agents, task):
    execution = run_agent(api, task).json()["id"]
    api.post(f"/v1/tasks/{task}/cancel")
    services.scheduler.run_once()
    assert api.get(f"/v1/executions/{execution}").json()["state"] == "CANCELLED"
    services.scheduler.run_once()
    assert agents.released == [task]
    services.scheduler.run_once()
    assert agents.released == [task]  # only once


# ------------------------------------------------------------------ authentication


def test_expired_login_waits_in_auth_required_and_resumes_after_login(api, services, agents, task):
    execution = run_agent(api, task, provider="claude").json()["id"]
    agents.finish_agent(execution, (FIXTURES / "claude-auth-failure.jsonl").read_bytes(), exit_code=1)
    services.scheduler.run_once()
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "FAILED" and view["failure_class"] == "AUTH"
    task_view = api.get(f"/v1/tasks/{task}").json()
    assert task_view["state"] == "AUTH_REQUIRED" and "auth-claude" in task_view["state_reason"]
    assert credential(api, "claude")["status"] == "AUTH_REQUIRED"
    with services.ctx.db.transaction() as cur:
        cur.execute("SELECT n.priority FROM notifications n JOIN events e ON e.seq = n.event_seq "
                    "WHERE e.type = 'AUTH_REQUIRED' AND e.task_id = (SELECT id FROM tasks WHERE key = %s)", (task,))
        assert [r["priority"] for r in cur.fetchall()] == ["ATTENTION"]
    assert run_agent(api, task).status_code == 409  # no new work while waiting

    response = api.post("/v1/credentials/claude/default/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tasks_resumed"] == [task] and body["executions_continued"][0]["resumed_session"] is True
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "READY"
    new = body["executions_continued"][0]["continued_by"]
    cmd = agents.specs[new]["command"]
    assert cmd[cmd.index("--resume") + 1] == "ce18493e-0e98-40ca-b87d-ae563b4790ab"
    assert "login had expired" in agents.specs[new]["inputs"]["prompt.md"]
    assert credential(api, "claude")["status"] == "READY"
    # Confirming again does not continue the same execution twice.
    assert api.post("/v1/credentials/claude/default/ready").json()["executions_continued"] == []


def test_missing_credential_volume_reruns_the_assignment_after_login(api, services, agents, task):
    agents.fail_with = AgentManagerError(424, "auth_required", "provider credential cred-codex-default is not set up")
    execution = run_agent(api, task).json()["id"]
    assert api.get(f"/v1/executions/{execution}").json()["failure_class"] == "AUTH"
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "AUTH_REQUIRED"
    agents.fail_with = None
    agents.volumes.discard(("codex", "default"))
    assert api.post("/v1/credentials/codex/default/ready").status_code == 409  # log in first
    agents.volumes.add(("codex", "default"))
    body = api.post("/v1/credentials/codex/default/ready").json()
    new = body["executions_continued"][0]
    assert new["resumed_session"] is False
    spec = agents.specs[new["continued_by"]]
    assert "Add OAuth login" in spec["inputs"]["prompt.md"] and "resume" not in spec["command"]
    assert api.get(f"/v1/executions/{new['continued_by']}").json()["resume_of"] == execution


def test_only_the_operator_confirms_logins(api, task):
    from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

    response = api.post("/v1/credentials/claude/default/ready", token=PLUGIN_TOKEN, principal="telegram:1")
    assert response.status_code == 403
    assert api.post("/v1/credentials/gemini/default/ready").status_code == 400


def test_provider_health(api, agents, task):
    providers = {p["provider"]: p for p in api.get("/v1/credentials").json()["providers"]}
    assert providers["codex"]["ok"] and providers["claude"]["ok"]
    agents.volumes.discard(("claude", "default"))
    providers = {p["provider"]: p for p in api.get("/v1/credentials").json()["providers"]}
    assert not providers["claude"]["ok"] and "auth-claude" in providers["claude"]["detail"]


# ------------------------------------------------------------------------- secrets


def test_secrets_are_granted_by_reference_only(api, services, projects_root, agents):
    key = ready_task(api, services, projects_root, {".hermes/project.yaml": PROJECT_YAML})
    response = run_agent(api, key, secrets=["TEST_DATABASE_URL", "NPM_TOKEN", "PROD_API_KEY", "UNKNOWN"])
    assert response.status_code == 201, response.text
    view = api.get(f"/v1/executions/{response.json()['id']}").json()
    assert view["grant"]["capabilities"]["secrets"] == ["demo/test/TEST_DATABASE_URL", "demo/test/NPM_TOKEN"]
    assert any("PROD_API_KEY denied" in r for r in view["grant_reductions"])  # production needs an approval
    spec = agents.specs[view["id"]]
    assert spec["secret_env"] == ["demo/test/NPM_TOKEN"]
    reviewer = run_agent(api, key, role="REVIEWER", capabilities={"workspace": "READ"}, secrets=["TEST_DATABASE_URL"])
    assert api.get(f"/v1/executions/{reviewer.json()['id']}").json()["grant"]["capabilities"]["secrets"] == []


def test_the_dashboard_shows_each_agents_plan_usage_and_a_person_can_pause_it(api, services, agents, task):
    """docs/design/agents-dashboard.md: usage from the CLI's own events; pausing is a person's decision."""
    import time

    reset = int(time.time()) + 3600
    limits = {"primary": {"used_percent": 88.0, "window_minutes": 300, "resets_at": reset},
              "secondary": {"used_percent": 61.0, "window_minutes": 10080, "resets_at": reset + 86400}, "observed_at": int(time.time()),
              "reasoning": "never kept"}
    execution = run_agent(api, task).json()["id"]
    agents.finish_agent(execution, (FIXTURES / "codex-success.jsonl").read_bytes(),
                        extra={"ho/rate-limits.json": json.dumps(limits).encode()})
    services.scheduler.run_once()
    with services.ctx.db.transaction() as cur:  # stand-in: Codex's login was confirmed (a missing login reads login_needed)
        cur.execute("INSERT INTO credential_refs (provider, identity, status) VALUES ('codex', %s, 'READY') "
                    "ON CONFLICT (provider, identity) DO UPDATE SET status = 'READY'", (services.ctx.provider_identity,))
    view = {a["provider"]: a for a in api.get("/v1/agents").json()["agents"]}
    codex = view["codex"]
    assert codex["session"]["used_percent"] == 88.0 and codex["week"]["used_percent"] == 61.0 and codex["status"] == "near_limit"
    assert view["claude"]["session"] is None and view["claude"]["status"] == "login_needed"
    with services.ctx.db.transaction() as cur:  # the raw file is never an artifact; only the numbers are kept
        cur.execute("SELECT count(*) AS n FROM artifacts WHERE path LIKE '%%rate-limits%%'")
        assert cur.fetchone()["n"] == 0

    from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

    assert api.post("/v1/agents/codex/pause", {}, token=PLUGIN_TOKEN, principal="telegram:12345").status_code == 403
    paused = api.post("/v1/agents/codex/pause", {}, token=PLUGIN_TOKEN, principal="dashboard:operator")
    assert paused.status_code == 200 and paused.json()["status"] == "paused" and not paused.json()["enabled"]
    from control_plane.agents import paused as paused_providers

    with services.ctx.unit_of_work() as uow:  # what routing (Orchestration._available) skips: new work goes elsewhere
        assert paused_providers(uow, services.ctx.provider_identity) == {"codex"}
    refused = run_agent(api, task, provider="codex")  # every launch path goes through here: nothing new starts
    assert refused.status_code == 409 and "paused" in refused.text
    api.post("/v1/agents/codex/resume", {}, token=PLUGIN_TOKEN, principal="dashboard:operator")
    with services.ctx.unit_of_work() as uow:
        assert paused_providers(uow, services.ctx.provider_identity) == set()
    assert api.post("/v1/agents/gemini/pause", {}, token=PLUGIN_TOKEN, principal="dashboard:operator").status_code == 404


def test_an_execution_waiting_for_dispatch_does_not_start_once_its_agent_is_paused(api, services, agents, task):
    from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

    with services.ctx.db.transaction() as cur:  # stand-in: Agent Manager degraded, so the request waits unstarted
        cur.execute("INSERT INTO component_health (component, state) VALUES ('agent_manager', 'DEGRADED') "
                    "ON CONFLICT (component) DO UPDATE SET state = 'DEGRADED'")
    execution = run_agent(api, task).json()["id"]
    assert api.post("/v1/agents/codex/pause", {}, token=PLUGIN_TOKEN, principal="dashboard:operator").status_code == 200
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE component_health SET state = 'HEALTHY' WHERE component = 'agent_manager'")
    services.executions.dispatch(execution)
    assert api.get(f"/v1/executions/{execution}").json()["state"] == "REQUESTED" and execution not in agents.containers
    api.post("/v1/agents/codex/resume", {}, token=PLUGIN_TOKEN, principal="dashboard:operator")
    services.executions.dispatch(execution)
    assert api.get(f"/v1/executions/{execution}").json()["state"] != "REQUESTED"


def test_renewing_a_paused_agents_login_holds_its_interrupted_work_until_resume(api, services, agents, task):
    """Codex review: the continuation must not be lost when the login is renewed while the agent is paused."""
    from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

    execution = run_agent(api, task, provider="claude").json()["id"]
    agents.finish_agent(execution, (FIXTURES / "claude-auth-failure.jsonl").read_bytes(), exit_code=1)
    services.scheduler.run_once()
    assert api.post("/v1/agents/claude/pause", {}, token=PLUGIN_TOKEN, principal="dashboard:operator").status_code == 200
    body = api.post("/v1/credentials/claude/default/ready").json()
    assert credential(api, "claude")["status"] == "READY" and body["tasks_resumed"] == [task]
    new = body["executions_continued"][0]["continued_by"]
    assert api.get(f"/v1/executions/{new}").json()["state"] == "REQUESTED" and new not in agents.containers  # held
    api.post("/v1/agents/claude/resume", {}, token=PLUGIN_TOKEN, principal="dashboard:operator")
    services.executions.dispatch(new)
    assert api.get(f"/v1/executions/{new}").json()["state"] != "REQUESTED"


def test_an_agent_launches_with_the_model_a_person_chose(api, services, agents, task):
    """A default model per agent (Dashboard) must reach the execution, and every launch must keep working."""
    from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

    plain = run_agent(api, task, provider="claude")
    assert plain.status_code == 201, plain.text  # no model chosen: the CLI's own default
    chosen = api.post("/v1/agents/claude/model", {"model": "claude-sonnet-5"}, token=PLUGIN_TOKEN, principal="dashboard:operator")
    assert chosen.status_code == 200, chosen.text
    response = run_agent(api, task, provider="claude")
    assert response.status_code == 201, response.text
    assert "claude-sonnet-5" in agents.specs[response.json()["id"]]["command"]


def test_an_agent_cut_at_its_time_limit_fails_as_timeout(api, services, agents, task):
    """T-1: the planning step ran past its 20 minutes; Agent Manager stopped it (exit 143) before sync marked it, and
    it was recorded as UNKNOWN instead of TIMEOUT."""
    execution = run_agent(api, task, provider="codex").json()["id"]
    with services.ctx.db.transaction() as cur:  # stand-in: the grant's expiry passed while it ran
        cur.execute("UPDATE executions SET spec = jsonb_set(spec, '{grant,expires_at}', to_jsonb((now() - interval '5 seconds')::text)) "
                    "WHERE id = %s", (execution,))
    agents.finish_agent(execution, (FIXTURES / "codex-success.jsonl").read_bytes()[:200], exit_code=143, reason="signal")
    services.scheduler.run_once()
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "FAILED" and view["failure_class"] == "TIMEOUT", view


def test_an_agent_waits_for_docker_memory_instead_of_failing(api, services, agents, task):
    """T-3: "not enough Docker memory" failed each orchestrator step at once, and three of them blocked the task."""
    from control_plane.agentmgr import AgentManagerError

    agents.fail_with = AgentManagerError(409, "capacity_exceeded", "not enough Docker memory: needs 4096 MiB, 1649 MiB available")
    response = run_agent(api, task, provider="codex")
    execution = response.json()["id"]
    services.executions.dispatch(execution)
    services.executions.dispatch(execution)
    view = api.get(f"/v1/executions/{execution}").json()
    assert view["state"] == "REQUESTED"  # waiting, not failed, and no dispatch attempt spent
    agents.fail_with = None  # memory freed
    services.executions.dispatch(execution)
    assert api.get(f"/v1/executions/{execution}").json()["state"] != "REQUESTED"
    agents.fail_with = AgentManagerError(409, "capacity_exceeded", "not enough Docker memory")
    late = run_agent(api, task, provider="codex").json()["id"]
    with services.ctx.db.transaction() as cur:  # an old execution still gets its full wait from the first rejection
        cur.execute("UPDATE executions SET created_at = now() - interval '2 hours' WHERE id = %s", (late,))
    services.executions.dispatch(late)
    assert api.get(f"/v1/executions/{late}").json()["state"] == "REQUESTED"
    with services.ctx.db.transaction() as cur:  # waiting this long for memory means it will not fit: CAPACITY
        cur.execute("UPDATE executions SET spec = jsonb_set(spec, '{capacity_wait_since}', to_jsonb((now() - interval '31 minutes')::text)) "
                    "WHERE id = %s", (late,))
    services.executions.dispatch(late)
    view = api.get(f"/v1/executions/{late}").json()
    assert view["state"] == "FAILED" and view["failure_class"] == "CAPACITY"
