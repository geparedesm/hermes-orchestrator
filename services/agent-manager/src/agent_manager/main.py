"""agent-manager entry point."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from ho_core.config import load_platform_config
from ho_core.logs import configure

from .app import create_app
from .docker_ops import DockerOps
from .secrets import SecretStore


def main() -> None:
    configure("agent-manager", os.environ.get("HO_LOG_LEVEL", "INFO"))
    config_dir = Path(os.environ.get("HO_CONFIG_DIR", "/app/config"))
    platform = load_platform_config(config_dir, os.environ["HO_MACHINE_PROFILE"])
    token = Path(os.environ["HO_AGENT_MANAGER_TOKEN_FILE"]).read_text().strip()
    projects_root = Path(os.environ.get("HO_PROJECTS_ROOT", "/projects"))
    projects_root_host = os.environ["HO_PROJECTS_ROOT_HOST"]
    if not projects_root.is_dir():
        raise SystemExit(f"projects root {projects_root} is not mounted")
    secrets_dir = Path(os.environ.get("HO_PROJECT_SECRETS_DIR", "/var/lib/ho/project-secrets"))
    ops = DockerOps(platform, config_dir, secrets=SecretStore(secrets_dir if secrets_dir.is_dir() else None))
    app = create_app(ops, token=token, projects_root=projects_root,
                     projects_root_host=projects_root_host)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("HO_PORT", "8082")), log_config=None,
                access_log=False, server_header=False)


if __name__ == "__main__":
    main()
