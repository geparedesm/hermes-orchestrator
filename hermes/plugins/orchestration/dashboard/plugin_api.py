"""Dashboard backend of the orchestration plugin, mounted by Hermes at /api/plugins/orchestration/.

Hermes's Dashboard authentication protects these routes (verified with unauthenticated requests). They proxy
the orchestrator Task API as `dashboard:operator`; the control plane enforces which actions that principal may
take (approvals need it in `platform.approvers`) and records it as the creator of tasks made here. No state is kept here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

_spec = importlib.util.spec_from_file_location("orchestration_dashboard_client",
                                               Path(__file__).resolve().parent.parent / "orch_client.py")
_client = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_client)  # type: ignore[union-attr]

PRINCIPAL = "dashboard:operator"
TASK_ACTIONS = ("pause", "resume", "cancel", "retry")
def same_origin_mutation(request: Request) -> None:
    """Preview application code must not submit authenticated Dashboard actions across origins."""
    origin = request.headers.get('origin')
    if request.method in ('GET', 'HEAD', 'OPTIONS') or not origin:
        return
    try:
        parsed = urlsplit(origin)
        valid = parsed.scheme in ('http', 'https') and parsed.netloc == request.headers.get('host')
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(status_code=403, detail='Dashboard actions require the Dashboard origin')


router = APIRouter(dependencies=[Depends(same_origin_mutation)])


class DecisionBody(BaseModel):
    decision: str
    note: str | None = None


# Mirrors ho_core.attachments (this plugin runs inside Hermes, without the platform's packages); the control
# plane checks names, types, content, and sizes again.
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BASE64 = (10 * 1024 * 1024 + 2) // 3 * 4
MAX_CREATE_BODY = (25 * 1024 * 1024 * 4) // 3 + 4 * 1024 * 1024
_UPLOADS = asyncio.Semaphore(2)  # task creations with attachments read at a time


class AttachmentBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1, max_length=MAX_ATTACHMENT_BASE64)


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
    attachments: list[AttachmentBody] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    # Review the plan before work starts (only adds supervision to the project's setting).
    approve_plan: bool = False
    delivery_mode: Literal["SUBTASKS", "FEATURE"] | None = None
    human_test_required: bool | None = None
    model_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)



class NewProjectBody(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    description: str = Field(min_length=1, max_length=500)
    github: bool = True
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,128}$")


class NewChildBody(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,128}$")


_SLUG = r"^[a-z0-9][a-z0-9-]{0,62}$"


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
def board() -> Any:
    return _call(_api().board)


@router.get("/projects")
def projects() -> Any:
    return _call(_api().projects)


@router.get("/workers")
def workers() -> Any:
    return _call(_api().workers)


@router.get("/agents")
def agents() -> Any:
    return _call(_api().agents)


class AgentModelBody(BaseModel):
    model: str | None = Field(default=None, max_length=100)


@router.post("/agents/{provider}/model")
def agent_model(provider: str, body: AgentModelBody) -> Any:
    return _call(_api().set_agent_model, provider, body.model)


@router.post("/agents/{provider}/models/{role}")
def role_model(provider: str, role: str, body: AgentModelBody) -> Any:
    return _call(_api().set_role_model, provider, role, body.model)


@router.get("/previews")
def previews() -> Any:
    return _call(_api().previews)


class PreviewFeedbackBody(BaseModel):
    candidate_id: UUID
    text: str = Field(min_length=1, max_length=4000)


@router.post("/tasks/{key}/preview-feedback")
def preview_feedback(key: str, body: PreviewFeedbackBody) -> Any:
    return _call(_api().preview_feedback, key, str(body.candidate_id), body.text)


@router.post("/previews/{candidate_id}/{verb}")
def preview_action(candidate_id: UUID, verb: str) -> Any:
    return _call(_api().preview_action, str(candidate_id), verb)


@router.post("/agents/{provider}/{verb}")
def agent_action(provider: str, verb: str) -> Any:
    if verb not in ("pause", "resume"):
        raise HTTPException(status_code=404, detail="unknown action")
    return _call(_api().agent_action, provider, verb)


@router.get("/projects/{slug}/children")
def children(slug: str) -> Any:
    if not re.match(_SLUG, slug):
        raise HTTPException(status_code=404, detail="unknown project")
    return _call(_api().children, slug)


@router.get("/approvals")
def approvals(state: Literal["PENDING", "EXPIRED"] = "PENDING") -> Any:
    return _call(_api().approvals if state == "PENDING" else _api().expired_approvals)


@router.get("/tasks/{key}")
def task(key: str) -> Any:
    return _call(_api().task_view, key)


@router.get("/tasks/{key}/manifests/{manifest_id}")
def manifest(key: str, manifest_id: str) -> Any:
    return _call(_api().manifest, key, manifest_id)


# ---------------------------------------------------------------- actions (the control plane authorizes each one)


@router.post("/tasks")
async def create_task(request: Request) -> Any:
    # Read under a byte limit counted while the body arrives (attachments make it large), a few at a time.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_CREATE_BODY:
        raise HTTPException(status_code=413, detail="request too large: attachments are limited to 25 MiB in total")
    async with _UPLOADS:
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_CREATE_BODY:
                raise HTTPException(status_code=413, detail="request too large: attachments are limited to 25 MiB in total")
        try:
            body = NewTaskBody.model_validate_json(bytes(raw))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]) from exc
        del raw
        keys = [k.strip().upper() for k in body.depends_on if k.strip()]
        if any(not k.startswith("T-") or not k[2:].isdigit() for k in keys):
            raise HTTPException(status_code=400, detail="dependencies are task keys such as T-12")
        files = [a.model_dump() for a in body.attachments]
        delivery = {k: v for k, v in {"delivery_mode": body.delivery_mode,
                    "human_test_required": body.human_test_required, "model_overrides": body.model_overrides}.items()
                    if v is not None and v != {}}
        return await run_in_threadpool(_call, lambda: _api().create(
            body.project, body.request.strip(), (body.title or "").strip() or None, body.priority, budget=body.budget,
            depends_on=keys, idempotency_key=body.idempotency_key, attachments=files, approve_plan=body.approve_plan,
            **delivery))


@router.get("/tasks/{key}/attachments/{attachment_id}")
def attachment(key: str, attachment_id: UUID) -> Response:
    if not re.match(r"^T-[0-9]+$", key):
        raise HTTPException(status_code=404, detail="unknown task")
    content, headers = _call(_api().attachment, key, str(attachment_id))
    # A download, never content rendered in the Dashboard's origin.
    return Response(content, media_type="application/octet-stream", headers={
        "Content-Disposition": headers.get("content-disposition") or headers.get("Content-Disposition") or "attachment",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.get("/tasks/{key}/ui-design")
def ui_design(key: str) -> Any:
    """The task's current UI design proposal: version, state, the changes it makes, and its files."""
    if not re.match(r"^T-[0-9]+$", key):
        raise HTTPException(status_code=404, detail="unknown task")
    result = _call(_api().ui_design, key)
    # Where the images go, for "Sent to WhatsApp"; only the platform name, never the chat it is sent to.
    if os.environ.get("HO_UI_DESIGN_DELIVERY", "").lower() == "true":
        result["delivery_platform"] = os.environ.get("HO_UI_DESIGN_DELIVER_PLATFORM", "").strip().lower() or None
    return result


UI_DESIGN_FILES = {"desktop.png": "image/png", "mobile.png": "image/png", "design.md": "text/markdown; charset=utf-8"}


@router.get("/tasks/{key}/ui-design/{design_id}/{name}")
def ui_design_file(key: str, design_id: UUID, name: str) -> Response:
    # Only the two exported images and the design notes; the encrypted design.pen stays with the platform. The
    # control plane checked each file's type and hash; the page reads them as blobs, never as navigable documents.
    if not re.match(r"^T-[0-9]+$", key) or name not in UI_DESIGN_FILES:
        raise HTTPException(status_code=404, detail="unknown design file")
    content, _ = _call(_api().ui_design_file, key, str(design_id), name)
    if name.endswith(".png") and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=502, detail="the design image is not a PNG")
    return Response(content, media_type=UI_DESIGN_FILES[name], headers={
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'",
        "Content-Disposition": f'{"inline" if name.endswith(".png") else "attachment"}; filename="{name}"'})


@router.post("/tasks/{key}/ui-design/{design_id}/delivered/{name}")
def ui_design_image_delivered(key: str, design_id: UUID, name: str) -> Any:
    # The authenticated page reports each image only after fetching its validated bytes.
    if not re.fullmatch(r"T-[0-9]+", key) or name not in ("desktop.png", "mobile.png"):
        raise HTTPException(status_code=404, detail="unknown design image")
    return _call(_api().call, "POST", f"/v1/tasks/{key}/ui-design/{design_id}/delivered/{name}", {})


@router.post("/tasks/{key}/manifest")
def generate_manifest(key: str) -> Any:
    return _call(_api().generate_manifest, key)


class BudgetBody(BaseModel):
    add: dict[Literal["runtime_minutes", "agent_launches", "retries", "review_cycles", "subtasks", "provider_usage_units"],
              int] = Field(min_length=1)


@router.post("/tasks/{key}/budget")
def raise_budget(key: str, body: BudgetBody) -> Any:
    """Ask for more budget for a task. It becomes a BUDGET_INCREASE decision, decided like any other."""
    if not re.match(r"^T-[0-9]+$", key):
        raise HTTPException(status_code=404, detail="unknown task")
    # Positive whole numbers; provider usage is counted in tokens (one agent run reserves 200,000), so its bound is larger.
    if any(v <= 0 or v > (10**10 if counter == "provider_usage_units" else 100_000) for counter, v in body.add.items()):
        raise HTTPException(status_code=400, detail="increases are positive whole numbers within the counter's range")
    return _call(_api().budget, key, dict(body.add))


@router.post("/tasks/{key}/{verb}")
def task_action(key: str, verb: str) -> Any:
    if verb not in TASK_ACTIONS:
        raise HTTPException(status_code=404, detail="unknown action")
    return _call(_api().action, key, verb)


@router.get("/project-creations")
def project_creations() -> Any:
    return _call(_api().project_creations)


@router.post("/project-creations")
def create_project(body: NewProjectBody) -> Any:
    """A new project from the platform's generic template. The control plane asks for a CREATE_PROJECT approval
    before anything is written; the Dashboard operator decides it like any other approval."""
    return _call(lambda: _api().create_project(body.name, body.description.strip(), body.github,
                                               idempotency_key=body.idempotency_key))


@router.post("/project-creations/{operation_id}/{verb}")
def project_creation_action(operation_id: UUID, verb: Literal["retry", "abandon"]) -> Any:
    return _call(_api().project_creation_action, str(operation_id), verb)


@router.post("/projects/{slug}/children")
def create_child(slug: str, body: NewChildBody) -> Any:
    """A new child project (an Odoo module of odoo-base). The control plane asks for a CREATE_CHILD approval
    before anything is written; the Dashboard operator decides it like any other approval."""
    if not re.match(_SLUG, slug):
        raise HTTPException(status_code=404, detail="unknown project")
    return _call(lambda: _api().create_child(slug, body.name, body.title.strip(), body.description.strip(),
                                             idempotency_key=body.idempotency_key))


@router.post("/projects/{slug}/children/{operation_id}/{verb}")
def child_action(slug: str, operation_id: UUID, verb: Literal["retry", "abandon"]) -> Any:
    if not re.match(_SLUG, slug):
        raise HTTPException(status_code=404, detail="unknown project")
    return _call(_api().child_action, slug, str(operation_id), verb)


class AnswersBody(BaseModel):
    answers: dict[str, dict[str, str]] | None = None
    use_recommended: bool = False


@router.post("/tasks/{key}/clarifications/{reference}/answer")
def answer_questions(key: str, reference: UUID, body: AnswersBody) -> Any:
    """The operator's answers to a round of planning questions (the control plane checks they are a person's)."""
    return _call(_api().answer_task, key, str(reference), body.model_dump())


@router.post("/approvals/{approval_id}/renew")
def renew(approval_id: UUID) -> Any:
    return _call(_api().renew, str(approval_id))


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: DecisionBody) -> Any:
    if body.decision not in ("APPROVE", "REJECT"):
        raise HTTPException(status_code=400, detail="decision is APPROVE or REJECT")
    return _call(_api().decide, approval_id, body.decision == "APPROVE", body.note)
