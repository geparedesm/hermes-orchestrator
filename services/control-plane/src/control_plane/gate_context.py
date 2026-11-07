"""The effective Quality Gate contract delivered before every agent assignment."""
from __future__ import annotations

import json
from typing import Any

from ho_core.verification import plan_verification

from .context import UnitOfWork
from .db import Row


def contract(config: dict[str, Any], paths: list[str]) -> str:
    # A preview from declared scope, never a promise about the eventual integrated diff.
    plan = plan_verification(config, paths)
    commands = config.get('commands') or {}
    severities = sorted({'HIGH', 'CRITICAL'} | set((config.get('quality_gate') or {}).get('block_on_findings', [])))
    lines = ['## Quality Gate contract (pinned effective configuration)',
             'Meet the task requirements and acceptance criteria, including the person’s answers and approved UI design.',
             'These checks are computed by the same planner as integration verification. Declared scope is only a preview; '
             'the platform recomputes risk and requirements from the actual integrated diff.',
             f'Estimated risk from declared paths: {plan.risk.risk.value}.',
             'Required for the declared scope:']
    for item in plan.requirements:
        lines.append(f'- {item.name} ({item.source})' + (f': {item.command}' if item.command else ''))
    lines += ['Configured verification commands (execute in the prepared workspace when applicable):']
    lines += [f'- {name}: {command}' for name, command in commands.items()
              if name in ('install', 'build', 'lint', 'typecheck', 'test', 'security', 'post_merge') and command]
    lines += ['At MEDIUM risk or above, lint and typecheck are required; HIGH or above also requires security checks.',
              'HIGH risk includes authentication, permissions, payments, migrations, infrastructure, dependencies and configured sensitive paths.',
              'Write and run relevant tests with code changes. Missing required commands are test gaps, not successful checks. '
              'Code without a test run, CRITICAL changes, and sensitive changes with test gaps need an explicit human exception.',
              f'No open review findings of these severities: {", ".join(severities)}.',
              'Cross-review must cover the integrated commit: the other provider reviews single-provider work; both review mixed-provider work.',
              'Integration must be conflict-free on the current target. The platform runs verification, integration review and the gate on that exact commit.',
              'No policy violations: stay within granted capabilities; do not attempt sudo, Docker, ungranted credential access, protected-branch writes or approval bypasses.',
              'UI changes require the current approved design before implementation; backend-only scope must not silently edit UI files.',
              'Only humans can grant exceptions, increase budgets, approve designs/plans, or approve merge. Report concrete missing inputs; never fabricate evidence.']
    verification = config.get('verification') or {}
    for setting in ('sensitive_paths', 'critical_paths'):
        if verification.get(setting):
            lines.append(f"Project {setting}: {', '.join(verification[setting])}")
    if (config.get('quality_gate') or {}).get('ci_checks', True):
        lines.append('For GitHub projects, required CI checks on the pull request must pass; the platform creates the PR.')
    if plan.gaps:
        lines += ['Potential gaps in the declared scope:'] + [f'- {g["detail"]}' for g in plan.gaps]
    return '\n'.join(lines)


def assignment_contract(uow: UnitOfWork, task: Row, config: dict[str, Any]) -> str:
    uow.cur.execute("SELECT estimated_scope FROM subtasks WHERE task_id = %s AND state <> 'CANCELLED'", (task['id'],))
    paths = sorted({p for row in uow.cur.fetchall() for p in (row['estimated_scope'] or {}).get('files', [])})
    text = contract(config, paths)
    uow.cur.execute('SELECT commit_sha, outcome, requirements FROM quality_gate_evaluations WHERE task_id = %s '
                    'ORDER BY evaluated_at DESC LIMIT 1', (task['id'],))
    gate = uow.cur.fetchone()
    if gate and gate['outcome'] != 'PASS':
        text += '\n\nPrevious Quality Gate evaluation (historical; fixes need a new evaluation on their commit):\n'
        text += json.dumps({**gate, 'requirements': [r for r in gate['requirements'] if r.get('status') != 'PASS']},
                           default=str, ensure_ascii=False)
        uow.cur.execute("SELECT r.reviewer_provider, f.severity, f.path, f.line, f.description FROM review_findings f "
                        "JOIN (SELECT DISTINCT ON (reviewer_provider) id, reviewer_provider FROM reviews "
                        "WHERE task_id = %s AND commit_sha = %s AND subtask_id IS NULL "
                        "ORDER BY reviewer_provider, created_at DESC) r ON r.id = f.review_id "
                        "WHERE f.status = 'OPEN' AND f.severity = ANY(%s) ORDER BY r.reviewer_provider, f.severity, f.path, f.line",
                        (task['id'], gate['commit_sha'], sorted({'HIGH', 'CRITICAL'} |
                         set((config.get('quality_gate') or {}).get('block_on_findings', [])))))
        findings = uow.cur.fetchall()
        if findings:
            text += '\nFindings from that evaluated commit (historical until checked against the corrections):\n'
            text += json.dumps(findings, default=str, ensure_ascii=False)
    return text
