"""The Dashboard's new-task route and the client body it sends (no Hermes, no control plane)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2] / "hermes" / "plugins" / "orchestration"
KEY = "dash-0123456789abcdef"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # as Hermes loads it: postponed annotations resolve through the module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dashboard(monkeypatch):
    module = _load("orchestration_dashboard_api_test", ROOT / "dashboard" / "plugin_api.py")
    calls = []

    class FakeApi:
        def __init__(self, principal):
            self.principal = principal

        def create(self, *args, **kwargs):
            calls.append((self.principal, args, kwargs))
            return {"key": "T-7", "state": "BACKLOG"}

    monkeypatch.setattr(module._client, "TaskApi", FakeApi)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), calls


def test_a_task_is_created_as_the_dashboard_operator(dashboard):
    client, calls = dashboard
    response = client.post("/tasks", json={"project": "odoo-job-finder", "request": "  Add tests  ", "title": "",
                                           "priority": "HIGH", "budget": "SMALL", "depends_on": ["t-3", " T-4 "],
                                           "idempotency_key": KEY})
    assert response.status_code == 200 and response.json()["key"] == "T-7"
    [(principal, args, kwargs)] = calls
    assert principal == "dashboard:operator"
    assert args == ("odoo-job-finder", "Add tests", None, "HIGH")
    assert kwargs == {"budget": "SMALL", "depends_on": ["T-3", "T-4"], "idempotency_key": KEY}


@pytest.mark.parametrize("change,status", [
    ({"budget": "UNLIMITED"}, 422),  # a separate human decision, never offered at creation
    ({"request": ""}, 422),
    ({"project": "../etc"}, 422),
    ({"idempotency_key": "short"}, 422),
    ({"depends_on": ["DROP TABLE"]}, 400),
])
def test_invalid_requests_are_refused(dashboard, change, status):
    client, calls = dashboard
    body = {"project": "demo", "request": "Do it", "idempotency_key": KEY, **change}
    assert client.post("/tasks", json=body).status_code == status and not calls


def test_client_sends_budget_dependencies_and_the_idempotency_key(monkeypatch):
    client = _load("orchestration_client_test", ROOT / "orch_client.py")
    sent = []
    api = client.TaskApi("dashboard:operator", base_url="http://cp")
    monkeypatch.setattr(api, "call", lambda *a, **k: sent.append((a, k)) or {"key": "T-1"})
    api.create("demo", "Do it", None, "normal", budget="large", depends_on=["T-2"], idempotency_key=KEY)
    [(args, kwargs)] = sent
    assert args == ("POST", "/v1/tasks", {"project": "demo", "request": "Do it", "priority": "NORMAL",
                                          "budget_profile": "LARGE", "related_tasks": [{"task": "T-2", "kind": "DEPENDENCY"}]})
    assert kwargs == {"idempotency_key": KEY}
