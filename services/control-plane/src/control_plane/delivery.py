"""Whole-feature assignments and a reviewable planning contract."""
from __future__ import annotations
import re
from typing import Any
from .context import Context, UnitOfWork
from .db import Row

PLAN_SECTIONS = (
    'Objective', 'Current implementation', 'Acceptance criteria', 'Technical design',
    'Implementation steps', 'Verification', 'Human test', 'Risks and assumptions',
)
PLANNING_INSTRUCTIONS = (
    'Keep one accountable developer for the complete feature. Create exactly one IMPLEMENT subtask, '
    'including its tests and corrections; use RESEARCH only when necessary. Do not split by file or layer. '
    'Explore the repository first and cite concrete files in the plan. Preserve the original request and human answers. '
    'SET_PLAN.text must be a Markdown plan with these exact level-two headings: '
    + ', '.join('## ' + section for section in PLAN_SECTIONS) + '. '
    'Each section needs concrete content. Acceptance criteria must describe observable outcomes, verification must '
    'map those outcomes to checks, and Human test must give reproducible steps and required data. '
    'For Odoo, include any required synthetic acceptance scenarios in an idempotent .hermes/preview-seed.py hook. '
    'It runs in the isolated Odoo shell after module upgrades, with env provided, never against the source database. '
    'Explain unknowns and dependencies rather than inventing facts. Detail should match the task complexity. '
    'The implementation subtask must carry the whole feature, not just a first stage. '
    'Use the same local key when revising its assignment. Important scope changes require clarification. '
    'The platform still enforces independent review, tests, budgets, and human merge approval.'
)


def validate_feature_plan(items: list[dict[str, Any]], document: str | None) -> None:
    if sum(item.get('kind', 'IMPLEMENT') == 'IMPLEMENT' for item in items) != 1:
        raise ValueError('FEATURE mode requires exactly one IMPLEMENT subtask responsible for the complete feature')
    if any(item.get('kind') == 'TEST_AUTHORING' for item in items):
        raise ValueError('FEATURE mode includes test authoring in the responsible developer assignment')
    if not document or len(document) > 60000:
        raise ValueError('FEATURE mode requires SET_PLAN.text with a structured plan (at most 60000 characters)')
    for section in PLAN_SECTIONS:
        match = re.search(r'^## ' + re.escape(section) + r'\s*\n(.*?)(?=^## |\Z)', document, re.M | re.S)
        if not match or not match.group(1).strip():
            raise ValueError(f'plan needs a nonempty section: ## {section}')


def feature_context(ctx: Context, uow: UnitOfWork, task: Row, original: str) -> str:
    """Authoritative context is restored even when no provider session survived a restart."""
    parts = ['# Complete feature context', 'Current requirements take precedence over the original request and earlier answers.',
             '## Original request', original]
    uow.cur.execute('SELECT a.path FROM requirement_versions r JOIN artifacts a ON a.id = r.artifact_id '
                    'WHERE r.task_id = %s ORDER BY r.version DESC LIMIT 1', (task['id'],))
    row = uow.cur.fetchone()
    if row:
        parts += ['## Current requirements', ctx.artifacts.read(row['path']).decode('utf-8', 'replace')]
    uow.cur.execute('SELECT a.path FROM feature_plans p JOIN artifacts a ON a.id = p.document_artifact_id '
                    'WHERE p.task_id = %s ORDER BY p.version DESC LIMIT 1', (task['id'],))
    row = uow.cur.fetchone()
    if row:
        parts += ['## Implementation plan', ctx.artifacts.read(row['path']).decode('utf-8', 'replace')]
    uow.cur.execute("SELECT questions, answers FROM clarifications WHERE task_id = %s AND state = 'ANSWERED' ORDER BY asked_at",
                    (task['id'],))
    for entry in uow.cur.fetchall():
        for question in entry['questions']:
            answer = (entry['answers'] or {}).get(question['id']) or {}
            parts.append(f"Human clarification: {question['text']} — {answer.get('text') or answer.get('label') or ''}")
    parts.append('Own the complete requested behavior. Check every acceptance criterion. Keep decisions and unresolved '
                 'issues in the operational result so a subsequent correction can continue. Report scope changes.')
    return '\n\n'.join(parts)
