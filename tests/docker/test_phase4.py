"""Phase 4 against a real Docker daemon: provider images, inputs, secrets, session stores,
credential mounts, and the real CLIs reaching their providers only through the egress proxy."""

from __future__ import annotations

import base64
import json

import pytest

from conftest import credential_volume  # type: ignore[import-not-found]
from ho_core.adapters import AgentAssignment, ClaudeAdapter, CodexAdapter, FailureClass, OutputBundle
from ho_core.enums import Role

D = Role.DEVELOPER
SECRET = "postgres://app:s3cr3t-Value-42@db:5432/app"


def bundle(result: dict) -> OutputBundle:
    return OutputBundle({k: base64.b64decode(v) for k, v in result["files"].items()}, logs=result["logs"],
                        exit_code=result["status"]["exit_code"])


def agent_body(api, adapter, *, egress="PROVIDER_ONLY", identity="hotest", **kwargs):
    body = api.request(D, "true", provider=adapter.provider, identity=identity, workspace="WRITE", git="LOCAL_COMMIT",
                       egress=egress, **kwargs)
    body["workspace"] = "proj-a/.hermes/worktrees/w1"
    plan = adapter.build_execution(AgentAssignment(role=D, prompt="Reply with a completed result.", workspace="WRITE",
                                                   git="LOCAL_COMMIT", egress=egress, max_turns=2))
    body.update(image=plan.image, command=plan.command, inputs=plan.inputs, session=True)
    return body


# ------------------------------------------------------------------ images and mounts


def test_provider_images_run_their_cli_with_the_right_credential_mount(api, client):
    credential_volume(client, "claude", "hotest", {"oauth_token": "not-a-real-token"})
    body = api.request(D, """
        claude --version; codex --version 2>/dev/null || echo no-codex
        test -r /run/ho-credentials/claude/oauth_token && echo token=readable
        touch /run/ho-credentials/claude/x 2>/dev/null && echo cred=writable || echo cred=read-only
        test -z "$(ls -A /run/ho-credentials/codex)" && echo other-provider=empty || echo other-provider=mounted
        """, provider="claude", egress="PROVIDER_ONLY", workspace="NONE", workspace_path=None)
    logs = api.run(body)["logs"]
    for expected in ("2.1.280 (Claude Code)", "no-codex", "token=readable", "cred=read-only", "other-provider=empty"):
        assert expected in logs, logs


def test_codex_credential_mount_is_writable_for_token_refresh(api, client, credential):
    body = api.request(D, "codex --version; touch /run/ho-credentials/codex/x && echo cred=writable",
                       egress="NONE", workspace="NONE", workspace_path=None)
    logs = api.run(body)["logs"]
    assert "codex-cli 0.159.2" in logs and "cred=writable" in logs


@pytest.mark.parametrize("image,provider", [("claude-generic", "codex"), ("codex-generic", None), ("agent-base", "codex")])
def test_image_must_match_the_granted_provider(api, credential, image, provider):
    role = D if provider else Role.TESTER
    body = api.request(role, "true", provider=provider, egress="NONE", workspace="NONE", workspace_path=None)
    body["image"] = image
    response = api.create(body)
    assert response.status_code == 403 and "provider" in response.json()["message"]


def test_input_files_are_delivered(api, credential):
    body = api.request(D, "cat /run/ho-input/prompt.md; stat -c '%u' /run/ho-input/prompt.md", egress="NONE",
                       workspace="NONE", workspace_path=None)
    body["inputs"] = {"prompt.md": "Implement the login page."}
    logs = api.run(body)["logs"]
    assert "Implement the login page." in logs and "10001" in logs
    body = api.request(D, "true", egress="NONE", workspace="NONE", workspace_path=None)
    body["inputs"] = {"../escape": "x"}
    assert api.create(body).status_code == 403


# ------------------------------------------------------------------------ secrets


def test_file_secrets_are_in_memory_mode_0600_and_redacted(api, client, credential, secrets_dir):
    (secrets_dir / "proj-a" / "test" / "DB_URL").write_text(SECRET + "\n")
    body = api.request(D, """
        cat /run/ho/secrets/DB_URL; echo
        stat -c 'mode=%a uid=%u' /run/ho/secrets/DB_URL
        grep ' /run/ho/secrets ' /proc/mounts | cut -d' ' -f3
        env | grep -c s3cr3t || true
        cp /run/ho/secrets/DB_URL /output/leak.txt
        """, egress="NONE", workspace="NONE", workspace_path=None)
    body["grant"]["capabilities"]["secrets"] = ["proj-a/test/DB_URL"]
    result = api.run(body)
    logs = result["logs"]
    assert SECRET not in logs and "[REDACTED:DB_URL]" in logs
    assert "mode=600 uid=10001" in logs and "tmpfs" in logs
    assert "\n0\n" in logs  # not in the environment
    assert base64.b64decode(result["files"]["leak.txt"]) == b"[REDACTED:DB_URL]"
    container = client.containers.get(f"ho-w-{body['execution_id'].replace('-', '')[-12:]}")
    assert SECRET not in json.dumps(container.attrs)  # not in the container's configuration


def test_env_secrets_and_missing_secrets(api, credential, secrets_dir):
    (secrets_dir / "proj-a" / "test" / "NPM_TOKEN").write_text("npm_abcdefghij0123456789")
    body = api.request(D, 'echo "token=$NPM_TOKEN"; test -e /run/ho/secrets && echo files || echo no-files',
                       egress="NONE", workspace="NONE", workspace_path=None)
    body["grant"]["capabilities"]["secrets"] = ["proj-a/test/NPM_TOKEN"]
    body["secret_env"] = ["proj-a/test/NPM_TOKEN"]
    logs = api.run(body)["logs"]
    assert "npm_abcdefghij0123456789" not in logs and "[REDACTED:NPM_TOKEN]" in logs and "no-files" in logs
    missing = api.request(D, "true", egress="NONE", workspace="NONE", workspace_path=None)
    missing["grant"]["capabilities"]["secrets"] = ["proj-a/test/NOT_SET"]
    response = api.create(missing)
    assert response.status_code == 403 and "not set" in response.json()["message"]
    other = api.request(D, "true", egress="NONE", workspace="NONE", workspace_path=None)
    other["grant"]["capabilities"]["secrets"] = ["proj-b/test/NPM_TOKEN"]
    assert api.create(other).status_code == 403


# -------------------------------------------------------------------- session store


def test_session_store_persists_within_the_task_and_is_removed_with_it(api, client, credential):
    first = api.request(D, "echo saved > /run/ho-sessions/marker && echo written", egress="NONE", workspace="NONE",
                        workspace_path=None)
    first["session"] = True
    assert "written" in api.run(first)["logs"]
    second = api.request(D, "cat /run/ho-sessions/marker", task=first["task"], egress="NONE", workspace="NONE", workspace_path=None)
    second["session"] = True
    assert "saved" in api.run(second)["logs"]
    other = api.request(D, "cat /run/ho-sessions/marker 2>/dev/null || echo empty", egress="NONE", workspace="NONE",
                        workspace_path=None)
    other["session"] = True
    assert "empty" in api.run(other)["logs"]  # another task has its own store
    for execution in (first["execution_id"], second["execution_id"]):
        api.client.delete(f"/v1/executions/{execution}", headers=api.headers)
    volume = f"ho-sess-{first['task'].lower()}-codex"
    assert client.volumes.get(volume)
    api.client.delete(f"/v1/tasks/{first['task']}/environment", headers=api.headers)
    assert not client.volumes.list(filters={"name": volume})


def test_credentials_and_images_are_listed_without_contents(api, credential):
    listed = api.client.get("/v1/credentials", headers=api.headers).json()["credentials"]
    assert any(c["provider"] == "codex" and c["identity"] == "hotest" and c["volume"] == "cred-codex-hotest" for c in listed)
    assert "oauth_token" not in json.dumps(listed)
    images = api.client.get("/v1/images", headers=api.headers).json()
    assert "codex-generic" in images["images"] and images["versions"]["codex"] == "0.159.2"


# ---------------------------------------------------------- real CLIs, no real login


def test_runner_reports_a_missing_login(api, client):
    credential_volume(client, "codex", "empty")
    body = agent_body(api, CodexAdapter(), identity="empty")
    result = CodexAdapter().collect_result(bundle(api.run(body)))
    assert result.failure_class == FailureClass.AUTH and result.error == "credential_missing"


def test_codex_refresh_is_written_back_only_when_the_store_is_unchanged(api, client, projects_root):
    """The runner's write-back logic, with a stand-in `codex` that rewrites auth.json like a token refresh."""
    credential_volume(client, "codex", "refresh", {"auth.json": '{"tokens": "old"}'})
    fake_bin = projects_root / "proj-a" / ".hermes" / "worktrees" / "w1" / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    (fake_bin / "codex").write_text('#!/bin/bash\nprintf \'{"tokens": "new"}\' > "$CODEX_HOME/auth.json"\n'
                                    'printf \'%s\\n\' \'{"type":"thread.started","thread_id":"t1"}\'\n')
    (fake_bin / "codex").chmod(0o755)
    body = agent_body(api, CodexAdapter(), identity="refresh", egress="NONE")
    body["env"] = {"PATH": "/workspace/bin:/usr/local/bin:/usr/bin:/bin"}
    result = api.run(body)
    assert base64.b64decode(result["files"]["ho/credential.txt"]).strip() == b"refreshed"
    check = api.request(D, "cat /run/ho-credentials/codex/auth.json", identity="refresh", egress="NONE",
                        workspace="NONE", workspace_path=None)
    assert '{"tokens": "new"}' in api.run(check)["logs"]


@pytest.mark.internet
def test_real_codex_reaches_its_provider_only_through_the_proxy(api, client):
    """Codex 0.159.2 with an invalid ChatGPT login: the request must reach OpenAI through the
    PROVIDER_ONLY proxy (official domains in config/defaults.yaml) and fail as AUTH."""
    fake_login = json.dumps({"tokens": {"id_token": "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6ImFAYi5jIn0.x",
                                        "access_token": "invalid", "refresh_token": "invalid", "account_id": "a"},
                             "last_refresh": "2026-09-01T00:00:00Z"})
    credential_volume(client, "codex", "bogus", {"auth.json": fake_login})
    result = api.run(agent_body(api, CodexAdapter(), identity="bogus"), timeout=240)
    parsed = CodexAdapter().collect_result(bundle(result))
    assert parsed.failure_class == FailureClass.AUTH, parsed
    assert parsed.session_id  # the session started before the provider rejected the login
    allowed = {e.get("host") for e in result["egress"] if e.get("event") == "EGRESS_ALLOWED"}
    print("codex egress:", sorted(allowed), sorted({e.get("host") for e in result["egress"] if e.get("event") == "EGRESS_DENIED"}))
    assert allowed & {"chatgpt.com", "api.openai.com", "auth.openai.com"}, result["egress"]


@pytest.mark.internet
def test_real_claude_reaches_its_provider_only_through_the_proxy(api, client):
    """Claude Code 2.1.280 with an invalid subscription token: the request must reach Anthropic
    through the PROVIDER_ONLY proxy and fail as AUTH."""
    credential_volume(client, "claude", "bogus", {"oauth_token": "sk-ant-oat01-invalid"})
    result = api.run(agent_body(api, ClaudeAdapter(), identity="bogus"), timeout=240)
    parsed = ClaudeAdapter().collect_result(bundle(result))
    assert parsed.failure_class == FailureClass.AUTH, parsed
    assert "sk-ant-oat01-invalid" not in result["logs"]
    allowed = {e.get("host") for e in result["egress"] if e.get("event") == "EGRESS_ALLOWED"}
    print("claude egress:", sorted(allowed), sorted({e.get("host") for e in result["egress"] if e.get("event") == "EGRESS_DENIED"}))
    assert "api.anthropic.com" in allowed, result["egress"]
