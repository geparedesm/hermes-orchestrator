"""Git rules shared by the control plane and Git Service (MASTER_SPEC sections 36-41).

Pure functions only:
- protected branches and allowed branch names (section 39, 41);
- the signed merge authorization Git Service verifies before any merge (SECURITY_MODEL.md section 6.3);
- human change classification from diff hunks (section 37).
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .enums import StrEnum

HARD_PROTECTED = frozenset({"main", "master"})
_BRANCH = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)(?!.*\.lock$)(?!.*/$)[A-Za-z0-9._/-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_WORKSPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class GitPolicyError(ValueError):
    pass


def valid_branch(name: str) -> bool:
    return bool(_BRANCH.match(name or "")) and not name.startswith("-")


def valid_sha(value: str) -> bool:
    return bool(_SHA.match(value or ""))


def valid_workspace(name: str) -> bool:
    return bool(_WORKSPACE.match(name or ""))


def protected_branches(configured: Iterable[str] = (), default_branch: str | None = None) -> frozenset[str]:
    """Hard-protected set: main, master, the repository's default branch, and the project's list."""
    names = set(HARD_PROTECTED) | {b for b in configured if b}
    if default_branch:
        names.add(default_branch)
    return frozenset(names)


def check_task_branch(branch: str, *, prefix: str, protected: Iterable[str]) -> None:
    """Branches the platform may create, push, or delete: only its own, never a protected one."""
    if not valid_branch(branch):
        raise GitPolicyError(f"invalid branch name {branch!r}")
    if branch in set(protected):
        raise GitPolicyError(f"{branch} is a protected branch")
    if not prefix or not branch.startswith(prefix):
        raise GitPolicyError(f"platform branches must start with {prefix!r}")


def task_branch(prefix: str, task_key: str, workspace: str) -> str:
    return f"{prefix}{task_key.lower()}/{workspace}"


# --------------------------------------------------------------------------
# Merge authorization (control plane -> Git Service)
# --------------------------------------------------------------------------

MERGE_SUBJECT_KEYS = ("project", "target_branch", "target_sha", "head_sha", "method", "pr_number")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sign_merge(key: bytes, *, approval_id: str, subject: dict[str, Any], expires_at: datetime) -> dict[str, Any]:
    """Authorization for exactly one merge: the approved subject, the approval ID, and an expiry."""
    body = {"approval_id": approval_id, "subject": {k: subject.get(k) for k in MERGE_SUBJECT_KEYS},
            "expires_at": expires_at.astimezone(timezone.utc).isoformat()}
    return {**body, "signature": hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()}


def verify_merge(key: bytes, token: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Return the approved subject, or raise GitPolicyError if the token is forged, altered, or expired."""
    try:
        body = {"approval_id": str(token["approval_id"]), "subject": dict(token["subject"]), "expires_at": str(token["expires_at"])}
        signature = str(token["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GitPolicyError("malformed merge authorization") from exc
    expected = hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()
    if not key or not hmac.compare_digest(signature, expected):
        raise GitPolicyError("merge authorization signature is invalid")
    if datetime.fromisoformat(body["expires_at"]) <= (now or datetime.now(timezone.utc)):
        raise GitPolicyError("merge authorization has expired")
    subject = body["subject"]
    if set(subject) != set(MERGE_SUBJECT_KEYS):
        raise GitPolicyError("merge authorization subject is incomplete")
    if not (valid_sha(subject["target_sha"] or "") and valid_sha(subject["head_sha"] or "")):
        raise GitPolicyError("merge authorization must bind exact commits")
    return {"approval_id": body["approval_id"], **subject}


# --------------------------------------------------------------------------
# Human change classification (section 37)
# --------------------------------------------------------------------------


class ChangeLevel(StrEnum):
    NONE = "NONE"
    LOW = "LOW"  # different files: continue
    MEDIUM = "MEDIUM"  # same file, different areas: continue + reconciliation
    HIGH = "HIGH"  # same lines: checkpoint + rebase/replan
    CRITICAL = "CRITICAL"  # contradictory structure: APPROVAL_REQUIRED


_ORDER = [ChangeLevel.NONE, ChangeLevel.LOW, ChangeLevel.MEDIUM, ChangeLevel.HIGH, ChangeLevel.CRITICAL]
# Hunks this close (in lines of the base version) count as touching the same area.
NEAR_LINES = 3


def max_level(*levels: ChangeLevel) -> ChangeLevel:
    return max(levels, key=_ORDER.index, default=ChangeLevel.NONE)


@dataclass
class FileChange:
    path: str
    status: str  # A, M, D, R (git --name-status letters)
    ranges: list[tuple[int, int]] = field(default_factory=list)  # changed line ranges in the base version


@dataclass
class FileClassification:
    path: str
    level: ChangeLevel
    reason: str

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "level": self.level.value, "reason": self.reason}


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def parse_hunks(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Changed line ranges in the old (base) version, per file, from `git diff -U0` output."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            current = parts[1] if len(parts) == 2 else None
            if current is not None:
                ranges.setdefault(current, [])
        elif current is not None:
            match = _HUNK.match(line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) if match.group(2) is not None else 1)
                # A pure insertion (count 0) sits between lines start and start+1.
                ranges[current].append((start, start + max(count, 1) - 1) if count else (start, start + 1))
    return ranges


def _overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
    return any(x1 <= y2 + NEAR_LINES and y1 <= x2 + NEAR_LINES for x1, x2 in a for y1, y2 in b)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def classify_divergence(
    human: dict[str, FileChange],
    task: dict[str, FileChange],
    *,
    sensitive_paths: Iterable[str] = (),
    critical_paths: Iterable[str] = (),
) -> tuple[ChangeLevel, list[FileClassification]]:
    """Classify human changes made since the task's base commit against the task's own changes."""
    if not human:
        return ChangeLevel.NONE, []
    results: list[FileClassification] = []
    for path in sorted(set(human) & set(task)):
        h, t = human[path], task[path]
        if "D" in (h.status, t.status) or h.status.startswith("R") or t.status.startswith("R"):
            level, reason = ChangeLevel.CRITICAL, "deleted or renamed on one side and changed on the other"
        elif h.status == "A" or t.status == "A" or _overlap(h.ranges, t.ranges):
            level, reason = ChangeLevel.HIGH, "the same lines were changed by the user and by the task"
        else:
            level, reason = ChangeLevel.MEDIUM, "same file, different areas"
        if level != ChangeLevel.MEDIUM and _matches(path, critical_paths):
            level, reason = ChangeLevel.CRITICAL, f"{reason}; critical path"
        elif _matches(path, sensitive_paths):
            level = max_level(level, ChangeLevel.HIGH)
            reason = f"{reason}; sensitive path"
        results.append(FileClassification(path, level, reason))
    overall = max_level(ChangeLevel.LOW, *(r.level for r in results))
    return overall, results
