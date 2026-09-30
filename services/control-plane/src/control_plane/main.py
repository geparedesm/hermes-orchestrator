"""control-plane entry point."""

from __future__ import annotations

import uvicorn
from ho_core.logs import configure

from .agentmgr import AgentManagerClient
from .app import build_services, create_app
from .artifacts import ArtifactStore
from .auth import Authenticator
from .context import Context
from .coordination import Coordinator
from .db import Database
from .gitsvc import GitServiceClient
from .settings import Settings


def build_app(settings: Settings):
    if not settings.tokens:
        raise RuntimeError("no API tokens configured (HO_PLUGIN_TOKEN_FILE / HO_OPERATOR_TOKEN_FILE)")
    db = Database(settings.database_url)
    db.open()
    ctx = Context(
        db=db,
        platform=settings.platform_config(),
        coordinator=Coordinator(settings.redis_url),
        artifacts=ArtifactStore(settings.artifact_dir),
        git=GitServiceClient(settings.git_service_url, settings.git_service_token),
        agents=(AgentManagerClient(settings.agent_manager_url, settings.agent_manager_token or "")
                if settings.agent_manager_url else None),
        provider_identity=settings.provider_identity,
    )
    services = build_services(ctx, Authenticator(settings.tokens), run_scheduler=settings.run_scheduler)
    return create_app(services)


def main() -> None:
    settings = Settings.from_env()
    configure("control-plane", settings.log_level)
    uvicorn.run(build_app(settings), host=settings.bind_host, port=settings.bind_port, log_config=None,
                access_log=False, proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
