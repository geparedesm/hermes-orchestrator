"""The Hermes orchestration plugin's command logic, without Hermes or the control plane."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "hermes" / "plugins" / "orchestration"


def _load():
    spec = importlib.util.spec_from_file_location("orchestration_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["orchestration_plugin"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
client = sys.modules["orchestration_plugin.orch_client"]


class FakeApi:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))
            if name == "tasks":
                tasks = [{"key": "T-1", "state": "RUNNING", "title": "a"}, {"key": "T-2", "state": "DONE", "title": "b"}]
                return {"tasks": [t for t in tasks if not kwargs.get("active") or t["state"] != "DONE"]}
            if name == "decide":
                return {"action": "MERGE", "state": "APPROVED" if args[1] else "REJECTED", "summary": "s"}
            if name == "action":
                return {"key": args[0], "state": "PAUSED"}
            if name == "budget":
                return {"id": "A1"} if len(args) > 1 and args[1] else {"state": "OK", "consumed": {}}
            if name == "create":
                return {"key": "T-3", "state": "BACKLOG"}
            return {}
        return record


def test_listing_asks_the_server_for_active_tasks():
    api = FakeApi()
    assert plugin.run_verb(api, ["tasks"], human=False) == "T-1 [RUNNING] a"
    assert api.calls == [("tasks", ())]  # filtered server-side (active=True), before the server's limit


def test_human_actions_need_an_identity():
    with pytest.raises(client.ApiError) as exc:
        plugin.run_verb(FakeApi(), ["approve", "A1"], human=False)
    assert exc.value.status == 403
    api = FakeApi()
    assert plugin.run_verb(api, ["approve", "A1", "looks", "good"], human=True) == "MERGE approved: s"
    assert api.calls == [("decide", ("A1", True, "looks good"))]


def test_creation_is_allowed_without_a_human():
    api = FakeApi()
    assert plugin.run_verb(api, ["create", "demo", "add", "a", "feature"], human=False).startswith("Created T-3")
    assert api.calls == [("create", ("demo", "add a feature"))]


def test_budget_increase_parsing():
    api = FakeApi()
    assert "/orch approve A1" in plugin.run_verb(api, ["budget", "T-1", "agent_launches=5"], human=True)
    assert api.calls == [("budget", ("T-1", {"agent_launches": 5}))]
    with pytest.raises(client.ApiError):
        plugin.run_verb(api, ["budget", "T-1", "agent_launches=lots"], human=True)


def test_usage_errors_and_help():
    assert "/orch commands" in plugin.run_verb(FakeApi(), [], human=True)
    with pytest.raises(client.ApiError, match="usage"):
        plugin.run_verb(FakeApi(), ["status"], human=True)
    with pytest.raises(client.ApiError, match="unknown command"):
        plugin.run_verb(FakeApi(), ["merge", "T-1"], human=True)


def test_task_keys_are_validated():
    assert client._key(" t-12 ") == "T-12"
    with pytest.raises(client.ApiError):
        client._key("../approvals")


def test_slash_without_a_session_identity_refuses_human_actions(monkeypatch):
    monkeypatch.setattr(plugin, "TaskApi", lambda principal: FakeApi())
    assert plugin.handle_slash("cancel T-1").startswith("Not allowed")
    assert plugin.handle_slash("tasks") == "T-1 [RUNNING] a"
    assert plugin.handle_slash('revise T-1 "unterminated').startswith("Could not parse")


def test_tools_are_read_and_create_only():
    names = {name for name, _, _ in plugin.TOOLS}
    assert names == {"orch_task_create", "orch_task_status", "orch_task_list", "orch_task_inspect", "orch_project_list",
                     "orch_approvals_list"}
    for _, schema, _ in plugin.TOOLS:
        assert schema["parameters"]["type"] == "object"


def test_tool_errors_are_json(monkeypatch):
    def failing(principal):
        api = FakeApi()
        api.task = lambda key: (_ for _ in ()).throw(client.ApiError(404, "no such task"))
        return api
    monkeypatch.setattr(plugin, "TaskApi", failing)
    handler = plugin._tool(dict(((n, f) for n, _, f in plugin.TOOLS))["orch_task_status"])
    assert json.loads(handler({"task": "T-9"})) == {"error": "no such task", "status": 404}


def test_registration_uses_the_verified_contract():
    calls = []

    class Ctx:
        def register_tool(self, **kw):
            calls.append(("tool", kw["name"], sorted(kw)))

        def register_command(self, name, handler, **kw):
            calls.append(("command", name))

        def register_cli_command(self, name, help, setup_fn, handler_fn, **kw):
            calls.append(("cli", name))

    plugin.register(Ctx())
    assert ("command", "orch") in calls and ("cli", "orchestration") in calls
    assert sum(1 for c in calls if c[0] == "tool") == 6
    assert all(set(c[2]) >= {"name", "toolset", "schema", "handler"} for c in calls if c[0] == "tool")


def test_unknown_outcomes_are_not_reported_as_not_applied(monkeypatch):
    def timing_out(principal):
        api = FakeApi()
        api.action = lambda key, verb: (_ for _ in ()).throw(client.ApiError(client.UNKNOWN, "no answer"))
        return api
    monkeypatch.setattr(plugin, "TaskApi", timing_out)
    monkeypatch.setattr(plugin, "session_principal", lambda: "telegram:1")
    message = plugin.handle_slash("retry T-1")
    assert "may or may not have been applied" in message and "before repeating" in message


def test_unreachable_before_sending_is_not_applied(monkeypatch):
    def refused(principal):
        api = FakeApi()
        api.action = lambda key, verb: (_ for _ in ()).throw(client.ApiError(0, "connection refused"))
        return api
    monkeypatch.setattr(plugin, "TaskApi", refused)
    monkeypatch.setattr(plugin, "session_principal", lambda: "telegram:1")
    assert "was not applied" in plugin.handle_slash("retry T-1")
