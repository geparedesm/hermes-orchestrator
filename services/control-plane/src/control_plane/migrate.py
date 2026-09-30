"""Apply database migrations (run by the one-shot `migrate` Compose service)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    directory = Path(os.environ.get("HO_MIGRATIONS_DIR", "/app/migrations"))
    config = Config(str(directory / "alembic.ini"))
    config.set_main_option("script_location", str(directory))
    target = sys.argv[1] if len(sys.argv) > 1 else "head"
    command.upgrade(config, target)
    print(f"migrations applied up to {target}")


if __name__ == "__main__":
    main()
