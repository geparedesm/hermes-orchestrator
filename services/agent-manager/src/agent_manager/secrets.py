"""Secrets Broker v1 file backend (SECURITY_MODEL.md section 7.3).

Operator-managed files outside the projects root, one per secret:

    <secrets dir>/<project>/<environment>/<NAME>        (mode 0600, mounted read-only here only)

The control plane decides which references a grant contains but never reads
values. Agent Manager reads a value only to deliver it into an execution's
in-memory secrets mount, and again to replace it in collected output.
"""

from __future__ import annotations

import re
from pathlib import Path

from .plan import Rejected

_REF = re.compile(r"^([a-z0-9][a-z0-9-]*)/(local|test|staging|production)/([A-Z][A-Z0-9_]{0,63})$")
MAX_SECRET_BYTES = 64 * 1024
REPLACEMENT = "[REDACTED:{name}]"


class SecretStore:
    def __init__(self, root: Path | None) -> None:
        self.root = root

    def _path(self, ref: str) -> Path:
        match = _REF.match(ref)
        if not match:
            raise Rejected(f"invalid secret reference {ref!r}")
        if self.root is None or not self.root.is_dir():
            raise Rejected("the project secrets store is not mounted")
        path = self.root.joinpath(*match.groups())
        # Resolve symlinks and require the file to stay inside the store.
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or not resolved.is_file():
            raise Rejected(f"secret {ref} is not set in the secrets store")
        return resolved

    def read(self, ref: str) -> str:
        path = self._path(ref)
        if path.stat().st_size > MAX_SECRET_BYTES:
            raise Rejected(f"secret {ref} is larger than {MAX_SECRET_BYTES // 1024} KiB")
        value = path.read_text(encoding="utf-8").rstrip("\n")
        if not value:
            raise Rejected(f"secret {ref} is empty")
        return value

    def values_for(self, refs: list[str]) -> dict[str, str]:
        """Current values by NAME for redaction; unreadable secrets are skipped."""
        values = {}
        for ref in refs:
            try:
                values[ref.rsplit("/", 1)[1]] = self.read(ref)
            except Rejected:
                continue
        return values

    @staticmethod
    def redact(text: str, values: dict[str, str]) -> str:
        # Longest first, so a secret that contains another is replaced whole.
        for name, value in sorted(values.items(), key=lambda item: -len(item[1])):
            if len(value) >= 4:
                text = text.replace(value, REPLACEMENT.format(name=name))
        return text

    @staticmethod
    def redact_bytes(data: bytes, values: dict[str, str]) -> bytes:
        for name, value in sorted(values.items(), key=lambda item: -len(item[1])):
            if len(value) >= 4:
                data = data.replace(value.encode(), REPLACEMENT.format(name=name).encode())
        return data
