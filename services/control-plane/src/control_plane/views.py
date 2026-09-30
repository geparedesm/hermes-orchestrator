"""API representations of database rows. Never include secrets or file contents."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from .db import Row


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _str(value: UUID | None) -> str | None:
    return str(value) if value else None


def project_view(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "host_path": row["host_path"],
        "git_remote": row["git_remote"],
        "default_branch": row["default_branch"],
        "head_commit": row["head_commit"],
        "status": row["status"],
        "registered_by": row["registered_by"],
        "registered_at": _iso(row["registered_at"]),
    }


def config_view(row: Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "source": row["source"],
        "source_commit": row["source_commit"],
        "status": row["status"],
        "effective_hash": row["effective_hash"],
        "policy_version": row["policy_version"],
        "project_yaml": row["project_yaml"],
        "effective_config": row["effective_config"],
        "rejected_layers": row["rejected_layers"],
        "clamped": row["clamped"],
    }


def approval_view(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "action": row["action"],
        "state": row["state"],
        "risk": row["risk"],
        "summary": row["summary"],
        "project_id": str(row["project_id"]),
        "task_id": _str(row["task_id"]),
        "subject": row["subject"],
        "state_hash": row["state_hash"],
        "requested_by": row["requested_by"],
        "requested_at": _iso(row["requested_at"]),
        "expires_at": _iso(row["expires_at"]),
        "decided_by": row["decided_by"],
        "decided_at": _iso(row["decided_at"]),
        "decision_note": row["decision_note"],
        "invalidated_reason": row["invalidated_reason"],
    }


def task_view(row: Row, *, project_slug: str, budget: Row | None = None, pending_approvals: list[str] | None = None) -> dict[str, Any]:
    """Matches schemas/task.schema.json#/$defs/taskSummary plus operational fields."""
    view: dict[str, Any] = {
        "key": row["key"],
        "project": project_slug,
        "title": row["title"],
        "state": row["state"],
        "priority": row["priority"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }
    if row["resume_state"]:
        view["resume_state"] = row["resume_state"]
    if row["state_reason"]:
        view["state_reason"] = row["state_reason"]
    if row["risk"]:
        view["risk"] = row["risk"]
    if pending_approvals:
        view["pending_approvals"] = pending_approvals
    if budget:
        view["budget"] = {"profile": budget["profile"], "state": budget["state"]}
    return view
