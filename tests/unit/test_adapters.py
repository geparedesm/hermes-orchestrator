from __future__ import annotations

import json
from pathlib import Path

import pytest

from ho_core import schemas
from ho_core.adapters import (
    AgentAssignment,
    ClaudeAdapter,
    CodexAdapter,
    FailureClass,
    OutputBundle,
    adapter_for,
    image_suffix,
)
from ho_core.adapters.base import result_schema
from ho_core.enums import Role

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
RESULT = {"status": "completed", "summary": "Fixed the OAuth callback redirect and its test.",
          "changed_files": ["src/auth.js"], "tests": {"ran": True, "passed": True, "command": "npm test", "summary": "12 passed"},
          "commits": ["3f2a9c1"], "follow_ups": [], "blocked_reason": None}


def bundle(events: str | None = None, *, exit_code: int = 0, reason: str = "cli_exited", **extra: bytes) -> OutputBundle:
    files = {"ho/runner.json": json.dumps({"exit_code": exit_code, "reason": reason}).encode()}
    if events:
        files["ho/events.jsonl"] = (FIXTURES / events).read_bytes()
    files.update({f"ho/{k.replace('_json', '.json').replace('_txt', '.txt')}": v for k, v in extra.items()})
    return OutputBundle(files=files, exit_code=exit_code)


def assignment(**kwargs) -> AgentAssignment:
    return AgentAssignment(**{"role": Role.DEVELOPER, "prompt": "Add OAuth", "workspace": "WRITE", "git": "LOCAL_COMMIT", **kwargs})


# ------------------------------------------------------------------- building


def test_claude_command_confines_configuration_and_tools():
    plan = ClaudeAdapter().build_execution(assignment(toolchain="node"))
    cmd = plan.command
    assert plan.image == "claude-node" and cmd[0] == "/opt/ho/bin/ho-agent-run"
    assert cmd[cmd.index("--setting-sources") + 1] == "user"  # repository settings, hooks, and .mcp.json are not read
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk" and cmd[cmd.index("--permission-prompts") + 1] == "none"
    assert cmd[cmd.index("--tools") + 1] == "Bash,Edit,Glob,Grep,Read,Write"  # no web tools without egress
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == result_schema()
    assert "--dangerously-skip-permissions" not in cmd
    assert "## Assignment" in plan.inputs["prompt.md"] and "Add OAuth" in plan.inputs["prompt.md"]


@pytest.mark.parametrize("role,tools", [(Role.REVIEWER, "Bash,Glob,Grep,Read"), (Role.ORCHESTRATOR, "Glob,Grep,Read")])
def test_claude_tools_follow_the_role(role, tools):
    cmd = ClaudeAdapter().build_execution(assignment(role=role, workspace="READ", git="NONE")).command
    assert cmd[cmd.index("--tools") + 1] == tools


def test_claude_web_tools_only_with_egress_and_resume():
    cmd = ClaudeAdapter().build_execution(assignment(egress="STANDARD", resume_session="abc", model="opus")).command
    assert cmd[cmd.index("--tools") + 1].endswith("WebFetch,WebSearch")
    assert cmd[cmd.index("--resume") + 1] == "abc" and cmd[cmd.index("--model") + 1] == "opus"


def test_codex_command_uses_container_sandbox_and_untrusted_project():
    plan = CodexAdapter().build_execution(assignment())
    cmd = plan.command
    assert plan.image == "codex-generic" and cmd[:3] == ["/opt/ho/bin/ho-agent-run", "exec", "--json"] and cmd[-1] == "-"
    configs = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-c"]
    assert 'sandbox_mode="danger-full-access"' in configs and 'approval_policy="never"' in configs
    assert 'forced_login_method="chatgpt"' in configs  # never silently switch to API-key billing
    assert 'projects."/workspace".trust_level="untrusted"' in configs
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert json.loads(plan.inputs["result.schema.json"]) == result_schema()


def test_codex_resume_puts_the_session_before_the_prompt():
    cmd = CodexAdapter().build_execution(assignment(resume_session="0199a213")).command
    assert cmd[1:3] == ["exec", "resume"] and cmd[-2:] == ["0199a213", "-"]


def test_result_schema_is_valid_and_has_no_metadata():
    schema = result_schema()
    assert "$schema" not in schema and "description" not in json.dumps(schema)
    assert set(schema["required"]) == set(schema["properties"])  # structured outputs need every property required
    assert not schemas.errors_for("agent-result", RESULT)


def test_review_schema_keeps_properties_named_description():
    """Regression: stripping annotations removed the findings' `description` property, which made
    the schema impossible to satisfy (found in a real Claude Code review run)."""
    from jsonschema import Draft202012Validator

    schema = result_schema("review-result")

    def check(node):
        if isinstance(node, dict):
            if "required" in node:
                assert set(node["required"]) <= set(node.get("properties", {})), node["required"]
            for value in node.values():
                check(value)
    check(schema)
    finding = {"severity": "LOW", "category": "style", "path": None, "line": None, "description": "x"}
    review = {"verdict": "approved", "summary": "ok", "requirements_met": True, "unmet_requirements": [], "findings": [finding]}
    assert not list(Draft202012Validator(schema).iter_errors(review))


def test_image_suffix_composes_profiles():
    assert image_suffix(["generic"]) == "generic"
    assert image_suffix(["python", "node", "generic"]) == "node-python"
    assert adapter_for("codex").provider == "codex"
    with pytest.raises(ValueError):
        adapter_for("gemini")


# -------------------------------------------------------------------- results


def test_claude_success_is_normalized_and_reasoning_dropped():
    adapter = ClaudeAdapter()
    result = adapter.collect_result(bundle("claude-success.jsonl"))
    assert result.ok and result.status == "completed" and result.failure_class is None
    assert result.session_id == "5f0c7a9e-1111-4222-8333-944455556666"
    assert result.commits == ["3f2a9c1"] and result.tests["passed"] is True
    serialized = json.dumps([e.__dict__ for e in result.events]) + json.dumps(result.as_json())
    assert "PRIVATE REASONING" not in serialized and "I will fix" not in serialized
    types = [e.type for e in result.events]
    assert types.count("COMMAND_STARTED") == 3 and "TOOL_USED" in types and types[-1] == "RESULT"
    assert result.high_risk_commands == 1  # reading the credential volume is flagged
    usage = adapter.collect_usage(bundle("claude-success.jsonl")).units
    assert usage["input_tokens"] == 1200 and usage["num_turns"] == 7 and usage["reported_cost_usd_estimate"] == 0.4123


def test_claude_auth_failure_is_classified():
    result = ClaudeAdapter().collect_result(bundle("claude-auth-failure.jsonl", exit_code=1))
    assert not result.ok and result.failure_class == FailureClass.AUTH
    assert "401" in (result.error or "")


def test_claude_missing_credential_and_truncated_stream():
    result = ClaudeAdapter().collect_result(bundle(exit_code=3, reason="credential_missing"))
    assert result.failure_class == FailureClass.AUTH
    final = (FIXTURES / "claude-success.jsonl").read_text().splitlines()[-1].encode()
    truncated = bundle(final_json=final)  # events.jsonl lost to output limits; final.json survives
    assert ClaudeAdapter().collect_result(truncated).ok


@pytest.mark.parametrize("error,expected", [("rate_limit", FailureClass.QUOTA), ("overloaded", FailureClass.TRANSIENT)])
def test_claude_retry_errors_classify(tmp_path, error, expected):
    events = [{"type": "system", "subtype": "api_retry", "error": error, "error_status": 529},
              {"type": "result", "subtype": "success", "is_error": True, "result": "API Error"}]
    b = OutputBundle({"ho/events.jsonl": "\n".join(json.dumps(e) for e in events).encode(),
                      "ho/runner.json": b'{"exit_code": 1, "reason": "cli_exited"}'})
    assert ClaudeAdapter().classify_failure(b) == expected


def test_claude_max_turns_and_bad_structured_output_are_task_failures():
    events = [{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""}]
    b = OutputBundle({"ho/events.jsonl": json.dumps(events[0]).encode(), "ho/runner.json": b'{"exit_code": 1}'})
    assert ClaudeAdapter().collect_result(b).failure_class == FailureClass.TASK
    bad = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"status": "done"}}
    b = OutputBundle({"ho/events.jsonl": json.dumps(bad).encode(), "ho/runner.json": b'{"exit_code": 0}'})
    result = ClaudeAdapter().collect_result(b)
    assert not result.ok and result.failure_class == FailureClass.TASK and "schema" in (result.error or "")


def test_codex_success_is_normalized_and_reasoning_dropped():
    adapter = CodexAdapter()
    b = bundle("codex-success.jsonl", last_message_json=json.dumps(RESULT).encode(), credential_txt=b"refreshed\n")
    result = adapter.collect_result(b)
    assert result.ok and result.status == "completed" and result.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert result.credential_refreshed
    serialized = json.dumps([e.__dict__ for e in result.events])
    assert "PRIVATE REASONING" not in serialized and "aggregated_output" not in serialized and "12 passed" not in serialized
    types = [e.type for e in result.events]
    assert types == ["SESSION_STARTED", "COMMAND_STARTED", "COMMAND_FINISHED", "FILES_CHANGED",
                     "COMMAND_STARTED", "COMMAND_FINISHED", "TURN_COMPLETED"]
    usage = adapter.collect_usage(b).units
    assert usage == {"input_tokens": 24763, "cached_input_tokens": 24448, "output_tokens": 122,
                     "reasoning_output_tokens": 64, "num_turns": 1}


def test_codex_auth_failure_and_missing_credential():
    result = CodexAdapter().collect_result(bundle("codex-auth-failure.jsonl", exit_code=1))
    assert not result.ok and result.failure_class == FailureClass.AUTH and "401" in (result.error or "")
    assert CodexAdapter().collect_result(bundle(exit_code=3, reason="credential_missing")).failure_class == FailureClass.AUTH


@pytest.mark.parametrize("message,expected", [
    ("You've hit your usage limit. Try again later.", FailureClass.QUOTA),
    ("stream disconnected before completion: error sending request", FailureClass.TRANSIENT),
    ("something unexpected", FailureClass.UNKNOWN),
])
def test_codex_failure_messages_classify(message, expected):
    events = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.failed", "error": {"message": message}}]
    b = OutputBundle({"ho/events.jsonl": "\n".join(json.dumps(e) for e in events).encode(),
                      "ho/runner.json": b'{"exit_code": 1, "reason": "cli_exited"}'})
    assert CodexAdapter().classify_failure(b) == expected


def test_codex_invalid_final_message_is_a_task_failure():
    b = bundle("codex-success.jsonl", last_message_json=b'{"status": "completed"}')
    result = CodexAdapter().collect_result(b)
    assert not result.ok and result.failure_class == FailureClass.TASK


def test_health_check():
    health = CodexAdapter().health_check(pinned_images=["codex-generic"], credential_present=True, credential_status="READY")
    assert health.ok
    health = ClaudeAdapter().health_check(pinned_images=["codex-generic"], credential_present=True, credential_status="READY")
    assert not health.ok and "make images" in health.detail
    health = ClaudeAdapter().health_check(pinned_images=["claude-node"], credential_present=True, credential_status="AUTH_REQUIRED")
    assert not health.ok and "auth-claude" in health.detail


def test_orchestrator_reads_its_project_through_add_dir():
    from ho_core.adapters.base import AgentAssignment
    from ho_core.adapters.claude import ClaudeAdapter
    from ho_core.enums import Role

    plan = ClaudeAdapter().build_execution(AgentAssignment(role=Role.ORCHESTRATOR, prompt="plan", read_dirs=("/projects/shop",)))
    index = plan.command.index("--add-dir")
    assert plan.command[index + 1] == "/projects/shop"
    plain = ClaudeAdapter().build_execution(AgentAssignment(role=Role.DEVELOPER, prompt="do it"))
    assert "--add-dir" not in plain.command
