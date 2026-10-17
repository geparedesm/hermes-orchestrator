"""New projects at the projects root from a platform template (docs/design/new-projects.md).

Templates ship read-only in this service's image (`HO_TEMPLATES_DIR`). Each one is turned into Git objects in a
private cache repository, so it is pinned by its tree SHA and rendered by the same code as a parent's template
(children.render): no links, no `..`, bounded size. The steps mirror child creation and reuse its evidence:

- `plan` writes nothing: it pins the template tree and checks the destination.
- `publish` creates `<root>/<name>` exclusively from the opened root (never following a link), writes the files, and
  makes the first commit with the operation id as a trailer.
- `remote` creates the GitHub repository described with the operation id, and adopts it on a retry only with that
  evidence.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from ho_core.gitpolicy import PROJECT_NAME

from . import children
from .children import Unowned
from .gitcmd import git, run
from .repo_ops import Refused

_TEMPLATE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_lock = threading.Lock()


def templates_dir() -> Path:
    return Path(os.environ.get("HO_TEMPLATES_DIR", "/opt/ho/templates"))


def _cache() -> Path:
    """A private bare repository that only holds template objects (created on first use)."""
    path = Path(os.environ.get("HO_TEMPLATE_CACHE") or Path(tempfile.gettempdir()) / "ho-template-cache")
    if not (path / "objects").is_dir():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        run(path, "init", "-q", "--bare")
    return path


def available() -> list[str]:
    base = templates_dir()
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.is_symlink() and _TEMPLATE_NAME.match(p.name)) \
        if base.is_dir() else []


def template_tree(name: str) -> str:
    """The template's tree SHA in the cache repository: its files as they are in the image, regular files only."""
    if not _TEMPLATE_NAME.match(name or "") or name not in available():
        raise Refused(f"unknown template {name!r}")
    base = templates_dir() / name
    files: list[tuple[str, str, bool]] = []
    total = 0
    for directory, subdirs, names in os.walk(base, followlinks=False):
        for sub in list(subdirs):
            if os.path.islink(os.path.join(directory, sub)):
                raise Refused(f"template {name} has a link: {sub}")
        for item in names:
            full = os.path.join(directory, item)
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                raise Refused(f"template {name} has an entry that is not a regular file: {item}")
            relative = PurePosixPath(os.path.relpath(full, base)).as_posix()
            total += info.st_size
            files.append((relative, full, bool(info.st_mode & 0o111)))
    if not files or len(files) > children.MAX_FILES or total > children.MAX_BYTES:
        raise Refused(f"template {name} is empty or too large")
    cache = _cache()
    with _lock, tempfile.TemporaryDirectory() as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        for relative, full, executable in sorted(files):
            blob = git(cache, "hash-object", "-w", "--", full)
            run(cache, "update-index", "--add", "--cacheinfo", f"{'100755' if executable else '100644'},{blob},{relative}", env=env)
        return git(cache, "write-tree", env=env)


def _destination(root: Path, name: str) -> tuple[Path, str]:
    if not PROJECT_NAME.match(name or ""):
        raise Refused(f"invalid name {name!r}: lowercase letters, digits, and dashes, starting with a letter")
    info = os.lstat(root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused("the projects root must be a directory")
    return root / name, name


def plan(root: Path, *, template: str, name: str) -> dict[str, Any]:
    """Pin the template tree and check the destination, writing nothing."""
    tree = template_tree(template)
    destination, relative = _destination(root, name)
    if os.path.lexists(destination):
        raise Refused(f"{relative} already exists in the projects folder")
    return {"template_tree": tree, "destination": relative,
            "files": len(children._template_entries(_cache(), tree))}


def _values(subject: dict[str, Any]) -> dict[str, str]:
    return {**{k: str(v) for k, v in (subject.get("params") or {}).items()}, "name": subject["name"]}


def publish(root: Path, subject: dict[str, Any]) -> dict[str, Any]:
    """Render the pinned template into a new `<root>/<name>` and make the first commit. On a retry, a directory this
    operation already published is reused; anything else there is a person's to resolve."""
    if template_tree(subject["template"]) != subject["template_tree"]:
        raise Refused("the template no longer matches the approved one")
    destination, relative = _destination(root, subject["name"])
    if os.path.lexists(destination):
        own = None if os.path.islink(destination) else children._own_commit(destination, subject["operation_id"])
        if own:
            return {"destination": relative, "commit": own, "reused": True}
        raise Unowned(f"{relative} already exists and was not created by this operation")
    files = children.render(_cache(), subject["template_tree"], _values(subject))
    base_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        commit = children.create_repository(base_fd, destination, relative, subject["name"], files,
                                            subject["commit_message"].replace("{name}", subject["name"]),
                                            subject["operation_id"])
    finally:
        os.close(base_fd)
    return {"destination": relative, "commit": commit, "reused": False}


def remote(root: Path, subject: dict[str, Any], commit: str) -> dict[str, Any]:
    if not subject.get("github"):
        raise Refused("this project was approved without a GitHub repository")
    destination, relative = _destination(root, subject["name"])
    return children.publish_remote(destination, relative, subject, commit,
                                   f"hermes-orchestrator project {subject['operation_id']}")
