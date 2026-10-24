"""Exact-pin image recovery, with no registry fallback or provider execution."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest
from docker.errors import NotFound

from agent_manager.docker_ops import DockerOps
from agent_manager.images import ImageNotAllowed, ImageRecovery, ImageUnavailable

PIN = "sha256:" + "a" * 64


def setup_ops(tmp_path):
    (tmp_path / "images.lock.yaml").write_text(f'images:\n  claude-odoo: "{PIN}"\n')
    client = Mock()
    ops = object.__new__(DockerOps)
    ops.client, ops.config_dir = client, tmp_path
    ops.image_recovery = ImageRecovery(client, tmp_path)
    return ops, client


def test_absent_image_is_infrastructure_not_policy(tmp_path):
    ops, client = setup_ops(tmp_path)
    client.images.get.side_effect = NotFound("absent")
    with pytest.raises(ImageUnavailable, match="make images"):
        ops.ensure_image("claude-odoo")
    client.api.load_image.assert_not_called()
    with pytest.raises(ImageNotAllowed):
        ops.ensure_image("not-allowed")


def test_recovery_streams_once_and_rechecks_exact_pin(tmp_path):
    ops, client = setup_ops(tmp_path)
    recovery = ops.image_recovery
    recovery.archive.parent.mkdir()
    recovery.archive.write_bytes(b"archive")
    entered, finish, done = threading.Event(), threading.Event(), threading.Event()

    def load(source):
        assert source.read() == b"archive"
        entered.set()
        assert finish.wait(5)
        client.images.get.side_effect = None
        client.images.get.return_value = SimpleNamespace(id=PIN, labels={"org.hermes-orchestrator.role": "claude-worker"})
        yield {"stream": "loaded"}

    client.api.load_image.side_effect = load
    client.images.get.side_effect = NotFound("absent")
    original = recovery._restore

    def restore():
        try:
            original()
        finally:
            done.set()

    recovery._restore = restore
    with pytest.raises(ImageUnavailable, match="restoring"):
        ops.ensure_image("claude-odoo")
    assert entered.wait(5)
    for _ in range(3):
        with pytest.raises(ImageUnavailable, match="restoring"):
            ops.ensure_image("claude-odoo")
    finish.set()
    assert done.wait(5)
    assert ops.ensure_image("claude-odoo").id == PIN
    client.api.load_image.assert_called_once()
    assert all(call.args == (PIN,) for call in client.images.get.call_args_list)
    assert PIN not in recovery._missing
    done.clear()
    client.images.get.side_effect = NotFound("deleted again")
    with pytest.raises(ImageUnavailable, match="restoring"):
        ops.ensure_image("claude-odoo")
    assert done.wait(5)
    assert client.api.load_image.call_count == 2


def test_corrupt_archive_failure_does_not_start_an_import_loop(tmp_path):
    ops, client = setup_ops(tmp_path)
    recovery = ops.image_recovery
    recovery.archive.parent.mkdir()
    recovery.archive.write_bytes(b"bad archive")
    client.api.load_image.return_value = iter([{"error": "invalid tar"}])
    recovery._restore()
    assert "failed" in recovery._error
    # Record the attempted generation, as request does before starting the background thread.
    stat = recovery.archive.stat()
    recovery._generation = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    assert "failed" in recovery.request(PIN)
    client.api.load_image.assert_called_once()


def test_unlabelled_image_still_refused(tmp_path):
    ops, client = setup_ops(tmp_path)
    client.images.get.return_value = SimpleNamespace(id=PIN, labels={})
    with pytest.raises(ImageNotAllowed, match="not a platform image"):
        ops.ensure_image("claude-odoo")


def test_recovery_endpoint_requires_service_authentication(tmp_path):
    from fastapi.testclient import TestClient
    from agent_manager.app import create_app

    ops, _ = setup_ops(tmp_path)
    app = create_app(ops, token="service-test-token", projects_root=tmp_path,
                     projects_root_host=str(tmp_path), reap_seconds=0)
    with TestClient(app) as api:
        assert api.post("/v1/images/claude-odoo/ensure").status_code == 403
        assert api.post("/v1/images/claude-odoo/ensure", headers={"Authorization": "Bearer wrong"}).status_code == 403
        response = api.post("/v1/images/unpinned/ensure", headers={"Authorization": "Bearer service-test-token"})
        assert response.status_code == 403 and response.json()["error"] == "image_not_allowed"



def test_unrecoverable_image_does_not_poison_other_image_recovery(tmp_path, monkeypatch):
    ops, client = setup_ops(tmp_path)
    recovery = ops.image_recovery
    recovery.archive.parent.mkdir()
    recovery.archive.write_bytes(b"archive missing one image")
    client.api.load_image.return_value = []
    def lookup(image_id):
        if image_id == PIN:
            raise NotFound("not in archive")
        return SimpleNamespace(id=image_id)
    client.images.get.side_effect = lookup
    # Drive import completion explicitly so each request can be observed without timing races.
    monkeypatch.setattr("agent_manager.images.threading.Thread", Mock())
    assert "restoring" in recovery.request(PIN)
    recovery._restore()
    assert "does not contain" in recovery.request(PIN)
    other = "sha256:" + "b" * 64
    assert "restoring" in recovery.request(other)
    recovery._restore()
    recovery.confirmed(other)
    assert "does not contain" in recovery.request(PIN)
    assert "restoring" in recovery.request(other)  # deleted again after its successful restoration
    recovery._restore()
    assert client.api.load_image.call_count == 3



def test_recovery_can_repeat_when_task_paused_before_observing_import(tmp_path, monkeypatch):
    ops, client = setup_ops(tmp_path)
    recovery = ops.image_recovery
    recovery.archive.parent.mkdir()
    recovery.archive.write_bytes(b"valid archive")
    client.api.load_image.return_value = []
    client.images.get.return_value = SimpleNamespace(id=PIN)
    monkeypatch.setattr("agent_manager.images.threading.Thread", Mock())
    assert "restoring" in recovery.request(PIN)
    recovery._restore()
    # No caller confirms the image: a paused task does not poll. Docker may prune the idle image later.
    assert "restoring" in recovery.request(PIN)
    recovery._restore()
    assert client.api.load_image.call_count == 2



def test_request_during_verification_can_start_its_own_recovery(tmp_path, monkeypatch):
    ops, client = setup_ops(tmp_path)
    recovery = ops.image_recovery
    recovery.archive.parent.mkdir()
    recovery.archive.write_bytes(b"archive")
    client.api.load_image.return_value = []
    thread = Mock()
    monkeypatch.setattr("agent_manager.images.threading.Thread", thread)
    other = "sha256:" + "b" * 64
    injected = False
    def lookup(image_id):
        nonlocal injected
        if image_id == PIN:
            if not injected:
                injected = True
                assert "restoring" in recovery.request(other)
            raise NotFound("not in archive")
        return SimpleNamespace(id=image_id)
    client.images.get.side_effect = lookup
    assert "restoring" in recovery.request(PIN)
    recovery._restore()
    assert thread.return_value.start.call_count == 2
    assert recovery._running  # the first import must not clear the second import's state
    recovery._restore()
    assert "restoring" in recovery.request(other)  # a later deletion is still recoverable
