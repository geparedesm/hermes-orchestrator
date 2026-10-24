"""Restore an exact deleted image using Docker's real save/load protocol, without network or workers."""
import gzip
import subprocess
import threading
import uuid

import pytest
from docker.errors import NotFound

from agent_manager.docker_ops import DockerOps
from agent_manager.images import ImageRecovery, ImageUnavailable


def test_exact_deleted_image_restored_from_local_archive(client, tmp_path):
    tag = f"ho-test-image-recovery:{uuid.uuid4().hex}"
    context = tmp_path / "build"
    context.mkdir()
    (context / "Dockerfile").write_text('FROM scratch\nCOPY marker /marker\nLABEL org.hermes-orchestrator.role="test-worker"\n')
    (context / "marker").write_text(uuid.uuid4().hex)
    # Match the platform builder, including containerd/BuildKit image manifests and attestations.
    subprocess.run(["docker", "build", "--quiet", "--tag", tag, str(context)], check=True, capture_output=True)
    image = client.images.get(tag)
    pin = image.id
    recovery = ImageRecovery(client, tmp_path)
    try:
        (tmp_path / "images.lock.yaml").write_text(f'images:\n  agent-base: "{pin}"\n')
        recovery.archive.parent.mkdir()
        with gzip.open(recovery.archive, "wb", compresslevel=1) as output:
            for chunk in client.api.get_image(pin):
                output.write(chunk)
        client.images.remove(tag)
        with pytest.raises(NotFound):
            client.images.get(pin)
        ops = object.__new__(DockerOps)
        ops.client, ops.config_dir, ops.image_recovery = client, tmp_path, recovery
        done = threading.Event()
        original = recovery._restore
        def restore():
            try:
                original()
            finally:
                done.set()
        recovery._restore = restore
        with pytest.raises(ImageUnavailable):
            ops.ensure_image("agent-base")
        assert done.wait(30) and recovery._error is None
        # No task observed the first import: deleting the idle image must still allow a second recovery.
        client.images.remove(pin, force=True)
        done.clear()
        with pytest.raises(ImageUnavailable):
            ops.ensure_image("agent-base")
        assert done.wait(30) and recovery._error is None
        assert ops.ensure_image("agent-base").id == pin
    finally:
        try:
            client.images.remove(pin, force=True)
        except NotFound:
            pass
