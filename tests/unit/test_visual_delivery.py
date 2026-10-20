"""No network: the sender is opt-in and acknowledges only images a fake adapter delivered."""
import asyncio
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hermes_plugin import plugin

visual = importlib.import_module(plugin.__name__ + '.visual_delivery')
PNG = b'\x89PNG\r\n\x1a\n' + b'fake image bytes'


class Api:
    def __init__(self, *, content=PNG, delivered=()):
        self.receipts = []
        self.content = content
        self.design = {'key': 'T-8', 'id': 'design-id', 'number': 12, 'version': 1,
                       'delivered_images': list(delivered),
                       'manifest': {name: {'sha256': hashlib.sha256(PNG).hexdigest()} for name in visual.IMAGES}}

    def call(self, method, path, **kwargs):
        if path == '/v1/ui-designs/deliveries':
            return {'designs': [self.design]}
        if method == 'GET':
            return self.content, {}
        self.receipts.append(path)
        assert method == 'POST' and '/delivered/' in path
        return {}


class Adapter:
    def __init__(self, success=True):
        self.sent = []
        self.success = success

    async def send_image_file(self, *, chat_id, image_path, caption):
        self.sent.append((chat_id, Path(image_path).read_bytes(), caption))
        return SimpleNamespace(success=self.success, error=None if self.success else 'offline')


def test_no_delivery_without_explicit_activation_and_destination(monkeypatch):
    for name in ('HO_UI_DESIGN_DELIVERY', 'HO_UI_DESIGN_DELIVER_PLATFORM', 'HO_UI_DESIGN_DELIVER_CHAT_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HO_HERMES_DELIVER', 'telegram')
    monkeypatch.setenv('HO_HERMES_DELIVER_CHAT_ID', 'general-chat')
    assert visual.destination() is None
    visual.register(SimpleNamespace())  # no platform registration or background task
    monkeypatch.setenv('HO_UI_DESIGN_DELIVERY', 'true')
    assert visual.destination() is None
    monkeypatch.setenv('HO_UI_DESIGN_DELIVER_PLATFORM', 'telegram')
    assert visual.destination() is None
    monkeypatch.setenv('HO_UI_DESIGN_DELIVER_CHAT_ID', 'authorized-chat')
    assert visual.destination() == ('telegram', 'authorized-chat')


def test_sends_both_images_to_only_the_configured_destination():
    api, adapter = Api(), Adapter()
    asyncio.run(visual.deliver_once(api, adapter, 'authorized-chat', 'es'))
    assert len(adapter.sent) == 2
    assert {entry[0] for entry in adapter.sent} == {'authorized-chat'}
    assert all(entry[1] == PNG and 'solicitud de aprobación' in entry[2] for entry in adapter.sent)
    assert len(api.receipts) == 2


def test_failed_upload_is_not_acknowledged():
    api, adapter = Api(), Adapter(success=False)
    with pytest.raises(RuntimeError, match='could not deliver'):
        asyncio.run(visual.deliver_once(api, adapter, 'authorized-chat'))
    assert not api.receipts


def test_changed_image_is_not_sent_or_acknowledged():
    api, adapter = Api(content=PNG + b'tampered'), Adapter()
    with pytest.raises(ValueError, match='differs'):
        asyncio.run(visual.deliver_once(api, adapter, 'authorized-chat'))
    assert not adapter.sent and not api.receipts


def test_retries_only_the_image_that_has_not_been_delivered():
    api, adapter = Api(delivered=['desktop.png']), Adapter()
    asyncio.run(visual.deliver_once(api, adapter, 'authorized-chat'))
    assert len(adapter.sent) == len(api.receipts) == 1
    assert api.receipts[0].endswith('/delivered/mobile.png')
