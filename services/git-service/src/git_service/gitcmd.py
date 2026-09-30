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


IDENTITY = ("Hermes Orchestrator", "hermes-orchestrator@localhost")


def _environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HO_GIT_HOME", "/nonexistent"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",  # read-only operations must not write the index
        "GIT_PAGER": "cat",
        "GIT_AUTHOR_NAME": IDENTITY[0], "GIT_AUTHOR_EMAIL": IDENTITY[1],
        "GIT_COMMITTER_NAME": IDENTITY[0], "GIT_COMMITTER_EMAIL": IDENTITY[1],
        "LC_ALL": "C",
    }
    if os.environ.get("GH_CONFIG_DIR"):
        env["GH_CONFIG_DIR"] = os.environ["GH_CONFIG_DIR"]
    env.update(extra or {})
    return env


def run(repo: Path, *args: str, protocols: str = "never", config: tuple[str, ...] = (), trusted: tuple[Path, ...] = (),
        env: dict[str, str] | None = None, timeout: int = GIT_TIMEOUT_SECONDS, input_text: str | None = None,
        ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    """Run git with the hardening above. `protocols` names the transports this call may use
    ("never" for local-only work); `trusted` adds other exact safe.directory paths (for example
    the untrusted clone a fetch reads from, which is never checked out or run here)."""
    command = ["git", "-c", f"safe.directory={repo}"]
    for path in trusted:
        command += ["-c", f"safe.directory={path}"]
    command += [
        "-c", f"core.hooksPath={_EMPTY_HOOKS_DIR}",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "diff.external=",
        "-c", "protocol.allow=never",
    ]
    for proto in ([] if protocols == "never" else protocols.split(",")):
        command += ["-c", f"protocol.{proto}.allow=always"]
    for item in config:
        command += ["-c", item]
    command += ["-C", str(repo), *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=_environment(env),
                                check=False, input=input_text)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc
    if result.returncode not in ok_codes:
        raise GitError(result.stderr.strip()[:500] or f"git {args[0]} failed")
    return result


def git(repo: Path, *args: str, **kwargs) -> str:
    return run(repo, *args, **kwargs).stdout.strip()


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
