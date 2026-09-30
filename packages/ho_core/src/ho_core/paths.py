"""Path confinement helpers (SECURITY_MODEL.md sections 8.1 and 13)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class PathOutsideRoot(ValueError):
    pass


def resolve_inside(root: Path, candidate: str | Path) -> Path:
    """Resolve `candidate` (absolute or relative to root) and require it inside `root`.

    Symlinks are resolved first, so a link pointing outside the root is rejected.
    """
    root_resolved = root.resolve(strict=True)
    path = Path(candidate)
    if not path.is_absolute():
        path = root_resolved / path
    resolved = path.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathOutsideRoot(f"{candidate} is outside {root}")
    return resolved


def host_to_relative(projects_root_host: str, host_path: str) -> str:
    """Translate a host path into a path relative to the projects root.

    Pure string logic: the control plane never sees the host filesystem. `~` is
    not expanded here; callers pass absolute host paths or root-relative paths.
    """
    root = PurePosixPath(projects_root_host)
    path = PurePosixPath(host_path)
    if not path.is_absolute():
        path = root / path
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if not parts:
                raise PathOutsideRoot(f"{host_path} is outside {projects_root_host}")
            parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    normalized = PurePosixPath(*parts) if parts else PurePosixPath("/")
    try:
        relative = normalized.relative_to(root)
    except ValueError as exc:
        raise PathOutsideRoot(f"{host_path} is outside {projects_root_host}") from exc
    if str(relative) in ("", "."):
        raise PathOutsideRoot("the projects root itself cannot be registered")
    return str(relative)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63]
    if not slug or not _SLUG.match(slug):
        raise ValueError(f"cannot derive a valid slug from {name!r}")
    return slug


def is_slug(value: str) -> bool:
    return bool(_SLUG.match(value))
