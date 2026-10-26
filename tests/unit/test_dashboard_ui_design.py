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

        def call(self, method, path, body):
            calls.append((method, path, body))
            return {"delivered": path.rsplit("/", 1)[-1]}

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


@pytest.mark.parametrize("name", ["desktop.png", "mobile.png"])
def test_image_receipt_is_forwarded_without_approving(dashboard, name):
    client, calls = dashboard
    response = client.post(f"/tasks/T-10/ui-design/{DESIGN}/delivered/{name}", json={})
    assert response.status_code == 200
    assert calls == [("POST", f"/v1/tasks/T-10/ui-design/{DESIGN}/delivered/{name}", {})]


@pytest.mark.parametrize("name", ["design.md", "design.pen", "other.png"])
def test_receipts_are_limited_to_design_images(dashboard, name):
    client, calls = dashboard
    assert client.post(f"/tasks/T-10/ui-design/{DESIGN}/delivered/{name}", json={}).status_code == 404
    assert not calls


def test_cross_origin_image_receipt_is_refused(dashboard):
    client, calls = dashboard
    response = client.post(f"/tasks/T-10/ui-design/{DESIGN}/delivered/desktop.png",
                           json={}, headers={"origin": "https://other.example"})
    assert response.status_code == 403
    assert not calls


def test_browser_records_delivery_only_after_a_successful_image_download():
    import subprocess

    script = (ROOT / "dashboard" / "dist" / "index.js").read_text()
    function = script[script.index("  function designFile("):script.index("  function useDesignImages(")]
    program = r"""
const assert = require('node:assert/strict');
let calls = [], ok = true, receiptFails = false;
const blob = {};
const SDK = {authedFetch: async () => ({ok, status: 502, blob: async () => {calls.push('bytes'); return blob;}})};
const post = async (path) => {calls.push(path); if (receiptFails) throw new Error('stale design');};
FUNCTION
(async () => {
 const design = {id: 'version-3', state: 'PENDING'};
 assert.equal(await designFile('T-10', design, 'desktop.png'), blob);
 assert.deepEqual(calls, ['bytes', '/tasks/T-10/ui-design/version-3/delivered/desktop.png']);
 calls = []; ok = false;
 await assert.rejects(designFile('T-10', design, 'mobile.png'));
 assert.deepEqual(calls, []);
 ok = true; receiptFails = true;
 await assert.rejects(designFile('T-10', design, 'mobile.png'), /stale design/);
 calls = []; receiptFails = false;
 await designFile('T-10', {...design, state: 'APPROVED'}, 'desktop.png');
 assert.deepEqual(calls, ['bytes']);
 calls = [];
 await designFile('T-10', design, 'design.md');
 assert.deepEqual(calls, ['bytes']);
})().catch(e => {console.error(e); process.exit(1);});
"""
    subprocess.run(["node", "-e", program.replace("FUNCTION", function)], check=True, capture_output=True, text=True)
