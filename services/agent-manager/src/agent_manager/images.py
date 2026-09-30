"""Image allowlist (ARCHITECTURE.md section 10).

Callers ask for a symbolic image name ("agent-base"); Agent Manager resolves it
to the pinned image ID in config/images.lock.yaml (written by `make images`).
Anything not pinned there cannot be launched.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

LOCK_FILE = "images.lock.yaml"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class ImageNotAllowed(ValueError):
    pass


def load_allowlist(config_dir: Path) -> dict[str, str]:
    path = config_dir / LOCK_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    images = data.get("images", {})
    allowlist = {}
    for name, image_id in images.items():
        if _NAME.match(str(name)) and _DIGEST.match(str(image_id)):
            allowlist[str(name)] = str(image_id)
    return allowlist


def resolve(config_dir: Path, name: str) -> str:
    if not _NAME.match(name or ""):
        raise ImageNotAllowed(f"invalid image name {name!r}")
    image_id = load_allowlist(config_dir).get(name)
    if not image_id:
        raise ImageNotAllowed(f"image {name!r} is not pinned in {LOCK_FILE}")
    return image_id
