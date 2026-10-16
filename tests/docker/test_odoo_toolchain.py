"""The odoo toolchain: `ho-odoo-test` installs a module from the real /workspace mount into a throwaway database
on a PostgreSQL service and runs its tests (docs/design/child-projects.md)."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import docker
import pytest
import yaml

from conftest import ROOT  # type: ignore[import-not-found]

MODULE = Path(__file__).resolve().parents[1] / "fixtures" / "odoo_module"


def _image() -> str:
    if os.environ.get("HO_TEST_ODOO_IMAGE"):  # an image built apart, before `make images` pins it
        return os.environ["HO_TEST_ODOO_IMAGE"]
    lock = yaml.safe_load((ROOT / "config" / "images.lock.yaml").read_text())
    image = (lock.get("images") or {}).get("runner-odoo")
    if not image:
        pytest.skip('build it with HO_TOOLCHAINS="... odoo" make images')
    return image


@pytest.fixture
def database():
    client = docker.from_env()
    network = client.networks.create(f"ho-odoo-test-{uuid.uuid4().hex[:8]}")
    postgres = client.containers.run("postgres:16-bookworm", detach=True, network=network.name, name=f"pg-{uuid.uuid4().hex[:8]}",
                                     environment={"POSTGRES_USER": "odoo", "POSTGRES_PASSWORD": "odoo", "POSTGRES_DB": "postgres"})
    try:
        yield client, network, postgres.name
    finally:
        postgres.remove(force=True)
        network.remove()


def run(database, workspace: Path, module: str) -> tuple[int, str]:
    client, network, host = database
    container = client.containers.run(
        _image(), ["ho-odoo-test", module], detach=True, network=network.name, user="10001:10001",
        environment={"HO_ODOO_DB_HOST": host}, volumes={str(workspace): {"bind": "/workspace", "mode": "ro"}},
        read_only=True, tmpfs={"/tmp": "size=512m"}, cap_drop=["ALL"], security_opt=["no-new-privileges"])
    try:
        status = container.wait(timeout=900)["StatusCode"]
        return status, container.logs().decode(errors="replace")
    finally:
        container.remove(force=True)


def test_a_module_is_installed_and_its_tests_run(database, tmp_path):
    workspace = tmp_path / "ho_sample"
    shutil.copytree(MODULE, workspace)
    status, logs = run(database, workspace, "ho_sample")
    assert status == 0, logs[-3000:]
    assert "ho-odoo-test: ho_sample installed; 0 failed, 0 error(s) of" in logs


def test_a_failing_test_fails_the_run(database, tmp_path):
    workspace = tmp_path / "ho_sample"
    shutil.copytree(MODULE, workspace)
    test = workspace / "tests" / "test_install.py"
    test.write_text(test.read_text().replace('assertEqual(module.state, "installed")', 'assertEqual(module.state, "uninstalled")'))
    status, logs = run(database, workspace, "ho_sample")
    assert status != 0 and "FAILED" in logs, logs[-3000:]


def test_a_module_odoo_cannot_load_fails_the_run(database, tmp_path):
    workspace = tmp_path / "ho_sample"
    shutil.copytree(MODULE, workspace)
    manifest = workspace / "__manifest__.py"
    manifest.write_text(manifest.read_text().replace('"installable": True', '"installable": False'))
    status, logs = run(database, workspace, "ho_sample")
    assert status != 0 and "FAILED" in logs, logs[-3000:]


def test_a_workspace_without_a_module_is_refused(database, tmp_path):
    workspace = tmp_path / "empty"
    workspace.mkdir()
    status, logs = run(database, workspace, "ho_sample")
    assert status == 2 and "no __manifest__.py" in logs, logs[-3000:]


def test_login_shells_find_the_test_runner():
    """T-50: agents run commands in login shells, whose /etc/profile resets PATH; ho-odoo-test must still be found."""
    client = docker.from_env()
    container = client.containers.run(_image(), ["bash", "-lc", "command -v ho-odoo-test && echo found"], detach=True,
                                      user="10001:10001")
    try:
        assert container.wait(timeout=120)["StatusCode"] == 0
        assert "found" in container.logs().decode()
    finally:
        container.remove(force=True)


def test_standalone_python_can_extract_a_pdf_with_the_pinned_module_dependency():
    client = docker.from_env()
    command = "from io import BytesIO; from pypdf import PdfReader, PdfWriter; b=BytesIO(); w=PdfWriter(); w.add_blank_page(72,72); w.write(b); b.seek(0); assert len(PdfReader(b).pages)==1"
    container = client.containers.run(_image(), ["/usr/bin/python3", "-c", command], detach=True,
                                      user="10001:10001", network_mode="none", read_only=True)
    try:
        assert container.wait(timeout=120)["StatusCode"] == 0, container.logs().decode()
    finally:
        container.remove(force=True)
