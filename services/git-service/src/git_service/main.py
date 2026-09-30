"""git-service entry point."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from ho_core.logs import configure

from .app import create_app


def main() -> None:
    configure("git-service", os.environ.get("HO_LOG_LEVEL", "INFO"))
    token = Path(os.environ["HO_GIT_SERVICE_TOKEN_FILE"]).read_text().strip()
    root = Path(os.environ.get("HO_PROJECTS_ROOT", "/projects"))
    if not root.is_dir():
        raise SystemExit(f"projects root {root} is not mounted")
    uvicorn.run(create_app(root, token), host="0.0.0.0", port=int(os.environ.get("HO_PORT", "8081")),
                log_config=None, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
