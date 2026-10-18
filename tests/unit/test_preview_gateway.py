"""The public preview must authenticate every application request and isolate its cookies."""
import httpx
from fastapi.testclient import TestClient
from control_plane.preview_gateway import GatewayConfig, create_gateway


def client_for(user='operator', available=True):
    calls = []
    def upstream(request):
        calls.append(request)
        if request.url.host == 'hermes':
            if request.url.path == '/api/auth/me':
                return httpx.Response(200 if available else 401, json={'user_id': user})
            return httpx.Response(200, text='Login', headers={'set-cookie': '__Host-hermes_session_at=token; Secure; HttpOnly; Path=/'})
        return httpx.Response(200, text='Test application', headers={'set-cookie': 'session_id=odoo-session; Path=/'})
    config = GatewayConfig('http://hermes:9119', 'http://odoo:8069', 'https://test.gp-developer.com', 'operator')
    return TestClient(create_gateway(config, transport=httpx.MockTransport(upstream)), base_url=config.public_origin), calls


def test_unauthenticated_browser_cannot_reach_application():
    client, calls = client_for(available=False)
    r = client.get('/web', follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'].startswith('/login')
    assert all(r.url.host == 'hermes' for r in calls)


def test_other_accounts_are_refused():
    client, calls = client_for(user='someone-else')
    assert client.get('/web').status_code == 403
    assert all(r.url.host == 'hermes' for r in calls)


def test_authentication_cookies_and_identity_headers_never_reach_application():
    client, calls = client_for()
    client.cookies.set('__Host-hermes_session_at', 'secret')
    client.cookies.set('session_id', 'odoo-session')
    r = client.get('/web', headers={'X-HO-Principal': 'host-cli:operator', 'Authorization': 'Bearer secret'})
    assert r.status_code == 200
    application = calls[-1]
    assert application.url.host == 'odoo'
    assert 'secret' not in str(application.headers)
    assert application.headers['cookie'] == 'session_id=odoo-session'
    assert 'x-ho-principal' not in application.headers
    assert r.headers['cache-control'] == 'no-store'


def test_cross_origin_changes_and_database_manager_are_refused():
    client, _ = client_for()
    assert client.post('/web/dataset/call_kw', headers={'origin': 'https://other.example'}).status_code == 403
    assert client.get('/web/database/manager').status_code == 403


def test_login_uses_existing_hermes_authentication():
    client, calls = client_for(available=False)
    response = client.get('/login')
    assert response.status_code == 200 and response.text == 'Login'
    assert calls[-1].url.host == 'hermes'
    assert '__Host-hermes_session_at=' in response.headers['set-cookie']


def test_verified_operator_gets_a_separate_preview_session_without_a_second_password_prompt(tmp_path):
    password = tmp_path / 'preview-password'
    password.write_text('preview-only-password')
    version = tmp_path / 'version.json'
    version.write_text('{"label":"Preview version 3 · T-12"}')
    requests = []
    def upstream(request):
        requests.append(request)
        if request.url.host == 'hermes':
            return httpx.Response(200, json={'user_id': 'operator'})
        if request.url.path == '/web/session/authenticate':
            return httpx.Response(200, json={'result': {'uid': 2}}, headers={'set-cookie': 'session_id=preview-session; Path=/'})
        assert request.headers['cookie'] == 'session_id=preview-session'
        return httpx.Response(200, text='<html><body>Application</body></html>', headers={'content-type': 'text/html'})
    config = GatewayConfig('http://hermes:9119', 'http://odoo:8069', 'https://test.gp-developer.com', 'operator',
                           odoo_password_file=str(password), version_file=str(version), dashboard_url='https://hermes.example/orchestration')
    client = TestClient(create_gateway(config, transport=httpx.MockTransport(upstream)), base_url=config.public_origin)
    result = client.get('/web')
    assert result.status_code == 200 and 'Preview version 3' in result.text
    assert 'preview-only-password' not in result.text
    assert 'Secure' in result.headers['set-cookie'] and 'HttpOnly' in result.headers['set-cookie']
    assert result.headers['content-length'] == str(len(result.content))
    assert len([r for r in requests if r.url.path == '/web/session/authenticate']) == 1


def test_opening_the_whatsapp_link_starts_testing_but_never_approves(tmp_path):
    version = tmp_path / 'version.json'
    candidate = '01234567-1234-1234-1234-0123456789ab'
    version.write_text('{"candidate_id":"' + candidate + '","label":"Version 1"}')
    token = tmp_path / 'plugin-token'
    token.write_text('configured-plugin-token')
    calls = []
    def upstream(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == 'hermes':
            return httpx.Response(200, json={'user_id': 'operator'})
        if request.url.host == 'control-plane':
            assert request.headers['authorization'] == 'Bearer configured-plugin-token'
            assert request.headers['x-ho-principal'] == 'dashboard:operator'
            return httpx.Response(200, json={'state': 'TESTING'})
        assert 'configured-plugin-token' not in str(request.headers)
        return httpx.Response(200, text='Application')
    config = GatewayConfig('http://hermes:9119', 'http://odoo:8069', 'https://test.gp-developer.com', 'operator',
                           version_file=str(version), api_url='http://control-plane:8080', plugin_token_file=str(token))
    client = TestClient(create_gateway(config, transport=httpx.MockTransport(upstream)), base_url=config.public_origin)
    assert client.get('/web').status_code == 200
    assert ('control-plane', '/v1/previews/' + candidate + '/begin') in calls
    assert not any('decision' in path for _, path in calls)


def test_websocket_requires_the_same_verified_operator_session():
    import pytest
    from starlette.websockets import WebSocketDisconnect
    def upstream(request):
        return httpx.Response(401, json={'error': 'unauthenticated'})
    config = GatewayConfig('http://hermes:9119', 'http://odoo:8069', 'https://test.gp-developer.com', 'operator',
                           websocket_url='ws://odoo:8072/websocket')
    client = TestClient(create_gateway(config, transport=httpx.MockTransport(upstream)), base_url=config.public_origin)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect('/websocket', headers={'origin': config.public_origin}):
            pass
    assert closed.value.code == 1008


def test_preview_cannot_mint_native_hermes_tokens_or_websocket_tickets():
    client, calls = client_for()
    for path in ('/auth/native/token', '/auth/native/refresh', '/api/auth/ws-ticket'):
        assert client.post(path, json={}, headers={'Origin': 'https://test.gp-developer.com'}).status_code == 403
    assert not calls
