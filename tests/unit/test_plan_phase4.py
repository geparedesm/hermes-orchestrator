"""Agent Manager planning for Phase 4 (pure): inputs, secrets, session stores, credential mounts."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_manager.plan import Rejected, build_plan
from agent_manager.secrets import SecretStore
from ho_core.config import build_project_config, load_platform_config
from ho_core.enums import Role
from ho_core.policy.engine import GrantRequest, evaluate_grant

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def platform():
    return load_platform_config(ROOT / "config", "mac-m2-pro")


def request(platform, role=Role.DEVELOPER, provider="codex", image=None, **extra):
    execution = str(uuid.uuid4())
    project_yaml = {"version": 1, "project": {"name": "demo"},
                    "secrets": [{"name": "DB_URL", "environment": "test"}, {"name": "NPM_TOKEN", "environment": "test"}]}
    config = build_project_config(platform, project_yaml).data
    grant, _ = evaluate_grant(GrantRequest(grant_id=f"G-{execution[:8]}", project="demo", task="T-1", execution=execution,
                                           worker="w-1-a", role=role, provider=provider, secrets=["DB_URL", "NPM_TOKEN"]),
                              config, platform, now=datetime.now(timezone.utc))
    body = {"execution_id": execution, "task": "T-1", "project": "demo", "role": role.value, "project_path": "demo",
            "image": image or (f"{provider}-generic" if provider else "agent-base"), "command": ["true"], "grant": grant}
    body.update(extra)
    return body


def plan(platform, body, tmp_path):
    return build_plan(body, platform=platform, projects_root=tmp_path, projects_root_host=str(tmp_path))


def test_credential_mounts_per_provider(platform, tmp_path):
    codex = plan(platform, request(platform), tmp_path)
    mount = next(m for m in codex.mounts if m.source == "cred-codex-default")
    assert mount.target == "/run/ho-credentials/codex" and not mount.read_only  # Codex refreshes its login
    claude = plan(platform, request(platform, provider="claude"), tmp_path)
    mount = next(m for m in claude.mounts if m.source == "cred-claude-default")
    assert mount.target == "/run/ho-credentials/claude" and mount.read_only
    assert "api.anthropic.com" in claude.allowed_domains and "chatgpt.com" not in claude.allowed_domains
    assert set(codex.allowed_domains) == {"chatgpt.com", "auth.openai.com", "api.openai.com"}


@pytest.mark.parametrize("provider,image", [("codex", "claude-generic"), ("codex", "agent-base"), (None, "codex-node")])
def test_provider_images_need_the_matching_grant(platform, tmp_path, provider, image):
    role = Role.DEVELOPER if provider else Role.TESTER
    with pytest.raises(Rejected, match="provider"):
        plan(platform, request(platform, role=role, provider=provider, image=image), tmp_path)


def test_secrets_are_planned_by_reference(platform, tmp_path):
    body = request(platform, secret_env=["demo/test/NPM_TOKEN"])
    result = plan(platform, body, tmp_path)
    assert result.secrets == {"demo/test/DB_URL": ("DB_URL", "file"), "demo/test/NPM_TOKEN": ("NPM_TOKEN", "env")}
    assert result.labels["ho.secrets"] == "demo/test/DB_URL,demo/test/NPM_TOKEN"
    with pytest.raises(Rejected):
        plan(platform, request(platform, secret_env=["demo/test/OTHER"]), tmp_path)
    with pytest.raises(Rejected):
        plan(platform, request(platform, env={"DB_URL": "override"}), tmp_path)  # cannot shadow a secret
    body = request(platform)
    body["grant"]["capabilities"]["secrets"] = ["other/test/DB_URL"]
    with pytest.raises(Rejected, match="another project"):
        plan(platform, body, tmp_path)


def test_inputs_and_session(platform, tmp_path):
    result = plan(platform, request(platform, inputs={"prompt.md": "hi"}, session=True), tmp_path)
    assert result.inputs == {"prompt.md": "hi"} and result.session_volume == "ho-sess-t-1-codex"
    assert any(m.target == "/run/ho-sessions" and m.source == "ho-sess-t-1-codex" for m in result.mounts)
    for bad in ({"../x": "a"}, {"prompt.md": "x" * (600 * 1024)}):
        with pytest.raises(Rejected):
            plan(platform, request(platform, inputs=bad), tmp_path)
    with pytest.raises(Rejected, match="session"):
        plan(platform, request(platform, role=Role.TESTER, provider=None, session=True), tmp_path)


def test_secret_store(tmp_path):
    store = SecretStore(tmp_path)
    (tmp_path / "demo" / "test").mkdir(parents=True)
    (tmp_path / "demo" / "test" / "DB_URL").write_text("postgres://u:pw-123456@db/x\n")
    assert store.read("demo/test/DB_URL") == "postgres://u:pw-123456@db/x"
    (tmp_path / "demo" / "test" / "LINK").symlink_to("/etc/hosts")
    for ref in ("demo/test/MISSING", "demo/test/LINK", "../etc/test/X", "demo/prod/DB_URL"):
        with pytest.raises(Rejected):
            store.read(ref)
    with pytest.raises(Rejected, match="not mounted"):
        SecretStore(None).read("demo/test/DB_URL")
    values = store.values_for(["demo/test/DB_URL", "demo/test/MISSING"])
    assert store.redact("url=postgres://u:pw-123456@db/x", values) == "url=[REDACTED:DB_URL]"
    assert store.redact_bytes(b"postgres://u:pw-123456@db/x", values) == b"[REDACTED:DB_URL]"
