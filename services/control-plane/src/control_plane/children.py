"""Child projects created from a parent's template (docs/design/child-projects.md).

A person requests a child (an Odoo module of odoo-base). The request pins everything the creation uses (parent
commit, template tree, destination, GitHub repository) in a CREATE_CHILD approval; nothing is written before it is
approved. Once approved, the creation is a durable operation (`child_creations`) that advances one recorded step at
a time through Git Service (publish, then GitHub), then registers and scans the child. A retry continues from the
last recorded step; anything found without this operation's evidence stops it in NEEDS_RECOVERY for a person.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from ho_core.enums import ApprovalAction, ProjectStatus, Risk
from ho_core.gitpolicy import CHILD_NAME, sign_child
from ho_core.ids import uuid7

from .approvals import Approvals
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import ApiError, BadRequest, Conflict, Forbidden, NotFound
from .events import record_event
from .gitsvc import Unowned
from .auth import Principal
from .projects import Projects

log = logging.getLogger(__name__)

_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE = re.compile(r"[\"'\\{}<>&`]")
LIVE = ("AWAITING_APPROVAL", "RUNNING", "NEEDS_RECOVERY")
RETRY_AFTER = timedelta(minutes=2)  # a RUNNING creation untouched this long is resumed by the scheduler


def _line(value: str, limit: int) -> str:
    return _CONTROL.sub(" ", value).strip()[:limit]


class Children:
    def __init__(self, ctx: Context, projects: Projects, approvals: Approvals) -> None:
        self.ctx = ctx
        self.projects = projects
        self.approvals = approvals
        approvals.register_handler(ApprovalAction.CREATE_CHILD, self._on_decision)

    # ------------------------------------------------------------------ queries

    def _settings(self, uow: UnitOfWork, parent: Row) -> tuple[dict[str, Any], Row]:
        config = self.projects.active_config(uow, parent["id"])
        settings = (config["effective_config"] if config else {}).get("children")
        if parent["status"] != ProjectStatus.PROJECT_READY or not config or not settings:
            raise Conflict(f"{parent['slug']} cannot create child projects: it must be ready and declare `children` in "
                           ".hermes/project.yaml")
        return settings, config

    def list(self, uow: UnitOfWork, slug: str) -> list[dict[str, Any]]:
        parent = self.projects.get(uow, slug)
        uow.cur.execute("SELECT c.*, p.slug AS child_slug, a.number AS approval_number FROM child_creations c "
                        "LEFT JOIN projects p ON p.id = c.child_project_id LEFT JOIN approvals a ON a.id = c.approval_id "
                        "WHERE c.parent_id = %s ORDER BY c.created_at DESC LIMIT 50", (parent["id"],))
        return [self.view(r) for r in uow.cur.fetchall()]

    @staticmethod
    def view(row: Row) -> dict[str, Any]:
        params = row["subject"].get("params") or {}
        return {"id": str(row["id"]), "name": row["name"], "title": params.get("title"), "state": row["state"],
                "step": row["step"], "destination": row["destination"], "github_repo": row["github_repo"],
                "child": row.get("child_slug"), "approval_number": row.get("approval_number"), "error": row["error"],
                "requested_by": row["requested_by"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    # ----------------------------------------------------------------- commands

    def request(self, uow: UnitOfWork, *, principal: Principal, slug: str, name: str, title: str,
                description: str) -> dict[str, Any]:
        """Pin what the creation will use and ask a person to approve it. Writes nothing outside the database."""
        self._human(principal)
        parent = self.projects.get(uow, slug, lock=True)
        settings, config = self._settings(uow, parent)
        if not CHILD_NAME.match(name):
            raise BadRequest("name must be lowercase letters, digits, and underscores, starting with a letter (2-41 characters)")
        title, description = _line(title, 120), _line(description, 500)
        if not title:
            raise BadRequest("a title is required")
        if _UNSAFE.search(title + description):
            # Values land inside Python strings, XML, and Markdown of the template.
            raise BadRequest('title and description may not contain quotes, backslashes, braces, or < > &')
        github = settings["github"]
        repo = github["repo_name"].replace("{name}", name)
        if not _REPO.match(repo):
            raise BadRequest(f"the repository name {repo!r} is not valid")
        plan = self.ctx.git.child_plan(parent["relative_path"], template=settings["template"],
                                       children_path=settings["path"], name=name)
        operation_id = uuid7()
        params = {**(settings.get("variables") or {}), "title": title, "description": description}
        subject = {"operation_id": str(operation_id), "parent": parent["relative_path"], "parent_sha": plan["parent_sha"],
                   "template_path": settings["template"], "template_tree": plan["template_tree"],
                   "children_path": settings["path"], "name": name, "params": params, "owner": github["owner"],
                   "repo": repo, "private": bool(github.get("private", True)),
                   "commit_message": settings.get("commit_message") or "[ADD] Scaffold {name}"}
        label = settings.get("label") or "child project"
        full_repo = f"{github['owner']}/{repo}"
        try:
            with uow.cur.connection.transaction():
                uow.cur.execute(
                    "INSERT INTO child_creations (id, parent_id, name, subject, destination, github_repo, state, requested_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'AWAITING_APPROVAL', %s)",
                    (operation_id, parent["id"], name, jsonb(subject), plan["destination"], full_repo, principal.value))
                approval = self.approvals.request(
                    uow, action=ApprovalAction.CREATE_CHILD, project_id=parent["id"], subject=subject,
                    config_hash=config["effective_hash"], risk=Risk.MEDIUM, requested_by=principal.value,
                    summary=f"Create {label} {name} as the {'private' if subject['private'] else 'public'} repository "
                            f"{full_repo} from {slug}@{plan['parent_sha'][:12]}")
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"{name} or {full_repo} is already being created, or another {label} is waiting for a decision "
                           f"in {slug}") from exc
        uow.cur.execute("UPDATE child_creations SET approval_id = %s WHERE id = %s RETURNING *", (approval["id"], operation_id))
        created = uow.cur.fetchone()
        record_event(uow.cur, "CHILD_PROJECT_REQUESTED", actor=principal.value, project_id=parent["id"],
                     summary=f"{label} {name} requested in {slug}", data={"operation": str(operation_id), "repo": full_repo},
                     pending=uow.events)
        return {**self.view(created), "approval": {"id": str(approval["id"]), "number": approval.get("number")}}

    def retry(self, uow: UnitOfWork, *, principal: Principal, slug: str, operation_id: UUID) -> dict[str, Any]:
        """Resume a creation stopped for a person (NEEDS_RECOVERY) or still running, from its last recorded step."""
        self._human(principal)
        row = self._get(uow, slug, operation_id)
        if row["state"] not in ("RUNNING", "NEEDS_RECOVERY"):
            raise Conflict(f"this creation is {row['state']}; only running or stopped creations can be retried")
        uow.cur.execute("UPDATE child_creations SET state = 'RUNNING', error = NULL, updated_at = now() WHERE id = %s "
                        "RETURNING *", (row["id"],))
        resumed = uow.cur.fetchone()
        record_event(uow.cur, "CHILD_PROJECT_RETRIED", actor=principal.value, project_id=row["parent_id"],
                     summary=f"creation of {row['name']} resumed", data={"operation": str(row["id"])}, pending=uow.events)
        uow.after_commit.append(lambda: self.run(row["id"]))
        return self.view(resumed)

    def abandon(self, uow: UnitOfWork, *, principal: Principal, slug: str, operation_id: UUID) -> dict[str, Any]:
        """Give up a stopped creation. Nothing is deleted: what it made locally or on GitHub stays for a person."""
        self._human(principal)
        row = self._get(uow, slug, operation_id)
        if row["state"] not in ("RUNNING", "NEEDS_RECOVERY"):
            raise Conflict(f"this creation is {row['state']}")
        uow.cur.execute("UPDATE child_creations SET state = 'FAILED', error = coalesce(error, 'abandoned'), updated_at = now() "
                        "WHERE id = %s RETURNING *", (row["id"],))
        abandoned = uow.cur.fetchone()
        record_event(uow.cur, "CHILD_PROJECT_FAILED", actor=principal.value, project_id=row["parent_id"],
                     summary=f"creation of {row['name']} abandoned at step {row['step'].lower()}",
                     data={"operation": str(row["id"]), "evidence": row["evidence"]}, pending=uow.events)
        return self.view(abandoned)

    def _human(self, principal: Principal) -> None:
        # Creating repositories is a person's decision (AD-10): only approvers may ask for it.
        if principal.value not in self.ctx.platform["platform"]["approvers"]:
            raise Forbidden(f"{principal.value} may not create child projects")

    def _get(self, uow: UnitOfWork, slug: str, operation_id: UUID) -> Row:
        parent = self.projects.get(uow, slug)
        uow.cur.execute("SELECT * FROM child_creations WHERE id = %s AND parent_id = %s FOR UPDATE", (operation_id, parent["id"]))
        row = uow.cur.fetchone()
        if row is None:
            raise NotFound("creation not found")
        return row

    # ------------------------------------------------------------------ decision and steps

    def _on_decision(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        uow.cur.execute("SELECT * FROM child_creations WHERE approval_id = %s FOR UPDATE", (approval["id"],))
        row = uow.cur.fetchone()
        if row is None or row["state"] != "AWAITING_APPROVAL":
            return
        if not approved:
            uow.cur.execute("UPDATE child_creations SET state = 'REJECTED', updated_at = now() WHERE id = %s", (row["id"],))
            return
        parent = self._project(uow, row["parent_id"])
        config = self.projects.active_config(uow, parent["id"])
        head = self.ctx.git.inspect(parent["relative_path"]).get("head")
        # What was approved must still be what is there: the parent's commit and its active configuration.
        current = {**row["subject"], "parent_sha": head}
        if config is None or not self.approvals.consume(uow, approval, subject=current, config_hash=config["effective_hash"]):
            uow.cur.execute("UPDATE child_creations SET state = 'INVALIDATED', error = %s, updated_at = now() WHERE id = %s",
                            ("the parent changed after the request; request it again", row["id"]))
            record_event(uow.cur, "BLOCKED", actor="control-plane", project_id=parent["id"],
                         summary=f"{parent['slug']} changed after {row['name']} was requested; request it again",
                         pending=uow.events)
            return
        uow.cur.execute("UPDATE child_creations SET state = 'RUNNING', updated_at = now() WHERE id = %s", (row["id"],))
        uow.after_commit.append(lambda: self.run(row["id"]))

    def _project(self, uow: UnitOfWork, project_id: UUID) -> Row:
        uow.cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        row = uow.cur.fetchone()
        assert row is not None
        return row

    def _authorization(self, row: Row) -> dict[str, Any]:
        # Re-signed for each run from the consumed approval: retries outlive any single token.
        return sign_child(self.ctx.merge_key, approval_id=str(row["approval_id"]), subject=row["subject"],
                          expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))

    def run(self, operation_id: UUID) -> None:
        """Advance a RUNNING creation step by step, recording each result before the next step starts."""
        for _ in range(4):
            try:
                with self.ctx.unit_of_work() as uow:
                    uow.cur.execute("SELECT * FROM child_creations WHERE id = %s FOR UPDATE", (operation_id,))
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
                log.exception("child creation %s failed", operation_id)
                self._stop(operation_id, "RUNNING", f"unexpected error: {exc}"[:500])
                return

    def _stop(self, operation_id: UUID, state: str, error: str) -> None:
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("UPDATE child_creations SET state = %s, error = %s, updated_at = now() WHERE id = %s RETURNING *",
                            (state, error[:500], operation_id))
            row = uow.cur.fetchone()
            if row is not None and state == "NEEDS_RECOVERY":
                record_event(uow.cur, "BLOCKED", actor="control-plane", project_id=row["parent_id"],
                             summary=f"creation of {row['name']} needs you: {error[:300]}",
                             data={"operation": str(operation_id)}, pending=uow.events)

    def _step(self, uow: UnitOfWork, row: Row) -> None:
        parent = self._project(uow, row["parent_id"])
        subject, evidence = row["subject"], dict(row["evidence"])
        if row["step"] == "APPROVED":
            result = self.ctx.git.child_publish(parent["relative_path"], self._authorization(row))
            evidence.update(commit=result["commit"], destination=result["destination"])
            nxt = "PUBLISHED"
        elif row["step"] == "PUBLISHED":
            result = self.ctx.git.child_remote(parent["relative_path"], self._authorization(row), evidence["commit"])
            evidence.update(repo=result["repo"], url=result["url"])
            nxt = "PUSHED"
        else:  # PUSHED: register and scan the child; you approve its configuration as for any project
            child = self._register(uow, parent, row, evidence)
            uow.cur.execute("UPDATE child_creations SET step = 'REGISTERED', state = 'DONE', child_project_id = %s, "
                            "error = NULL, updated_at = now() WHERE id = %s", (child["id"], row["id"]))
            record_event(uow.cur, "CHILD_PROJECT_CREATED", actor="control-plane", project_id=parent["id"],
                         summary=f"{row['name']} created as {evidence.get('repo')}; approve its configuration to start",
                         data={"operation": str(row["id"]), "child": child["slug"], "repo": evidence.get("repo")},
                         pending=uow.events)
            return
        uow.cur.execute("UPDATE child_creations SET step = %s, evidence = %s, error = NULL, updated_at = now() WHERE id = %s",
                        (nxt, jsonb(evidence), row["id"]))
        log.info("child creation %s: %s (%s)", row["id"], nxt, subject["name"])

    def _register(self, uow: UnitOfWork, parent: Row, row: Row, evidence: dict[str, Any]) -> Row:
        relative = f"{parent['relative_path']}/{evidence['destination']}"
        uow.cur.execute("SELECT * FROM projects WHERE relative_path = %s AND status <> 'UNREGISTERED'", (relative,))
        child = uow.cur.fetchone()
        if child is None:
            principal = Principal.parse(row["requested_by"])
            with uow.cur.connection.transaction():
                child = self.projects.register(uow, principal=principal, path=relative, name=row["name"], slug=None)
            uow.cur.execute("UPDATE projects SET parent_project_id = %s WHERE id = %s RETURNING *", (parent["id"], child["id"]))
            child = uow.cur.fetchone()
            self.projects.scan(uow, slug=child["slug"], principal=principal)
        return child

    def sync(self) -> dict[str, int]:
        """Scheduler hook: resume creations whose last attempt failed for a transient reason."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT id FROM child_creations WHERE state = 'RUNNING' AND updated_at < now() - %s LIMIT 5",
                            (RETRY_AFTER,))
            due = [r["id"] for r in uow.cur.fetchall()]
        for operation_id in due:
            self.run(operation_id)
        return {"child_creations": len(due)}
