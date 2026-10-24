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
                       'delivered_images': list(delivered), 'summary': '- Tenants list gets a document column',
                       'manifest': {name: {'sha256': hashlib.sha256(PNG).hexdigest()} for name in visual.IMAGES}}

    def call(self, method, path, **kwargs):
        if path == '/v1/ui-designs/deliveries':
            return {'designs': [self.design]}
        if path == '/v1/tasks/T-8/ui-design':
            return {'design': {**{k: v for k, v in self.design.items() if k != 'key'}, 'state': 'PENDING', 'current': True}}
        if path.endswith('/ui-design'):
            return {'design': None}
        if method == 'GET':
            return self.content, {}
        self.receipts.append(path)
        assert method == 'POST' and '/delivered/' in path
        return {}


class Adapter:
    def __init__(self, success=True):
        self.sent = []
        self.texts = []
        self.success = success

    async def send(self, *, chat_id, content):
        self.texts.append((chat_id, content))
        return SimpleNamespace(success=True, error=None, message_id='text-1')

    async def send_image_file(self, *, chat_id, image_path, caption):
        self.sent.append((chat_id, Path(image_path).read_bytes(), caption))
        return SimpleNamespace(success=self.success, error=None if self.success else 'offline', message_id='image-1')


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


@pytest.fixture(autouse=True)
def fresh_state():
    visual._summarized.clear()
    visual._resend.clear()
    yield
    visual._tasks.clear()


def test_sends_the_changes_then_both_images_to_only_the_configured_destination():
    api, adapter = Api(), Adapter()
    asyncio.run(visual.deliver_once(api, adapter, 'authorized-chat', 'es'))
    [(chat, text)] = adapter.texts
    assert chat == 'authorized-chat'
    assert 'T-8 · Diseño de UI v1' in text and 'Tenants list gets a document column' in text and 'si 12' in text
    assert len(adapter.sent) == 2
    assert {entry[0] for entry in adapter.sent} == {'authorized-chat'}
    assert [entry[2] for entry in adapter.sent] == ['T-8 · UI v1 · Escritorio', 'T-8 · UI v1 · Móvil']
    assert all(entry[1] == PNG for entry in adapter.sent)
    assert len(api.receipts) == 2


def test_a_design_without_summary_still_says_what_to_review():
    api, adapter = Api(), Adapter()
    api.design['summary'] = ''
    asyncio.run(visual.deliver_once(api, adapter, 'authorized-chat'))
    assert 'no summary of changes' in adapter.texts[0][1]


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


def test_a_retried_image_does_not_repeat_the_changes_message():
    api, failing = Api(), Adapter(success=False)
    with pytest.raises(RuntimeError):
        asyncio.run(visual.deliver_once(api, failing, 'authorized-chat'))
    adapter = Adapter()
    asyncio.run(visual.deliver_once(api, adapter, 'authorized-chat'))
    assert len(failing.texts) == 1 and not adapter.texts
    assert len(adapter.sent) == len(api.receipts) == 2


def test_resend_needs_running_delivery(monkeypatch):
    monkeypatch.delenv('HO_UI_DESIGN_DELIVERY', raising=False)
    assert visual.request_resend('t-8') is False
    monkeypatch.setenv('HO_UI_DESIGN_DELIVERY', 'true')
    monkeypatch.setenv('HO_UI_DESIGN_DELIVER_PLATFORM', 'whatsapp')
    monkeypatch.setenv('HO_UI_DESIGN_DELIVER_CHAT_ID', 'authorized-chat')
    assert visual.request_resend('t-8') is False  # no connected channel yet
    visual._tasks['whatsapp'] = object()
    assert visual.request_resend('t-8') is True and visual._resend == {'T-8'}


def test_resend_sends_the_current_proposal_again_without_new_receipts():
    api, adapter = Api(delivered=list(visual.IMAGES)), Adapter()
    visual._resend.update({'T-8', 'T-9'})
    asyncio.run(visual.resend_once(api, adapter, 'authorized-chat', 'es'))
    texts = [text for _, text in adapter.texts]
    assert any('Tenants list gets a document column' in t and 'si 12' in t for t in texts)
    assert any('No hay un diseño de UI pendiente de aprobación en T-9' in t for t in texts)
    assert len(adapter.sent) == 2 and not api.receipts and not visual._resend
