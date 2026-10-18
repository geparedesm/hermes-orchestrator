"""Delivery settings, model precedence and human-test approval invariants."""
import pytest
from conftest import PLUGIN_TOKEN
from control_plane.agents import default_model
from control_plane.delivery import PLAN_SECTIONS, validate_feature_plan
from control_plane.errors import Conflict
from control_plane.previews import Previews
import test_git as git_tests
from test_git import workspace, clone, commit, integrate_and_pass, pass_gate

agents = git_tests.agents
repo = git_tests.repo
task = git_tests.task

pytestmark = pytest.mark.integration


def test_role_models_are_human_only_and_do_not_change_other_roles(api, services):
    path = '/v1/agents/claude/models/ORCHESTRATOR'
    assert api.post(path, {'model': 'opus'}, token=PLUGIN_TOKEN, principal='agent:claude').status_code == 403
    assert api.post('/v1/agents/claude/model', {'model': 'sonnet'}).status_code == 200
    assert api.post(path, {'model': 'opus'}).status_code == 200
    with services.ctx.unit_of_work() as uow:
        assert default_model(uow, 'claude', 'ORCHESTRATOR') == 'opus'
        assert default_model(uow, 'claude', 'DEVELOPER') == 'sonnet'
    assert api.post(path, {'model': None}).status_code == 200
    with services.ctx.unit_of_work() as uow:
        assert default_model(uow, 'claude', 'ORCHESTRATOR') == 'sonnet'


def test_task_settings_are_persisted_and_models_require_an_approver(api, task):
    body = {'project': 'demo', 'request': 'Whole feature', 'delivery_mode': 'FEATURE', 'human_test_required': True,
            'model_overrides': {'claude': {'ORCHESTRATOR': 'opus'}}}
    denied = api.post('/v1/tasks', body, token=PLUGIN_TOKEN, principal='agent:claude')
    assert denied.status_code == 403
    response = api.post('/v1/tasks', body)
    assert response.status_code == 201, response.text
    view = response.json()
    assert view['delivery_mode'] == 'FEATURE' and view['human_test_required']
    assert view['model_overrides'] == body['model_overrides']


def candidate_setup(api, services, repo, agents, task):
    ws = workspace(api, task)
    commit(clone(repo, ws), 'feature', {'feature.py': "print('feature')\n"})
    integrate_and_pass(api, services, agents, task)
    pass_gate(api, services, agents, task)
    # Existing test tasks remain backward compatible; this task now requires an actual human-test candidate.
    with services.ctx.db.transaction() as cur:
        cur.execute('UPDATE tasks SET human_test_required = true WHERE key = %s', (task,))
    response = api.post('/v1/previews', {'environment': 'odoo', 'url': 'https://test.gp-developer.com/', 'tasks': [task]})
    assert response.status_code == 201, response.text
    return response.json()


def test_preview_is_stable_and_attestation_is_operator_only(api, services, repo, agents, task):
    candidate = candidate_setup(api, services, repo, agents, task)
    body = {'environment': 'odoo', 'url': candidate['url'], 'tasks': [task]}
    assert api.post('/v1/previews', body).status_code == 409
    assert api.post(f"/v1/previews/{candidate['id']}/ready", {'manifest_hash': candidate['manifest_hash']},
                    token=PLUGIN_TOKEN, principal='dashboard:operator').status_code == 403
    assert api.post(f"/v1/previews/{candidate['id']}/ready", {'manifest_hash': '0' * 64}).status_code == 409
    assert api.post(f"/v1/previews/{candidate['id']}/begin").status_code == 409


def test_merge_requires_current_preview_and_human_test_session(api, services, repo, agents, task):
    candidate = candidate_setup(api, services, repo, agents, task)
    assert api.post(f'/v1/tasks/{task}/git/merge-request').status_code == 409
    ready = api.post(f"/v1/previews/{candidate['id']}/ready", {'manifest_hash': candidate['manifest_hash']})
    assert ready.status_code == 200, ready.text
    approval = api.post(f'/v1/tasks/{task}/git/merge-request').json()
    assert approval['subject']['human_test']['manifest_hash'] == candidate['manifest_hash']
    # Inspecting a candidate is not accepting it. The final decision remains explicit.
    with services.ctx.unit_of_work() as uow:
        t = services.tasks.get(uow, task)
        changes = services.git._changes(uow, t)
        with pytest.raises(Conflict):
            Previews(services.ctx).binding(uow, t, changes, require_testing=True)
    assert api.post(f"/v1/previews/{candidate['id']}/begin").status_code == 200
    assert api.post(f"/v1/previews/{candidate['id']}/retire").status_code == 200
    assert api.post(f'/v1/tasks/{task}/git/merge-request').status_code == 409


def test_feature_plan_requires_one_owner_and_every_section():
    items = [{'kind': 'IMPLEMENT'}]
    document = '\n\n'.join('## ' + section + '\nConcrete implementation evidence.' for section in PLAN_SECTIONS)
    validate_feature_plan(items, document)
    with pytest.raises(ValueError):
        validate_feature_plan(items * 2, document)
    with pytest.raises(ValueError):
        validate_feature_plan(items, 'A long but unstructured explanation')


def test_tested_candidate_can_merge_only_after_explicit_decision(api, services, repo, agents, task):
    candidate = candidate_setup(api, services, repo, agents, task)
    assert api.post(f"/v1/previews/{candidate['id']}/ready", {'manifest_hash': candidate['manifest_hash']}).status_code == 200
    approval = api.post(f'/v1/tasks/{task}/git/merge-request').json()
    rejected = api.post(f"/v1/approvals/{approval['id']}/decision", {'decision': 'APPROVE'})
    assert rejected.status_code == 409
    assert api.get(f'/v1/tasks/{task}').json()['state'] == 'READY_FOR_MERGE'
    assert api.post(f"/v1/previews/{candidate['id']}/begin").status_code == 200
    # Opening the environment is not a merge decision.
    assert api.get(f'/v1/tasks/{task}').json()['state'] == 'READY_FOR_MERGE'
    approved = api.post(f"/v1/approvals/{approval['id']}/decision", {'decision': 'APPROVE'})
    assert approved.status_code == 200, approved.text
    assert api.get(f'/v1/tasks/{task}').json()['state'] == 'VERIFYING'


def test_complete_feature_plan_is_persisted_and_bound_to_approval(api, services, task):
    from control_plane.delivery import feature_context
    from control_plane.orchestration import Orchestration
    orchestrator = Orchestration(services.ctx, services.tasks, services.approvals, services.executions,
                                 services.git, services.verifications, services.reviews, services.gate)
    # Harness stands in for dispatch entering planning; all later transitions use the normal service.
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE tasks SET delivery_mode = 'FEATURE', state = 'PLANNING', approve_plan = true WHERE key = %s", (task,))
    document = '\n\n'.join('## ' + section + '\nConcrete plan for the complete feature.' for section in PLAN_SECTIONS)
    items = [{'key': 'feature', 'title': 'Complete feature', 'kind': 'IMPLEMENT', 'description': 'Deliver and test the full request',
              'files': ['feature.py'], 'depends_on': [], 'risk': 'LOW', 'preferred_provider': 'codex'}]
    with services.ctx.unit_of_work() as uow:
        row = services.tasks.get(uow, task)
        orchestrator._set_requirements(uow, row, 'The whole requested behavior', source='ORCHESTRATOR', reason='Planning')
        row = services.tasks.get(uow, task)
        orchestrator._set_plan(uow, row, items, document=document)
    with services.ctx.unit_of_work() as uow:
        row = services.tasks.get(uow, task)
        assert row['state'] == 'APPROVAL_REQUIRED'
        context = feature_context(services.ctx, uow, row, 'Original request')
        assert document in context and 'Original request' in context and 'The whole requested behavior' in context
        subject = orchestrator._plan_subject(uow, row, row['current_plan_version'])
        assert subject['plan_document_sha256']
        assert len(subject['subtasks']) == 1
    # A changed document with the same assignment is a new plan requiring another approval.
    with services.ctx.unit_of_work() as uow:
        row = services.tasks.get(uow, task)
        orchestrator._set_plan(uow, row, items, document=document + '\nAdditional verification scenario.')
        row = services.tasks.get(uow, task)
        assert row['current_plan_version'] == 2 and row['plan_approval_state'] == 'PENDING'


@pytest.mark.parametrize("task_override,project_override,expected", [(True, True, "task-model"),
                          (False, True, "project-model"), (False, False, "role-model")])
def test_dispatched_model_uses_task_then_project_then_role_default(api, services, agents, task,
                                                                 task_override, project_override, expected):
    from control_plane.db import jsonb
    from test_agents import run_agent
    assert api.post('/v1/agents/codex/models/DEVELOPER', {'model': 'role-model'}).status_code == 200
    with services.ctx.db.transaction() as cur:
        cur.execute("UPDATE project_configs SET effective_config = jsonb_set(effective_config, '{agents,models}', %s) "
                    "WHERE id = (SELECT config_id FROM tasks WHERE key = %s)",
                    (jsonb({'codex': {'DEVELOPER': 'project-model'}} if project_override else {}), task))
        cur.execute('UPDATE tasks SET model_overrides = %s WHERE key = %s',
                    (jsonb({'codex': {'DEVELOPER': 'task-model'}} if task_override else {}), task))
    response = run_agent(api, task)
    assert response.status_code == 201, response.text
    assert response.json()['assignment']['model'] == expected


def test_feedback_preserves_requirements_and_invalidates_the_merge(api, services, repo, agents, task):
    from control_plane.orchestration import Orchestration
    candidate = candidate_setup(api, services, repo, agents, task)
    services.orchestration = Orchestration(services.ctx, services.tasks, services.approvals, services.executions,
                                           services.git, services.verifications, services.reviews, services.gate)
    body = {'candidate_id': candidate['id'], 'text': 'The submit button must also work on mobile.'}
    assert api.post(f'/v1/tasks/{task}/preview-feedback', body, token=PLUGIN_TOKEN, principal='agent:codex').status_code == 403
    result = api.post(f'/v1/tasks/{task}/preview-feedback', body)
    assert result.status_code == 200, result.text
    with services.ctx.unit_of_work() as uow:
        row = services.tasks.get(uow, task)
        text = services.orchestration._requirements_text(uow, row)
        assert 'Add a feature' in text and body['text'] in text
        assert row['state'] == 'RUNNING'


def test_a_shared_candidate_requires_all_tasks_to_be_ready_for_the_same_version(api, services, repo, agents, task):
    # Use a prepared real candidate, then a second manifest member standing in for another repository.
    # The missing member must abort the entire group decision before the real task merges.
    from control_plane.db import jsonb
    candidate = candidate_setup(api, services, repo, agents, task)
    assert api.post(f"/v1/previews/{candidate['id']}/ready", {'manifest_hash': candidate['manifest_hash']}).status_code == 200
    approval = api.post(f'/v1/tasks/{task}/git/merge-request').json()
    assert api.post(f"/v1/previews/{candidate['id']}/begin").status_code == 200
    subject = approval['subject']
    subject['human_test']['tasks'] = [task, 'T-999999']
    # Harness simulates a stale/incomplete group; no valid state hash can bypass the all-members check.
    with services.ctx.db.transaction() as cur:
        cur.execute('UPDATE approvals SET subject = %s WHERE id = %s', (jsonb(subject), approval['id']))
    response = api.post(f"/v1/approvals/{approval['id']}/decision", {'decision': 'APPROVE'})
    assert response.status_code == 409, response.text
    assert api.get(f'/v1/tasks/{task}').json()['state'] == 'READY_FOR_MERGE'


def test_one_explicit_decision_authorizes_all_repositories_in_the_tested_group(api, services, repo, agents, task, projects_root):
    from conftest import git_repo
    # Real Git repositories, reviews and Quality Gates for both independently mergeable tasks.
    git_tests.ready_for_merge(api, services, repo, agents, task)
    second_repo = git_repo(projects_root / 'demo2', {'.hermes/project.yaml': git_tests.PROJECT_YAML,
                                                   'app.py': git_tests.APP, 'schema/api.txt': 'v1\n'})
    assert api.post('/v1/projects', {'path': 'demo2'}).status_code == 201
    scan = api.post('/v1/projects/demo2/scan').json()['approval']['id']
    assert api.post(f'/v1/approvals/{scan}/decision', {'decision': 'APPROVE'}).status_code == 200
    second = api.post('/v1/tasks', {'project': 'demo2', 'request': 'Add the other feature'}).json()['key']
    services.scheduler.run_once()
    git_tests.ready_for_merge(api, services, second_repo, agents, second)
    with services.ctx.db.transaction() as cur:
        cur.execute('UPDATE tasks SET human_test_required = true WHERE key = ANY(%s)', ([task, second],))
    prepared = api.post('/v1/previews', {'environment': 'odoo', 'url': 'https://test.gp-developer.com/', 'tasks': [task, second]})
    assert prepared.status_code == 201, prepared.text
    candidate = prepared.json()
    assert api.post(f"/v1/previews/{candidate['id']}/ready", {'manifest_hash': candidate['manifest_hash']}).status_code == 200
    assert api.post(f"/v1/previews/{candidate['id']}/begin").status_code == 200
    approval = api.post(f'/v1/tasks/{task}/git/merge-request').json()
    response = api.post(f"/v1/approvals/{approval['id']}/decision", {'decision': 'APPROVE'})
    assert response.status_code == 200, response.text
    assert api.get(f'/v1/tasks/{task}').json()['state'] == 'VERIFYING'
    assert api.get(f'/v1/tasks/{second}').json()['state'] == 'VERIFYING'


def test_platform_human_testing_cannot_be_disabled_by_a_task_request(api, services, task):
    services.ctx.platform['project_defaults']['delivery'] = {'mode': 'FEATURE', 'human_test_required': True}
    result = api.post('/v1/tasks', {'project': 'demo', 'request': 'Another feature', 'human_test_required': False})
    assert result.status_code == 201, result.text
    assert result.json()['human_test_required'] is True
    assert result.json()['delivery_mode'] == 'FEATURE'
