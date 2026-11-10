"""Protocol and container boundaries for account-only quota probes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from docker.errors import NotFound
from requests.exceptions import ReadTimeout

from agent_manager import quota
from agent_manager.plan import Rejected
from ho_core.quota import snapshot

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)
STAMP = NOW.timestamp()


def node(function, value):
    code = "const q=require(process.argv[1]);process.stdout.write(JSON.stringify(q[process.argv[2]](JSON.parse(process.argv[3]),Number(process.argv[4]))));"
    result = subprocess.run(["node", "-e", code, str(quota.SCRIPT), function, json.dumps(value), str(STAMP)],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def codex(percent=15, **extra):
    return {"rateLimits": {"primary": {"usedPercent": percent, "windowDurationMins": 300, "resetsAt": STAMP + 3600},
                           "secondary": {"usedPercent": 45, "windowDurationMins": 10080, "resetsAt": STAMP + 86400}, **extra}}


def claude():
    return [{"type": "assistant", "usage_report": {"rate_limits": {"limits": [
        {"kind": "session", "percent": 20, "resets_at": (NOW + timedelta(hours=1)).isoformat()},
        {"kind": "weekly_all", "percent": 30, "resets_at": (NOW + timedelta(days=2)).isoformat()},
        {"kind": "weekly_scoped", "percent": 99, "scope": {"model": {"display_name": "Sonnet"}},
         "resets_at": (NOW + timedelta(days=2)).isoformat()}]}}},
        {"type": "result", "local_command": "usage", "num_turns": 0, "duration_api_ms": 0, "modelUsage": {}, "is_error": False}]


def test_codex_reads_duration_mapped_global_bucket_and_discards_other_data():
    value = codex()
    value["rateLimitsByLimitId"] = {"codex": value["rateLimits"], "other": {"secret": "must not escape"}}
    result = node("codexSnapshot", value)
    assert result["status"] == "OK" and [w["window"] for w in result["windows"]] == ["session", "week"]
    assert result["windows"][1]["used_percent"] == 45
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("value", [codex(True), codex(-1), codex(101), codex(limitId="model-specific"), {},
                                   codex(primary={"usedPercent": 3, "windowDurationMins": 15, "resetsAt": STAMP + 3600}, secondary=None)])
def test_codex_rejects_invalid_or_unmapped_allowances(value):
    assert node("codexSnapshot", value)["status"] == "INVALID_RESPONSE"


def test_claude_reads_only_global_plan_windows_and_checks_zero_inference():
    result = node("claudeSnapshot", claude())
    assert result["status"] == "OK" and [w["used_percent"] for w in result["windows"]] == [20, 30]


@pytest.mark.parametrize("key,value", [("local_command", "unknown"), ("num_turns", 1), ("duration_api_ms", 25),
                                      ("is_error", True), ("modelUsage", {"opus": {"inputTokens": 1}})])
def test_claude_probe_must_be_a_successful_local_usage_command(key, value):
    events = claude()
    events[-1][key] = value
    assert node("claudeSnapshot", events)["status"] == "INVALID_RESPONSE"


def test_claude_missing_profile_is_explicit_and_missing_week_is_not_fabricated():
    assert node("claudeSnapshot", [claude()[-1]]) == {"status": "PROFILE_REQUIRED", "windows": []}
    value = claude()
    value[0]["usage_report"]["rate_limits"]["limits"] = value[0]["usage_report"]["rate_limits"]["limits"][:1]
    result = node("claudeSnapshot", value)
    assert len(result["windows"]) == 1 and result["windows"][0]["window"] == "session"


@pytest.mark.parametrize("field,value", [("used_percent", float("nan")), ("used_percent", True), ("used_percent", 101),
                                        ("resets_at", STAMP - 1), ("observed_at", STAMP + 1)])
def test_boundary_revalidates_numeric_values_and_times(field, value):
    result = {"status": "OK", "windows": [{"window": "session", "used_percent": 20, "resets_at": STAMP + 1000,
                                            "observed_at": STAMP - 1}]}
    result["windows"][0][field] = value
    assert snapshot(result, now=NOW)["status"] == "INVALID_RESPONSE"


def test_boundary_drops_untrusted_fields_even_on_errors():
    assert snapshot({"status": "PROFILE_REQUIRED", "windows": [{"token": "private"}], "error": "private"}) == {
        "status": "PROFILE_REQUIRED", "windows": []}
    assert snapshot(["not a response"])["status"] == "INVALID_RESPONSE"


@pytest.fixture
def ops():
    client = MagicMock()
    client.volumes.get.return_value.attrs = {"Labels": {"ho.credential": "codex/qa"}}
    image = SimpleNamespace(id="sha256:" + "a" * 64, labels={"org.hermes-orchestrator.provider": "codex"})
    proxy = MagicMock()
    value = SimpleNamespace(client=client, _lock=threading.Lock(), _quota_locks={p: threading.Lock() for p in ("codex", "claude")},
                            platform={"machine": {"provider_domains": {"codex": ["chatgpt.com"]}}},
                            ensure_image=MagicMock(return_value=image), _find=MagicMock(return_value=None),
                            _check_capacity=MagicMock(), _ensure_network=MagicMock(), _start_proxy=MagicMock(return_value=proxy))
    client.containers.create.return_value.wait.return_value = {"StatusCode": 0}
    client.containers.create.return_value.logs.return_value = json.dumps({"status": "OK", "windows": [
        {"window": "week", "used_percent": 40, "resets_at": STAMP + 86400, "observed_at": STAMP - 1}]}).encode()
    return value


def test_probe_container_has_one_identity_no_project_and_only_proxy_network(ops):
    result = quota.refresh(ops, "codex", "qa")
    assert result["status"] == "OK"
    _, kwargs = ops.client.containers.create.call_args
    assert kwargs["entrypoint"] == ["/opt/ho/pen-node/bin/node"]
    assert kwargs["working_dir"] == "/tmp" and kwargs["user"] == "10001:10001"
    assert kwargs["read_only"] and not kwargs["privileged"] and kwargs["cap_drop"] == ["ALL"]
    assert len(kwargs["mounts"]) == 1 and kwargs["mounts"][0]["Source"] == "cred-codex-qa"
    assert kwargs["network_mode"].startswith("ho-e-") and kwargs["dns"] == ["127.0.0.1"]
    assert "ho.execution" not in kwargs["labels"]  # recovery must not treat probes as orphan executions
    assert kwargs["environment"]["HO_QUOTA_PROBE"] == "1" and "HTTPS_PROXY" in kwargs["environment"]
    ops.client.containers.create.return_value.remove.assert_called_once_with(force=True, v=True)
    ops._start_proxy.return_value.remove.assert_called_once()
    ops._ensure_network.return_value.remove.assert_called_once()


def test_timeout_cleans_every_resource_and_returns_no_provider_output(ops):
    ops.client.containers.create.return_value.wait.side_effect = ReadTimeout("private upstream content")
    assert quota.refresh(ops, "codex", "qa") == {"status": "TIMEOUT", "windows": []}
    ops.client.containers.create.return_value.remove.assert_called_once()
    ops._start_proxy.return_value.remove.assert_called_once()
    ops._ensure_network.return_value.remove.assert_called_once()


def test_wrong_identity_or_absent_credential_never_creates_a_container(ops):
    ops.client.volumes.get.return_value.attrs = {"Labels": {"ho.credential": "codex/other"}}
    assert quota.refresh(ops, "codex", "qa")["status"] == "AUTH_REQUIRED"
    ops.client.volumes.get.side_effect = NotFound("absent")
    assert quota.refresh(ops, "codex", "qa")["status"] == "AUTH_REQUIRED"
    ops.client.containers.create.assert_not_called()


def test_concurrent_probe_joins_existing_work_and_bad_identity_is_rejected(ops):
    ops._quota_locks["codex"].acquire()
    assert quota.refresh(ops, "codex", "qa")["status"] == "BUSY"
    ops._quota_locks["codex"].release()
    ops._find.return_value = MagicMock()
    assert quota.refresh(ops, "codex", "qa")["status"] == "BUSY"
    with pytest.raises(Rejected):
        quota.refresh(ops, "codex", "../default")
    ops.client.containers.create.assert_not_called()
