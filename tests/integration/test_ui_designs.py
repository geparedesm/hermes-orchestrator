"""Visual approval is enforced below the model, survives revisions, and pins worker inputs."""
import base64
import json

import pytest

import test_orchestration as orch
from conftest import PLUGIN_TOKEN
from control_plane.executions import ExecutionRequest
from control_plane.auth import Principal
from control_plane.errors import Conflict, Forbidden

agents = orch.agents
repo = orch.repo
task = orch.task
pytestmark = [pytest.mark.integration, pytest.mark.orchestration]
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1sAAAAASUVORK5CYII=')
FILES = {'desktop.png': PNG, 'mobile.png': PNG, 'design.pen': b'opaque encrypted design',
         'design.md': b'# Proposal\nDesktop and mobile; colors and spacing.\n'}


def plan(services, agents, task, *, with_key=True):
    if with_key:
        orch.q(services, "UPDATE project_configs SET effective_config = jsonb_set(effective_config, '{secrets}', "
               "'[{\"name\":\"PEN_CLI_KEY\",\"environment\":\"local\",\"delivery\":\"file\"}]')")
    orch.step(services, agents, task,
              {'type': 'SET_REQUIREMENTS', 'text': 'Create a responsive frontend profile page'},
              {'type': 'SET_PLAN', 'subtasks': [orch.plan_item('page', files=['views/profile.xml'])]},
              {'type': 'REQUEST_EXECUTION', 'subtask': 'page'})
    return orch.q(services, 'SELECT * FROM ui_designs ORDER BY version DESC LIMIT 1')[0]


def finish_design(services, agents, task, files=None):
    [execution] = [e for e in orch.active(services, task, 'DEVELOPER') if e['spec'].get('purpose', {}).get('ui_design')]
    spec = agents.specs[str(execution['id'])]
    assert spec.get('workspace') is None
    assert spec['grant']['capabilities']['workspace'] == 'NONE'
    assert spec['env']['NODE_USE_ENV_PROXY'] == '1'
    extra = {'design/' + name: content for name, content in (files if files is not None else FILES).items()}
    result = {**orch.DONE, 'changed_files': [], 'commits': []}
    if spec['image'].startswith('claude'):
        final = {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'design',
                 'structured_output': result, 'usage': {}}
        agents.finish_agent(str(execution['id']), json.dumps(final).encode(), extra=extra)
    else:
        agents.finish_agent(str(execution['id']), (orch.FIXTURES / 'codex-success.jsonl').read_bytes(),
                            extra={**extra, 'ho/last_message.json': json.dumps(result).encode()})
    services.scheduler.run_once()
    return orch.q(services, 'SELECT * FROM ui_designs ORDER BY version DESC LIMIT 1')[0]


def receipt(api, task, design):
    for name in ('desktop.png', 'mobile.png'):
        response = api.post(f"/v1/tasks/{task}/ui-design/{design['id']}/delivered/{name}", {},
                            token=PLUGIN_TOKEN, principal='hermes:visual-delivery')
        assert response.status_code == 200, response.text


def test_missing_pen_credentials_blocks_before_any_workspace(api, services, agents, task):
    design = plan(services, agents, task, with_key=False)
    assert design['state'] == 'FAILED'
    assert orch.state(api, task) == 'BLOCKED'
    assert 'PEN_CLI_KEY' in design['feedback']
    assert not orch.q(services, 'SELECT id FROM workspaces')
    assert not orch.active(services, task, 'DEVELOPER')


def test_design_images_required_then_explicit_human_approval_releases_developer(api, services, agents, task):
    design = plan(services, agents, task)
    assert design['state'] == 'PREPARING'
    assert not orch.q(services, 'SELECT id FROM workspaces')
    design = finish_design(services, agents, task)
    assert design['state'] == 'PENDING'
    assert orch.state(api, task) == 'APPROVAL_REQUIRED'
    assert set(design['manifest']) == set(FILES)
    for name, expected in FILES.items():
        response = api.get(f"/v1/tasks/{task}/ui-design/{design['id']}/{name}")
        assert response.status_code == 200 and response.content == expected
    approval = str(design['approval_id'])
    assert api.post(f'/v1/approvals/{approval}/decision', {'decision': 'APPROVE'}).status_code == 409
    receipt(api, task, design)
    assert api.post(f'/v1/approvals/{approval}/decision', {'decision': 'APPROVE'},
                    token=PLUGIN_TOKEN, principal='hermes:agent').status_code == 403
    approved = api.post(f'/v1/approvals/{approval}/decision', {'decision': 'APPROVE'})
    assert approved.status_code == 200, approved.text
    assert approved.json()['state'] == 'CONSUMED'
    services.scheduler.run_once()
    orch.step(services, agents, task, {'type': 'REQUEST_EXECUTION', 'subtask': 'page'})
    [developer] = orch.active(services, task, 'DEVELOPER')
    refs = developer['spec']['attachments']
    assert {r['name'] for r in refs} == {'ui-' + n for n in FILES}
    assert developer['spec']['ui_design_id'] == str(design['id'])
    assert 'ui-desktop.png' in developer['spec']['assignment']['prompt']


def test_missing_export_never_requests_an_approval(api, services, agents, task):
    plan(services, agents, task)
    design = finish_design(services, agents, task, {k: v for k, v in FILES.items() if k != 'mobile.png'})
    assert design['state'] == 'FAILED' and orch.state(api, task) == 'BLOCKED'
    assert 'mobile.png' in design['feedback']
    assert not orch.q(services, "SELECT id FROM approvals WHERE action = 'UI_DESIGN'")


def test_rejection_generates_a_new_version_and_passes_human_feedback(api, services, agents, task):
    plan(services, agents, task)
    design = finish_design(services, agents, task)
    response = api.post(f"/v1/approvals/{design['approval_id']}/decision", {'decision': 'REJECT', 'note': 'Use a lighter header'})
    assert response.status_code == 200, response.text
    services.scheduler.run_once()
    orch.step(services, agents, task, {'type': 'REQUEST_UI_DESIGN'})
    newer = orch.q(services, 'SELECT * FROM ui_designs ORDER BY version DESC LIMIT 1')[0]
    assert newer['version'] == design['version'] + 1 and newer['state'] == 'PREPARING'
    [execution] = orch.active(services, task, 'DEVELOPER')
    assert 'Use a lighter header' in execution['spec']['assignment']['prompt']
    assert not orch.q(services, 'SELECT id FROM workspaces')


def test_requirements_revision_invalidates_visual_approval(api, services, agents, task):
    plan(services, agents, task)
    design = finish_design(services, agents, task)
    response = api.post(f'/v1/tasks/{task}/revise', {'text': 'Change the frontend to a blue theme'})
    assert response.status_code == 200, response.text
    assert orch.state(api, task) != 'APPROVAL_REQUIRED'
    assert api.post(f"/v1/approvals/{design['approval_id']}/decision", {'decision': 'APPROVE'}).status_code == 409
    with services.ctx.unit_of_work() as uow:
        current = services.tasks.get(uow, task)
        with pytest.raises(Conflict):
            services.executions.ui_designs.ensure_approved(uow, current)


def test_manual_execution_cannot_spoof_the_designer_exception(api, services, agents, task):
    plan(services, agents, task)
    with services.ctx.unit_of_work() as uow:
        req = ExecutionRequest(role='DEVELOPER', provider='claude', prompt='implement')
        with pytest.raises(Conflict, match='UI_DESIGN'):
            services.executions.request(uow, principal=Principal('host-cli', 'operator'), task_key=task, req=req)
        req.purpose = {'ui_design': '00000000-0000-0000-0000-000000000000'}
        with pytest.raises(Forbidden):
            services.executions.request(uow, principal=Principal('host-cli', 'operator'), task_key=task, req=req)


def test_image_receipts_are_not_operator_or_agent_approvals(api, services, agents, task):
    plan(services, agents, task)
    design = finish_design(services, agents, task)
    path = f"/v1/tasks/{task}/ui-design/{design['id']}"
    assert api.post(path + '/delivered/desktop.png', {}).status_code == 403
    assert api.post(path + '/reviewed', {}, token=PLUGIN_TOKEN, principal='hermes:agent').status_code == 403
    assert api.post(path + '/reviewed', {}).status_code == 200
    assert orch.q(services, 'SELECT state FROM ui_designs')[0]['state'] == 'PENDING'


def test_modified_export_cannot_be_approved(api, services, agents, task):
    plan(services, agents, task)
    design = finish_design(services, agents, task)
    receipt(api, task, design)
    artifact_id = design['manifest']['desktop.png']['artifact_id']
    path = orch.q(services, 'SELECT path FROM artifacts WHERE id = %s', artifact_id)[0]['path']
    services.ctx.artifacts._path(path).write_bytes(PNG + b'changed')
    response = api.post(f"/v1/approvals/{design['approval_id']}/decision", {'decision': 'APPROVE'})
    assert response.status_code == 409
    assert orch.q(services, 'SELECT state FROM ui_designs')[0]['state'] == 'PENDING'
    assert not orch.q(services, 'SELECT id FROM workspaces')


def test_new_plan_requires_a_new_visual_approval(api, services, agents, task):
    plan(services, agents, task)
    design = finish_design(services, agents, task)
    receipt(api, task, design)
    assert api.post(f"/v1/approvals/{design['approval_id']}/decision", {'decision': 'APPROVE'}).status_code == 200
    services.scheduler.run_once()
    orch.step(services, agents, task,
              {'type': 'SET_PLAN', 'subtasks': [orch.plan_item('page', title='Responsive updated profile', files=['views/profile.xml'])]},
              {'type': 'REQUEST_EXECUTION', 'subtask': 'page'})
    newer = orch.q(services, 'SELECT * FROM ui_designs ORDER BY version DESC LIMIT 1')[0]
    assert newer['state'] == 'PREPARING' and newer['version'] == design['version'] + 1
    [execution] = orch.active(services, task, 'DEVELOPER')
    assert execution['spec'].get('workspace') is None
    assert execution['spec']['design_package_id'] == str(design['id'])


def test_expired_visual_approval_renews_the_same_package(api, services, agents, task):
    plan(services, agents, task)
    design = finish_design(services, agents, task)
    receipt(api, task, design)
    orch.q(services, "UPDATE approvals SET state = 'EXPIRED' WHERE id = %s", design['approval_id'])
    response = api.post(f"/v1/approvals/{design['approval_id']}/renew", {})
    assert response.status_code == 201, response.text
    renewed = response.json()
    assert renewed['subject']['manifest_hash'] == services.executions.ui_designs.subject(design)['manifest_hash']
    assert api.post(f"/v1/approvals/{renewed['id']}/decision", {'decision': 'APPROVE'}).status_code == 200


def test_preexisting_queued_frontend_execution_is_checked_again_at_dispatch(api, services, agents, task):
    with services.ctx.unit_of_work() as uow:
        queued = services.executions.request(uow, principal=Principal('host-cli', 'operator'), task_key=task,
                                             req=ExecutionRequest(role='DEVELOPER', provider='claude', prompt='Implement the feature'))
        current = services.tasks.get(uow, task)
        services.orchestration._set_plan(uow, current, [orch.plan_item('page', files=['views/profile.xml'])])
        # A persisted execution from before the UI policy deployment has no design binding or UI flag.
        uow.cur.execute('UPDATE tasks SET ui_design_required = false WHERE id = %s', (current['id'],))
    row = orch.q(services, 'SELECT state, failure_class FROM executions WHERE id = %s', queued['id'])[0]
    assert row == {'state': 'CANCELLED', 'failure_class': 'POLICY'}
    assert str(queued['id']) not in agents.specs


def test_chat_approval_prompt_waits_for_both_images(api, services, agents, task):
    import httpx
    plan(services, agents, task)
    design = finish_design(services, agents, task)
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})

    services.outbox.url = 'https://hermes.invalid/webhooks/orchestration'
    services.outbox.client = httpx.Client(transport=httpx.MockTransport(handler))
    from control_plane.notifications import send_test
    with services.ctx.unit_of_work() as uow:
        send_test(uow, actor='host-cli:operator')
    services.outbox.deliver()
    assert any(p.get('event') == 'NOTIFICATION_TEST' for p in posted)
    assert not any(p.get('event') == 'APPROVAL_REQUIRED' and 'UI' in p.get('text', '') for p in posted)
    receipt(api, task, design)
    services.outbox.deliver()
    assert any(p.get('event') == 'APPROVAL_REQUIRED' and 'UI' in p.get('text', '') for p in posted)
