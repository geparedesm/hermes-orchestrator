"""Opt-in image delivery to one explicitly authorized destination.

Disabled by default. This helper cannot approve designs and never chooses a destination from task content.
Each proposal arrives as a message with the changes it makes, then the desktop and mobile images; a person can ask
for the current proposal again with `/orch design T-n`.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path

from .orch_client import TaskApi

log = logging.getLogger(__name__)
_tasks: dict[str, asyncio.Task] = {}
_resend: set[str] = set()  # task keys whose current proposal a person asked to see again
_summarized: set[str] = set()  # designs whose changes message went out, so a retried image does not repeat it
IMAGES = ('desktop.png', 'mobile.png')

_TEXT = {
    'es': {'title': '🎨 {key} · Diseño de UI v{version}: cambios propuestos',
           'none': 'El diseño no trae un resumen de cambios; revisa las dos imágenes.',
           'next': 'A continuación, las imágenes de escritorio y móvil.',
           'decide': 'Responde *si {number}* para aprobarlo o *no {number} <qué cambiar>* para pedir otra versión.',
           'caption': '{key} · UI v{version} · {label}', 'desktop': 'Escritorio', 'mobile': 'Móvil',
           'missing': 'No hay un diseño de UI pendiente de aprobación en {key}.'},
    'en': {'title': '🎨 {key} · UI design v{version}: proposed changes',
           'none': 'The design has no summary of changes; review both images.',
           'next': 'The desktop and mobile images follow.',
           'decide': 'Reply *yes {number}* to approve it or *no {number} <what to change>* to ask for another version.',
           'caption': '{key} · UI v{version} · {label}', 'desktop': 'Desktop', 'mobile': 'Mobile',
           'missing': 'There is no UI design waiting for approval in {key}.'},
}


def destination():
    """No inheritance from general notification/home-channel settings: activation is a separate explicit choice."""
    if os.environ.get('HO_UI_DESIGN_DELIVERY', '').lower() != 'true':
        return None
    platform = os.environ.get('HO_UI_DESIGN_DELIVER_PLATFORM', '').strip().lower()
    chat_id = os.environ.get('HO_UI_DESIGN_DELIVER_CHAT_ID', '').strip()
    if not platform or not chat_id:
        log.error('UI image delivery is disabled: configure its explicitly authorized platform and chat ID')
        return None
    return platform, chat_id


def request_resend(key: str) -> bool:
    """Queue a task's current proposal for the authorized destination; False when image delivery is not running."""
    if destination() is None or not _tasks:
        return False
    _resend.add(key.upper())
    return True


def changes_message(design: dict, lang: str = 'en') -> str:
    text = _TEXT.get(lang, _TEXT['en'])
    number = design.get('number') or '?'
    parts = [text['title'].format(key=design['key'], version=design['version']),
             (design.get('summary') or '').strip() or text['none'],
             text['next'] + '\n' + text['decide'].format(number=number)]
    return '\n\n'.join(parts)


def _check(result, what: str) -> None:
    if not result.success:
        raise RuntimeError(f'Hermes could not deliver the {what}: ' + str(result.error or 'unsupported delivery'))


async def _send_changes(adapter, chat_id: str, design: dict, lang: str) -> None:
    result = await adapter.send(chat_id=chat_id, content=changes_message(design, lang))
    _check(result, 'UI design changes')
    log.info('UI design %s v%s: changes sent (message %s)', design['key'], design['version'], getattr(result, 'message_id', None))


async def _send_image(api, adapter, chat_id: str, design: dict, name: str, lang: str) -> None:
    key, design_id = design['key'], design['id']
    content, _ = await asyncio.to_thread(api.call, 'GET', f'/v1/tasks/{key}/ui-design/{design_id}/{name}', raw=True)
    if len(content) > 10 * 1024 * 1024 or not content.startswith(b'\x89PNG\r\n\x1a\n') \
            or hashlib.sha256(content).hexdigest() != design['manifest'][name]['sha256']:
        raise ValueError('UI image differs from the recorded pen.dev export')
    text = _TEXT.get(lang, _TEXT['en'])
    caption = text['caption'].format(key=key, version=design['version'], label=text[name.removesuffix('.png')])
    with tempfile.TemporaryDirectory(prefix='ho-ui-') as folder:
        image = Path(folder) / name
        image.write_bytes(content)
        result = await adapter.send_image_file(chat_id=chat_id, image_path=str(image), caption=caption)
    _check(result, 'UI image')
    log.info('UI design %s v%s: %s sent (message %s)', key, design['version'], name, getattr(result, 'message_id', None))


def _native_images(adapter) -> None:
    method_name = getattr(type(adapter).send_image_file, '__qualname__', '')
    if method_name.startswith('BasePlatformAdapter.'):
        raise RuntimeError('This channel does not implement native image delivery; no receipt can be recorded')


async def deliver_once(api, adapter, chat_id: str, lang: str = 'en') -> None:
    _native_images(adapter)
    pending = await asyncio.to_thread(api.call, 'GET', '/v1/ui-designs/deliveries')
    for design in pending.get('designs', []):
        missing = [name for name in IMAGES if name not in design.get('delivered_images', [])]
        if not missing:
            continue
        if str(design['id']) not in _summarized:
            await _send_changes(adapter, chat_id, design, lang)
            _summarized.add(str(design['id']))
        for name in missing:
            await _send_image(api, adapter, chat_id, design, name, lang)
            await asyncio.to_thread(api.call, 'POST', f"/v1/tasks/{design['key']}/ui-design/{design['id']}/delivered/{name}", body={})


async def resend_once(api, adapter, chat_id: str, lang: str = 'en') -> None:
    """Send the changes and both images of each requested task again; receipts are already recorded."""
    _native_images(adapter)
    while _resend:
        key = _resend.pop()
        status = await asyncio.to_thread(api.call, 'GET', f'/v1/tasks/{key}/ui-design')
        design = status.get('design')
        if not design or design.get('state') != 'PENDING' or not design.get('current'):
            text = _TEXT.get(lang, _TEXT['en'])['missing'].format(key=key)
            _check(await adapter.send(chat_id=chat_id, content=text), 'UI design reply')
            continue
        design = {**design, 'key': key}
        await _send_changes(adapter, chat_id, design, lang)
        for name in IMAGES:
            await _send_image(api, adapter, chat_id, design, name, lang)


def register(ctx) -> None:
    target = destination()
    if target is None:
        return
    platform, chat_id = target

    def connected(native, adapter):
        async def run():
            api = TaskApi('hermes:visual-delivery')
            lang = (os.environ.get('HO_NOTIFY_LANGUAGE') or 'en')[:2]
            while destination() == target:
                for step in (deliver_once, resend_once):
                    try:
                        await step(api, adapter, chat_id, lang)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception('UI image delivery deferred; the design remains unapproved')
                await asyncio.sleep(10)
        old = _tasks.get(platform)
        if old:
            old.cancel()
        _tasks[platform] = asyncio.get_running_loop().create_task(run(), name='orchestration-ui-images')

    ctx.register_platform_handler(platform, connected)
