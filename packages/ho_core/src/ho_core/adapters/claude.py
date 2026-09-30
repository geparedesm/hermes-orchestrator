"""ClaudeAdapter: Claude Code in print mode (`claude -p`) inside a worker.

Official interfaces used (code.claude.com docs, verified for the pinned version
in workers/versions.env):
- `-p --output-format stream-json --verbose`: newline-delimited events; the last
  line is the `result` message with session_id, usage, and structured_output.
- `--json-schema`: structured final answer (the agent-result schema).
- `--setting-sources user` plus an empty per-execution CLAUDE_CONFIG_DIR: the
  repository's `.claude/settings*.json` and `.mcp.json` are not read, so
  repository hooks and MCP servers do not run (OI-02). `--settings
  {"disableAllHooks": true}` and `--strict-mcp-config` add to that.
- `--tools` / `--allowedTools` / `--permission-mode dontAsk` /
  `--permission-prompts none`: role-scoped tools, no prompts in unattended runs.
- `--resume <session_id>`: continue a session stored in the task's session volume.
- Subscription authentication through CLAUDE_CODE_OAUTH_TOKEN, created with
  `claude setup-token` (the in-container runner reads it from the credential volume).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..enums import Role
from .base import (
    MAX_EVENTS,
    RUNNER,
    AdapterEvent,
    AgentAssignment,
    ExecutionPlan,
    ExecutionResult,
    FailureClass,
    OutputBundle,
    ProviderHealth,
    UsageRecord,
    classify_text,
    command_event,
    compose_prompt,
    normalize_structured,
    result_schema,
    runner_state,
)

_TOOLS = {
    Role.DEVELOPER: ["Bash", "Edit", "Glob", "Grep", "Read", "Write"],
    Role.REVIEWER: ["Bash", "Glob", "Grep", "Read"],
    Role.ORCHESTRATOR: ["Glob", "Grep", "Read"],
}
_WEB_TOOLS = ["WebFetch", "WebSearch"]
_AUTH_ERRORS = {"authentication_failed", "oauth_org_not_allowed"}
_QUOTA_ERRORS = {"rate_limit", "billing_error", "account_on_hold"}
_TRANSIENT_ERRORS = {"overloaded", "server_error"}


class ClaudeAdapter:
    provider = "claude"

    def build_execution(self, assignment: AgentAssignment) -> ExecutionPlan:
        tools = list(_TOOLS.get(assignment.role, _TOOLS[Role.REVIEWER]))
        if assignment.egress in ("ALLOWLIST", "STANDARD"):
            tools += _WEB_TOOLS
        tool_list = ",".join(tools)
        command = [
            RUNNER,
            "-p",
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", "user",
            "--settings", json.dumps({"disableAllHooks": True}),
            "--strict-mcp-config",
            "--tools", tool_list,
            "--allowedTools", tool_list,
            "--disallowedTools", "mcp__*",
            "--permission-mode", "dontAsk",
            "--permission-prompts", "none",
            "--max-turns", str(assignment.max_turns),
            "--json-schema", json.dumps(result_schema(assignment.result_schema), separators=(",", ":")),
        ]
        if assignment.resume_session:
            command += ["--resume", assignment.resume_session]
        if assignment.model:
            command += ["--model", assignment.model]
        return ExecutionPlan(
            image=f"claude-{assignment.toolchain}",
            command=command,
            inputs={"prompt.md": compose_prompt(assignment)},
        )

    # ------------------------------------------------------------------ events

    def parse_event(self, event: dict[str, Any]) -> AdapterEvent | None:
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            return AdapterEvent("SESSION_STARTED", {"session_id": event.get("session_id"), "model": event.get("model"),
                                                    "version": event.get("claude_code_version")})
        if kind == "system" and subtype == "api_retry":
            return AdapterEvent("PROVIDER_RETRY", {"attempt": event.get("attempt"), "error": event.get("error"),
                                                   "status": event.get("error_status")})
        if kind == "assistant":
            # Only tool calls are operational evidence; text and thinking blocks are dropped (section 86).
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_input = block.get("input") or {}
                    if block.get("name") == "Bash" and isinstance(tool_input.get("command"), str):
                        return command_event("COMMAND_STARTED", tool_input["command"])
                    path = tool_input.get("file_path") or tool_input.get("path")
                    return AdapterEvent("TOOL_USED", {"tool": str(block.get("name"))[:64],
                                                      **({"path": str(path)[:300]} if path else {})})
            return None
        if kind == "result":
            return AdapterEvent("RESULT", {k: event.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                                                     "api_error_status", "terminal_reason")})
        return None

    def _events(self, bundle: OutputBundle) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        raw = list(bundle.json_lines("events.jsonl"))
        final = next((e for e in reversed(raw) if e.get("type") == "result"), None)
        if final is None:  # events.jsonl truncated: the runner kept the last line separately
            final = next((e for e in bundle.json_lines("final.json") if e.get("type") == "result"), None)
        return raw, final

    # ------------------------------------------------------------------ results

    def collect_result(self, bundle: OutputBundle, schema: str = "agent-result") -> ExecutionResult:
        raw, final = self._events(bundle)
        result = ExecutionResult(provider=self.provider, ok=False)
        for event in raw:
            parsed = self.parse_event(event)
            if parsed is None or len(result.events) >= MAX_EVENTS:
                continue
            result.events.append(parsed)
            if parsed.type == "COMMAND_STARTED" and parsed.data["class"] == "HIGH_RISK":
                result.high_risk_commands += 1
        result.session_id = next((e.get("session_id") for e in raw if e.get("session_id")), None)
        if final is not None:
            result.session_id = final.get("session_id") or result.session_id
            if not final.get("is_error") and normalize_structured(self.provider, final.get("structured_output"), result, schema):
                result.ok = True
            elif final.get("is_error"):
                result.error = str(final.get("result") or final.get("subtype"))[:500]
            else:
                result.error = "the final answer did not match the result schema"
        result.failure_class = self.classify_failure(bundle) if not result.ok else None
        if result.failure_class and not result.error:
            result.error = str(runner_state(bundle).get("reason") or "execution failed")
        return result

    def classify_failure(self, bundle: OutputBundle) -> FailureClass | None:
        runner = runner_state(bundle)
        if runner.get("reason") == "credential_missing":
            return FailureClass.AUTH
        raw, final = self._events(bundle)
        retry_errors = {e.get("error") for e in raw if e.get("type") == "system" and e.get("subtype") == "api_retry"}
        retry_errors |= {e.get("error") for e in raw if e.get("type") == "assistant" and e.get("error")}
        status = (final or {}).get("api_error_status")
        if retry_errors & _AUTH_ERRORS or status in (401, 403):
            return FailureClass.AUTH
        if retry_errors & _QUOTA_ERRORS or status == 429:
            return FailureClass.QUOTA
        if final is None:
            if bundle.oom_killed:
                return FailureClass.TASK
            return classify_text(bundle.logs) or FailureClass.UNKNOWN
        if final.get("subtype") == "error_max_turns":
            return FailureClass.TASK
        if retry_errors & _TRANSIENT_ERRORS or (isinstance(status, int) and status >= 500):
            return FailureClass.TRANSIENT
        if final.get("is_error"):
            return classify_text(str(final.get("result") or "")) or FailureClass.UNKNOWN
        return FailureClass.TASK  # finished, but without a valid structured result

    def collect_usage(self, bundle: OutputBundle) -> UsageRecord:
        _, final = self._events(bundle)
        usage = (final or {}).get("usage") or {}
        units = {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                           "cache_read_input_tokens") if isinstance(usage.get(k), int)}
        for key in ("num_turns", "duration_ms", "duration_api_ms"):
            if isinstance((final or {}).get(key), int):
                units[key] = final[key]  # type: ignore[index]
        if isinstance((final or {}).get("total_cost_usd"), (int, float)):
            # Client-side estimate reported by the CLI; subscription usage is not billed per request.
            units["reported_cost_usd_estimate"] = final["total_cost_usd"]  # type: ignore[index]
        return UsageRecord(self.provider, units)

    def health_check(self, *, pinned_images: Iterable[str], credential_present: bool, credential_status: str) -> ProviderHealth:
        pinned = any(name.startswith("claude-") for name in pinned_images)
        ok = pinned and credential_present and credential_status != "AUTH_REQUIRED"
        detail = ("ready" if ok else "build images with `make images`" if not pinned
                  else "run `make auth-claude`" if not credential_present or credential_status == "AUTH_REQUIRED" else "")
        return ProviderHealth(self.provider, ok, pinned, credential_present, credential_status, detail)

