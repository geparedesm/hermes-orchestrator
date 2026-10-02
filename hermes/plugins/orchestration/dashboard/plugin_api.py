"""Dashboard backend of the orchestration plugin, mounted by Hermes at /api/plugins/orchestration/.

Hermes's Dashboard authentication protects these routes (verified with unauthenticated requests). They proxy
the orchestrator Task API as `dashboard:operator`; the control plane enforces which actions that principal may
take (approvals need it in `platform.approvers`) and records it as the creator of tasks made here. No state is kept here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

_spec = importlib.util.spec_from_file_location("orchestration_dashboard_client",
                                               Path(__file__).resolve().parent.parent / "orch_client.py")
_client = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_client)  # type: ignore[union-attr]

PRINCIPAL = "dashboard:operator"
TASK_ACTIONS = ("pause", "resume", "cancel", "retry")
router = APIRouter()


class DecisionBody(BaseModel):
    decision: str
    note: str | None = None


class NewTaskBody(BaseModel):
    project: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    request: str = Field(min_length=1, max_length=20000)
    title: str | None = Field(default=None, max_length=200)
    priority: Literal["CRITICAL", "HIGH", "NORMAL", "LOW"] = "NORMAL"
    # UNLIMITED is not offered here: it is a separate human decision (AD-10), requested on the task.
    budget: Literal["SMALL", "NORMAL", "LARGE"] | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    # Generated once per form by the page, so a repeated click or a retried request creates one task.
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,128}$")


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


# ---------------------------------------------------------------- reads


@router.get("/overview")
def overview() -> dict[str, Any]:
    api = _api()
    return {"tasks": _call(api.tasks, None, None, True).get("tasks", []), "approvals": _call(api.approvals).get("approvals", []),
            "projects": _call(api.projects).get("projects", [])}


@router.get("/summary")
def summary() -> Any:
    return _call(_api().summary)


@router.get("/board")
def board() -> dict[str, Any]:
    return {"tasks": _call(_api().tasks, None, None, True).get("tasks", [])}


@router.get("/projects")
def projects() -> Any:
    return _call(_api().projects)


@router.get("/workers")
def workers() -> Any:
    return _call(_api().workers)


@router.get("/approvals")
def approvals() -> Any:
    return _call(_api().approvals)


@router.get("/tasks/{key}")
def task(key: str) -> Any:
    return _call(_api().task_view, key)


@router.get("/tasks/{key}/manifests/{manifest_id}")
def manifest(key: str, manifest_id: str) -> Any:
    return _call(_api().manifest, key, manifest_id)


# ---------------------------------------------------------------- actions (the control plane authorizes each one)


@router.post("/tasks")
def create_task(body: NewTaskBody) -> Any:
    keys = [k.strip().upper() for k in body.depends_on if k.strip()]
    if any(not k.startswith("T-") or not k[2:].isdigit() for k in keys):
        raise HTTPException(status_code=400, detail="dependencies are task keys such as T-12")
    return _call(lambda: _api().create(body.project, body.request.strip(), (body.title or "").strip() or None, body.priority,
                                       budget=body.budget, depends_on=keys, idempotency_key=body.idempotency_key))


@router.post("/tasks/{key}/manifest")
def generate_manifest(key: str) -> Any:
    return _call(_api().generate_manifest, key)


@router.post("/tasks/{key}/{verb}")
def task_action(key: str, verb: str) -> Any:
    if verb not in TASK_ACTIONS:
        raise HTTPException(status_code=404, detail="unknown action")
    return _call(_api().action, key, verb)


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: DecisionBody) -> Any:
    if body.decision not in ("APPROVE", "REJECT"):
        raise HTTPException(status_code=400, detail="decision is APPROVE or REJECT")
    return _call(_api().decide, approval_id, body.decision == "APPROVE", body.note)
