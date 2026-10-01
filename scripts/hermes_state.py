"""Export or import Hermes's state for scripts/backup.sh and scripts/restore.sh (runs in the Hermes image).

export: /data -> /out/hermes-data.tar.gz, consistent and without credentials:
  - SQLite databases are copied with SQLite's online backup API (safe while Hermes runs, WAL included);
  - credentials and runtime files are left out (.env, auth*.json, *.lock, logs, caches, our plugin mount);
  - secret values in config.yaml (the webhook route secret and the Dashboard login) are removed; hermes-init
    sets them again from ./secrets on start.
import: /in/hermes-data.tar.gz -> /data (the volume is emptied first, except the plugin mount).
"""

from __future__ import annotations

import io
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

DATA = Path("/data")
SKIP_NAMES = {".env", "auth.json", "auth.lock"}
SKIP_DIRS = {"logs", "cache", ".cache", "plugins/orchestration", "tmp"}
SECRET_PATHS = (("dashboard", "basic_auth"),)


def _skipped(relative: Path) -> bool:
    text = relative.as_posix()
    if relative.name in SKIP_NAMES or relative.name.startswith("auth") and relative.suffix == ".json":
        return True
    if relative.suffix in (".lock", ".pid", ".sock") or relative.name.endswith(("-wal", "-shm")):
        return True
    return any(text == d or text.startswith(d + "/") for d in SKIP_DIRS)


def _scrub(config: dict) -> dict:
    for path in SECRET_PATHS:
        node = config
        for key in path[:-1]:
            node = node.get(key) or {}
        node.pop(path[-1], None)
    routes = (((config.get("platforms") or {}).get("webhook") or {}).get("extra") or {}).get("routes") or {}
    for route in routes.values():
        if isinstance(route, dict):
            route.pop("secret", None)
    return config


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def export(out: Path) -> None:
    with tarfile.open(out, "w:gz") as tar, tempfile.TemporaryDirectory() as scratch:
        for path in sorted(DATA.rglob("*")):
            relative = path.relative_to(DATA)
            if path.is_symlink() or not path.is_file() or _skipped(relative):
                continue
            if relative.as_posix() == "config.yaml":
                data = yaml.safe_dump(_scrub(yaml.safe_load(path.read_text()) or {}), sort_keys=False).encode()
                info = tarfile.TarInfo(relative.as_posix())
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif _is_sqlite(path):
                copy = Path(scratch) / relative.name
                source, target = sqlite3.connect(path), sqlite3.connect(copy)  # the backup API copies a consistent snapshot
                with target:
                    source.backup(target)
                source.close()
                target.close()
                tar.add(copy, arcname=relative.as_posix())
                copy.unlink()
            else:
                tar.add(path, arcname=relative.as_posix())


def restore(archive: Path) -> None:
    for child in DATA.iterdir():
        if child.name == "plugins":
            for plugin in child.iterdir():
                if plugin.name != "orchestration":
                    shutil.rmtree(plugin) if plugin.is_dir() else plugin.unlink()
            continue
        shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(DATA, filter="data")


if __name__ == "__main__":
    {"export": lambda: export(Path(sys.argv[2])), "import": lambda: restore(Path(sys.argv[2]))}[sys.argv[1]]()
