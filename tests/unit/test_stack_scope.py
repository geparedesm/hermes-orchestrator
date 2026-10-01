"""Several platform stacks on one Docker host: names and labels keep their resources apart."""

from __future__ import annotations

import importlib


def test_main_stack_keeps_plain_names(monkeypatch):
    monkeypatch.delenv("HO_STACK", raising=False)
    from agent_manager import stack
    importlib.reload(stack)
    assert stack.NAME == "hermes-orchestrator" and stack.task_slug("T-1") == "t-1"


def test_other_stacks_prefix_task_scoped_names(monkeypatch):
    from agent_manager import compose, stack
    monkeypatch.setenv("HO_STACK", "ho-smoke9")
    try:
        importlib.reload(stack)
        assert stack.task_slug("T-1") == "ho-smoke9-t-1" and stack.LABEL == "ho.stack=ho-smoke9"
        assert compose.project_name("T-1", "shop") == "ho-ho-smoke9-t-1-shop"
    finally:
        monkeypatch.delenv("HO_STACK")
        importlib.reload(stack)
