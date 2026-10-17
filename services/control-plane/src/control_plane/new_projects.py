"""New projects at the projects root from a platform template (docs/design/new-projects.md).

A person asks for a project (name, description, GitHub optional). The request reserves the name, which is the
folder and the slug, and pins everything the creation uses in a CREATE_PROJECT approval; nothing is written before
it is approved. Once approved, the creation is a durable operation (`child_creations`, kind PROJECT) that advances
one recorded step at a time through Git Service (publish, then GitHub when chosen), then registers and scans the
project. A retry continues from the last recorded step; anything found without this operation's evidence stops it
in NEEDS_RECOVERY for a person.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from ho_core.enums import ApprovalAction, Risk
from ho_core.gitpolicy import PROJECT_NAME, sign_project
from ho_core.ids import uuid7

from .approvals import Approvals
from .auth import Principal
from .children import LIVE, RETRY_AFTER, _line
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import ApiError, BadRequest, Conflict, Forbidden, NotFound
from .events import record_event
from .gitsvc import Unowned
from .projects import Projects

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[\"'\\{}<>&`]")


class NewProjects:
    def __init__(self, ctx: Context, projects: Projects, approvals: Approvals) -> None:
        self.ctx = ctx
        self.projects = projects
        self.approvals = approvals
        approvals.register_handler(ApprovalAction.CREATE_PROJECT, self._on_decision)
        approvals.register_renewer(ApprovalAction.CREATE_PROJECT, self._renew)

    def _settings(self) -> dict[str, Any]:
        return {"template": "generic", "private": True, "commit_message": "[ADD] Create {name}",
                **(self.ctx.platform["platform"].get("new_projects") or {})}

    # ------------------------------------------------------------------ queries

    def list(self, uow: UnitOfWork) -> list[dict[str, Any]]:
        """Creations that still need attention or are running (the Dashboard shows them above the projects)."""
        uow.cur.execute("SELECT c.*, p.slug AS child_slug, a.number AS approval_number, a.state AS approval_state "
                        "FROM child_creations c LEFT JOIN projects p ON p.id = c.child_project_id "
                        "LEFT JOIN approvals a ON a.id = c.approval_id "
                        "WHERE c.kind = 'PROJECT' AND c.state = ANY(%s) ORDER BY c.created_at DESC LIMIT 20", (list(LIVE),))
        return [self.view(r) for r in uow.cur.fetchall()]

    @staticmethod
    def view(row: Row) -> dict[str, Any]:
        params = row["subject"].get("params") or {}
        return {"id": str(row["id"]), "name": row["name"], "description": params.get("description"), "state": row["state"],
                "step": row["step"], "destination": row["destination"], "github_repo": row["github_repo"],
                "project": row.get("child_slug"), "approval_number": row.get("approval_number"),
                "approval_state": row.get("approval_state"), "error": row["error"],
                "requested_by": row["requested_by"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    # ----------------------------------------------------------------- commands

    def request(self, uow: UnitOfWork, *, principal: Principal, name: str, description: str, github: bool) -> dict[str, Any]:
        """Reserve the name, pin what the creation will use, and ask a person to approve it. Writes nothing outside the
        database."""
        self._human(principal)
        if not PROJECT_NAME.match(name or ""):
            raise BadRequest("name must be lowercase letters, digits, and dashes, starting with a letter (2-41 characters)")
        description = _line(description, 500)
        if not description:
            raise BadRequest("a description is required: your agents read it first")
        if _UNSAFE.search(description):
            # The value lands inside Markdown and YAML of the template.
            raise BadRequest("the description may not contain quotes, backslashes, braces, or < > &")
        # The name is the project's slug too, which is unique across every project (also nested ones).
        uow.cur.execute("SELECT 1 FROM projects WHERE slug = %s AND status <> 'UNREGISTERED'", (name,))
        if uow.cur.fetchone() is not None:
            raise Conflict(f"a project named {name} is already registered")
        settings = self._settings()
        plan = self.ctx.git.project_plan(template=settings["template"], name=name)
        owner = settings.get("github_owner")
        if github and not owner:
            raise BadRequest("no GitHub owner is configured (platform.new_projects.github_owner)")
        operation_id = uuid7()
        subject = {"operation_id": str(operation_id), "template": settings["template"], "template_tree": plan["template_tree"],
                   "name": name, "params": {"description": description}, "github": bool(github), "owner": owner if github else None,
                   "repo": name if github else None, "private": bool(settings.get("private", True)),
                   "commit_message": settings["commit_message"]}
        full_repo = f"{owner}/{name}" if github else None
        where = f" with its own {'private' if subject['private'] else 'public'} GitHub repository {full_repo}" if github else ", without GitHub"
        try:
            with uow.cur.connection.transaction():
                uow.cur.execute(
                    "INSERT INTO child_creations (id, kind, parent_id, name, subject, destination, github_repo, state, requested_by) "
                    "VALUES (%s, 'PROJECT', NULL, %s, %s, %s, %s, 'AWAITING_APPROVAL', %s)",
                    (operation_id, name, jsonb(subject), plan["destination"], full_repo, principal.value))
                approval = self.approvals.request(
                    uow, action=ApprovalAction.CREATE_PROJECT, project_id=None, subject=subject,
                    config_hash="0" * 64, risk=Risk.MEDIUM, requested_by=principal.value,
                    summary=f"Create project {name}{where}")
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"{name}{' or ' + full_repo if full_repo else ''} is already being created") from exc
        uow.cur.execute("UPDATE child_creations SET approval_id = %s WHERE id = %s RETURNING *", (approval["id"], operation_id))
        created = uow.cur.fetchone()
        record_event(uow.cur, "PROJECT_CREATION_REQUESTED", actor=principal.value,
                     summary=f"project {name} requested", data={"operation": str(operation_id), "repo": full_repo},
                     pending=uow.events)
        return {**self.view(created), "approval": {"id": str(approval["id"]), "number": approval.get("number")}}

    def retry(self, uow: UnitOfWork, *, principal: Principal, operation_id: UUID) -> dict[str, Any]:
        self._human(principal)
        row = self._get(uow, operation_id)
        if row["state"] not in ("RUNNING", "NEEDS_RECOVERY"):
            raise Conflict(f"this creation is {row['state']}; only running or stopped creations can be retried")
        uow.cur.execute("UPDATE child_creations SET state = 'RUNNING', error = NULL, updated_at = now() WHERE id = %s "
                        "RETURNING *", (row["id"],))
        resumed = uow.cur.fetchone()
        record_event(uow.cur, "PROJECT_CREATION_RETRIED", actor=principal.value, summary=f"creation of {row['name']} resumed",
                     data={"operation": str(row["id"])}, pending=uow.events)
        uow.after_commit.append(lambda: self.run(row["id"]))
        return self.view(resumed)

    def abandon(self, uow: UnitOfWork, *, principal: Principal, operation_id: UUID) -> dict[str, Any]:
        """Give up a stopped creation and free its name. Nothing is deleted: what it made stays for a person."""
        self._human(principal)
        row = self._get(uow, operation_id)
        expired = False
        if row["state"] == "AWAITING_APPROVAL":
            uow.cur.execute("SELECT state FROM approvals WHERE id = %s", (row["approval_id"],))
            expired = (uow.cur.fetchone() or {}).get("state") in ("EXPIRED", "INVALIDATED")
        if row["state"] not in ("RUNNING", "NEEDS_RECOVERY") and not expired:
            raise Conflict(f"this creation is {row['state']}")
        uow.cur.execute("UPDATE child_creations SET state = 'FAILED', error = coalesce(error, 'abandoned'), updated_at = now() "
                        "WHERE id = %s RETURNING *", (row["id"],))
        abandoned = uow.cur.fetchone()
        record_event(uow.cur, "PROJECT_CREATION_FAILED", actor=principal.value,
                     summary=f"creation of {row['name']} abandoned at step {row['step'].lower()}",
                     data={"operation": str(row["id"]), "evidence": row["evidence"]}, pending=uow.events)
        return self.view(abandoned)

    def _human(self, principal: Principal) -> None:
        # Creating repositories is a person's decision (AD-10): only approvers may ask for it.
        if principal.value not in self.ctx.platform["platform"]["approvers"]:
            raise Forbidden(f"{principal.value} may not create projects")

    def _get(self, uow: UnitOfWork, operation_id: UUID) -> Row:
        uow.cur.execute("SELECT * FROM child_creations WHERE id = %s AND kind = 'PROJECT' FOR UPDATE", (operation_id,))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound("creation not found")
        return row

    # ------------------------------------------------------------------ decision and steps

    def _on_decision(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        uow.cur.execute("SELECT * FROM child_creations WHERE approval_id = %s AND kind = 'PROJECT' FOR UPDATE", (approval["id"],))
        row = uow.cur.fetchone()
        if row is None or row["state"] != "AWAITING_APPROVAL":
            return
        if not approved:
            uow.cur.execute("UPDATE child_creations SET state = 'REJECTED', updated_at = now() WHERE id = %s", (row["id"],))
            return
        if not self.approvals.consume(uow, approval, subject=row["subject"], config_hash="0" * 64):
            uow.cur.execute("UPDATE child_creations SET state = 'INVALIDATED', error = %s, updated_at = now() WHERE id = %s",
                            ("the request changed after it was approved; request it again", row["id"]))
            return
        uow.cur.execute("UPDATE child_creations SET state = 'RUNNING', updated_at = now() WHERE id = %s", (row["id"],))
        uow.after_commit.append(lambda: self.run(row["id"]))

    def _renew(self, uow: UnitOfWork, expired: Row, principal: Principal) -> Row:
        uow.cur.execute("SELECT * FROM child_creations WHERE approval_id = %s AND kind = 'PROJECT' FOR UPDATE", (expired["id"],))
        row = uow.cur.fetchone()
        if row is None or row["state"] != "AWAITING_APPROVAL":
            raise Conflict("this creation is no longer waiting for a decision: request the project again")
        renewed = self.approvals.request(uow, action=ApprovalAction.CREATE_PROJECT, project_id=None,
                                         subject=expired["subject"], config_hash=expired["config_hash"],
                                         summary=expired["summary"], requested_by=principal.value, risk=Risk(expired["risk"]))
        uow.cur.execute("UPDATE child_creations SET approval_id = %s, updated_at = now() WHERE id = %s", (renewed["id"], row["id"]))
        return renewed

    def _authorization(self, row: Row) -> dict[str, Any]:
        # Re-signed for each run from the consumed approval: retries outlive any single token.
        return sign_project(self.ctx.merge_key, approval_id=str(row["approval_id"]), subject=row["subject"],
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))

    def run(self, operation_id: UUID) -> None:
        """Advance a RUNNING creation step by step, recording each result before the next step starts."""
        for _ in range(4):
            try:
                with self.ctx.unit_of_work() as uow:
                    uow.cur.execute("SELECT * FROM child_creations WHERE id = %s AND kind = 'PROJECT' FOR UPDATE", (operation_id,))
                    row = uow.cur.fetchone()
                    if row is None or row["state"] != "RUNNING" or row["step"] == "REGISTERED":
                        return
                    self._step(uow, row)
            except Unowned as exc:
                self._stop(operation_id, "NEEDS_RECOVERY", str(exc))
                return
            except ApiError as exc:
                self._stop(operation_id, "RUNNING", str(exc))  # recorded; the scheduler or a person retries
                return
            except Exception as exc:  # noqa: BLE001 - a creation never takes the scheduler down
                log.exception("project creation %s failed", operation_id)
                self._stop(operation_id, "RUNNING", f"unexpected error: {exc}"[:500])
                return

    def _stop(self, operation_id: UUID, state: str, error: str) -> None:
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("UPDATE child_creations SET state = %s, error = %s, updated_at = now() WHERE id = %s RETURNING *",
                            (state, error[:500], operation_id))
            row = uow.cur.fetchone()
            if row is not None and state == "NEEDS_RECOVERY":
                record_event(uow.cur, "BLOCKED", actor="control-plane", summary=f"creation of {row['name']} needs you: {error[:300]}",
                             data={"operation": str(operation_id)}, pending=uow.events)

    def _step(self, uow: UnitOfWork, row: Row) -> None:
        subject, evidence = row["subject"], dict(row["evidence"])
        if row["step"] == "APPROVED":
            result = self.ctx.git.project_publish(self._authorization(row))
            evidence.update(commit=result["commit"], destination=result["destination"])
            nxt = "PUBLISHED"
        elif row["step"] == "PUBLISHED":
            if subject.get("github"):
                result = self.ctx.git.project_remote(self._authorization(row), evidence["commit"])
                evidence.update(repo=result["repo"], url=result["url"])
            nxt = "PUSHED"  # without GitHub there is nothing to push: straight on to registering
        else:  # PUSHED: register and scan it; the person approves its configuration as for any project
            project = self._register(uow, row, evidence)
            uow.cur.execute("UPDATE child_creations SET step = 'REGISTERED', state = 'DONE', child_project_id = %s, "
                            "error = NULL, updated_at = now() WHERE id = %s", (project["id"], row["id"]))
            record_event(uow.cur, "PROJECT_CREATED", actor="control-plane", project_id=project["id"],
                         summary=f"project {row['name']} created" + (f" as {evidence['repo']}" if evidence.get("repo") else "")
                                 + "; approve its configuration to start",
                         data={"operation": str(row["id"]), "repo": evidence.get("repo")}, pending=uow.events)
            return
        uow.cur.execute("UPDATE child_creations SET step = %s, evidence = %s, error = NULL, updated_at = now() WHERE id = %s",
                        (nxt, jsonb(evidence), row["id"]))
        log.info("project creation %s: %s (%s)", row["id"], nxt, subject["name"])

    def _register(self, uow: UnitOfWork, row: Row, evidence: dict[str, Any]) -> Row:
        relative = evidence["destination"]
        uow.cur.execute("SELECT * FROM projects WHERE relative_path = %s AND status <> 'UNREGISTERED'", (relative,))
        project = uow.cur.fetchone()
        if project is None:
            principal = Principal.parse(row["requested_by"])
            with uow.cur.connection.transaction():
                # The reserved name is the slug, passed explicitly (never derived again).
                project = self.projects.register(uow, principal=principal, path=relative, name=row["name"], slug=row["name"],
                                                 reservation=row["id"])
            self.projects.scan(uow, slug=project["slug"], principal=principal)
        return project

    def sync(self) -> dict[str, int]:
        """Scheduler hook: resume creations whose last attempt failed for a transient reason."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT id FROM child_creations WHERE kind = 'PROJECT' AND state = 'RUNNING' "
                            "AND updated_at < now() - %s LIMIT 5", (RETRY_AFTER,))
            due = [r["id"] for r in uow.cur.fetchall()]
        for operation_id in due:
            self.run(operation_id)
        return {"project_creations": len(due)}
