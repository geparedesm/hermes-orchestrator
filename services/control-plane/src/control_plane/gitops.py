"""Git isolation, reconciliation, and approved merges (MASTER_SPEC sections 36-41; ARCHITECTURE.md section 6.4).

The control plane decides; Git Service acts and re-checks every rule itself.

Flow for one task:
  workspace()        isolated clone + branch per workspace, task base commit pinned
  collect()          hardened fetch of each workspace's commits
  check_divergence() human change classification (section 37), also run by `monitor`
  integrate()        merge collected work onto the current target, then retest it
  push()/pull_request()  GitHub projects
  request_merge()    MERGE approval bound to target commit, head commit, method, and PR
  (approval)         consume -> MERGING -> Git Service merge -> VERIFYING -> post-merge tests -> DONE
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from ho_core.enums import ApprovalAction, Risk, TaskState
from ho_core.gitpolicy import sign_merge, task_branch
from ho_core.ids import uuid7
from ho_core.statemachine import ACTIVE_STATES, Trigger

from .approvals import Approvals
from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import ApiError, BadRequest, Conflict, UpstreamError
from .events import record_event
from .executions import ExecutionRequest, Executions
from .tasks import Tasks

log = logging.getLogger(__name__)
S = TaskState
CONTROL_PLANE = Principal("control-plane", "git")
HUMAN_CONFLICT = "human_change_conflict"
_LEVELS = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


class GitChanges:
    def __init__(self, ctx: Context, tasks: Tasks, approvals: Approvals, executions: Executions) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.approvals = approvals
        self.executions = executions
        approvals.register_handler(ApprovalAction.MERGE, self._on_merge_decision)
        approvals.register_handler(ApprovalAction.HIGH_RISK_OPERATION, self._on_conflict_decision)
        executions.workspace_check = self.check_workspace
        # Set by verification.Verifications and verification.QualityGate (Phase 6).
        self.verifications: Any = None
        self.quality_gate: Any = None

    # ---------------------------------------------------------------- helpers

    def _context(self, uow: UnitOfWork, task_key: str, *, lock: bool = False) -> tuple[Row, Row, dict[str, Any]]:
        task = self.tasks.get(uow, task_key, lock=lock)
        uow.cur.execute("SELECT * FROM projects WHERE id = %s", (task["project_id"],))
        project = uow.cur.fetchone()
        assert project is not None
        uow.cur.execute("SELECT effective_config, effective_hash FROM project_configs WHERE id = %s", (task["config_id"],))
        config = uow.cur.fetchone()
        if config is None:
            raise Conflict(f"{task_key} has no pinned project configuration")
        return task, project, {**config["effective_config"], "_hash": config["effective_hash"]}

    @staticmethod
    def _policy(config: dict[str, Any]) -> dict[str, Any]:
        git = config.get("git") or {}
        return {"prefix": git.get("branch_prefix", "hermes/"), "protected": list(git.get("protected_branches") or [])}

    def _changes(self, uow: UnitOfWork, task: Row, *, lock: bool = False) -> Row | None:
        uow.cur.execute(f"SELECT * FROM git_changes WHERE task_id = %s{' FOR UPDATE' if lock else ''}", (task["id"],))
        return uow.cur.fetchone()

    def _workspaces(self, uow: UnitOfWork, task: Row, kinds: tuple[str, ...] = ("DEVELOPMENT", "CONFLICT")) -> list[Row]:
        uow.cur.execute("SELECT * FROM workspaces WHERE task_id = %s AND status = 'ACTIVE' AND kind = ANY(%s) ORDER BY created_at",
                        (task["id"], list(kinds)))
        return uow.cur.fetchall()

    def _event(self, uow: UnitOfWork, task: Row, event: str, summary: str, data: dict[str, Any] | None = None,
               actor: str = "git-service") -> None:
        record_event(uow.cur, event, actor=actor, project_id=task["project_id"], task_id=task["id"], summary=summary,
                     data=data or {}, pending=uow.events)

    def check_workspace(self, uow: UnitOfWork, task: Row, workspace: str) -> None:
        """Executions may mount only an active workspace registered to their own task."""
        name = workspace.rsplit("/", 1)[-1]
        uow.cur.execute("SELECT 1 FROM workspaces WHERE task_id = %s AND name = %s AND status = 'ACTIVE'", (task["id"], name))
        if uow.cur.fetchone() is None:
            raise BadRequest(f"workspace {name} is not an active workspace of {task['key']}; create it with `ho git workspace`")

    # ------------------------------------------------------------- workspaces

    def workspace(self, uow: UnitOfWork, task_key: str, *, principal: Principal, suffix: str | None = None,
                  kind: str = "DEVELOPMENT", base_ref: str | None = None) -> Row:
        task, project, config = self._context(uow, task_key, lock=True)
        if kind == "DEVELOPMENT" and S(task["state"]) not in ACTIVE_STATES:
            raise Conflict(f"{task_key} is {task['state']}; workspaces are created for active tasks")
        policy = self._policy(config)
        target = task["target_branch"] or project["default_branch"] or "main"
        uow.cur.execute("SELECT count(*) AS n FROM workspaces WHERE task_id = %s", (task["id"],))
        suffix = suffix or f"w{uow.cur.fetchone()['n'] + 1}"  # type: ignore[index]
        name = f"{task_key.lower()}-{suffix}"
        branch = task_branch(policy["prefix"], task_key, suffix)
        pin = None
        if base_ref is None:
            if task["base_commit"]:
                base_ref = f"refs/hermes/tasks/{task_key}/base"
            else:
                base_ref, pin = target, f"refs/hermes/tasks/{task_key}/base"
        prepared = self.ctx.git.prepare_workspace(project["relative_path"], name, branch, base_ref, pin)
        if not task["base_commit"]:
            uow.cur.execute("UPDATE tasks SET base_commit = %s, target_branch = %s WHERE id = %s",
                            (prepared["base_sha"], target, task["id"]))
            uow.cur.execute(
                "INSERT INTO git_changes (task_id, project_id, target_branch, base_sha) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (task_id) DO NOTHING",
                (task["id"], project["id"], target, prepared["base_sha"]),
            )
        uow.cur.execute(
            """
            INSERT INTO workspaces (id, project_id, task_id, name, kind, path, branch, base_sha, head_sha, status, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s) RETURNING *
            """,
            (uuid7(), project["id"], task["id"], name, kind, prepared["path"], branch, prepared["base_sha"],
             prepared["base_sha"], principal.value),
        )
        row = uow.cur.fetchone()
        assert row is not None
        self._event(uow, task, "WORKSPACE_CREATED", f"{kind.lower()} workspace {name} on {branch} at {prepared['base_sha'][:12]}",
                    {"workspace": name, "branch": branch, "base_sha": prepared["base_sha"]})
        return row

    def collect(self, uow: UnitOfWork, task_key: str) -> list[dict[str, Any]]:
        task, project, _ = self._context(uow, task_key, lock=True)
        results = []
        for ws in self._workspaces(uow, task):
            collected = self.ctx.git.collect(project["relative_path"], ws["name"], ws["branch"], ws["base_sha"])
            new = collected["head_sha"] != ws["head_sha"]
            uow.cur.execute("UPDATE workspaces SET head_sha = %s, collected_at = now() WHERE id = %s",
                            (collected["head_sha"], ws["id"]))
            if new and collected["commits"]:
                self._event(uow, task, "COMMIT_CREATED",
                            f"{len(collected['commits'])} commit(s) in {ws['name']}, {collected['files_changed']} file(s) changed",
                            {"workspace": ws["name"], "head_sha": collected["head_sha"],
                             "commits": [c["sha"] for c in collected["commits"][:50]]})
            if not collected["descends_from_base"]:
                self._event(uow, task, "BLOCKED", f"{ws['name']} no longer descends from the task base; its history was rewritten",
                            {"workspace": ws["name"]})
            results.append({"workspace": ws["name"], **collected})
        return results

    # ------------------------------------------------------------- divergence

    def check_divergence(self, uow: UnitOfWork, task_key: str) -> dict[str, Any]:
        """Classify human changes on the target since the task's base and act on the level (section 37)."""
        task, project, config = self._context(uow, task_key, lock=True)
        changes = self._changes(uow, task, lock=True)
        if changes is None:
            raise Conflict(f"{task_key} has no workspace yet")
        heads = [changes["integration_ref"]] if changes["integration_ref"] else \
            [f"refs/hermes/workspaces/{w['name']}" for w in self._workspaces(uow, task) if w["collected_at"]]
        if not heads:
            heads = [f"refs/hermes/tasks/{task_key}/base"]
        verification = config.get("verification") or {}
        results = [self.ctx.git.divergence(project["relative_path"], base_sha=changes["base_sha"], head_ref=ref,
                                           target_branch=changes["target_branch"],
                                           sensitive_paths=verification.get("sensitive_paths", []),
                                           critical_paths=verification.get("critical_paths", []))
                   for ref in heads]
        worst = max(results, key=lambda r: _LEVELS.index(r["level"]))
        level = worst["level"]
        previous_target = changes["divergence_target_sha"]
        uow.cur.execute(
            "UPDATE git_changes SET divergence = %s, divergence_level = %s, divergence_target_sha = %s, "
            "reconcile_required = reconcile_required OR %s, updated_at = now() WHERE task_id = %s",
            (jsonb({"checks": results}), level, worst["target_sha"], level in ("MEDIUM", "HIGH", "CRITICAL"), task["id"]),
        )
        if level != "NONE" and (worst["target_sha"] != previous_target or worst["uncommitted_changes"]):
            files = sorted({f["path"] for r in results for f in r["overlapping"]})
            self._event(uow, task, "HUMAN_CHANGE_DETECTED",
                        f"{level}: {worst['human_commits']} new commit(s) on {changes['target_branch']}"
                        + (" and uncommitted changes" if worst["uncommitted_changes"] else "")
                        + (f"; overlapping files: {', '.join(files[:5])}" if files else ""),
                        {"level": level, "target_sha": worst["target_sha"], "overlapping": files[:100]})
            self._act_on_level(uow, task, project, config, level, worst)
        return {"task": task_key, "level": level, "checks": results}

    def _act_on_level(self, uow: UnitOfWork, task: Row, project: Row, config: dict[str, Any], level: str,
                      result: dict[str, Any]) -> None:
        state = S(task["state"])
        if state == S.READY_FOR_MERGE:
            # The approved state would no longer match: reintegrate before asking for a merge.
            self.approvals.invalidate_open(uow, project_id=project["id"], task_id=task["id"], action=ApprovalAction.MERGE,
                                           reason=f"{task['target_branch'] or 'target'} changed after the merge request")
            self.tasks.transition(uow, task, S.RUNNING, trigger=Trigger.SYSTEM, actor="control-plane",
                                  reason="target branch changed; reintegration needed")
            return
        if level == "CRITICAL" and state in ACTIVE_STATES:
            subject = {"kind": HUMAN_CONFLICT, "target_sha": result["target_sha"], "head_sha": result["head_sha"],
                       "overlapping": result["overlapping"][:50]}
            self.approvals.request(uow, action=ApprovalAction.HIGH_RISK_OPERATION, project_id=project["id"], task_id=task["id"],
                                   subject=subject, config_hash=config["_hash"], risk=Risk.CRITICAL,
                                   summary=f"{task['key']}: human changes contradict the task's changes; continue anyway?",
                                   requested_by="control-plane", from_task_state=task["state"])
            self.tasks.transition(uow, task, S.APPROVAL_REQUIRED, trigger=Trigger.SYSTEM, actor="control-plane",
                                  reason="critical human change conflict (section 37)")

    def _on_conflict_decision(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        if (approval["subject"] or {}).get("kind") != HUMAN_CONFLICT or approval["task_id"] is None:
            return
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (approval["task_id"],))
        task = uow.cur.fetchone()
        if task is None or task["state"] != S.APPROVAL_REQUIRED:
            return
        if approved:
            self.tasks.transition(uow, task, S(task["resume_state"]), trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason="continue after human change conflict; reconciliation required")
        else:
            self.tasks.transition(uow, task, S.BLOCKED, trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason="human change conflict: task stopped for replanning")

    def monitor(self) -> int:
        """Scheduler hook: re-check tasks whose target branch moved (human changes while agents work)."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                """
                SELECT t.key, p.relative_path, g.target_branch, g.divergence_target_sha, g.base_sha
                FROM git_changes g JOIN tasks t ON t.id = g.task_id JOIN projects p ON p.id = g.project_id
                WHERE t.state = ANY(%s) LIMIT 50
                """,
                ([s.value for s in ACTIVE_STATES],),
            )
            rows = uow.cur.fetchall()
        checked = 0
        for row in rows:
            try:
                current = self.ctx.git.refs(row["relative_path"], [row["target_branch"]])["refs"][row["target_branch"]]
                if current and current != (row["divergence_target_sha"] or row["base_sha"]):
                    with self.ctx.unit_of_work() as uow:
                        self.check_divergence(uow, row["key"])
                    checked += 1
            except ApiError as exc:
                log.warning("divergence check of %s deferred: %s", row["key"], exc)
        return checked

    # ------------------------------------------------------------ integration

    def integrate(self, uow: UnitOfWork, task_key: str, *, retest: bool = True) -> dict[str, Any]:
        """Merge collected workspace work onto the current target (section 38), then retest the result."""
        self.collect(uow, task_key)
        task, project, config = self._context(uow, task_key, lock=True)
        changes = self._changes(uow, task, lock=True)
        if changes is None:
            raise Conflict(f"{task_key} has no workspace yet")
        heads = [f"refs/hermes/workspaces/{w['name']}" for w in self._workspaces(uow, task)
                 if w["head_sha"] and w["head_sha"] != w["base_sha"]]
        if not heads:
            raise Conflict(f"{task_key} has no committed work to integrate")
        result = self.ctx.git.integrate(project["relative_path"], task_key, changes["target_branch"], heads)
        if not result["ok"]:
            uow.cur.execute("UPDATE git_changes SET integration_conflicts = %s, updated_at = now() WHERE task_id = %s",
                            (jsonb({"files": result["conflicts"], "ref": result["conflicting_ref"]}), task["id"]))
            self._event(uow, task, "INTEGRATION_CONFLICT",
                        f"{result['conflicting_ref']} conflicts with {changes['target_branch']} in {', '.join(result['conflicts'][:5])}",
                        {"conflicts": result["conflicts"], "ref": result["conflicting_ref"]})
            return result
        uow.cur.execute(
            """
            UPDATE git_changes SET integration_ref = %s, integration_sha = %s, integration_target_sha = %s,
                   integration_conflicts = NULL, reconcile_required = false, divergence_target_sha = %s,
                   retest_status = NULL, retest_execution_id = NULL, updated_at = now()
            WHERE task_id = %s
            """,
            (result["integration_ref"], result["integration_sha"], result["target_sha"], result["target_sha"], task["id"]),
        )
        self._event(uow, task, "INTEGRATION_COMPLETED",
                    f"integrated {len(heads)} workspace(s) onto {changes['target_branch']} {result['target_sha'][:12]}: "
                    f"{result['files_changed']} file(s), +{result['insertions']}/-{result['deletions']}",
                    {"integration_sha": result["integration_sha"], "target_sha": result["target_sha"]})
        if retest and self.verifications is not None:
            verification = self.verifications.start(uow, task_key, ref=result["integration_ref"], purpose="INTEGRATION")
            uow.cur.execute("UPDATE git_changes SET retest_status = 'RUNNING' WHERE task_id = %s", (task["id"],))
            result["verification"] = str(verification["id"])
        return result

    def resolve_conflicts(self, uow: UnitOfWork, task_key: str, *, provider: str, principal: Principal) -> dict[str, Any]:
        """Agent-assisted reconciliation (section 38): a clone at the target with the conflicting work merged
        and its conflicts left in place, and a DEVELOPER execution asked to resolve them."""
        task, project, _ = self._context(uow, task_key, lock=True)
        changes = self._changes(uow, task)
        conflicts = (changes or {}).get("integration_conflicts") or {}
        if not conflicts:
            raise Conflict(f"{task_key} has no integration conflict to resolve")
        uow.cur.execute("SELECT count(*) AS n FROM workspaces WHERE task_id = %s", (task["id"],))
        suffix = f"resolve{uow.cur.fetchone()['n'] + 1}"  # type: ignore[index]
        name = f"{task_key.lower()}-{suffix}"
        branch = task_branch(self._policy(self._context(uow, task_key)[2])["prefix"], task_key, suffix)
        prepared = self.ctx.git.conflict_workspace(project["relative_path"], name, branch, changes["target_branch"],  # type: ignore[index]
                                                   conflicts["ref"])
        # Work already integrated through this workspace replaces the conflicting one.
        uow.cur.execute("UPDATE workspaces SET status = 'RETAINED' WHERE task_id = %s AND name = %s",
                        (task["id"], conflicts["ref"].rsplit("/", 1)[-1]))
        uow.cur.execute(
            "INSERT INTO workspaces (id, project_id, task_id, name, kind, path, branch, base_sha, head_sha, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'CONFLICT', %s, %s, %s, %s, 'ACTIVE', %s)",
            (uuid7(), project["id"], task["id"], name, prepared["path"], branch, prepared["base_sha"], prepared["base_sha"],
             principal.value),
        )
        files = ", ".join(prepared["conflicts"])
        prompt = (f"A merge of the task's work onto {changes['target_branch']} is in progress in /workspace and stopped "  # type: ignore[index]
                  f"with conflicts in: {files}. The target branch contains changes made by a human; keep their intent. "
                  "Resolve every conflict, run the project's tests, and commit the merge (git commit --no-edit). "
                  "If the two changes contradict each other and cannot both be kept, do not guess: report blocked.")
        execution = self.executions.request(uow, principal=principal, task_key=task_key, req=ExecutionRequest(
            role="DEVELOPER", provider=provider, prompt=prompt, workspace=prepared["path"],
            capabilities={"workspace": "WRITE", "git": "LOCAL_COMMIT", "tests": "EXECUTE"}))
        return {"workspace": name, "conflicts": prepared["conflicts"], "execution": str(execution["id"])}

    # ----------------------------------------------------------------- GitHub

    def _github(self, uow: UnitOfWork, task_key: str) -> tuple[Row, Row, dict[str, Any], Row]:
        task, project, config = self._context(uow, task_key, lock=True)
        changes = self._changes(uow, task, lock=True)
        if changes is None or not changes["integration_sha"]:
            raise Conflict(f"integrate {task_key} before pushing")
        return task, project, config, changes

    def push(self, uow: UnitOfWork, task_key: str) -> dict[str, Any]:
        task, project, config, changes = self._github(uow, task_key)
        policy = self._policy(config)
        branch = f"{policy['prefix']}{task_key.lower()}"
        result = self.ctx.git.push(project["relative_path"], ref=changes["integration_ref"], branch=branch,
                                   expected_remote_sha=changes["pushed_sha"], **policy)
        uow.cur.execute("UPDATE git_changes SET remote_branch = %s, pushed_sha = %s, updated_at = now() WHERE task_id = %s",
                        (branch, result["sha"], task["id"]))
        self._event(uow, task, "BRANCH_PUSHED", f"pushed {result['sha'][:12]} to {branch}", {"branch": branch, "sha": result["sha"]})
        return result

    def pull_request(self, uow: UnitOfWork, task_key: str) -> dict[str, Any]:
        task, project, config, changes = self._github(uow, task_key)
        if changes["pushed_sha"] != changes["integration_sha"]:
            self.push(uow, task_key)
            changes = self._changes(uow, task)  # type: ignore[assignment]
        body = "\n".join([
            f"Task {task_key}: {task['title']}", "",
            f"- Base: `{changes['base_sha'][:12]}`; integrated onto `{changes['integration_target_sha'][:12]}`",
            f"- Retest: {changes['retest_status'] or 'not run'}",
            f"- Human changes since base: {changes['divergence_level'] or 'not checked'}", "",
            "Prepared by Hermes Orchestrator. Merging requires an explicit human approval in the platform.",
        ])
        result = self.ctx.git.pull_request(project["relative_path"], branch=changes["remote_branch"], base=changes["target_branch"],
                                           title=f"{task_key}: {task['title']}"[:250], body=body, **self._policy(config))
        uow.cur.execute("UPDATE git_changes SET pr_number = %s, pr_url = %s, updated_at = now() WHERE task_id = %s",
                        (result["number"], result["url"], task["id"]))
        self._event(uow, task, "PR_CREATED" if result["created"] else "PR_UPDATED", f"pull request #{result['number']}",
                    {"number": result["number"], "url": result["url"]})
        return result

    def checks(self, uow: UnitOfWork, task_key: str) -> dict[str, Any]:
        task, project, _, changes = self._github(uow, task_key)
        if not changes["pr_number"]:
            raise Conflict(f"{task_key} has no pull request")
        result = self.ctx.git.pr_checks(project["relative_path"], changes["pr_number"])
        uow.cur.execute("UPDATE git_changes SET ci_status = %s, updated_at = now() WHERE task_id = %s",
                        (jsonb(result), task["id"]))
        return result

    # ------------------------------------------------------------------ merge

    def _merge_subject(self, uow: UnitOfWork, task: Row, project: Row, config: dict[str, Any], changes: Row) -> dict[str, Any]:
        """The exact state a merge approval binds (SECURITY_MODEL.md section 6.2), read from Git now."""
        method = (config.get("git") or {}).get("merge_method", "merge")
        remote = self.ctx.git.refs(project["relative_path"], [changes["target_branch"]])
        github = remote["remote"]["kind"] == "github" and (config.get("git") or {}).get("require_pull_request", True)
        gate = self.quality_gate.latest_passing(uow, task, changes["integration_sha"]) if self.quality_gate else None
        if github:
            if not changes["pr_number"]:
                raise Conflict("this project merges through a pull request; run `ho git pr` first")
            pr = self.ctx.git.pr_view(project["relative_path"], changes["pr_number"])
            return {"project": project["relative_path"], "target_branch": pr["baseRefName"], "target_sha": pr["base_sha"],
                    "head_sha": pr["headRefOid"], "method": method, "pr_number": changes["pr_number"], "quality_gate": gate}
        return {"project": project["relative_path"], "target_branch": changes["target_branch"],
                "target_sha": remote["refs"][changes["target_branch"]], "head_sha": changes["integration_sha"],
                "method": method, "pr_number": None, "quality_gate": gate}

    def request_merge(self, uow: UnitOfWork, task_key: str, *, principal: Principal) -> Row:
        task, project, config = self._context(uow, task_key, lock=True)
        if task["state"] != S.READY_FOR_MERGE:
            raise Conflict(f"{task_key} is {task['state']}; merges are requested from READY_FOR_MERGE")
        changes = self._changes(uow, task, lock=True)
        if changes is None or not changes["integration_sha"]:
            raise Conflict(f"{task_key} has no integrated change")
        if changes["retest_status"] in ("RUNNING", "FAILED"):
            raise Conflict(f"the integrated change's tests are {changes['retest_status']}")
        subject = self._merge_subject(uow, task, project, config, changes)
        if self.quality_gate is not None and not subject["quality_gate"]:
            raise Conflict(f"{task_key} has no passing Quality Gate evaluation for its integrated change")
        if subject["pr_number"] is None and subject["target_sha"] != changes["integration_target_sha"]:
            raise Conflict(f"{changes['target_branch']} moved since integration; run `ho git integrate {task_key}` again")
        if subject["pr_number"] is not None and subject["head_sha"] != changes["integration_sha"]:
            raise Conflict("the pull request head is not the integrated change; push it again")
        self.approvals.invalidate_open(uow, project_id=project["id"], task_id=task["id"], action=ApprovalAction.MERGE,
                                       reason="superseded by a new merge request")
        approval = self.approvals.request(
            uow, action=ApprovalAction.MERGE, project_id=project["id"], task_id=task["id"], subject=subject,
            config_hash=config["_hash"], risk=Risk.MEDIUM, requested_by=principal.value, from_task_state=task["state"],
            summary=f"merge {task_key} into {subject['target_branch']} ({subject['method']}"
                    + (f", PR #{subject['pr_number']}" if subject["pr_number"] else "") + ")",
        )
        uow.cur.execute("UPDATE git_changes SET merge_approval_id = %s, updated_at = now() WHERE task_id = %s",
                        (approval["id"], task["id"]))
        return approval

    def _on_merge_decision(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (approval["task_id"],))
        task = uow.cur.fetchone()
        if task is None or task["state"] != S.READY_FOR_MERGE or approval["from_task_state"] != S.READY_FOR_MERGE:
            return
        if not approved:
            self.tasks.transition(uow, task, S.FIX_REQUIRED, trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason="merge rejected by a human")
            return
        _, project, config = self._context(uow, task["key"], lock=True)
        changes = self._changes(uow, task, lock=True)
        assert changes is not None
        try:
            subject = self._merge_subject(uow, task, project, config, changes)
        except ApiError as exc:
            subject = {"unavailable": str(exc)}
        if not self.approvals.consume(uow, approval, subject=subject, config_hash=config["_hash"]):
            self.tasks.transition(uow, task, S.RUNNING, trigger=Trigger.SYSTEM, actor="control-plane",
                                  reason="the approved state changed before the merge; reintegration needed")
            return
        self.tasks.transition(uow, task, S.MERGING, trigger=Trigger.APPROVAL, actor=principal.value,
                              reason=f"merge approved by {principal.value}")
        uow.cur.execute(
            "INSERT INTO operation_intents (id, project_id, task_id, kind, target, request, state) VALUES (%s, %s, %s, 'MERGE', %s, %s, 'PENDING')",
            (uuid7(), project["id"], task["id"], str(approval["id"]), jsonb({"subject": subject})),
        )
        task_key = task["key"]
        uow.after_commit.append(lambda: self.execute_merge(task_key))

    def execute_merge(self, task_key: str) -> None:
        """Call Git Service with a signed, single-approval authorization. Idempotent: Git Service
        records each approval's merge, so a retry after a lost response returns the same commit."""
        with self.ctx.unit_of_work() as uow:
            task, project, _ = self._context(uow, task_key)
            if task["state"] != S.MERGING:
                return
            changes = self._changes(uow, task)
            assert changes is not None
            uow.cur.execute("SELECT * FROM approvals WHERE id = %s", (changes["merge_approval_id"],))
            approval = uow.cur.fetchone()
            assert approval is not None
            token = sign_merge(self.ctx.merge_key, approval_id=str(approval["id"]), subject=approval["subject"],
                               expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
        try:
            result = self.ctx.git.merge(project["relative_path"], task_key, token)
        except (Conflict, BadRequest, ApiError) as exc:
            if isinstance(exc, UpstreamError):
                log.warning("merge of %s not confirmed yet; will retry: %s", task_key, exc)
                return
            with self.ctx.unit_of_work() as uow:
                task = self.tasks.get(uow, task_key, lock=True)
                self._intent(uow, task, "FAILED", str(exc))
                self.tasks.transition(uow, task, S.BLOCKED, trigger=Trigger.SYSTEM, actor="git-service",
                                      reason=f"merge refused: {str(exc)[:200]}")
            return
        with self.ctx.unit_of_work() as uow:
            task, project, config = self._context(uow, task_key, lock=True)
            if task["state"] != S.MERGING:
                return
            changes = self._changes(uow, task, lock=True)
            assert changes is not None
            uow.cur.execute(
                "UPDATE git_changes SET merge_commit_sha = %s, merged_at = now(), merged_by_approval_id = %s, updated_at = now() "
                "WHERE task_id = %s", (result["merge_sha"], changes["merge_approval_id"], task["id"]))
            self._intent(uow, task, "CONFIRMED", None)
            self._event(uow, task, "MERGE_COMPLETED", f"merged into {result['target_branch']} as {result['merge_sha'][:12]} "
                        f"({result['method']})", {"merge_sha": result["merge_sha"], "approval_id": str(changes["merge_approval_id"])})
            task = self.tasks.transition(uow, task, S.VERIFYING, trigger=Trigger.SYSTEM, actor="git-service",
                                         reason="merge confirmed; post-merge verification")
            uow.cur.execute("UPDATE git_changes SET post_merge_status = 'RUNNING' WHERE task_id = %s", (task["id"],))
            verification = self.verifications.start(uow, task_key, ref=f"refs/hermes/merges/{changes['merge_approval_id']}",
                                                    purpose="POST_MERGE")
            uow.cur.execute("UPDATE git_changes SET verification_execution_id = NULL WHERE task_id = %s", (task["id"],))
            self._event(uow, task, "POST_MERGE_STARTED", f"post-merge verification {str(verification['id'])[:8]} started",
                        actor="control-plane")
            if changes["remote_branch"] and (config.get("git") or {}).get("delete_remote_branch_after_merge", True):
                path, branch, policy = project["relative_path"], changes["remote_branch"], self._policy(config)
                uow.after_commit.append(lambda: self._delete_remote(path, branch, policy))

    def _delete_remote(self, path: str, branch: str, policy: dict[str, Any]) -> None:
        try:
            self.ctx.git.delete_branch(path, branch=branch, **policy)
        except ApiError as exc:
            log.warning("could not delete remote branch %s: %s", branch, exc)

    def _intent(self, uow: UnitOfWork, task: Row, state: str, error: str | None) -> None:
        uow.cur.execute(
            "UPDATE operation_intents SET state = %s, attempts = attempts + 1, last_error = %s, updated_at = now() "
            "WHERE task_id = %s AND kind = 'MERGE' AND state IN ('PENDING', 'SENT')", (state, error, task["id"]))

    def _finish_verification(self, uow: UnitOfWork, task: Row, *, passed: bool, note: str) -> None:
        uow.cur.execute("UPDATE git_changes SET post_merge_status = COALESCE(post_merge_status, %s) WHERE task_id = %s",
                        ("PASSED" if passed else "FAILED", task["id"]))
        if passed:
            self._event(uow, task, "POST_MERGE_VERIFIED", note, actor="control-plane")
            task = self.tasks.transition(uow, task, S.DONE, trigger=Trigger.SYSTEM, actor="control-plane", reason=note)
            self._cleanup_workspaces(uow, task)
        else:
            self.tasks.transition(uow, task, S.BLOCKED, trigger=Trigger.SYSTEM, actor="control-plane", reason=note)

    def _cleanup_workspaces(self, uow: UnitOfWork, task: Row) -> None:
        """Workspaces are removed after a successful merge (DATA_MODEL.md retention)."""
        uow.cur.execute("SELECT w.*, p.relative_path FROM workspaces w JOIN projects p ON p.id = w.project_id "
                        "WHERE w.task_id = %s AND w.status <> 'REMOVED'", (task["id"],))
        for ws in uow.cur.fetchall():
            path, name, ws_id = ws["relative_path"], ws["name"], ws["id"]

            def remove(path: str = path, name: str = name, ws_id: UUID = ws_id) -> None:
                try:
                    self.ctx.git.remove_workspace(path, name)
                    with self.ctx.unit_of_work() as inner:
                        inner.cur.execute("UPDATE workspaces SET status = 'REMOVED', removed_at = now() WHERE id = %s", (ws_id,))
                except ApiError as exc:
                    log.warning("could not remove workspace %s: %s", name, exc)
            uow.after_commit.append(remove)

    # ------------------------------------------------------------------- sync

    def sync(self) -> dict[str, int]:
        """Scheduler hook: retry unconfirmed merges and watch for human changes."""
        stats = {"merges": 0, "verified": 0}
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT key FROM tasks WHERE state = 'MERGING'")
            merging = [r["key"] for r in uow.cur.fetchall()]
        for key in merging:
            self.execute_merge(key)
            stats["merges"] += 1
        stats["divergence_checks"] = self.monitor()
        return stats
