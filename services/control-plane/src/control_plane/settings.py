"""Runtime settings from environment variables and Docker secret files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from ho_core.config import load_platform_config


def _read_secret(path: str | None) -> str | None:
    if not path:
        return None
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"secret file {path} is empty")
    return value


@dataclass
class Settings:
    database_url: str
    redis_url: str | None
    config_dir: Path
    machine_profile: str
    artifact_dir: Path
    git_service_url: str
    git_service_token: str
    agent_manager_url: str | None = None
    agent_manager_token: str | None = None
    # token -> identity name
    tokens: dict[str, str] = field(default_factory=dict)
    projects_root_host: str | None = None
    run_scheduler: bool = True
    log_level: str = "INFO"
    bind_host: str = "0.0.0.0"
    bind_port: int = 8080
    # Provider identity whose credential volume executions use (cred-<provider>-<identity>).
    provider_identity: str = "default"
    merge_key: bytes = b""
    # Phase 7: lead READY tasks with the multi-agent orchestrator (off: tasks wait for manual work).
    orchestration: bool = False
    # Outbox delivery to Hermes (Phase 9 wires the receiving side); unset: notifications wait in the outbox.
    hermes_webhook_url: str | None = None
    hermes_webhook_token: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        database_url = env.get("HO_DATABASE_URL")
        if not database_url:
            password = _read_secret(env.get("HO_DB_PASSWORD_FILE"))
            database_url = (
                f"postgresql://{env.get('HO_DB_USER', 'ho_app')}:{quote(password or '', safe='')}"
                f"@{env.get('HO_DB_HOST', 'postgres')}:5432/{env.get('HO_DB_NAME', 'ho')}"
            )
        redis_url = env.get("HO_REDIS_URL")
        if not redis_url and env.get("HO_REDIS_PASSWORD_FILE"):
            password = _read_secret(env["HO_REDIS_PASSWORD_FILE"])
            redis_url = f"redis://:{quote(password or '', safe='')}@{env.get('HO_REDIS_HOST', 'redis')}:6379/0"

        tokens: dict[str, str] = {}
        for identity, variable in (("hermes-plugin", "HO_PLUGIN_TOKEN_FILE"), ("operator", "HO_OPERATOR_TOKEN_FILE")):
            token = _read_secret(env.get(variable))
            if token:
                tokens[token] = identity

        return cls(
            database_url=database_url,
            redis_url=redis_url,
            config_dir=Path(env.get("HO_CONFIG_DIR", "/app/config")),
            machine_profile=env.get("HO_MACHINE_PROFILE", "linux"),
            artifact_dir=Path(env.get("HO_ARTIFACT_DIR", "/var/lib/ho/artifacts")),
            git_service_url=env.get("HO_GIT_SERVICE_URL", "http://git-service:8081"),
            git_service_token=_read_secret(env.get("HO_GIT_SERVICE_TOKEN_FILE")) or "",
            agent_manager_url=env.get("HO_AGENT_MANAGER_URL"),
            agent_manager_token=_read_secret(env.get("HO_AGENT_MANAGER_TOKEN_FILE")),
            tokens=tokens,
            projects_root_host=env.get("HO_PROJECTS_ROOT_HOST"),
            run_scheduler=env.get("HO_RUN_SCHEDULER", "true").lower() == "true",
            log_level=env.get("HO_LOG_LEVEL", "INFO"),
            bind_port=int(env.get("HO_PORT", "8080")),
            provider_identity=env.get("HO_PROVIDER_IDENTITY") or "default",
            merge_key=(_read_secret(env.get("HO_MERGE_KEY_FILE")) or "").encode(),
            orchestration=env.get("HO_ORCHESTRATION", "false").lower() == "true",
            hermes_webhook_url=env.get("HO_HERMES_WEBHOOK_URL") or None,
            hermes_webhook_token=_read_secret(env.get("HO_HERMES_WEBHOOK_TOKEN_FILE")),
        )

    def platform_config(self) -> dict:
        config = load_platform_config(self.config_dir, self.machine_profile)
        if self.projects_root_host:
            # The Compose mount and the control plane must agree on the root.
            config["platform"]["projects_root_host"] = self.projects_root_host
        return config
