"""Phase 11 approval-controlled platform updates."""

from __future__ import annotations

import pytest

from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration


def test_update_needs_an_approval_bound_to_the_running_version(api, services):
    services.ctx.platform["platform"]["version"] = "1.0.0"
    assert api.post("/v1/platform/updates", {"to_version": "1.1.0"}, token=PLUGIN_TOKEN, principal="x:y").status_code == 403
    approval = api.post("/v1/platform/updates", {"to_version": "1.1.0"}).json()
    assert approval["action"] == "UPDATE" and approval["project_id"] is None
    assert api.post(f"/v1/platform/updates/{approval['id']}/start", {}).status_code == 409  # not approved yet
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"}, principal="host-cli:operator")
    services.ctx.platform["platform"]["version"] = "1.0.1"  # the platform changed after the decision
    assert api.post(f"/v1/platform/updates/{approval['id']}/start", {}).status_code == 409


def test_update_is_recorded_from_start_to_finish(api, services):
    services.ctx.platform["platform"]["version"] = "1.0.0"
    approval = api.post("/v1/platform/updates", {"to_version": "1.1.0"}).json()
    api.post(f"/v1/approvals/{approval['id']}/decision", {"decision": "APPROVE"}, principal="host-cli:operator")
    run = api.post(f"/v1/platform/updates/{approval['id']}/start", {"backup": "backups/x"}).json()
    assert run["state"] == "STARTED" and run["to_version"] == "1.1.0"
    assert api.post(f"/v1/platform/updates/{approval['id']}/start", {}).status_code == 409  # consumed: one update per approval
    done = api.post(f"/v1/platform/updates/runs/{run['id']}/finish", {"state": "ROLLED_BACK", "report": {"reason": "health"}}).json()
    assert done["state"] == "ROLLED_BACK"
    listing = api.get("/v1/platform/updates").json()
    assert listing["version"] == "1.0.0" and listing["updates"][0]["state"] == "ROLLED_BACK"


def test_same_version_is_refused(api, services):
    services.ctx.platform["platform"]["version"] = "2.0.0"
    assert api.post("/v1/platform/updates", {"to_version": "2.0.0"}).status_code == 409
    assert api.post("/v1/platform/updates", {"to_version": "../x"}).status_code == 400
