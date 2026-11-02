"""Render the real plugin with isolated API fixtures and the platform's React version.

Usage (Browser Runner): dashboard_simple_check.py SOURCE_ROOT OUTPUT_DIR REACT_BOOTSTRAP
Every request is intercepted; this test never authenticates to or mutates the live platform.
"""
import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

source, out, react_bootstrap = sys.argv[1:]
base = "http://hermes-preview.local"
source, out = Path(source), Path(out)
out.mkdir(parents=True, exist_ok=True)
plugin = source / 'hermes/plugins/orchestration/dashboard'
spec = importlib.util.spec_from_file_location('presentation', plugin / 'presentation.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
task = {'key': 'T-13', 'title': 'WhatsApp payment reminders', 'project': 'commercial-properties',
        'state': 'BLOCKED', 'resume_state': 'RUNNING', 'state_reason': 'Only severity counts were provided.',
        'human_test_required': True, 'ui_design_required': True, 'priority': 'NORMAL',
        'created_at': '2026-10-30T02:03:50Z', 'updated_at': '2026-10-30T20:14:18Z',
        'running': 0, 'subtasks': 1, 'subtasks_done': 0}
budget = {'profile': 'NORMAL', 'state': 'EXHAUSTED', 'limits': {'agent_launches': 40, 'retries': 6},
          'consumed': {'agent_launches': 40, 'retries': 4}, 'reserved': {}}
subtask = {'key': 'T-13-1', 'local_key': 'payment-reminders', 'title': task['title'], 'kind': 'IMPLEMENT',
           'state': 'BLOCKED', 'state_reason': 'The review findings are absent from the workspace.',
           'risk': 'HIGH', 'developer_provider': 'codex', 'attempts': 1, 'review_cycles': 0}
detail = {'task': task, 'budget': budget, 'dag': {'subtasks': [subtask], 'edges': [], 'plan_version': 1},
          'approvals': [], 'clarifications': [], 'executions': [], 'timeline': [], 'reviews': [], 'tests': [],
          'git': None, 'preview': None, 'quality_gate': None, 'feature_plan': '', 'attachments': [], 'manifests': [], 'checkpoints': []}
context = {'state': 'BLOCKED', 'budget': budget, 'pending_launches': []}
detail['guidance'] = m.guidance(detail, context)
summary = {'required_actions': {'approvals': [], 'clarifications': [], 'waiting_tasks': [task]},
           'recent_activity': [], 'usage': {}, 'running': [], 'queue': [], 'health': {}}
seen, errors, mutation_attempts, staged = {}, [], [], []
flags = {"fail_detail": False, "fail_mobile": False, "design": False}
simulated_posts = []
design_id = "01a16f5d-0727-7964-b953-c9f68e03b8c7"
with sync_playwright() as p:
    browser = p.chromium.launch(args=['--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1400, 'height': 1100})
    page.on('pageerror', lambda e: errors.append(str(e)))

    def route(request_route):
        request = request_route.request
        path = urlparse(request.url).path
        if path == '/react-preview.js':
            return request_route.fulfill(path=react_bootstrap, content_type='application/javascript')
        if path == '/orchestration':
            return request_route.fulfill(content_type='text/html', body="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>:root{--color-background:#08081c;--color-foreground:#ddd6ff;--color-muted-foreground:#b6b2cf;--color-primary:#c4b5fd;--color-primary-foreground:#100d24;--color-card:#0d0d28;--color-border:#30304e;--color-popover:#08081c;}body{margin:0;background:var(--color-background);font:15px Arial,sans-serif;color:var(--color-foreground);}button,a,input,select,textarea{font:inherit;}</style>
<link rel="stylesheet" href="/assets/orchestration/style.css"></head><body><div id="root"></div>
<script src="/react-preview.js"></script><script src="/assets/orchestration/index.js"></script></body></html>""")
        if 'orchestration' not in path:
            return request_route.fulfill(status=404, body='No external requests in isolated preview')
        if path.endswith('.js'):
            staged.append('js')
            return request_route.fulfill(path=str(plugin / 'dist/index.js'), content_type='application/javascript')
        if path.endswith('.css'):
            staged.append('css')
            return request_route.fulfill(path=str(plugin / 'dist/style.css'), content_type='text/css')
        prefix = '/api/plugins/orchestration'
        if not path.startswith(prefix):
            return request_route.fulfill(status=404)
        tail = path[len(prefix):]
        if flags['design'] and tail == '/tasks/T-13/ui-design':
            return request_route.fulfill(json={'design': {'id': design_id, 'approval_id': 'test-design-approval',
                'version': 1, 'state': 'PENDING', 'current': True, 'summary': 'Review the desktop and mobile screens.',
                'delivered_images': []}})
        if flags['design'] and tail.startswith(f'/tasks/T-13/ui-design/{design_id}/'):
            name = tail.rsplit('/', 1)[-1]
            if request.method == 'POST' and '/delivered/' in tail:
                simulated_posts.append(tail)
                return request_route.fulfill(json={'delivered': name})
            if name in {'desktop.png', 'mobile.png'}:
                if name == 'mobile.png' and flags['fail_mobile']:
                    return request_route.fulfill(status=503, body='Image unavailable')
                return request_route.fulfill(path=str(source / 'ui/assets/sample-ui-design-desktop.png'), content_type='image/png')
        if flags['design'] and tail == '/approvals/test-design-approval' and request.method == 'POST':
            simulated_posts.append(tail)
            detail['approvals'] = []
            summary['required_actions']['approvals'] = []
            return request_route.fulfill(json={'state': 'CONSUMED'})
        if request.method != 'GET':
            mutation_attempts.append(tail)
            return request_route.fulfill(status=403, json={'detail': 'Browser checks never mutate the platform.'})
        if tail == '/tasks/T-13' and flags['fail_detail']:
            return request_route.fulfill(status=503, json={'detail': 'Test status fetch failed'})
        values = {'/summary': summary, '/board': {'tasks': [task]}, '/tasks/T-13': detail,
                  '/agents': {'agents': []}, '/approvals': {'approvals': []}}
        return request_route.fulfill(json=values.get(tail, {}))

    page.route('**/*', route)
    page.clock.install()
    page.goto(base + '/orchestration', wait_until='networkidle')
    page.get_by_role('heading', name='Needs a decision', exact=True).wait_for(timeout=30000)
    assert {'js', 'css'} <= set(staged), f'Staged assets not loaded: {staged}'
    assert page.get_by_role('button', name='System', exact=True).count() == 0
    page.get_by_role('button', name='Tasks', exact=True).click()
    page.get_by_role('button', name='T-13 WhatsApp payment reminders', exact=False).click()
    dialog = page.get_by_role('dialog', name='T-13', exact=True)
    dialog.get_by_role('heading', name='Work is on hold', exact=True).wait_for()
    assert dialog.get_by_text('The agent needs the review details', exact=True).is_visible()
    assert dialog.get_by_text('The approved allowance is used up', exact=True).is_visible()
    assert dialog.get_by_role('button', name='Retry', exact=True).count() == 0
    assert dialog.get_by_text('Test this version', exact=True).count() == 0
    assert dialog.get_by_text('Usage units', exact=True).count() == 0
    page.screenshot(path=str(out / 'simple-desktop.png'), full_page=True, animations="disabled")
    dialog.get_by_role('button', name='Advanced', exact=True).click()
    assert dialog.get_by_text('Only severity counts were provided.', exact=True).is_visible()
    assert dialog.get_by_role('button', name='Retry', exact=True).count() == 0
    page.screenshot(path=str(out / 'advanced-desktop.png'), full_page=True, animations="disabled")
    dialog.get_by_role('button', name='Simple', exact=True).click()
    page.keyboard.press('Tab')
    page.keyboard.press('Shift+Tab')
    assert dialog.evaluate('(el) => el.contains(document.activeElement)')
    flags['fail_detail'] = True
    page.clock.fast_forward(11000)
    dialog.get_by_role('alert').wait_for()
    assert dialog.get_by_role('button', name='Read request and requirements', exact=True).is_disabled()
    flags['fail_detail'] = False
    dialog.get_by_role('button', name='Refresh status', exact=True).click()
    dialog.get_by_role('alert').wait_for(state='detached')
    seen['stale_controls_disabled'] = True
    for width in [390, 320, 768]:
        page.set_viewport_size({'width': width, 'height': 1000})
        assert dialog.is_visible()
        overflow = dialog.evaluate('(el) => el.scrollWidth > el.clientWidth + 1')
        assert not overflow, f'Dialog overflows at {width}px'
        page.screenshot(path=str(out / f'simple-{width}.png'), full_page=True, animations="disabled")
    page.keyboard.press('Escape')
    assert dialog.count() == 0
    page.get_by_role('button', name='Advanced', exact=True).click()
    page.reload(wait_until='networkidle')
    page.get_by_role('button', name='System', exact=True).wait_for()
    seen['saved_preference'] = True
    seen['focus_remained_in_dialog'] = True
    # Failed image delivery must keep the current design unapprovable.
    page.get_by_role('button', name='Simple', exact=True).click()
    flags.update({'design': True, 'fail_mobile': True})
    approval = {'id': 'test-design-approval', 'number': 1, 'action': 'UI_DESIGN', 'task': 'T-13',
                'state': 'PENDING', 'summary': 'Review the proposed UI', 'subject': {'kind': 'ui_design'}, 'risk': 'MEDIUM'}
    detail['approvals'] = [approval]
    summary['required_actions']['approvals'] = [approval]
    page.reload(wait_until='networkidle')
    approve = page.get_by_role('button', name='Approve design', exact=True)
    page.get_by_text('Image unavailable (503)', exact=True).wait_for()
    assert approve.is_disabled()
    flags['fail_mobile'] = False
    page.reload(wait_until='networkidle')
    approve.wait_for()
    from playwright.sync_api import expect
    expect(approve).to_be_enabled()
    approve.click()
    try:
        page.get_by_text('Your approval was recorded.', exact=True).wait_for(timeout=5000)
    except Exception as exc:
        raise AssertionError({'errors': errors, 'posts': simulated_posts,
                              'unexpected_posts': mutation_attempts, 'page': page.locator('body').inner_text()[-1600:]}) from exc
    assert detail['approvals'] == []
    assert any('/delivered/mobile.png' in x for x in simulated_posts)
    seen['design_image_prerequisites'] = True
    seen['decision_acknowledged'] = True
    seen['fixture_only_posts'] = len(simulated_posts)
    assert not errors, errors
    assert not mutation_attempts, mutation_attempts
    seen.update({'staged_assets': sorted(set(staged)), 'screenshots': 5, 'mobile_widths': [390, 320, 768],
                 'page_errors': errors, 'platform_mutations': mutation_attempts})
    browser.close()
print(json.dumps(seen))
