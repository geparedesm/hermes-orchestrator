"""Immutable shared test candidates. Only the operator can attest a deployment.

Applications are deployed separately; a passing test suite alone never marks a preview ready.
The human merge decision is bound to the candidate manifest as well as the exact Git state.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from uuid import UUID
from urllib.parse import urlsplit
from ho_core.hashing import hash_value
from ho_core.ids import uuid7
from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import BadRequest, Conflict, Forbidden, NotFound
from .events import record_event


class Previews:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _human(self, principal: Principal):
        if principal.value not in self.ctx.platform['platform']['approvers']:
            raise Forbidden('only an approver may manage a human test session')

    def get(self, uow: UnitOfWork, candidate_id: UUID | str, *, lock: bool = False) -> Row:
        uow.cur.execute('SELECT * FROM preview_candidates WHERE id = %s' + (' FOR UPDATE' if lock else ''), (candidate_id,))
        row = uow.cur.fetchone()
        if not row:
            raise NotFound('preview candidate not found')
        return row

    def prepare(self, uow: UnitOfWork, *, environment: str, url: str, tasks: list[str], principal: Principal, deployment: dict[str, Any] | None = None) -> Row:
        self._human(principal)
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', environment):
            raise BadRequest('invalid environment name')
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise BadRequest('invalid preview URL port') from exc
        if (parsed.scheme != 'https' or parsed.hostname != 'test.gp-developer.com' or port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise BadRequest('preview URL must be HTTPS on test.gp-developer.com without credentials or query parameters')
        if not tasks or len(set(tasks)) != len(tasks) or len(tasks) > 50:
            raise BadRequest('choose between 1 and 50 distinct tasks')
        # Serializes preparation even when no candidate exists yet.
        uow.cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('preview:' + environment,))
        uow.cur.execute("SELECT id FROM preview_candidates WHERE environment = %s AND state IN ('PREPARING', 'READY', 'TESTING')",
                        (environment,))
        if uow.cur.fetchone():
            raise Conflict('retire the current preview explicitly before preparing another version; tests must stay stable')
        entries = []
        included_projects = set()
        for key in sorted(tasks):
            uow.cur.execute('SELECT t.*, g.integration_sha, g.integration_target_sha FROM tasks t '
                            'JOIN git_changes g ON g.task_id = t.id WHERE t.key = %s FOR UPDATE OF t', (key,))
            task = uow.cur.fetchone()
            if not task or task['state'] != 'READY_FOR_MERGE' or not task['integration_sha']:
                raise Conflict(f'{key} must have a verified integrated change and be READY_FOR_MERGE')
            if task['project_id'] in included_projects:
                raise Conflict('a shared candidate may include only one task per repository; queue the remaining tasks')
            included_projects.add(task['project_id'])
            uow.cur.execute("SELECT id FROM quality_gate_evaluations WHERE task_id = %s AND commit_sha = %s "
                            "AND outcome = 'PASS' ORDER BY evaluated_at DESC LIMIT 1", (task['id'], task['integration_sha']))
            gate = uow.cur.fetchone()
            if not gate:
                raise Conflict(f'{key} has no passing Quality Gate for the candidate')
            uow.cur.execute('SELECT effective_hash FROM project_configs WHERE id = %s', (task['config_id'],))
            config = uow.cur.fetchone()
            entries.append({'task': key, 'head_sha': task['integration_sha'], 'target_sha': task['integration_target_sha'],
                            'quality_gate': str(gate['id']), 'config_hash': config['effective_hash'],
                            'requirements_version': task['current_requirements_version']})
        uow.cur.execute('SELECT coalesce(max(version), 0) + 1 AS version FROM preview_candidates WHERE environment = %s', (environment,))
        version = uow.cur.fetchone()['version']
        manifest = {'environment': environment, 'version': version, 'tasks': entries}
        if deployment:
            manifest['deployment'] = deployment
        candidate_id = uuid7()
        uow.cur.execute("INSERT INTO preview_candidates (id, environment, version, url, state, manifest, manifest_hash, "
                        "created_by, expires_at) VALUES (%s, %s, %s, %s, 'PREPARING', %s, %s, %s, %s)",
                        (candidate_id, environment, version, url, jsonb(manifest), hash_value(manifest), principal.value,
                         datetime.now(timezone.utc) + timedelta(days=7)))
        self._event(uow, candidate_id, 'PREVIEW_PREPARING', 'Preparing a shared human-test candidate', principal)
        return self.get(uow, candidate_id)

    def activate(self, uow: UnitOfWork, candidate_id: UUID, *, manifest_hash: str, principal: Principal) -> Row:
        row = self.get(uow, candidate_id, lock=True)
        if row['state'] != 'PREPARING' or row['expires_at'] <= datetime.now(timezone.utc):
            raise Conflict('only an unexpired PREPARING candidate may become ready')
        if manifest_hash != row['manifest_hash']:
            raise Conflict('deployed manifest does not match the prepared candidate')
        uow.cur.execute("UPDATE preview_candidates SET state = 'READY', ready_at = now() WHERE id = %s", (candidate_id,))
        self._event(uow, candidate_id, 'PREVIEW_ACTIVATED', 'The candidate is deployed and ready for human testing', principal)
        for entry in row['manifest']['tasks']:
            uow.cur.execute('SELECT id, project_id FROM tasks WHERE key = %s', (entry['task'],))
            task = uow.cur.fetchone()
            record_event(uow.cur, 'PREVIEW_READY', actor=principal.value, task_id=task['id'], project_id=task['project_id'],
                         summary=f"Test version {row['version']}: {row['url']}",
                         data={'candidate_id': str(candidate_id), 'url': row['url'], 'version': row['version']}, pending=uow.events)

        return self.get(uow, candidate_id)

    def begin(self, uow: UnitOfWork, candidate_id: UUID, principal: Principal) -> Row:
        self._human(principal)
        row = self.get(uow, candidate_id, lock=True)
        if row['state'] not in ('READY', 'TESTING') or row['expires_at'] <= datetime.now(timezone.utc):
            raise Conflict('the preview is not available for testing')
        if row['state'] == 'TESTING':
            return row
        uow.cur.execute("UPDATE preview_candidates SET state = 'TESTING' WHERE id = %s", (candidate_id,))
        self._event(uow, candidate_id, 'HUMAN_TEST_STARTED', 'The human opened the candidate for testing', principal)
        return self.get(uow, candidate_id)

    def retire(self, uow: UnitOfWork, candidate_id: UUID, principal: Principal) -> Row:
        self._human(principal)
        self.get(uow, candidate_id, lock=True)
        uow.cur.execute("UPDATE preview_candidates SET state = 'RETIRED' WHERE id = %s", (candidate_id,))
        self._event(uow, candidate_id, 'PREVIEW_RETIRED', 'Preview retired explicitly', principal)
        return self.get(uow, candidate_id)

    def binding(self, uow: UnitOfWork, task: Row, changes: Row, *, require_testing: bool = False) -> dict[str, Any] | None:
        if not task.get('human_test_required'):
            return None
        uow.cur.execute("SELECT * FROM preview_candidates WHERE state IN ('READY', 'TESTING') AND expires_at > now() "
                        "AND manifest->'tasks' @> %s ORDER BY created_at DESC FOR SHARE",
                        (jsonb([{'task': task['key']}]),))
        candidates = uow.cur.fetchall()
        uow.cur.execute('SELECT effective_hash FROM project_configs WHERE id = %s', (task['config_id'],))
        current_config = uow.cur.fetchone()
        uow.cur.execute("SELECT id FROM quality_gate_evaluations WHERE task_id = %s AND commit_sha = %s "
                        "AND outcome = 'PASS' ORDER BY evaluated_at DESC LIMIT 1", (task['id'], changes['integration_sha']))
        gate = uow.cur.fetchone()
        for candidate in candidates:
            entry = next(e for e in candidate['manifest']['tasks'] if e['task'] == task['key'])
            if (entry['head_sha'] == changes['integration_sha'] and entry['target_sha'] == changes['integration_target_sha']
                    and entry['requirements_version'] == task['current_requirements_version']
                    and current_config and entry['config_hash'] == current_config['effective_hash']
                    and gate and entry['quality_gate'] == str(gate['id'])):
                if require_testing and candidate['state'] != 'TESTING':
                    raise Conflict('open the test candidate before confirming that you tested it and authorizing the merge')
                return {'candidate_id': str(candidate['id']), 'manifest_hash': candidate['manifest_hash'],
                        'version': candidate['version'], 'url': candidate['url'],
                        'tasks': [item['task'] for item in candidate['manifest']['tasks']]}
        raise Conflict('human testing required: deploy a current candidate before requesting or approving the merge')

    def _event(self, uow: UnitOfWork, candidate_id: UUID, event: str, summary: str, principal: Principal):
        record_event(uow.cur, event, actor=principal.value, summary=summary,
                     data={'candidate_id': str(candidate_id)}, pending=uow.events)
