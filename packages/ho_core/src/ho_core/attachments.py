"""Files a person attaches to a task (shared by the control plane and Agent Manager).

Attachments are user data handed to agents read-only at /run/ho-input/attachments/. They are validated on the
way in (the control plane) and again before they reach a container (Agent Manager): a safe name, an allowed
type recognized from the extension and, for binary formats, from the content, and bounded sizes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
# Base64 of MAX_TOTAL_BYTES plus JSON framing: the most a request carrying attachments may send on the wire.
MAX_REQUEST_BYTES = (MAX_TOTAL_BYTES * 4) // 3 + 4 * 1024 * 1024
MOUNT = "/run/ho-input/attachments"

TEXT_TYPES = {
    "txt": "text/plain", "md": "text/markdown", "csv": "text/csv", "tsv": "text/tab-separated-values",
    "json": "application/json", "yaml": "application/yaml", "yml": "application/yaml", "xml": "application/xml",
    "py": "text/x-python", "js": "text/javascript", "ts": "text/x-typescript", "sql": "application/sql",
    "log": "text/plain",
}
BINARY_TYPES = {
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", ()),  # RIFF....WEBP, checked below
    "pdf": ("application/pdf", (b"%PDF-",)),
}
ALLOWED_EXTENSIONS = tuple(sorted({*TEXT_TYPES, *BINARY_TYPES}))
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class InvalidAttachment(ValueError):
    pass


@dataclass(frozen=True)
class Checked:
    name: str
    media_type: str
    size_bytes: int


def safe_name(name: str) -> str:
    """The file's base name reduced to letters, digits, dot, dash, and underscore (at most 100 characters)."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, extension = base.rpartition(".")
    stem, extension = (stem, extension) if dot else (base, "")
    stem = _SAFE.sub("_", stem).strip("._-")[: 100 - len(extension) - 1] or "file"
    extension = _SAFE.sub("", extension).lower()
    return f"{stem}.{extension}" if extension else stem


def check(name: str, content: bytes) -> Checked:
    """Validate one attachment; returns its safe name and the media type the platform assigns it."""
    clean = safe_name(name)
    if not NAME.match(clean):
        raise InvalidAttachment(f"{name!r}: invalid file name")
    extension = clean.rpartition(".")[2] if "." in clean else ""
    if extension not in ALLOWED_EXTENSIONS:
        raise InvalidAttachment(f"{clean}: type not allowed (allowed: {', '.join(ALLOWED_EXTENSIONS)})")
    if not content:
        raise InvalidAttachment(f"{clean}: empty file")
    if len(content) > MAX_FILE_BYTES:
        raise InvalidAttachment(f"{clean}: larger than {MAX_FILE_BYTES // 1024 // 1024} MiB")
    if extension in TEXT_TYPES:
        if b"\x00" in content:
            raise InvalidAttachment(f"{clean}: not a text file")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidAttachment(f"{clean}: text files must be UTF-8") from exc
        return Checked(clean, TEXT_TYPES[extension], len(content))
    media_type, signatures = BINARY_TYPES[extension]
    matches = (content[:4] == b"RIFF" and content[8:12] == b"WEBP") if extension == "webp" else content.startswith(signatures)
    if not matches:
        raise InvalidAttachment(f"{clean}: content is not a {extension.upper()} file")
    return Checked(clean, media_type, len(content))


def check_all(files: list[tuple[str, bytes]]) -> list[Checked]:
    """Validate a set of attachments: count, sizes, and names unique after cleaning."""
    if len(files) > MAX_FILES:
        raise InvalidAttachment(f"at most {MAX_FILES} attachments per task")
    if sum(len(content) for _, content in files) > MAX_TOTAL_BYTES:
        raise InvalidAttachment(f"attachments larger than {MAX_TOTAL_BYTES // 1024 // 1024} MiB in total")
    checked = [check(name, content) for name, content in files]
    names = [c.name.lower() for c in checked]
    if len(set(names)) != len(names):
        raise InvalidAttachment("two attachments have the same name")
    return checked
