"""Child projects created from a parent's template (docs/design/child-projects.md).

Every call is bound to a signed, approved subject that pins the parent commit, the template tree, the destination,
and the GitHub repository. Steps are idempotent and prove ownership before reusing what they find:

- `plan` reads, writes nothing: it pins the parent HEAD and the template tree and checks the destination.
- `publish` renders the template from Git objects into a new directory created exclusively, without following
  symbolic links, then makes the first commit with the operation id as a trailer (the evidence a retry looks for).
- `remote` creates the GitHub repository (described with the operation id), adopts it on a retry only with that
  evidence, and pushes `main` only onto an absent branch or the same commit.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from ho_core.gitpolicy import CHILD_NAME, valid_sha

from . import github
from .gitcmd import GitError, git, popen, run, try_git
from .repo_ops import Refused

MAX_FILES = 200
MAX_BYTES = 2 * 1024 * 1024
RESERVED = {".git", ".hermes"}
PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,31})\s*\}\}")
TRAILER = "Hermes-Operation"


class Unowned(Refused):
    """Something already exists where the creation would write, without this operation's evidence: a person
    decides (NEEDS_RECOVERY); nothing is adopted or deleted."""


# ---------------------------------------------------------------- destination


def _relative_parts(value: str, what: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    parts = path.parts
    if not value or path.is_absolute() or not parts or any(p in ("", ".", "..") for p in parts):
        raise Refused(f"{what} must be a relative path inside the parent project")
    return parts


def _checked_base(parent: Path, children_path: str) -> Path:
    """The children directory: inside the parent, every component a real directory (no symbolic links), not reserved."""
    parts = _relative_parts(children_path, "children.path")
    if parts[0] in RESERVED:
        raise Refused(f"children.path may not be under {parts[0]}")
    current = parent
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise Refused(f"{children_path} does not exist in the parent project") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refused(f"{children_path}: {part} must be a directory, not a link or a file")
    return current


def check_destination(parent: Path, children_path: str, name: str) -> tuple[Path, str]:
    """The destination's path and its path relative to the parent; refuses anything but a new, ignored directory."""
    if not CHILD_NAME.match(name):
        raise Refused(f"invalid name {name!r}: lowercase letters, digits, and underscores, starting with a letter")
    base = _checked_base(parent, children_path)
    relative = f"{children_path.rstrip('/')}/{name}"
    if run(parent, "check-ignore", "-q", "--no-index", "--", f"{relative}/", ok_codes=(0, 1)).returncode != 0:
        raise Refused(f"{relative} is not ignored by the parent's Git: children must live outside its history")
    if git(parent, "ls-files", "-z", "--", relative):
        raise Refused(f"{relative} has files tracked by the parent")
    return base / name, relative


# ---------------------------------------------------------------- template


def _template_entries(parent: Path, tree: str) -> list[tuple[str, str, tuple[str, ...]]]:
    listing = run(parent, "ls-tree", "-r", "-z", "--full-tree", tree).stdout
    entries = []
    for record in filter(None, listing.split("\0")):
        meta, name = record.split("\t", 1)
        mode, kind, obj = meta.split()
        parts = PurePosixPath(name).parts
        if kind != "blob" or mode not in ("100644", "100755"):
            raise Refused(f"template entry {name} is not a regular file (links and submodules are not allowed)")
        if not parts or any(p in ("", ".", "..", ".git") for p in parts):
            raise Refused(f"template entry {name} has an unsafe path")
        entries.append((mode, obj, parts))
    if not entries:
        raise Refused("the template is empty")
    if len(entries) > MAX_FILES:
        raise Refused(f"the template has more than {MAX_FILES} files")
    return entries


def _render(text: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise Refused(f"the template uses an unknown value {{{{{key}}}}}")
        return values[key]
    return PLACEHOLDER.sub(replace, text)


def render(parent: Path, tree: str, values: dict[str, str]) -> list[tuple[tuple[str, ...], bytes, bool]]:
    """The template's files rendered from Git objects: (path parts, content, executable)."""
    entries = _template_entries(parent, tree)
    out: list[tuple[tuple[str, ...], bytes, bool]] = []
    total = 0
    process = popen(parent, "cat-file", "--batch")
    assert process.stdin is not None and process.stdout is not None
    try:
        for mode, obj, parts in entries:
            process.stdin.write(f"{obj}\n".encode())
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise GitError(f"object {obj} unreadable")
            size = int(header[2])
            total += size
            if total > MAX_BYTES:
                raise Refused(f"the template is larger than {MAX_BYTES // 1024 // 1024} MiB")
            data = process.stdout.read(size)
            process.stdout.read(1)
            rendered_parts = tuple(_render(p, values) for p in parts)
            if any(p in ("", ".", "..", ".git") or "/" in p for p in rendered_parts):
                raise Refused(f"template path {'/'.join(parts)} renders to an unsafe path")
            try:
                # Binary files (a NUL byte, as Git decides, or not UTF-8) are copied as they are.
                content = data if b"\0" in data else _render(data.decode("utf-8"), values).encode("utf-8")
            except UnicodeDecodeError:
                content = data
            out.append((rendered_parts, content, mode == "100755"))
    finally:
        process.stdin.close()
        process.wait(timeout=30)
    return out


def _write_exclusive(root_fd: int, parts: tuple[str, ...], content: bytes, executable: bool) -> None:
    """Write one file below the directory `root_fd`, never following a link and never replacing anything."""
    fds = [root_fd]
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=fds[-1])
            except FileExistsError:
                pass
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if executable else 0o644,
                     dir_fd=fds[-1])
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
    finally:
        for fd in fds[1:]:
            os.close(fd)


# ---------------------------------------------------------------- steps


def _values(subject: dict[str, Any]) -> dict[str, str]:
    params = {k: str(v) for k, v in (subject.get("params") or {}).items()}
    return {**params, "name": subject["name"]}


def _tree_at(parent: Path, commit: str, path: str) -> str | None:
    """The tree object at `path` in `commit`, or None when it is missing or not a directory."""
    if not valid_sha(commit):
        return None
    obj = try_git(parent, "rev-parse", "--verify", "--quiet", f"{commit}:{path.rstrip('/')}")
    return obj if obj and try_git(parent, "cat-file", "-t", obj) == "tree" else None


def plan(parent: Path, *, template_path: str, children_path: str, name: str) -> dict[str, Any]:
    """Pin the parent's HEAD and the template tree, and check the destination and the template, writing nothing."""
    _relative_parts(template_path, "children.template")
    head = git(parent, "rev-parse", "--verify", "HEAD^{commit}")
    tree = _tree_at(parent, head, template_path)
    if not tree:
        raise Refused(f"{template_path} is not a directory in the parent's last commit {head[:12]}")
    destination, relative = check_destination(parent, children_path, name)
    if os.path.lexists(destination):
        raise Refused(f"{relative} already exists")
    entries = _template_entries(parent, tree)
    return {"parent_sha": head, "template_tree": tree, "destination": relative, "files": len(entries)}


def _own_commit(child: Path, operation_id: str) -> str | None:
    """The first commit of a repository this operation published, or None when it is not ours."""
    if not (child / ".git").is_dir() or (child / ".git").is_symlink():
        return None
    commits = try_git(child, "rev-list", "--max-parents=0", "refs/heads/main")
    if not commits or len(commits.split()) != 1:
        return None
    body = try_git(child, "log", "-1", "--format=%B", commits) or ""
    return commits if f"{TRAILER}: {operation_id}" in body else None


def publish(parent: Path, subject: dict[str, Any]) -> dict[str, Any]:
    """Render the pinned template into a new directory and make the first commit. On a retry, a directory this
    operation already published is reused; anything else at the destination is a person's to resolve."""
    if _tree_at(parent, subject["parent_sha"], subject["template_path"]) != subject["template_tree"]:
        raise Refused("the template no longer matches the approved one")
    destination, relative = check_destination(parent, subject["children_path"], subject["name"])
    if os.path.lexists(destination):
        own = None if os.path.islink(destination) else _own_commit(destination, subject["operation_id"])
        if own:
            return {"destination": relative, "commit": own, "reused": True}
        raise Unowned(f"{relative} already exists and was not created by this operation")
    files = render(parent, subject["template_tree"], _values(subject))
    base_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(subject["name"], mode=0o755, dir_fd=base_fd)  # exclusive: a concurrent directory wins
        except FileExistsError as exc:
            raise Unowned(f"{relative} appeared while the creation was starting") from exc
        root_fd = os.open(subject["name"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
        try:
            for parts, content, executable in files:
                _write_exclusive(root_fd, parts, content, executable)
        finally:
            os.close(root_fd)
    finally:
        os.close(base_fd)
    message = subject["commit_message"].replace("{name}", subject["name"])
    run(destination, "init", "-q", "-b", "main")
    run(destination, "add", "-A")
    run(destination, "commit", "-q", "--no-verify", "-m", message, "-m", f"{TRAILER}: {subject['operation_id']}")
    commit = git(destination, "rev-parse", "--verify", "HEAD^{commit}")
    return {"destination": relative, "commit": commit, "reused": False}


def _view(full_name: str) -> dict[str, Any] | None:
    result = github.gh("repo", "view", full_name, "--json", "nameWithOwner,description,isEmpty,isPrivate,url,defaultBranchRef",
                       ok_codes=(0, 1))
    if result.returncode != 0:
        if "Could not resolve" in result.stderr or "not found" in result.stderr.lower():
            return None
        raise GitError(result.stderr.strip()[:500] or "gh repo view failed")
    return json.loads(result.stdout)


def remote(parent: Path, subject: dict[str, Any], commit: str) -> dict[str, Any]:
    """Create (or, with this operation's evidence, adopt) the GitHub repository and push `main`."""
    if not valid_sha(commit):
        raise Refused("invalid commit")
    destination, relative = check_destination(parent, subject["children_path"], subject["name"])
    if _own_commit(destination, subject["operation_id"]) != commit:
        raise Unowned(f"{relative} is not the repository this operation published")
    full_name = f"{subject['owner']}/{subject['repo']}"
    marker = f"hermes-orchestrator child {subject['operation_id']}"
    found = _view(full_name)
    if found is None:
        github.gh("repo", "create", full_name, "--private" if subject["private"] else "--public", "--description", marker)
        found = _view(full_name)
        if found is None:
            raise GitError(f"{full_name} was created but cannot be read back")
    if marker not in (found.get("description") or ""):
        raise Unowned(f"{full_name} already exists and was not created by this operation")
    if bool(found.get("isPrivate")) != bool(subject["private"]):
        raise Unowned(f"{full_name} visibility differs from the approved one")
    url = str(found["url"])
    current = try_git(destination, "config", "--get", "remote.origin.url")
    if current is None:
        run(destination, "remote", "add", "origin", url)
    elif current != url:
        raise Unowned(f"{relative} already has another origin")
    remote_main = (github._git_remote(destination, "ls-remote", "origin", "refs/heads/main").split() or [None])[0]
    if remote_main and remote_main != commit:
        raise Unowned(f"{full_name} already has a different main")
    if not remote_main:
        github._git_remote(destination, "push", "--porcelain", "--no-verify", "--force-with-lease=refs/heads/main:",
                           "origin", f"{commit}:refs/heads/main")
    return {"repo": found["nameWithOwner"], "url": url, "commit": commit}
