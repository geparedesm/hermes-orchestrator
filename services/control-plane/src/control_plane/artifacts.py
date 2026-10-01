"""Artifact store: content on the artifact volume, metadata in PostgreSQL (AD-13)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ho_core.hashing import sha256_hex
from ho_core.ids import uuid7
from psycopg import Cursor

from .db import Row

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class StoredArtifact:
    id: UUID
    path: str
    sha256: str
    size_bytes: int


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(
        self,
        cur: Cursor[Row],
        *,
        project_id: UUID,
        kind: str,
        name: str,
        content: bytes,
        media_type: str,
        task_id: UUID | None = None,
    ) -> StoredArtifact:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact {name} exceeds {MAX_ARTIFACT_BYTES} bytes")
        if "/" in name or name.startswith("."):
            raise ValueError(f"invalid artifact name {name!r}")
        artifact_id = uuid7()
        scope = str(task_id) if task_id else "_project"
        relative = f"{project_id}/{scope}/{kind}/{artifact_id}-{name}"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so a crash never leaves a partial artifact behind.
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        digest = sha256_hex(content)
        cur.execute(
            """
            INSERT INTO artifacts (id, project_id, task_id, kind, path, sha256, size_bytes, media_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (artifact_id, project_id, task_id, kind, relative, digest, len(content), media_type),
        )
        return StoredArtifact(artifact_id, relative, digest, len(content))

    def read(self, relative: str) -> bytes:
        return self._path(relative).read_bytes()

    def delete(self, relative: str) -> None:
        """Remove an artifact's file (retention); its metadata and digest stay in PostgreSQL."""
        self._path(relative).unlink(missing_ok=True)

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("artifact path escapes the artifact root")
        return path
