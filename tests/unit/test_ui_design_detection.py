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


def test_review_summary_prefers_the_reviewer_section_then_before_after():
    from control_plane.ui_designs import review_summary
    both = "# T\n\n## Before / after\nOld vs new.\n\n## Summary for the reviewer\n- New filters\n- Document preview\n\n## Colors\nPurple\n"
    assert review_summary(both) == "- New filters\n- Document preview"
    before_after = "# T\n## Before / after\n**Before** (`views/x.xml`): a plain list.\n\n**After:**\n- A table\n- Filters\n## Assets\nNone\n"
    assert review_summary(before_after) == "- A table\n- Filters"
    assert review_summary("## Before / after\nAfter: one table.\n") == "one table."
    assert review_summary("## Before / after\nOnly a description.\n") == "Only a description."
    assert review_summary("# Only a title\nSome text\n") == ""
    long = review_summary("## Summary for the reviewer\n" + "word " * 600, limit=50)
    assert len(long) <= 51 and long.endswith("…")
