"""Authenticated reverse proxy for a human-test application.

No provider or project credentials are exposed. The existing Hermes login verifies the operator;
application requests never receive the Hermes authentication cookies. Not enabled by default.
"""
from __future__ import annotations
from dataclasses import dataclass
import asyncio
import posixpath
import html
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response
from websockets.asyncio.client import connect as connect_websocket
from websockets.exceptions import ConnectionClosed, InvalidHandshake

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
               'transfer-encoding', 'upgrade', 'host', 'content-length'}
AUTH_COOKIES = ('hermes_session_', 'hermes_sso_', '__Host-hermes_', '__Secure-hermes_')


def auth_cookie(name: str) -> bool:
    return name.startswith(AUTH_COOKIES)


@dataclass(frozen=True)
class GatewayConfig:
    hermes_url: str
    application_url: str
    public_origin: str
    operator_id: str
    odoo_password_file: str | None = None
    version_file: str | None = None
    dashboard_url: str = "/"
    api_url: str | None = None
    plugin_token_file: str | None = None
    websocket_url: str | None = None

    def __post_init__(self):
        origin = urlsplit(self.public_origin)
        if origin.scheme != 'https' or not origin.hostname or origin.path not in ('', '/') or origin.query or origin.fragment:
            raise ValueError('public_origin must be an HTTPS origin')
        if not self.operator_id:
            raise ValueError('an explicit operator identity is required')
        for url in (self.hermes_url, self.application_url):
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('invalid configured upstream')


def create_gateway(config: GatewayConfig, *, transport: Any = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request: Request, exc: httpx.HTTPError) -> Response:
        return JSONResponse({'error': 'upstream unavailable'}, status_code=503)

    @app.websocket('/websocket')
    async def socket_proxy(socket: WebSocket):
        if (not config.websocket_url or socket.headers.get('host') != urlsplit(config.public_origin).netloc
                or socket.headers.get('origin') != config.public_origin):
            await socket.close(code=1008)
            return
        auth_headers = {'cookie': '; '.join(f'{k}={v}' for k, v in socket.cookies.items() if auth_cookie(k)),
                        'host': urlsplit(config.public_origin).netloc, 'x-forwarded-proto': 'https'}
        try:
            async with httpx.AsyncClient(transport=transport, timeout=10, follow_redirects=False) as client:
                identity = await client.get(config.hermes_url.rstrip('/') + '/api/auth/me', headers=auth_headers)
                if identity.status_code != 200 or identity.json().get('user_id') != config.operator_id:
                    await socket.close(code=1008)
                    return
            cookies = '; '.join(f'{k}={v}' for k, v in socket.cookies.items() if not auth_cookie(k))
            target = config.websocket_url + ('?' + socket.url.query if socket.url.query else '')
            async with connect_websocket(target, origin=config.public_origin, proxy=None,
                additional_headers={'Cookie': cookies, 'X-Forwarded-Proto': 'https',
                                    'X-Forwarded-Host': urlsplit(config.public_origin).netloc}, max_size=4 * 1024 * 1024) as upstream:
                await socket.accept()
                async def from_browser():
                    while True:
                        message = await socket.receive()
                        if message['type'] == 'websocket.disconnect':
                            break
                        if message.get('text') is not None:
                            await upstream.send(message['text'])
                        elif message.get('bytes') is not None:
                            await upstream.send(message['bytes'])
                async def from_application():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await socket.send_bytes(message)
                        else:
                            await socket.send_text(message)
                tasks = [asyncio.create_task(from_browser()), asyncio.create_task(from_application())]
                try:
                    await asyncio.wait(tasks, timeout=60, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    try:
                        await socket.close(code=1000)
                    except RuntimeError:
                        pass
        except (httpx.HTTPError, ValueError, OSError, InvalidHandshake, ConnectionClosed, WebSocketDisconnect):
            try:
                await socket.close(code=1011)
            except RuntimeError:
                pass

    @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    async def proxy(request: Request, path: str) -> Response:
        if request.headers.get('host') != urlsplit(config.public_origin).netloc:
            return JSONResponse({'error': 'invalid host'}, status_code=400)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('origin') != config.public_origin:
            return JSONResponse({'error': 'cross-origin request refused'}, status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 32 * 1024 * 1024:
                return JSONResponse({'error': 'request too large'}, status_code=413)
        path = posixpath.normpath('/' + path).lstrip('/')
        allowed_auth_paths = {'login', 'api/auth/providers', 'api/auth/me', 'auth/password-login', 'auth/logout'}
        auth_path = path in allowed_auth_paths or path.startswith('assets/') or path.startswith('static/')
        if (path.startswith('auth/') or path.startswith('api/auth/')) and not auth_path:
            return JSONResponse({'error': 'authentication route unavailable in previews'}, status_code=403)
        # A fresh client per request prevents cookies from being shared between browser sessions.
        async with httpx.AsyncClient(transport=transport, timeout=60, follow_redirects=False) as client:
            headers = {'host': urlsplit(config.public_origin).netloc, 'x-forwarded-proto': 'https',
                       'x-forwarded-host': urlsplit(config.public_origin).netloc}
            headers['cookie'] = '; '.join(f'{k}={v}' for k, v in request.cookies.items() if auth_cookie(k))
            if auth_path:
                if request.headers.get('content-type'):
                    headers['content-type'] = request.headers['content-type']
                if request.headers.get('origin'):
                    headers['origin'] = config.public_origin
                upstream = await client.request(request.method, config.hermes_url.rstrip('/') + '/' + path,
                                                params=request.query_params, content=bytes(body), headers=headers)
                return response(upstream, allow_auth_cookies=True)
            try:
                identity = await client.get(config.hermes_url.rstrip('/') + '/api/auth/me', headers=headers)
            except httpx.HTTPError:
                return JSONResponse({'error': 'authentication unavailable'}, status_code=503)
            if identity.status_code != 200:
                if identity.status_code >= 500:
                    return JSONResponse({'error': 'authentication unavailable'}, status_code=503)
                return RedirectResponse('/login?next=%2F', status_code=303)
            try:
                authorized = identity.json().get('user_id') == config.operator_id
            except (ValueError, AttributeError):
                authorized = False
            if not authorized:
                return JSONResponse({'error': 'operator access required'}, status_code=403)
            # A normal navigation from WhatsApp also starts the human-test session, after authentication.
            if (config.api_url and config.plugin_token_file and config.version_file and request.method == 'GET'
                    and ('text/html' in request.headers.get('accept', '') or path in ('', 'web', 'odoo', 'web/login'))):
                try:
                    metadata = json.loads(Path(config.version_file).read_text())
                except (OSError, ValueError):
                    return JSONResponse({'error': 'preview version unavailable'}, status_code=503)
                candidate_id = str(metadata.get('candidate_id') or '')
                if candidate_id:
                    if not re.fullmatch(r'[0-9a-f-]{36}', candidate_id):
                        return JSONResponse({'error': 'invalid preview version'}, status_code=503)
                    token = Path(config.plugin_token_file).read_text().strip()
                    started = await client.post(config.api_url.rstrip('/') + '/v1/previews/' + candidate_id + '/begin',
                        json={}, headers={'Authorization': 'Bearer ' + token, 'X-HO-Principal': 'dashboard:operator'})
                    if started.status_code != 200:
                        return JSONResponse({'error': 'this test version is unavailable; return to Hermes'}, status_code=503)
            if path.startswith('web/database'):
                return JSONResponse({'error': 'database management is unavailable in previews'}, status_code=403)
            outgoing = {k: v for k, v in request.headers.items()
                        if k.lower() not in HOP_HEADERS | {'authorization', 'cookie'} and not k.lower().startswith('x-')}
            outgoing.update({'host': urlsplit(config.public_origin).netloc, 'x-forwarded-proto': 'https'})
            outgoing['cookie'] = '; '.join(f'{k}={v}' for k, v in request.cookies.items() if not auth_cookie(k))
            odoo_login = None
            if config.odoo_password_file and ('session_id' not in request.cookies or path == 'web/login'):
                odoo_login = await client.post(config.application_url.rstrip('/') + '/web/session/authenticate',
                    json={'jsonrpc': '2.0', 'method': 'call', 'params': {'db': 'preview', 'login': 'admin',
                          'password': Path(config.odoo_password_file).read_text().strip()}}, headers={
                          'host': urlsplit(config.public_origin).netloc, 'x-forwarded-proto': 'https'})
                if odoo_login.status_code != 200 or not (odoo_login.json().get('result') or {}).get('uid'):
                    return JSONResponse({'error': 'preview session unavailable'}, status_code=503)
                session = odoo_login.cookies.get('session_id')
                if not session:
                    return JSONResponse({'error': 'preview session unavailable'}, status_code=503)
                outgoing['cookie'] = 'session_id=' + session
                if path == 'web/login':
                    result = RedirectResponse('/odoo', status_code=303)
                    result.set_cookie('session_id', session, secure=True, httponly=True, samesite='lax')
                    return result

            try:
                upstream = await client.request(request.method, config.application_url.rstrip('/') + '/' + path,
                                                params=request.query_params, content=bytes(body), headers=outgoing)
            except httpx.HTTPError:
                return JSONResponse({'error': 'test environment unavailable'}, status_code=503)
            result = response(upstream, allow_auth_cookies=False)
            if odoo_login is not None:
                result.set_cookie('session_id', odoo_login.cookies['session_id'], secure=True, httponly=True, samesite='lax')
            if config.version_file and upstream.headers.get('content-type', '').startswith('text/html'):
                try:
                    metadata = json.loads(Path(config.version_file).read_text())
                    label = html.escape(str(metadata.get('label') or 'Preview'))
                except (OSError, ValueError):
                    label = 'Preview · version information unavailable'
                marker = ('<div data-hermes-preview="true" role="status" style="position:fixed;bottom:0;left:0;right:0;'
                          'z-index:2147483647;background:#161d2b;color:white;padding:6px 12px;font:13px sans-serif">'
                          + label + ' · <a style="color:#dbeafe" href="' + html.escape(config.dashboard_url, quote=True)
                          + '">Return to Hermes</a></div>').encode()
                result.body = re.sub(rb'(<body\b[^>]*>)', lambda m: m.group(1) + marker, result.body, count=1, flags=re.I)
                result.headers['content-length'] = str(len(result.body))

            # Hermes may refresh its session during verification. Return only its authentication cookies.
            for cookie in identity.headers.get_list('set-cookie'):
                if auth_cookie(cookie.split('=', 1)[0]):
                    result.headers.append('set-cookie', cookie)
            return result

    return app


def response(upstream: httpx.Response, *, allow_auth_cookies: bool) -> Response:
    result = Response(upstream.content, status_code=upstream.status_code,
                      headers={k: v for k, v in upstream.headers.items()
                               if k.lower() not in HOP_HEADERS | {'set-cookie', 'content-encoding'}})
    result.headers['cache-control'] = 'no-store'
    result.headers['x-content-type-options'] = 'nosniff'
    result.headers['referrer-policy'] = 'same-origin'
    for cookie in upstream.headers.get_list('set-cookie'):
        if auth_cookie(cookie.split('=', 1)[0]) == allow_auth_cookies:
            result.headers.append('set-cookie', cookie)
    return result


def gateway_from_env() -> FastAPI:
    """Uvicorn factory; missing operator identity fails closed at startup."""
    import os
    return create_gateway(GatewayConfig(
        hermes_url=os.environ['HO_PREVIEW_HERMES_URL'],
        application_url=os.environ['HO_PREVIEW_APPLICATION_URL'],
        public_origin=os.environ['HO_PREVIEW_PUBLIC_ORIGIN'],
        operator_id=os.environ['HO_PREVIEW_OPERATOR_ID'],
        odoo_password_file=os.environ.get('HO_PREVIEW_ODOO_PASSWORD_FILE'),
        version_file=os.environ.get('HO_PREVIEW_VERSION_FILE'),
        dashboard_url=os.environ.get('HO_PREVIEW_DASHBOARD_URL', '/'),
        api_url=os.environ.get('HO_PREVIEW_API_URL'),
        plugin_token_file=os.environ.get('HO_PREVIEW_PLUGIN_TOKEN_FILE'),
        websocket_url=os.environ.get('HO_PREVIEW_WEBSOCKET_URL'),
    ))
