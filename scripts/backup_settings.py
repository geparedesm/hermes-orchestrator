"""Export only known, single-line Compose settings; never source the operator's .env."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

ALLOWED = frozenset({
    "COMPOSE_PROJECT_NAME", "HO_MACHINE_PROFILE", "HO_PROJECTS_ROOT_HOST", "HO_VERSION",
    "HO_LOG_LEVEL", "HO_DOCKER_GID", "HO_PROJECT_SECRETS_HOST", "HO_PROVIDER_IDENTITY",
    "HO_ORCHESTRATION", "HO_APPROVERS", "HO_HERMES_DASHBOARD_PORT", "HO_HERMES_DELIVER",
    "HO_HERMES_DELIVER_CHAT_ID", "HO_HERMES_WEBHOOK_URL", "HO_NOTIFY_LANGUAGE",
    "TELEGRAM_ALLOWED_USERS",
})
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def settings(text: str) -> str:
    output = []
    pending = ""
    for line in text.splitlines():
        if pending:
            pending += "\n" + line
            try:
                shlex.split(pending, comments=True)
            except ValueError:
                continue
            pending = ""
            continue  # discard the entire multiline assignment, including its continuation
        match = ASSIGNMENT.match(line)
        if not match:
            continue
        key, value = match.groups()
        try:
            shlex.split(value, comments=True)
        except ValueError:
            pending = value
            continue
        if key in ALLOWED:
            output.append(f"{key}={value}\n")
    return "".join(output)


if __name__ == "__main__":
    sys.stdout.write(settings(Path(sys.argv[1]).read_text(encoding="utf-8")))
