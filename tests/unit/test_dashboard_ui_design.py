"""The Dashboard's UI design routes: status with the delivery channel, images inline, notes as a download."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_dashboard_new_task import ROOT, _load

PNG = b"\x89PNG\r\n\x1a\n" + b"image"
DESIGN = "01a1654a-3272-7b18-b4a4-c7741b43d4b1"


@pytest.fixture
def dashboard(monkeypatch):
    module = _load("orchestration_dashboard_ui_design_test", ROOT / "dashboard" / "plugin_api.py")
    calls = []

    class FakeApi:
        def __init__(self, principal):
            self.principal = principal

        def ui_design(self, key):
            calls.append(("status", key))
            return {"required": True, "design": {"id": DESIGN, "version": 1, "summary": "- New filters"}}

        def ui_design_file(self, key, design_id, name):
            calls.append(("file", key, design_id, name))
            return (b"not a png" if key == "T-99" else PNG if name.endswith(".png") else b"# Notes"), {}

    monkeypatch.setattr(module._client, "TaskApi", FakeApi)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), calls


def test_status_names_the_delivery_channel_only_when_delivery_is_on(dashboard, monkeypatch):
    client, calls = dashboard
    monkeypatch.setenv("HO_UI_DESIGN_DELIVER_CHAT_ID", "private-chat")
    monkeypatch.setenv("HO_UI_DESIGN_DELIVER_PLATFORM", "whatsapp")
    monkeypatch.delenv("HO_UI_DESIGN_DELIVERY", raising=False)
    assert "delivery_platform" not in client.get("/tasks/T-10/ui-design").json()
    monkeypatch.setenv("HO_UI_DESIGN_DELIVERY", "true")
    body = client.get("/tasks/T-10/ui-design").json()
    assert body["delivery_platform"] == "whatsapp" and body["design"]["summary"] == "- New filters"
    assert "private-chat" not in str(body)
    assert client.get("/tasks/x10/ui-design").status_code == 404


def test_images_are_inline_pngs_and_notes_a_download(dashboard):
    client, calls = dashboard
    image = client.get(f"/tasks/T-10/ui-design/{DESIGN}/desktop.png")
    assert image.status_code == 200 and image.content == PNG
    assert image.headers["content-type"] == "image/png" and image.headers["x-content-type-options"] == "nosniff"
    assert image.headers["content-security-policy"] == "default-src 'none'"
    notes = client.get(f"/tasks/T-10/ui-design/{DESIGN}/design.md")
    assert notes.status_code == 200 and notes.headers["content-disposition"].startswith("attachment")
    assert ("file", "T-10", DESIGN, "desktop.png") in calls


@pytest.mark.parametrize("path", [f"/tasks/T-10/ui-design/{DESIGN}/design.pen", f"/tasks/T-10/ui-design/{DESIGN}/../secrets",
                                  "/tasks/T-10/ui-design/not-a-uuid/desktop.png", f"/tasks/bad/ui-design/{DESIGN}/mobile.png"])
def test_only_the_two_images_and_the_notes_are_served(dashboard, path):
    client, calls = dashboard
    assert client.get(path).status_code in (404, 422)
    assert not [c for c in calls if c[0] == "file"]


def test_a_file_that_is_not_a_png_is_not_served_as_an_image(dashboard):
    client, _ = dashboard
    assert client.get(f"/tasks/T-99/ui-design/{DESIGN}/mobile.png").status_code == 502
