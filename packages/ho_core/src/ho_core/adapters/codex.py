"""CodexAdapter: Codex CLI non-interactive mode (`codex exec`) inside a worker.

Official interfaces used (learn.chatgpt.com docs and `codex exec --help`,
verified for the pinned version in workers/versions.env):
- `exec --json`: JSONL events (thread.started, turn.*, item.*, error).
- `--output-schema FILE` and `-o FILE`: structured final message (agent-result schema).
- `exec resume SESSION_ID -`: continue a session from the task's session volume.
- `-c key=value` overrides: sandbox_mode="danger-full-access" because the worker
  container is the sandbox (Codex's own bubblewrap sandbox cannot create user
  namespaces in a container without capabilities; the documented alternative for
  containers that are the security boundary), approval_policy="never" (no one
  answers prompts), file credential storage, ChatGPT login only (never silently
  switch to API-key billing, D13), no update checks or analytics, and the
  workspace marked untrusted so project `.codex/` config, hooks, and rules are
  skipped (OI-02). `--ignore-user-config` and `--ignore-rules` add to that.
- Reasoning items in the stream are dropped here and never stored (section 86).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .base import (
    INPUT_DIR,
    MAX_EVENTS,
    OUTPUT_DIR,
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

_CONFIG = [
    'sandbox_mode="danger-full-access"',
    'approval_policy="never"',
    'cli_auth_credentials_store="file"',
    'forced_login_method="chatgpt"',
    "check_for_update_on_startup=false",
    "analytics.enabled=false",
    "feedback.enabled=false",
    'history.persistence="none"',
    'projects."/workspace".trust_level="untrusted"',
    'model_reasoning_summary="none"',
    "hide_agent_reasoning=true",
]
_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


class CodexAdapter:
    provider = "codex"

    def build_execution(self, assignment: AgentAssignment) -> ExecutionPlan:
        options = ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                   "--output-schema", f"{INPUT_DIR}/result.schema.json",
                   "-o", f"/output/{OUTPUT_DIR}/last_message.json"]
        for item in _CONFIG:
            options += ["-c", item]
        if assignment.model:
            options += ["-m", assignment.model]
        if assignment.resume_session:
            command = [RUNNER, "exec", "resume", *options, assignment.resume_session, "-"]
        else:
            command = [RUNNER, "exec", *options, "-"]
        return ExecutionPlan(
            image=f"codex-{assignment.toolchain}",
            command=command,
            inputs={"prompt.md": compose_prompt(assignment), "result.schema.json": json.dumps(result_schema(), indent=2)},
        )

    # ------------------------------------------------------------------ events

    def parse_event(self, event: dict[str, Any]) -> AdapterEvent | None:
        kind = event.get("type")
        if kind == "thread.started":
            return AdapterEvent("SESSION_STARTED", {"session_id": event.get("thread_id")})
        if kind == "turn.completed":
            usage = event.get("usage") or {}
            return AdapterEvent("TURN_COMPLETED", {k: usage.get(k) for k in _USAGE_KEYS if isinstance(usage.get(k), int)})
        if kind == "turn.failed":
            return AdapterEvent("PROVIDER_ERROR", {"message": str((event.get("error") or {}).get("message"))[:500]})
        if kind == "error":
            return AdapterEvent("PROVIDER_RETRY" if str(event.get("message", "")).startswith("Reconnecting")
                                else "PROVIDER_ERROR", {"message": str(event.get("message"))[:500]})
        if kind not in ("item.started", "item.completed"):
            return None
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type == "command_execution" and isinstance(item.get("command"), str):
            if kind == "item.started":
                return command_event("COMMAND_STARTED", item["command"])
            return command_event("COMMAND_FINISHED", item["command"], exit_code=item.get("exit_code"), status=item.get("status"))
        if kind != "item.completed":
            return None
        if item_type == "file_change":
            changes = [{"path": str(c.get("path"))[:300], "kind": c.get("kind")} for c in (item.get("changes") or [])
                       if isinstance(c, dict)][:100]
            return AdapterEvent("FILES_CHANGED", {"changes": changes})
        if item_type in ("mcp_tool_call", "web_search"):
            return AdapterEvent("TOOL_USED", {"tool": item_type if item_type == "web_search" else str(item.get("tool"))[:64]})
        if item_type == "error":
            return AdapterEvent("PROVIDER_ERROR", {"message": str(item.get("message"))[:500]})
        return None  # agent_message, reasoning, todo_list: not operational evidence

    # ------------------------------------------------------------------ results

    def collect_result(self, bundle: OutputBundle) -> ExecutionResult:
        raw = list(bundle.json_lines("events.jsonl"))
        result = ExecutionResult(provider=self.provider, ok=False)
        for event in raw:
            parsed = self.parse_event(event)
            if parsed is None or len(result.events) >= MAX_EVENTS:
                continue
            result.events.append(parsed)
            if parsed.type == "COMMAND_STARTED" and parsed.data["class"] == "HIGH_RISK":
                result.high_risk_commands += 1
        result.session_id = next((e.get("thread_id") for e in raw if e.get("type") == "thread.started"), None)
        result.credential_refreshed = (bundle.text("credential.txt") or "").strip() == "refreshed"
        failed = any(e.get("type") == "turn.failed" for e in raw)
        last = bundle.text("last_message.json")
        structured: Any = None
        if last:
            try:
                structured = json.loads(last)
            except ValueError:
                structured = None
        if not failed and runner_state(bundle).get("exit_code") == 0 and normalize_structured(self.provider, structured, result):
            result.ok = True
        else:
            result.failure_class = self.classify_failure(bundle)
            result.error = self._last_error(raw) or (
                "the final answer did not match the result schema" if last else str(runner_state(bundle).get("reason") or "execution failed"))
        return result

    @staticmethod
    def _last_error(raw: list[dict[str, Any]]) -> str | None:
        for event in reversed(raw):
            if event.get("type") == "turn.failed":
                return str((event.get("error") or {}).get("message"))[:500]
            if event.get("type") == "error":
                return str(event.get("message"))[:500]
        return None

    def classify_failure(self, bundle: OutputBundle) -> FailureClass | None:
        runner = runner_state(bundle)
        if runner.get("reason") == "credential_missing":
            return FailureClass.AUTH
        raw = list(bundle.json_lines("events.jsonl"))
        message = self._last_error(raw)
        if message:
            return classify_text(message) or FailureClass.UNKNOWN
        if bundle.oom_killed:
            return FailureClass.TASK
        if runner.get("exit_code") == 0:
            return FailureClass.TASK  # finished, but without a valid structured result
        return classify_text(bundle.logs) or FailureClass.UNKNOWN

    def collect_usage(self, bundle: OutputBundle) -> UsageRecord:
        totals: dict[str, int] = {}
        turns = 0
        for event in bundle.json_lines("events.jsonl"):
            if event.get("type") != "turn.completed":
                continue
            turns += 1
            for key in _USAGE_KEYS:
                value = (event.get("usage") or {}).get(key)
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        return UsageRecord(self.provider, {**totals, "num_turns": turns})

    def health_check(self, *, pinned_images: Iterable[str], credential_present: bool, credential_status: str) -> ProviderHealth:
        pinned = any(name.startswith("codex-") for name in pinned_images)
        ok = pinned and credential_present and credential_status != "AUTH_REQUIRED"
        detail = ("ready" if ok else "build images with `make images`" if not pinned
                  else "run `make auth-codex`" if not credential_present or credential_status == "AUTH_REQUIRED" else "")
        return ProviderHealth(self.provider, ok, pinned, credential_present, credential_status, detail)
