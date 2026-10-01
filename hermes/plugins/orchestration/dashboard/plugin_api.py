"""Dashboard backend of the orchestration plugin, mounted by Hermes at /api/plugins/orchestration/.

Hermes's Dashboard authentication protects these routes (verified in Phase 9 with unauthenticated
requests). They proxy the orchestrator Task API as `dashboard:operator`; the control plane enforces
which actions that principal may take (approvals need it in `platform.approvers`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_spec = importlib.util.spec_from_file_location("orchestration_dashboard_client",
                                               Path(__file__).resolve().parent.parent / "orch_client.py")
_client = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_client)  # type: ignore[union-attr]

PRINCIPAL = "dashboard:operator"
router = APIRouter()


class DecisionBody(BaseModel):
    decision: str
    note: str | None = None


def _api():
    return _client.TaskApi(PRINCIPAL)


def _call(fn, *args: Any) -> Any:
    try:
        return fn(*args)
    except _client.ApiError as exc:
        status = 504 if exc.status == _client.UNKNOWN else (exc.status or 502)
        detail = (exc.message + "; the action may have been applied, refresh before repeating it"
                  if exc.status == _client.UNKNOWN else exc.message)
        raise HTTPException(status_code=status, detail=detail) from exc


@router.get("/overview")
def overview() -> dict[str, Any]:
    api = _api()
    return {"tasks": _call(api.tasks, None, None, True).get("tasks", []), "approvals": _call(api.approvals).get("approvals", []),
            "projects": _call(api.projects).get("projects", [])}


@router.get("/tasks/{key}")
def task(key: str) -> dict[str, Any]:
    api = _api()
    detail = _call(api.task, key)
    try:
        detail["orchestration"] = api.inspect(key)
    except _client.ApiError:
        detail["orchestration"] = None  # orchestration disabled or task not orchestrated
    return detail


@router.post("/tasks/{key}/{verb}")
def task_action(key: str, verb: str) -> Any:
    if verb not in ("pause", "resume", "cancel", "retry"):
        raise HTTPException(status_code=404, detail="unknown action")
    return _call(_api().action, key, verb)


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: DecisionBody) -> Any:
    if body.decision not in ("APPROVE", "REJECT"):
        raise HTTPException(status_code=400, detail="decision is APPROVE or REJECT")
    return _call(_api().decide, approval_id, body.decision == "APPROVE", body.note)
