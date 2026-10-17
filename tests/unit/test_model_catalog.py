"""Model choices in the Dashboard come from the providers themselves."""

from __future__ import annotations

import json

from control_plane.agents import CLAUDE_MODELS, model_catalog


def test_codex_models_come_from_the_generated_catalog(tmp_path):
    path = tmp_path / "models.lock.json"
    path.write_text(json.dumps({"codex": [{"id": "gpt-6.1-sol", "label": "GPT-6.1-Sol"}, {"id": "bad id!", "label": "x"},
                                          {"id": "gpt-6-luna"}]}))
    catalog = model_catalog(path)
    assert catalog["codex"] == [{"id": "gpt-6.1-sol", "label": "GPT-6.1-Sol"}, {"id": "gpt-6-luna", "label": "gpt-6-luna"}]
    assert catalog["claude"] == CLAUDE_MODELS  # documented aliases; the file cannot replace them


def test_without_a_catalog_only_claude_aliases_are_offered(tmp_path):
    assert model_catalog(tmp_path / "missing.json") == {"claude": CLAUDE_MODELS}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert model_catalog(broken) == {"claude": CLAUDE_MODELS}
