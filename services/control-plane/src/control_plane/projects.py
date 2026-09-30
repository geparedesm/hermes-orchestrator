"""Project Registry and read-only onboarding (MASTER_SPEC sections 13-17).

register -> scan (git-service, read-only) -> configuration proposal
-> PROJECT_READY approval -> PROJECT_READY

Nothing here writes to the project repository. Applying a proposed
.hermes/project.yaml to the repository happens later through a normal task
branch and merge approval.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

import psycopg.errors
from ho_core.config import build_project_config
from ho_core.detect import propose_project_config
from ho_core.enums import ApprovalAction, ConfigStatus, ProjectStatus, Risk
from ho_core.hashing import hash_value
from ho_core.ids import uuid7
from ho_core.paths import PathOutsideRoot, host_to_relative, is_slug, slugify
from ho_core.schemas import SchemaValidationError

from .approvals import Approvals
from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import BadRequest, Conflict, NotFound, ValidationFailed
from .events import record_event

_SCANNABLE = {ProjectStatus.REGISTERED, ProjectStatus.PROPOSED, ProjectStatus.PROJECT_READY, ProjectStatus.DRIFT_DETECTED}


class Projects:
    def __init__(self, ctx: Context, approvals: Approvals) -> None:
        self.ctx = ctx
        self.approvals = approvals
        approvals.register_handler(ApprovalAction.PROJECT_READY, self._on_project_ready_decision)

    # ------------------------------------------------------------------ queries

    def get(self, uow: UnitOfWork, slug: str, *, lock: bool = False) -> Row:
        uow.cur.execute(f"SELECT * FROM projects WHERE slug = %s{' FOR UPDATE' if lock else ''}", (slug,))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound(f"project {slug} not found")
        return row

    def list(self, uow: UnitOfWork, *, include_unregistered: bool = False) -> list[Row]:
        where = "" if include_unregistered else "WHERE status <> 'UNREGISTERED'"
        uow.cur.execute(f"SELECT * FROM projects {where} ORDER BY slug")
        return uow.cur.fetchall()

    def active_config(self, uow: UnitOfWork, project_id: UUID) -> Row | None:
        uow.cur.execute("SELECT * FROM project_configs WHERE project_id = %s AND status = 'ACTIVE'", (project_id,))
        return uow.cur.fetchone()

    def latest_config(self, uow: UnitOfWork, project_id: UUID) -> Row | None:
        uow.cur.execute(
            "SELECT * FROM project_configs WHERE project_id = %s ORDER BY created_at DESC LIMIT 1", (project_id,)
        )
        return uow.cur.fetchone()

    # ----------------------------------------------------------------- commands

    def register(self, uow: UnitOfWork, *, principal: Principal, path: str, name: str | None, slug: str | None) -> Row:
        root = self.ctx.platform["platform"]["projects_root_host"]
        try:
            relative = host_to_relative(root, path)
        except PathOutsideRoot as exc:
            raise BadRequest(f"{exc}. Only paths inside the projects root ({root}) can be registered.") from exc
        info = self.ctx.git.inspect(relative)
        if not info.get("is_git"):
            raise BadRequest(f"{path} is not a Git repository")
        display_name = (name or PurePosixPath(relative).name)[:100]
        try:
            project_slug = slug or slugify(display_name)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        if not is_slug(project_slug):
            raise BadRequest("slug must match ^[a-z0-9][a-z0-9-]{0,62}$")
        project_id = uuid7()
        try:
            with uow.cur.connection.transaction():  # savepoint so a conflict can be reported cleanly
                uow.cur.execute(
                    """
                    INSERT INTO projects (id, slug, name, relative_path, host_path, git_remote, default_branch,
                                          head_commit, status, registered_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'REGISTERED', %s)
                    RETURNING *
                    """,
                    (project_id, project_slug, display_name, relative, f"{root.rstrip('/')}/{relative}",
                     info.get("remote"), info.get("default_branch"), info.get("head"), principal.value),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"project {project_slug} or path {relative} is already registered") from exc
        row = uow.cur.fetchone()
        assert row is not None
        record_event(
            uow.cur, "PROJECT_REGISTERED", actor=principal.value, project_id=project_id,
            summary=f"Registered {project_slug} at {relative}",
            data={"slug": project_slug, "relative_path": relative, "remote": info.get("remote")}, pending=uow.events,
        )
        return row

    def scan(self, uow: UnitOfWork, *, slug: str, principal: Principal) -> dict[str, Any]:
        project = self.get(uow, slug, lock=True)
        if project["status"] not in _SCANNABLE:
            raise Conflict(f"project is {project['status']}")
        result = self.ctx.git.scan(project["relative_path"])
        report = result["report"]
        head = result.get("head")

        artifact = self.ctx.artifacts.write(
            uow.cur, project_id=project["id"], kind="onboarding", name="scan.json",
            content=json.dumps(result, indent=2, sort_keys=True).encode(), media_type="application/json",
        )
        scan_id = uuid7()
        uow.cur.execute(
            "INSERT INTO onboarding_scans (id, project_id, head_commit, report, artifact_id) VALUES (%s, %s, %s, %s, %s)",
            (scan_id, project["id"], head, jsonb(report), artifact.id),
        )

        notes: list[str] = []
        source = "PROPOSAL"
        project_yaml: dict[str, Any] = propose_project_config(project["name"], report)
        if result.get("project_yaml") is not None:
            source, project_yaml = "REPOSITORY", result["project_yaml"]
        if result.get("project_yaml_error"):
            notes.append(f".hermes/project.yaml ignored: {result['project_yaml_error']}")
        try:
            effective = build_project_config(
                self.ctx.platform, project_yaml, local_yaml=result.get("local_yaml"), default_branch=project["default_branch"],
            )
        except SchemaValidationError as exc:
            if source != "REPOSITORY":
                raise ValidationFailed("generated proposal is invalid", details=exc.errors) from exc
            notes.append(f".hermes/project.yaml is invalid ({'; '.join(exc.errors[:5])}); using a generated proposal")
            source, project_yaml = "PROPOSAL", propose_project_config(project["name"], report)
            effective = build_project_config(
                self.ctx.platform, project_yaml, local_yaml=result.get("local_yaml"), default_branch=project["default_branch"],
            )

        active = self.active_config(uow, project["id"])
        record_event(
            uow.cur, "PROJECT_SCAN_COMPLETED", actor=principal.value, project_id=project["id"],
            summary=f"Scanned {slug}: profiles {report.get('profiles')}",
            data={"scan_id": str(scan_id), "head": head, "risks": report.get("risks", []), "notes": notes},
            pending=uow.events,
        )
        uow.cur.execute("UPDATE projects SET head_commit = %s, updated_at = now() WHERE id = %s", (head, project["id"]))

        if active and active["effective_hash"] == effective.hash:
            return {"project": self.get(uow, slug), "config": active, "approval": None, "notes": notes, "changed": False}

        # A new proposal supersedes any open one.
        self.approvals.invalidate_open(
            uow, project_id=project["id"], task_id=None, action=ApprovalAction.PROJECT_READY, reason="superseded by a new scan"
        )
        uow.cur.execute(
            "UPDATE project_configs SET status = 'SUPERSEDED', updated_at = now() WHERE project_id = %s AND status = 'PROPOSED'",
            (project["id"],),
        )
        config_id = uuid7()
        uow.cur.execute(
            """
            INSERT INTO project_configs (id, project_id, source, source_commit, project_yaml, project_yaml_sha256,
                                         local_yaml, effective_config, effective_hash, policy_version,
                                         rejected_layers, clamped, scan_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PROPOSED')
            RETURNING *
            """,
            (config_id, project["id"], source, head, jsonb(project_yaml), hash_value(project_yaml),
             jsonb(result.get("local_yaml")), jsonb(effective.data), effective.hash, effective.policy_version,
             jsonb(effective.rejected), jsonb(effective.clamped), scan_id),
        )
        config = uow.cur.fetchone()
        assert config is not None

        new_status = ProjectStatus.DRIFT_DETECTED if active else ProjectStatus.PROPOSED
        if active:
            record_event(
                uow.cur, "CONFIG_DRIFT_DETECTED", actor="control-plane", project_id=project["id"],
                summary=f"{slug} configuration changed; approval required before new planning",
                data={"active_hash": active["effective_hash"], "proposed_hash": effective.hash}, pending=uow.events,
            )
        subject = self._ready_subject(project, config, head)
        approval = self.approvals.request(
            uow, action=ApprovalAction.PROJECT_READY, project_id=project["id"], subject=subject,
            config_hash=effective.hash, requested_by=principal.value, risk=Risk.MEDIUM,
            summary=f"Approve {'updated ' if active else ''}configuration for {slug} ({source.lower()}, profiles {report.get('profiles')})",
        )
        uow.cur.execute(
            "UPDATE projects SET status = %s, updated_at = now(), version = version + 1 WHERE id = %s",
            (new_status.value, project["id"]),
        )
        return {"project": self.get(uow, slug), "config": config, "approval": approval, "notes": notes, "changed": True}

    def unregister(self, uow: UnitOfWork, *, slug: str, principal: Principal) -> Row:
        project = self.get(uow, slug, lock=True)
        if project["status"] == ProjectStatus.UNREGISTERED:
            return project
        uow.cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE project_id = %s AND state NOT IN ('DONE', 'CANCELLED', 'FAILED')",
            (project["id"],),
        )
        if uow.cur.fetchone()["n"]:  # type: ignore[index]
            raise Conflict("project has unfinished tasks; cancel them first")
        self.approvals.invalidate_open(uow, project_id=project["id"], task_id=None, action=None, reason="project unregistered")
        uow.cur.execute(
            "UPDATE projects SET status = 'UNREGISTERED', updated_at = now(), version = version + 1 WHERE id = %s RETURNING *",
            (project["id"],),
        )
        row = uow.cur.fetchone()
        assert row is not None
        record_event(
            uow.cur, "PROJECT_UNREGISTERED", actor=principal.value, project_id=project["id"],
            summary=f"Unregistered {slug}; repository files were not touched", pending=uow.events,
        )
        return row

    # ---------------------------------------------------------------- approvals

    @staticmethod
    def _ready_subject(project: Row, config: Row, head: str | None) -> dict[str, Any]:
        return {
            "project": project["slug"],
            "config_id": str(config["id"]),
            "effective_hash": config["effective_hash"],
            "source": config["source"],
            "head_commit": head,
        }

    def _on_project_ready_decision(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        config_id = approval["subject"]["config_id"]
        uow.cur.execute("SELECT * FROM project_configs WHERE id = %s FOR UPDATE", (config_id,))
        config = uow.cur.fetchone()
        uow.cur.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (approval["project_id"],))
        project = uow.cur.fetchone()
        assert config is not None and project is not None

        if not approved:
            uow.cur.execute("UPDATE project_configs SET status = 'REJECTED', updated_at = now() WHERE id = %s", (config_id,))
            if project["status"] == ProjectStatus.PROPOSED:
                uow.cur.execute("UPDATE projects SET status = 'REGISTERED', updated_at = now() WHERE id = %s", (project["id"],))
            return

        # Recompute what was approved from the current state: the repository head
        # and the stored configuration must be unchanged.
        head = self.ctx.git.inspect(project["relative_path"]).get("head")
        current_subject = self._ready_subject(project, config, head)
        if config["status"] != ConfigStatus.PROPOSED:
            self.approvals.invalidate_open(
                uow, project_id=project["id"], task_id=None, action=ApprovalAction.PROJECT_READY,
                reason="configuration is no longer the open proposal",
            )
            consumed = False
        else:
            consumed = self.approvals.consume(uow, approval, subject=current_subject, config_hash=config["effective_hash"])
        if not consumed:
            record_event(
                uow.cur, "BLOCKED", actor="control-plane", project_id=project["id"],
                summary=f"{project['slug']} changed after the proposal; rescan required", pending=uow.events,
            )
            return
        uow.cur.execute(
            "UPDATE project_configs SET status = 'SUPERSEDED', updated_at = now() WHERE project_id = %s AND status = 'ACTIVE'",
            (project["id"],),
        )
        uow.cur.execute(
            "UPDATE project_configs SET status = 'ACTIVE', approval_id = %s, updated_at = now() WHERE id = %s",
            (approval["id"], config_id),
        )
        uow.cur.execute(
            "UPDATE projects SET status = 'PROJECT_READY', head_commit = %s, updated_at = now(), version = version + 1 WHERE id = %s",
            (head, project["id"]),
        )
        record_event(
            uow.cur, "PROJECT_READY", actor=principal.value, project_id=project["id"],
            summary=f"{project['slug']} is ready", data={"config_id": config_id, "effective_hash": config["effective_hash"]},
            pending=uow.events,
        )
        uow.wake_scheduler = True

