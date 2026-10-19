"""Operator-side Odoo preview controller; workers never receive Docker or API credentials.

A candidate is replaced only after explicit retirement. One commit per repository is included;
compatible module tasks share a frozen application. Unknown application families wait for an adapter.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from operator_api import ROOT, api

COMPOSE = ['docker', 'compose', '--env-file', str(ROOT / 'deploy/preview/runtime.env'),
           '-f', str(ROOT / 'deploy/preview/compose.yaml')]
BASE = Path('/srv/HermesProjects/odoo-base')


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def export(repo, sha, destination):
    if not repo.is_dir() or not git(repo, 'rev-parse', '--verify', sha + '^{commit}') == sha:
        raise RuntimeError('candidate source commit is unavailable')
    with tempfile.TemporaryFile() as archive:
        run(['git', '-C', str(repo), 'archive', sha], stdout=archive)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as files:
            # The stdlib data filter refuses paths and links escaping the build directory.
            files.extractall(destination, filter='data')


def update_runtime(image):
    path = ROOT / 'deploy/preview/runtime.env'
    lines = path.read_text().splitlines()
    lines = ['HO_PREVIEW_ODOO_IMAGE=' + image if line.startswith('HO_PREVIEW_ODOO_IMAGE=') else line for line in lines]
    path.write_text('\n'.join(lines) + '\n')


def cycle():
    previews = api('GET', '/v1/previews')['candidates']
    active = next((p for p in previews if p['environment'] == 'odoo' and p['state'] in ('PREPARING', 'READY', 'TESTING')), None)
    if active:
        members = active['manifest']['tasks']
        states = [api('GET', '/v1/tasks/' + item['task'])['state'] for item in members]
        finished = bool(members) and all(state in ('DONE', 'CANCELLED') for state in states)
        stale_preparation = (active['state'] == 'PREPARING'
            and (datetime.now(timezone.utc) - datetime.fromisoformat(active['created_at'])).total_seconds() > 600)
        # A version whose tasks changed (feedback sent one back to work) or that expired can no longer be approved: it
        # is retired, or the one-active-candidate rule would keep its corrected replacement from ever being prepared.
        stale = active['state'] in ('READY', 'TESTING') and any(state != 'READY_FOR_MERGE' for state in states)
        expired = bool(active.get('expires_at')) and datetime.fromisoformat(active['expires_at']) <= datetime.now(timezone.utc)
        if finished or stale_preparation or stale or expired:
            # Approving/completing the group ends the test session; no unfinished test is replaced.
            api('POST', '/v1/previews/' + active['id'] + '/retire', {})
        else:
            return
    projects = {p['slug']: p for p in api('GET', '/v1/projects')['projects']}
    tasks = api('GET', '/v1/tasks?state=READY_FOR_MERGE')['tasks']
    selected, by_project = [], {}
    for task in sorted(tasks, key=lambda t: t['created_at']):
        project = projects[task['project']]
        path = Path(project['host_path']).resolve()
        if not task.get('human_test_required') or project['slug'] in by_project:
            continue
        if path != BASE and path.parent != BASE / 'custom-addons':
            continue  # No invented launch commands for other application families.
        changes = api('GET', '/v1/tasks/' + task['key'] + '/git')
        selected.append((task, project, changes))
        by_project[project['slug']] = (task, project, changes)
    if not selected:
        return
    # Export pinned commits rather than copying working trees or secrets into the Docker build context.
    with tempfile.TemporaryDirectory(prefix='hermes-preview-build-') as temporary:
        context = Path(temporary)
        parent = next(p for p in projects.values() if Path(p['host_path']).resolve() == BASE)
        base_sha = git(BASE, 'rev-parse', parent['default_branch'])
        base_task = by_project.get('odoo-base')
        if base_task:
            base_sha = base_task[2]['changes']['integration_sha']
        export(BASE, base_sha, context)
        module_heads = {}
        for module in sorted((BASE / 'custom-addons').iterdir()):
            if not (module / '.git').exists():
                continue
            registered = next((p for p in projects.values() if Path(p['host_path']).resolve() == module.resolve()), None)
            if not registered:
                continue
            chosen = next((entry for entry in selected if Path(entry[1]['host_path']).resolve() == module.resolve()), None)
            sha = chosen[2]['changes']['integration_sha'] if chosen else git(module, 'rev-parse', registered['default_branch'])
            destination = context / 'custom-addons' / module.name
            destination.mkdir(parents=True, exist_ok=True)
            export(module, sha, destination)
            module_heads[module.name] = sha
        image = 'hermes-orchestrator/preview-odoo:' + str(time.time_ns())
        run(['docker', 'build', '-t', image, str(context)])
        image_id = subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'], text=True).strip()
    candidate = api('POST', '/v1/previews', {'environment': 'odoo', 'url': 'https://test.gp-developer.com/',
                    'tasks': [entry[0]['key'] for entry in selected],
                    'deployment': {'image_digest': image_id, 'base_sha': base_sha, 'module_heads': module_heads,
                        'snapshot_id': subprocess.check_output(COMPOSE + ['exec', '-T', 'db', 'psql', '-U', 'preview',
                            '-d', 'preview', '-Atc', "SELECT value FROM ir_config_parameter WHERE key='database.uuid'"], text=True).strip()}})
    # Refuse a source change that occurred between exporting the commits and preparing the manifest.
    for entry, planned in zip(sorted(selected, key=lambda x: x[0]['key']), candidate['manifest']['tasks'], strict=True):
        if entry[2]['changes']['integration_sha'] != planned['head_sha']:
            api('POST', '/v1/previews/' + candidate['id'] + '/retire', {})
            raise RuntimeError('candidate changed while its image was being built; retry on the next cycle')
    update_runtime(image_id)
    (ROOT / "deploy/preview/current.json").write_text(json.dumps({"label": "Preview version " + str(candidate["version"])
        + " · " + ", ".join(item["task"] for item in candidate["manifest"]["tasks"]), "candidate_id": candidate["id"]}))
    run(COMPOSE + ['stop', 'odoo'])
    try:
        run(COMPOSE + ['run', '--rm', '-T', 'odoo', 'upgrade'])
        run(COMPOSE + ['up', '-d', 'odoo'])
        for _ in range(90):
            check = subprocess.run(COMPOSE + ['exec', '-T', 'gateway', 'python', '-c',
                "import urllib.request; urllib.request.urlopen('http://odoo:8069/web/health',timeout=3)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError('candidate did not become healthy')
        api('POST', '/v1/previews/' + candidate['id'] + '/ready', {'manifest_hash': candidate['manifest_hash']})
    except Exception:
        api('POST', '/v1/previews/' + candidate['id'] + '/retire', {})
        raise
    print(json.dumps({'candidate': candidate['id'], 'version': candidate['version'], 'tasks': candidate['manifest']['tasks']}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    # One operator-side process may prepare images at a time.
    import fcntl
    with open(ROOT / 'deploy/preview/controller.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failures = 0
        while True:
            try:
                cycle()
                failures = 0
            except Exception as exc:
                failures += 1
                print(json.dumps({'error': str(exc)[:1000], 'consecutive_failures': failures}), flush=True)
                if args.once:
                    raise
            if args.once:
                break
            time.sleep(min(300, 30 * 2 ** min(failures, 4)))


if __name__ == '__main__':
    main()
