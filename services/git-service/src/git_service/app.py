"""Git Service API (ARCHITECTURE.md section 7.4).

Only the control plane calls it. Phase 2: read-only inspection and onboarding
scans. Phase 5: isolated workspaces, hardened collection, divergence
classification, integration, approved merges, and GitHub push/PR/checks.
Every rule that protects the user's work or protected branches is enforced
here again, independently of the caller.
"""

from __future__ import annotations

import hmac
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from ho_core.detect import MAX_FILE_BYTES, detect
from ho_core.gitpolicy import GitPolicyError, protected_branches, verify_merge
from ho_core.paths import PathOutsideRoot, resolve_inside
from pydantic import BaseModel, Field

from . import github, gitcmd, repo_ops


class ProjectPath(BaseModel):
    path: str = Field(min_length=1, max_length=1000, description="Path relative to the projects root")


class WorkspaceBody(ProjectPath):
    name: str = Field(min_length=1, max_length=64)


class PrepareBody(WorkspaceBody):
    branch: str = Field(min_length=1, max_length=200)
    base_ref: str = Field(min_length=1, max_length=200)
    pin_ref: str | None = Field(default=None, max_length=200)


class ReadViewBody(ProjectPath):
    branch: str = Field(min_length=1, max_length=200)


class PruneReadViewsBody(ProjectPath):
    keep: list[str] = Field(default_factory=list, max_length=1000)
    min_age_seconds: int = Field(default=3600, ge=0)


class CollectBody(WorkspaceBody):
    branch: str = Field(min_length=1, max_length=200)
    base_sha: str = Field(min_length=40, max_length=40)


class ConflictBody(WorkspaceBody):
    branch: str = Field(min_length=1, max_length=200)
    target_branch: str = Field(min_length=1, max_length=200)
    incoming_ref: str = Field(min_length=1, max_length=200)


class DivergenceBody(ProjectPath):
    base_sha: str = Field(min_length=40, max_length=40)
    head_ref: str = Field(min_length=1, max_length=200)
    target_branch: str = Field(min_length=1, max_length=200)
    sensitive_paths: list[str] = Field(default_factory=list, max_length=200)
    critical_paths: list[str] = Field(default_factory=list, max_length=200)


class IntegrateBody(ProjectPath):
    task: str = Field(pattern=r"^T-[0-9]+$")
    target_branch: str = Field(min_length=1, max_length=200)
    heads: list[str] = Field(min_length=1, max_length=50)


class ChangesBody(ProjectPath):
    base: str = Field(min_length=1, max_length=200)
    head: str = Field(min_length=1, max_length=200)


class RefsBody(ProjectPath):
    refs: list[str] = Field(min_length=1, max_length=50)


class MergeBody(ProjectPath):
    task: str = Field(pattern=r"^T-[0-9]+$")
    authorization: dict[str, Any]


class BranchPolicy(ProjectPath):
    prefix: str = Field(min_length=1, max_length=32)
    protected: list[str] = Field(default_factory=list, max_length=100)


class PushBody(BranchPolicy):
    ref: str = Field(min_length=1, max_length=200)
    branch: str = Field(min_length=1, max_length=200)
    expected_remote_sha: str | None = Field(default=None, max_length=40)


class PrBody(BranchPolicy):
    branch: str = Field(min_length=1, max_length=200)
    base: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=250)
    body: str = Field(default="", max_length=60_000)


class PrNumberBody(ProjectPath):
    number: int = Field(ge=1)


class DeleteBranchBody(BranchPolicy):
    branch: str = Field(min_length=1, max_length=200)


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


def _read_yaml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read a small YAML mapping without following symlinks."""
    if not path.exists() and not path.is_symlink():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, f"{path.name} must be a regular file"
    if path.stat().st_size > MAX_FILE_BYTES:
        return None, f"{path.name} is larger than {MAX_FILE_BYTES} bytes"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, f"{path.name} is not valid YAML: {str(exc)[:200]}"
    if data is None:
        return None, None
    if not isinstance(data, dict):
        return None, f"{path.name} must contain a mapping"
    return data, None


def create_app(projects_root: Path, token: str, merge_key: bytes = b"") -> FastAPI:
    app = FastAPI(title="Hermes Orchestrator Git Service", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    locks: dict[Path, threading.Lock] = defaultdict(threading.Lock)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"error": "git_service", "message": exc.message}, status_code=exc.status)

    @app.exception_handler(repo_ops.Refused)
    async def refused(_: Request, exc: repo_ops.Refused) -> JSONResponse:
        return JSONResponse({"error": "refused", "message": str(exc)}, status_code=409)

    @app.exception_handler(GitPolicyError)
    async def policy(_: Request, exc: GitPolicyError) -> JSONResponse:
        return JSONResponse({"error": "forbidden", "message": str(exc)}, status_code=403)

    @app.exception_handler(gitcmd.GitError)
    async def git_error(_: Request, exc: gitcmd.GitError) -> JSONResponse:
        return JSONResponse({"error": "git_failed", "message": str(exc)[:500]}, status_code=502)

    def authorized(authorization: str | None = Header(default=None)) -> None:
        presented = (authorization or "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(presented.encode(), token.encode()):
            raise ServiceError(401, "unauthorized")

    def repository(relative: str) -> Path:
        try:
            path = resolve_inside(projects_root, relative)
        except PathOutsideRoot as exc:
            raise ServiceError(400, str(exc)) from exc
        if path == projects_root.resolve():
            raise ServiceError(400, "the projects root itself is not a project")
        if not path.is_dir():
            raise ServiceError(404, f"{relative} does not exist in the projects root")
        return path

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    def git_repository(relative: str) -> Path:
        repo = repository(relative)
        if not (repo / ".git").is_dir() or (repo / ".git").is_symlink():
            raise ServiceError(400, f"{relative} is not a Git repository")
        return repo

    def locked(repo: Path, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with locks[repo]:
            return action()

    def protected_for(repo: Path, configured: list[str]) -> list[str]:
        default = gitcmd.inspect(repo).get("default_branch")
        return sorted(protected_branches(configured, default if isinstance(default, str) else None))

    @app.post("/v1/inspect", dependencies=[Depends(authorized)])
    def inspect(body: ProjectPath) -> dict[str, Any]:
        return {"path": body.path, **gitcmd.inspect(repository(body.path))}

    @app.post("/v1/scan", dependencies=[Depends(authorized)])
    def scan(body: ProjectPath) -> dict[str, Any]:
        repo = repository(body.path)
        info = gitcmd.inspect(repo)
        if not info.get("is_git"):
            raise ServiceError(400, f"{body.path} is not a Git repository")
        project_yaml, project_error = _read_yaml(repo / ".hermes" / "project.yaml")
        local_yaml, local_error = _read_yaml(repo / ".hermes.local.yaml")
        return {
            "path": body.path,
            "head": info.get("head"),
            "report": detect(repo).to_dict(),
            "project_yaml": project_yaml,
            "project_yaml_error": project_error,
            "local_yaml": local_yaml,
            "local_yaml_error": local_error,
        }

    # ------------------------------------------------------------ workspaces

    @app.post("/v1/workspaces/prepare", dependencies=[Depends(authorized)])
    def prepare(body: PrepareBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: repo_ops.prepare_workspace(repo, body.name, body.branch, body.base_ref, body.pin_ref))

    @app.post("/v1/read-views", dependencies=[Depends(authorized)])
    def read_view(body: ReadViewBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: repo_ops.read_view(repo, body.branch))

    @app.post("/v1/read-views/prune", dependencies=[Depends(authorized)])
    def prune_read_views(body: PruneReadViewsBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: repo_ops.prune_read_views(repo, set(body.keep), body.min_age_seconds))

    @app.post("/v1/workspaces/collect", dependencies=[Depends(authorized)])
    def collect(body: CollectBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: repo_ops.collect(repo, body.name, body.branch, body.base_sha))

    @app.post("/v1/workspaces/conflict", dependencies=[Depends(authorized)])
    def conflict_workspace(body: ConflictBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: repo_ops.prepare_conflict_workspace(
            repo, name=body.name, branch=body.branch, target_branch=body.target_branch, incoming_ref=body.incoming_ref))

    @app.post("/v1/workspaces/remove", dependencies=[Depends(authorized)])
    def remove(body: WorkspaceBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: repo_ops.remove_workspace(repo, body.name))

    # ------------------------------------------------- divergence and integration

    @app.post("/v1/divergence", dependencies=[Depends(authorized)])
    def divergence(body: DivergenceBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return repo_ops.divergence(repo, base_sha=body.base_sha, head_ref=body.head_ref, target_branch=body.target_branch,
                                   sensitive_paths=body.sensitive_paths, critical_paths=body.critical_paths)

    @app.post("/v1/integrate", dependencies=[Depends(authorized)])
    def integrate(body: IntegrateBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: repo_ops.integrate(repo, task=body.task, target_branch=body.target_branch, heads=body.heads))

    @app.post("/v1/changes", dependencies=[Depends(authorized)])
    def changes(body: ChangesBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return repo_ops.changes(repo, repo_ops.resolve_commit(repo, body.base), repo_ops.resolve_commit(repo, body.head))

    @app.post("/v1/refs", dependencies=[Depends(authorized)])
    def refs(body: RefsBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return {"refs": repo_ops.refs(repo, body.refs), "remote": github.remote_info(repo)}

    # ------------------------------------------------------------------ merge

    @app.post("/v1/merge", dependencies=[Depends(authorized)])
    def merge(body: MergeBody) -> dict[str, Any]:
        """Approved merge. The authorization is signed by the control plane for one approval and
        binds the exact target and head commits; it is verified here before anything is written."""
        repo = git_repository(body.path)
        subject = verify_merge(merge_key, body.authorization)
        if subject["project"] != body.path:
            raise GitPolicyError("the merge authorization is for another project")

        def run_merge() -> dict[str, Any]:
            if subject["pr_number"]:
                return github.merge_pr(repo, approval_id=subject["approval_id"], number=int(subject["pr_number"]),
                                       target_branch=subject["target_branch"], target_sha=subject["target_sha"],
                                       head_sha=subject["head_sha"], method=subject["method"])
            return repo_ops.merge_local(repo, approval_id=subject["approval_id"], task=body.task,
                                        target_branch=subject["target_branch"], target_sha=subject["target_sha"],
                                        head_sha=subject["head_sha"], method=subject["method"])
        return locked(repo, run_merge)

    # ---------------------------------------------------------------- GitHub

    @app.post("/v1/github/status", dependencies=[Depends(authorized)])
    def github_status(body: ProjectPath) -> dict[str, Any]:
        repo = git_repository(body.path)
        info = github.remote_info(repo)
        return {**info, "auth": github.auth_status() if info["kind"] == "github" else None}

    @app.post("/v1/github/push", dependencies=[Depends(authorized)])
    def push(body: PushBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: github.push(repo, ref=body.ref, branch=body.branch, prefix=body.prefix,
                                                protected=protected_for(repo, body.protected),
                                                expected_remote_sha=body.expected_remote_sha))

    @app.post("/v1/github/pr", dependencies=[Depends(authorized)])
    def pull_request(body: PrBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: github.create_or_update_pr(repo, branch=body.branch, base=body.base, title=body.title,
                                                               body=body.body, prefix=body.prefix,
                                                               protected=protected_for(repo, body.protected)))

    @app.post("/v1/github/pr/view", dependencies=[Depends(authorized)])
    def pr_view(body: PrNumberBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        pr = github.pr_view(repo, body.number)
        return {**pr, "base_sha": github.remote_branch_sha(repo, pr["baseRefName"])}

    @app.post("/v1/github/pr/checks", dependencies=[Depends(authorized)])
    def pr_checks(body: PrNumberBody) -> dict[str, Any]:
        return github.pr_checks(git_repository(body.path), body.number)

    @app.post("/v1/github/delete-branch", dependencies=[Depends(authorized)])
    def delete_branch(body: DeleteBranchBody) -> dict[str, Any]:
        repo = git_repository(body.path)
        return locked(repo, lambda: github.delete_branch(repo, branch=body.branch, prefix=body.prefix,
                                                         protected=protected_for(repo, body.protected)))

    return app
