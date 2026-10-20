"""Opt-in image delivery to one explicitly authorized destination.

Disabled by default. This helper cannot approve designs and never chooses a destination from task content.
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
IMAGES = ('desktop.png', 'mobile.png')


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


async def deliver_once(api, adapter, chat_id: str, lang: str = 'en') -> None:
    method_name = getattr(type(adapter).send_image_file, '__qualname__', '')
    if method_name.startswith('BasePlatformAdapter.'):
        raise RuntimeError('This channel does not implement native image delivery; no receipt can be recorded')
    pending = await asyncio.to_thread(api.call, 'GET', '/v1/ui-designs/deliveries')
    for design in pending.get('designs', []):
        key, design_id = design['key'], design['id']
        for name in IMAGES:
            if name in design.get('delivered_images', []):
                continue
            content, _ = await asyncio.to_thread(api.call, 'GET', f'/v1/tasks/{key}/ui-design/{design_id}/{name}', raw=True)
            if len(content) > 10 * 1024 * 1024 or not content.startswith(b'\x89PNG\r\n\x1a\n') \
                    or hashlib.sha256(content).hexdigest() != design['manifest'][name]['sha256']:
                raise ValueError('UI image differs from the recorded pen.dev export')
            label = ('Escritorio' if name == IMAGES[0] else 'Móvil') if lang == 'es' else name.removesuffix('.png').title()
            caption = (f"{key} · UI v{design['version']} · {label}\n"
                       + ('Revisa ambas imágenes; después recibirás la solicitud de aprobación.'
                          if lang == 'es' else 'Review both images; the approval request follows after delivery.'))
            with tempfile.TemporaryDirectory(prefix='ho-ui-') as folder:
                image = Path(folder) / name
                image.write_bytes(content)
                result = await adapter.send_image_file(chat_id=chat_id, image_path=str(image), caption=caption)
            if not result.success:
                raise RuntimeError('Hermes could not deliver the UI image: ' + str(result.error or 'unsupported image delivery'))
            await asyncio.to_thread(api.call, 'POST', f'/v1/tasks/{key}/ui-design/{design_id}/delivered/{name}', body={})


def register(ctx) -> None:
    target = destination()
    if target is None:
        return
    platform, chat_id = target

    def connected(native, adapter):
        async def run():
            api = TaskApi('hermes:visual-delivery')
            while destination() == target:
                try:
                    await deliver_once(api, adapter, chat_id, (os.environ.get('HO_NOTIFY_LANGUAGE') or 'en')[:2])
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
