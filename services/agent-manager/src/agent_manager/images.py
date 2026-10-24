"""Image allowlist (ARCHITECTURE.md section 10).

Callers ask for a symbolic image name ("agent-base"); Agent Manager resolves it
to the pinned image ID in config/images.lock.yaml (written by `make images`).
Anything not pinned there cannot be launched.
"""

from __future__ import annotations

import re
import logging
import threading
from pathlib import Path

import yaml
from docker.errors import DockerException

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


def load_versions(config_dir: Path) -> dict[str, str]:
    """Pinned CLI versions recorded by the build (informational)."""
    path = config_dir / LOCK_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in (data.get("versions") or {}).items()}


def resolve(config_dir: Path, name: str) -> str:
    if not _NAME.match(name or ""):
        raise ImageNotAllowed(f"invalid image name {name!r}")
    image_id = load_allowlist(config_dir).get(name)
    if not image_id:
        raise ImageNotAllowed(f"image {name!r} is not pinned in {LOCK_FILE}")
    return image_id


class ImageUnavailable(RuntimeError):
    """An allowed image is absent; recovery must not spend an agent attempt."""


class ImageRecovery:
    """Restore only operator-built archives; never replace a pin with a tag or rebuild worker code.

    One background import at a time avoids duplicate loads and long API requests.
    After restart the immutable archive can be retried; Docker remains the authority on image IDs.
    """

    def __init__(self, client, config_dir: Path) -> None:
        self.client = client
        self.archive = config_dir / "image-cache" / "execution-images.tar"
        self._lock = threading.Lock()
        self._generation = None
        self._running = False
        self._error = None
        self._missing = set()

    def request(self, image_id: str) -> str:
        try:
            stat = self.archive.stat()
        except OSError:
            return "local recovery archive unavailable; run make images to rebuild and cache approved worker images"
        generation = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._running:
                self._missing.add(image_id)
                return "restoring pinned worker images from the local recovery archive"
            if self._generation == generation and (self._error or image_id in self._missing):
                return self._error or "recovery archive does not contain the required pinned image; run make images"
            if self._generation != generation:
                self._missing.clear()
            self._missing.add(image_id)
            self._generation, self._running, self._error = generation, True, None
            threading.Thread(target=self._restore, daemon=True, name="image-recovery").start()
            return "restoring pinned worker images from the local recovery archive"

    def confirmed(self, image_id: str) -> None:
        with self._lock:
            # Another unavailable image must not prevent this image from recovering after a later prune.
            self._missing.discard(image_id)

    def _restore(self) -> None:
        try:
            # Stream the archive rather than holding gigabytes of layers in Agent Manager memory.
            with self.archive.open("rb") as archive:
                for event in self.client.api.load_image(archive):
                    if event.get("error") or event.get("errorDetail"):
                        raise RuntimeError("Docker rejected the local image recovery archive")
        except Exception:
            logging.getLogger(__name__).exception("Local pinned-image recovery failed")
            with self._lock:
                self._error = "local image recovery failed; rebuild the recovery archive with make images"
                self._running = False
            return

        with self._lock:
            attempted = list(self._missing)
            self._error, self._running = None, False  # import finished; new requests need not join metadata verification
        # Verify completion even if a paused task never observes the first import. Do not overwrite import state
        # afterward: another request can already be importing an independently missing image.
        for image_id in attempted:
            try:
                self.client.images.get(image_id)
            except DockerException:
                continue  # retain absent IDs; a later preflight can confirm transient metadata failures
            self.confirmed(image_id)
