"""Hardened Git invocation (SECURITY_MODEL.md section 9).

Repository configuration is attacker-controllable, so every command:
- ignores system and global configuration;
- disables hooks and fsmonitor (both can execute programs);
- trusts only the exact repository path (safe.directory), never '*';
- never prompts and never uses a pager or external diff/textconv tools.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 30
_EMPTY_HOOKS_DIR = "/nonexistent-hooks"


class GitError(RuntimeError):
    pass


def _environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",  # read-only operations must not write the index
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }


def git(repo: Path, *args: str) -> str:
    command = [
        "git",
        "-c", f"safe.directory={repo}",
        "-c", f"core.hooksPath={_EMPTY_HOOKS_DIR}",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "diff.external=",
        "-c", "protocol.allow=never",  # no network or file transports from read paths
        "-C", str(repo),
        *args,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
                                env=_environment(), check=False)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc
    if result.returncode != 0:
        raise GitError(result.stderr.strip()[:500] or f"git {args[0]} failed")
    return result.stdout.strip()


def try_git(repo: Path, *args: str) -> str | None:
    try:
        return git(repo, *args) or None
    except GitError:
        return None


_CREDENTIALS_IN_URL = re.compile(r"^(?P<scheme>[a-z+]+://)[^@/]+@", re.IGNORECASE)


def sanitize_remote(url: str | None) -> str | None:
    """Drop embedded credentials (https://user:token@host/...) before the URL leaves this service."""
    if not url:
        return None
    return _CREDENTIALS_IN_URL.sub(r"\g<scheme>", url)


def inspect(repo: Path) -> dict[str, str | bool | None]:
    if not (repo / ".git").exists():
        return {"is_git": False}
    head = try_git(repo, "rev-parse", "--verify", "HEAD")
    branch = try_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    remote_head = try_git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    default_branch = remote_head.split("/", 1)[1] if remote_head and "/" in remote_head else branch
    return {
        "is_git": True,
        "head": head,
        "branch": branch,
        "default_branch": default_branch,
        "remote": sanitize_remote(try_git(repo, "config", "--get", "remote.origin.url")),
    }
