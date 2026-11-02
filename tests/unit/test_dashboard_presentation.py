"""The Simple view reports current facts without changing execution or approval state."""
import importlib.util
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / 'hermes/plugins/orchestration/dashboard/presentation.py'
spec = importlib.util.spec_from_file_location('dashboard_presentation_test', PATH)
presentation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(presentation)
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def sample(state='BLOCKED'):
    return {'task': {'key': 'T-13', 'state': state, 'state_reason': 'old budget exhausted reason'},
            'budget': {'state': 'OK', 'limits': {'agent_launches': 40}, 'consumed': {'agent_launches': 21}},
            'dag': {'subtasks': []}, 'executions': [], 'approvals': [], 'timeline': []}


def explain(data):
    return presentation.guidance(data, {'state': data['task']['state'], 'budget': data['budget']}, NOW)


def test_current_budget_wins_over_an_old_reason():
    g = explain(sample())
    assert not g['needs_budget']
    assert [b['code'] for b in g['blockers']] == ['unknown']
    assert 'budget' not in g['explanation']
    assert g['can_retry']  # Advanced lets an operator retry after checking the evidence.


def test_missing_review_and_budget_are_separate_ordered_blockers():
    d = sample()
    d['dag']['subtasks'] = [{'state': 'BLOCKED', 'state_reason': 'The review findings are absent from the workspace.'}]
    d['budget']['state'] = 'EXHAUSTED'
    d['budget']['consumed']['agent_launches'] = 40
    original = deepcopy(d)
    g = explain(d)
    assert [b['code'] for b in g['blockers']] == ['review_input', 'budget']
    assert '40 / 40' in g['blockers'][1]['detail']
    assert not g['can_retry'] and g['needs_budget']
    assert d == original


def test_resolved_subtask_reason_is_not_reported_as_current():
    d = sample('RUNNING')
    d['dag']['subtasks'] = [{'state': 'ACCEPTED', 'state_reason': 'Review findings are absent.'}]
    d['executions'] = [{'state': 'RUNNING'}]
    assert not explain(d)['blockers']


@pytest.mark.parametrize('state', ['DONE', 'CANCELLED'])
def test_finished_work_has_no_active_budget_blocker(state):
    d = sample(state)
    d['budget']['state'] = 'EXHAUSTED'
    g = explain(d)
    assert not g['blockers'] and not g['needs_budget'] and not g['can_retry']


def test_failed_work_cannot_retry_with_exhausted_allowance():
    d = sample('FAILED')
    d['budget']['state'] = 'EXHAUSTED'
    assert not explain(d)['can_retry']


def test_context_failure_and_concurrent_state_change_disable_retry():
    d = sample()
    for context in [None, {'state': 'RUNNING'}]:
        g = presentation.guidance(d, context, NOW)
        assert not g['context_current'] and not g['can_retry']
        assert 'context' in [b['code'] for b in g['blockers']]


def test_queue_is_presented_without_inventing_a_retry_time():
    d = sample('QUEUED')
    wait = {'kind': 'SUBTASK_REVIEW', 'reason': 'provider held', 'requested_at': NOW.isoformat()}
    g = presentation.guidance(d, {'state': 'QUEUED', 'budget': d['budget'], 'pending_launches': [wait]}, NOW)
    assert g['waiting_details'] == [wait]
    assert any(b['code'] == 'queue' for b in g['blockers'])
    assert 'retry_at' not in g


def test_inactive_running_state_does_not_claim_an_agent_is_working():
    g = explain(sample('RUNNING'))
    assert g['title'] == 'The task needs attention'
    assert [b['code'] for b in g['blockers']] == ['idle']


@pytest.mark.parametrize('expiry,ready', [(None, False), ('bad-date', False),
    ('2026-11-02T11:00:00+00:00', False), ('2026-11-03T12:00:00+00:00', True)])
def test_only_current_verified_preview_is_prominent(expiry, ready):
    d = sample('READY_FOR_MERGE')
    d['preview'] = {'state': 'READY', 'expires_at': expiry}
    assert explain(d)['preview_ready'] is ready


def test_expired_decisions_and_old_failures_do_not_become_current_actions():
    d = sample('READY_FOR_MERGE')
    d['approvals'] = [{'state': 'PENDING', 'expires_at': '2026-11-02T11:00:00+00:00'},
                      {'state': 'CONSUMED'}, {'state': 'PENDING', 'expires_at': '2026-11-03T12:00:00+00:00'}]
    d['executions'] = [{'state': 'FAILED', 'failure_reason': 'old missing image'}]
    g = explain(d)
    assert g['pending_decisions'] == 1 and not g['blockers']


def test_unknown_state_is_not_made_up():
    assert explain(sample('NEW_UNKNOWN_STATE'))['title'] == 'Status needs checking'


def test_resume_after_allowance_change_is_available():
    d = sample('PAUSED_BUDGET')
    assert explain(d)['can_resume']
    d['budget']['state'] = 'EXHAUSTED'
    assert not explain(d)['can_resume']


def test_dashboard_endpoint_uses_its_transactional_snapshot(monkeypatch):
    import sys
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    path = PATH.parent / 'plugin_api.py'
    route_spec = importlib.util.spec_from_file_location('dashboard_presented_routes', path)
    routes = importlib.util.module_from_spec(route_spec)
    sys.modules[route_spec.name] = routes
    route_spec.loader.exec_module(routes)
    calls = []

    class FakeApi:
        def __init__(self, principal):
            assert principal == 'dashboard:operator'

        def task_view(self, key):
            calls.append(key)
            d = sample()
            d['orchestration'] = {'state': 'BLOCKED', 'budget': d['budget']}
            return d

    monkeypatch.setattr(routes._client, 'TaskApi', FakeApi)
    app = FastAPI()
    app.include_router(routes.router)
    response = TestClient(app).get('/tasks/T-13')
    assert response.status_code == 200
    assert response.json()['guidance']['context_current']
    assert response.json()['task']['state_reason'] == 'old budget exhausted reason'
    assert calls == ['T-13']


def test_old_preview_on_finished_task_does_not_ask_for_testing():
    d = sample('DONE')
    d['preview'] = {'state': 'READY', 'expires_at': '2026-11-03T12:00:00+00:00'}
    assert not explain(d)['preview_ready']
