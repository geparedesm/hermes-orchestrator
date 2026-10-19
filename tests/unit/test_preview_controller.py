"""Frozen human-test versions and safe candidate source extraction."""
import importlib.util
from pathlib import Path
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/preview'))
    spec = importlib.util.spec_from_file_location('preview_controller_test', ROOT / 'deploy/preview/controller.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_human_test_session_is_never_replaced_automatically(controller, monkeypatch):
    calls = []
    def api(method, path, body=None):
        calls.append((method, path))
        return {'candidates': [{'environment': 'odoo', 'state': 'TESTING', 'manifest': {'tasks': []}}]}
    monkeypatch.setattr(controller, 'api', api)
    controller.cycle()
    assert calls == [('GET', '/v1/previews')]


@pytest.mark.parametrize('candidate, task_state', [
    ({'state': 'TESTING', 'expires_at': '2999-01-01T00:00:00+00:00'}, 'RUNNING'),  # feedback sent it back to work
    ({'state': 'TESTING', 'expires_at': '2000-01-01T00:00:00+00:00'}, 'READY_FOR_MERGE'),  # expired
    ({'state': 'READY', 'expires_at': '2000-01-01T00:00:00+00:00'}, 'READY_FOR_MERGE'),
])
def test_a_version_that_can_no_longer_be_approved_is_retired(controller, monkeypatch, candidate, task_state):
    calls = []
    def api(method, path, body=None):
        calls.append((method, path))
        if path == '/v1/previews':
            return {'candidates': [{'id': 'c1', 'environment': 'odoo', 'manifest': {'tasks': [{'task': 'T-9'}]}, **candidate}]}
        if path == '/v1/tasks/T-9':
            return {'state': task_state}
        if path == '/v1/projects':
            return {'projects': []}
        if path.startswith('/v1/tasks?'):
            return {'tasks': []}
        return {}
    monkeypatch.setattr(controller, 'api', api)
    controller.cycle()
    assert ('POST', '/v1/previews/c1/retire') in calls


def test_a_current_test_session_is_kept(controller, monkeypatch):
    calls = []
    def api(method, path, body=None):
        calls.append((method, path))
        if path == '/v1/previews':
            return {'candidates': [{'id': 'c1', 'environment': 'odoo', 'state': 'TESTING', 'expires_at': '2999-01-01T00:00:00+00:00',
                                    'manifest': {'tasks': [{'task': 'T-9'}]}}]}
        return {'state': 'READY_FOR_MERGE'}
    monkeypatch.setattr(controller, 'api', api)
    controller.cycle()
    assert ('POST', '/v1/previews/c1/retire') not in calls


def test_archived_source_cannot_escape_the_build_directory(controller, tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'escape').symlink_to('/etc/passwd')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'candidate'], check=True)
    sha = controller.git(repo, 'rev-parse', 'HEAD')
    destination = tmp_path / 'build'
    destination.mkdir()
    with pytest.raises(tarfile.FilterError):
        controller.export(repo, sha, destination)
    assert not (destination / 'escape').exists()
