"""Generic credential redaction for ingested output (SECURITY_MODEL.md section 10).

Literal secret values are replaced by Agent Manager, which delivered them.
These patterns catch common credential formats that reach logs anyway.
"""

from __future__ import annotations

import re

REPLACEMENT = "[REDACTED]"

# Patterns with a capture group keep that group (for example "token=") and redact the rest.
_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)(\bauthorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)(\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"']?)[^\s\"']{12,}"),
]


def redact(text: str) -> tuple[str, bool]:
    """Return the redacted text and whether anything was replaced."""
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return (match.group(1) if match.re.groups else "") + REPLACEMENT

    for pattern in _PATTERNS:
        text = pattern.sub(replace, text)
    return text, changed
