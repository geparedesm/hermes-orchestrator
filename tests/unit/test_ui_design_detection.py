import subprocess
from pathlib import Path

import pytest
from control_plane.ui_designs import detects_ui
from ho_core import attachments


@pytest.mark.parametrize('text,files', [
    ('Cambia el diseño UI', []), ('Crea una página web', []), ('Add a portfolio', []),
    ('Update the layout', ['views/profile.xml']), ('Change spacing', ['src/components/Card.tsx']),
    ('Follow ui/portfolio.pen', []), ('Add a component', ['static/src/scss/portfolio.scss']),
])
def test_frontend_detection(text, files):
    assert detects_ui(text, files)


def test_backend_work_does_not_require_visual_design():
    assert not detects_ui('Add a greeting endpoint', ['app.py', 'tests/test_app.py'])


def test_encrypted_pen_attachment_is_opaque_and_bounded():
    assert attachments.check('design.pen', b'\x00\xffencrypted').media_type == 'application/vnd.pen.design'
    with pytest.raises(attachments.InvalidAttachment):
        attachments.check('design.pen', b'')


def test_headless_wrapper_refuses_nested_ai_runs():
    wrapper = Path(__file__).resolve().parents[2] / 'workers/agent-base/ho-pen'
    result = subprocess.run(['bash', str(wrapper), '--prompt', 'design something'], capture_output=True)
    assert result.returncode == 2
    assert b'nested AI-agent runs' in result.stderr
