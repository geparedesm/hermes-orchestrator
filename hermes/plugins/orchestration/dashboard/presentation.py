"""Read-only task explanations. No approvals, state changes or model-generated diagnoses."""
from __future__ import annotations

from datetime import datetime, timezone
import re

TERMINAL = {'DONE', 'FAILED', 'CANCELLED'}
ACTIVE_RUNS = {'REQUESTED', 'STARTING', 'RUNNING', 'STOPPING'}
LABELS = {
    'BACKLOG': ('Waiting to start', 'The task is waiting for its prerequisites.'),
    'READY': ('Ready to start', 'The task is ready for the next available agent.'),
    'QUEUED': ('Waiting for an agent', 'The next step is queued.'),
    'PLANNING': ('Preparing your request', 'Hermes is checking what needs to be built.'),
    'RUNNING': ('Working on your request', 'An agent is working on the change.'),
    'FIX_REQUIRED': ('Corrections are needed', 'The checks found something that needs to be fixed.'),
    'TESTING': ('Checking the change', 'The automated tests are running.'),
    'REVIEW': ('Reviewing the change', 'A second agent is reviewing the work.'),
    'QUALITY_GATE': ('Running the final checks', 'Hermes is checking whether the change is ready.'),
    'READY_FOR_MERGE': ('Ready to apply', 'The required checks passed. Review the decision before applying the change.'),
    'MERGING': ('Applying the change', 'The approved change is being applied to the project.'),
    'VERIFYING': ('Checking the applied result', 'Hermes is verifying the result after applying the change.'),
    'APPROVAL_REQUIRED': ('A decision is needed', 'Review the decision below to continue.'),
    'PAUSED': ('Paused', 'A person paused this task. Resume it when you are ready.'),
    'PAUSED_BUDGET': ('More runs need authorization', 'The task paused at an allowance limit.'),
    'AUTH_REQUIRED': ('An agent needs to sign in', 'An operator needs to reconnect the agent.'),
    'BLOCKED': ('Work is on hold', 'An operator needs to check why the task cannot continue.'),
    'FAILED': ('The task did not complete', 'Review the failure before deciding whether to try again.'),
    'CANCELLED': ('Work was stopped', 'The task was cancelled. It was not delivered as a completed change.'),
    'DONE': ('The change is complete', 'The task passed its required completion checks.'),
}
COUNTERS = {'agent_launches': 'agent runs', 'retries': 'retries', 'runtime_minutes': 'working minutes',
            'review_cycles': 'review rounds', 'subtasks': 'subtasks', 'provider_usage_units': 'usage units'}


def guidance(data: dict, context: dict | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    task = data['task']
    state = task['state']
    finished = state in TERMINAL
    consistent = context is not None and context.get('state') == state
    title, explanation = LABELS.get(state, ('Status needs checking', 'Open Advanced for the recorded details.'))
    blockers = []

    def add(code, heading, detail, owner, next_step):
        blockers.append({'code': code, 'title': heading, 'detail': detail, 'owner': owner, 'next_step': next_step})

    # Never turn an old task reason into a new fact. Only recognize missing-input reports on currently blocked subtasks.
    subtasks = (data.get('dag') or {}).get('subtasks') or []
    missing = any(s.get('state') == 'BLOCKED' and re.search(
        r'\b(?:findings?|review (?:reports?|details))\b[^.\n]*(?:absent|missing|unavailable|not supplied|not provided|cannot[^.\n]*access)|'
        r'(?:missing|absent|unavailable|cannot[^.\n]*access)[^.\n]*\b(?:findings?|review (?:reports?|details))\b',
        s.get('state_reason') or '', re.I)
        for s in subtasks)
    if not finished and missing:
        add('review_input', 'The agent needs the review details',
            'The blocked work reports that the review details were not supplied.', 'Operator',
            'Recover the exact review report and give it to the agent before retrying.')
    # Inspect supplies live budget state, whereas dashboard counters are useful evidence. Do not treat spent
    # allowance on a completed task as a current blocker, or at-limit in-flight work as a failed launch.
    budget = (context.get('budget') if consistent else None) or data.get('budget') or {}
    exhausted = budget.get('state') == 'EXHAUSTED'
    if not finished and exhausted:
        limits, used = budget.get('limits') or {}, budget.get('consumed') or {}
        counts = [f"{used.get(k, 0):g} / {v:g} {COUNTERS.get(k, k)}" for k, v in limits.items()
                  if isinstance(v, (int, float)) and isinstance(used.get(k, 0), (int, float)) and used.get(k, 0) >= v]
        add('budget', 'The approved allowance is used up', '; '.join(counts) or 'An allowance limit has been reached.',
            'Approver', 'Review a request for more allowance. Other blockers must also be resolved.')
    pending = context.get('pending_launches', []) if consistent else []
    if not finished and pending:
        add('queue', 'The next step is waiting', 'There is queued work that has not started.', 'Hermes / Operator',
            'Hermes checks the queue automatically. An operator can inspect the wait details in Advanced.')
    if state == 'AUTH_REQUIRED':
        add('auth', 'An agent needs to sign in', 'The platform reports that authentication is required.', 'Operator',
            'Reconnect the agent before continuing.')
    if not finished and not consistent:
        add('context', 'Some status details could not be checked',
            'The task summary is available, but its current execution context could not be confirmed.', 'Operator',
            'Refresh the status before retrying work.')
    if state == 'BLOCKED' and not missing:
        add('unknown', 'The recorded blockage needs checking',
            'The task is still blocked. Its earlier reason may no longer describe the current situation.', 'Operator',
            'Check the original reason and current evidence in Advanced before retrying.')
    running = any(e.get('state') in ACTIVE_RUNS for e in data.get('executions', []))
    if state in {'RUNNING', 'PLANNING', 'FIX_REQUIRED', 'REVIEW', 'TESTING', 'QUALITY_GATE'} and not running and not pending:
        if consistent:
            add('idle', 'No agent run is active', 'The task has no active agent run in the latest snapshot.', 'Operator',
                'Check whether the next step needs scheduling. Do not assume work is still running.')
    if blockers and all(b['code'] == 'queue' for b in blockers):
        title, explanation = 'Waiting for an agent', 'The next step is queued. No action is needed from you.'
    elif blockers and state not in TERMINAL:
        title = 'Work is on hold' if state == 'BLOCKED' else 'The task needs attention'
        explanation = 'The following items affect the next step. Resolve them in the order shown.'
    actionable = [a for a in data.get('approvals', []) if a.get('state') == 'PENDING'
                  and (not a.get('expires_at') or _future(a['expires_at'], now))]
    expired = sum(a.get('state') == 'EXPIRED' or (a.get('state') == 'PENDING' and
                  bool(a.get('expires_at')) and not _future(a['expires_at'], now))
                  for a in data.get('approvals', []))
    preview = data.get('preview') or {}
    preview_ready = not finished and preview.get('state') in {'READY', 'TESTING'} and _future(preview.get('expires_at'), now)
    latest = next(({'label': LABELS_EVENT[e['type']], 'at': e['occurred_at']}
                   for e in data.get('timeline', []) if e.get('type') in LABELS_EVENT), None)
    return {'title': title, 'explanation': explanation, 'blockers': blockers, 'checked_at': now.isoformat(),
            'context_current': consistent, 'can_retry': consistent and not exhausted and all(b['code'] == 'unknown' for b in blockers) and not running and
            state in {'BLOCKED', 'FAILED', 'CANCELLED'},
            'can_resume': consistent and not blockers and not running and state in {'PAUSED', 'PAUSED_BUDGET'},
            'needs_budget': exhausted and not finished, 'preview_ready': preview_ready,
            'pending_decisions': len(actionable), 'expired_decisions': expired, 'latest': latest,
            'waiting_details': pending}


def _future(value: str | None, now: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.tzinfo is not None and parsed > now
    except (AttributeError, ValueError):
        return False


LABELS_EVENT = {'TASK_CREATED': 'Task created', 'PLAN_VERSIONED': 'The work plan was updated',
                'SUBTASK_ACCEPTED': 'A review accepted the work', 'TEST_PASSED': 'Automated tests passed',
                'TEST_FAILED': 'Automated tests found an issue', 'REVIEW_PASSED': 'Review passed',
                'REVIEW_FAILED': 'A review requested corrections', 'READY_FOR_MERGE': 'The change is ready to apply',
                'TASK_COMPLETED': 'The task completed', 'BUDGET_RAISED': 'More allowance was authorized',
                'APPROVAL_DECIDED': 'A decision was recorded', 'BLOCKED': 'Work was put on hold'}
