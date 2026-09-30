"""Test evidence, cross-reviews, and the Quality Gate (MASTER_SPEC sections 50-58; ARCHITECTURE.md section 6.4).

Verifications
  One run per integrated commit (and one after each merge): a fresh clone of the commit, the
  project's test services from its own Compose files on the task's private network, a Test
  Runner execution that runs the planned steps (`ho-verify`), and a Browser Runner execution
  when browser tests are required. Results become `test_runs` rows and artifacts; the
  environment is removed afterwards.
Reviews
  A REVIEWER execution by a provider different from every developer of the change, returning
  the review-result schema (verdict, requirements, findings).
Quality Gate
  Evaluates every required condition against that evidence and records the result. Only a
  passing evaluation moves a task from QUALITY_GATE to READY_FOR_MERGE; a merge approval
  binds the evaluation it was requested on.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from ho_core.enums import ApprovalAction, Risk, TaskState
from ho_core.ids import uuid7
from ho_core.statemachine import Trigger
from ho_core.verification import (
    alternatives,
    assess_risk,
    is_doc_file,
    plan_verification,
    post_merge_steps,
)

from .agentmgr import AgentManagerError
from .approvals import Approvals
from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row, jsonb
from .errors import BadRequest, Conflict
from .events import record_event
from .executions import TERMINAL, ExecutionRequest, Executions
from .gitops import CONTROL_PLANE, GitChanges
from .tasks import Tasks

log = logging.getLogger(__name__)
S = TaskState
GATE_EXCEPTION = "quality_gate_exception"
BLOCKING_ALWAYS = {"HIGH", "CRITICAL"}
STEP_TIMEOUT_SECONDS = 1800


def _artifact_name(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base[37:] if len(base) > 37 and base[36] == "-" else base


class Verifications:
    def __init__(self, ctx: Context, tasks: Tasks, executions: Executions, git: GitChanges) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.executions = executions
        self.git = git
        git.verifications = self

    # ------------------------------------------------------------------ start

    def start(self, uow: UnitOfWork, task_key: str, *, ref: str, purpose: str) -> Row:
        task, project, config = self.git._context(uow, task_key, lock=True)
        changes = self.git._changes(uow, task)
        assert changes is not None
        commit = self.ctx.git.refs(project["relative_path"], [ref])["refs"][ref]
        if not commit:
            raise Conflict(f"{ref} does not exist")
        if purpose == "INTEGRATION":
            diff = self.ctx.git.changes(project["relative_path"], changes["base_sha"], commit)
            remote = self.ctx.git.refs(project["relative_path"], [changes["target_branch"]])["remote"]
            plan = plan_verification(config, [f["path"] for f in diff["files"]], insertions=diff["insertions"],
                                     deletions=diff["deletions"], github=remote["kind"] == "github")
            plan_json = {**plan.as_json(), "step_commands": plan.steps, "changed_files": [f["path"] for f in diff["files"]][:2000]}
        else:
            steps = post_merge_steps(config)
            plan_json = {"steps": [s[0] for s in steps], "step_commands": steps, "browser": False, "requirements": [],
                         "gaps": [], "needs_approval": [],
                         "risk": assess_risk([]).as_json()}
        uow.cur.execute("SELECT count(*) AS n FROM workspaces WHERE task_id = %s AND kind = 'VERIFICATION'", (task["id"],))
        suffix = f"verify{uow.cur.fetchone()['n'] + 1}"  # type: ignore[index]
        ws = self.git.workspace(uow, task_key, principal=CONTROL_PLANE, suffix=suffix, kind="VERIFICATION", base_ref=ref)
        uow.cur.execute(
            "INSERT INTO verifications (id, task_id, project_id, purpose, commit_sha, plan, state, workspace) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'PREPARING', %s) RETURNING *",
            (uuid7(), task["id"], project["id"], purpose, commit, jsonb(plan_json), ws["path"]),
        )
        row = uow.cur.fetchone()
        assert row is not None
        record_event(uow.cur, "TEST_STARTED", actor="control-plane", project_id=project["id"], task_id=task["id"],
                     summary=f"{purpose.lower().replace('_', '-')} verification of {commit[:12]}: "
                             f"{', '.join(plan_json['steps']) or 'no configured steps'}"
                             + (" + browser" if plan_json.get("browser") else "")
                             + (f" (risk {plan_json['risk']['risk']})" if purpose == "INTEGRATION" else ""),
                     data={"verification_id": str(row["id"]), "commit": commit}, pending=uow.events)
        verification_id = row["id"]
        uow.after_commit.append(lambda: self._launch(verification_id))
        return row

    def _launch(self, verification_id: UUID) -> None:
        with self.ctx.unit_of_work() as uow:
            row, task, project, config = self._load(uow, verification_id)
            if row["state"] != "PREPARING":
                return
        environment: dict[str, Any] | None = None
        test_env = config.get("test_environment") or {}
        if test_env.get("compose_files") or test_env.get("services"):
            try:
                environment = self.ctx.agents.start_environment(task["key"], {  # type: ignore[union-attr]
                    "project": project["slug"], "project_path": project["relative_path"],
                    "workspace": f"{project['relative_path']}/{row['workspace']}",
                    "compose_files": test_env.get("compose_files", []), "services": test_env.get("services", []),
                    "startup_timeout": int(test_env.get("startup_timeout_seconds", 180))})
            except AgentManagerError as exc:
                with self.ctx.unit_of_work() as uow:
                    self._fail(uow, verification_id, f"test services did not start: {exc.message}")
                return
        with self.ctx.unit_of_work() as uow:
            row, task, project, config = self._load(uow, verification_id, lock=True)
            plan = row["plan"]
            allow_waiting = row["purpose"] == "POST_MERGE"
            executions: list[UUID] = []
            steps = {f"step-{i * 10 + 10:02d}-{name}.sh": command for i, (name, command) in enumerate(plan["step_commands"])}
            common = {"retries": str(min(2, int((config.get("retries") or {}).get("transient", 1)))),
                      "step-timeout": str(STEP_TIMEOUT_SECONDS)}
            if steps:
                tester = self.executions.request(uow, principal=CONTROL_PLANE, task_key=task["key"], allow_waiting=allow_waiting,
                                                 req=ExecutionRequest(
                    role="TESTER", command=["/opt/ho/bin/ho-verify"], workspace=row["workspace"], inputs={**steps, **common},
                    capabilities={"workspace": "WRITE", "tests": "EXECUTE", "artifacts": "WRITE", "egress": "ALLOWLIST",
                                  "test_services": environment is not None},
                    purpose={"verification": str(verification_id), "scope": "FULL_SUITE" if row["purpose"] == "INTEGRATION"
                             else "POST_MERGE"}))
                executions.append(tester["id"])
            if plan.get("browser"):
                browser_cfg = config.get("browser_tests") or {}
                browser_steps = {"step-10-browser.sh": "python3 /opt/ho/bin/ho-browser-check"}
                if (config.get("commands") or {}).get("e2e"):
                    browser_steps["step-20-e2e.sh"] = config["commands"]["e2e"]
                browser = self.executions.request(uow, principal=CONTROL_PLANE, task_key=task["key"], req=ExecutionRequest(
                    role="BROWSER", image="browser-runner", command=["/opt/ho/bin/ho-verify"], workspace=row["workspace"],
                    inputs={**browser_steps, **common, "browser.json": json.dumps(
                        {"base_url": browser_cfg["base_url"], "paths": browser_cfg.get("paths") or ["/"]})},
                    capabilities={"workspace": "READ", "tests": "EXECUTE", "artifacts": "WRITE", "egress": "ALLOWLIST",
                                  "test_services": True},
                    purpose={"verification": str(verification_id), "scope": "BROWSER"}))
                executions.append(browser["id"])
            state = "RUNNING" if executions else "PASSED"
            uow.cur.execute("UPDATE verifications SET state = %s, environment = %s, execution_ids = %s, "
                            "finished_at = CASE WHEN %s = 'PASSED' THEN now() END WHERE id = %s",
                            (state, jsonb(environment), executions, state, verification_id))
            if not executions:
                self._on_finished(uow, row, "PASSED", note="no verification steps are configured")

    def _load(self, uow: UnitOfWork, verification_id: UUID, *, lock: bool = False) -> tuple[Row, Row, Row, dict[str, Any]]:
        uow.cur.execute(f"SELECT * FROM verifications WHERE id = %s{' FOR UPDATE' if lock else ''}", (verification_id,))
        row = uow.cur.fetchone()
        assert row is not None
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s", (row["task_id"],))
        task = uow.cur.fetchone()
        assert task is not None
        _, project, config = self.git._context(uow, task["key"])
        return row, task, project, config

    def _fail(self, uow: UnitOfWork, verification_id: UUID, error: str) -> None:
        row, task, _, _ = self._load(uow, verification_id, lock=True)
        uow.cur.execute("UPDATE verifications SET state = 'ERROR', error = %s, finished_at = now() WHERE id = %s",
                        (error[:500], verification_id))
        self._on_finished(uow, row, "ERROR", note=error)

    # ------------------------------------------------------------------- sync

    def sync(self) -> int:
        """Ingest verifications whose executions have all finished."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                """
                SELECT v.id FROM verifications v WHERE v.state = 'RUNNING'
                  AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.id = ANY(v.execution_ids) AND e.state <> ALL(%s))
                """,
                (list(TERMINAL),),
            )
            done = [r["id"] for r in uow.cur.fetchall()]
        for verification_id in done:
            with self.ctx.unit_of_work() as uow:
                self._ingest(uow, verification_id)
        return len(done)

    def _ingest(self, uow: UnitOfWork, verification_id: UUID) -> None:
        row, task, project, _ = self._load(uow, verification_id, lock=True)
        if row["state"] != "RUNNING":
            return
        passed, problems = True, []
        for execution_id in row["execution_ids"]:
            uow.cur.execute("SELECT * FROM executions WHERE id = %s", (execution_id,))
            execution = uow.cur.fetchone()
            assert execution is not None
            scope = (execution["spec"].get("purpose") or {}).get("scope", "FULL_SUITE")
            artifacts = self._artifacts(uow, execution)
            report = artifacts.get("test_results.json")
            if report is None:
                passed = False
                problems.append(f"{execution['role'].lower()} run {execution['state']} without results "
                                f"({execution['failure_reason'] or 'no test_results.json'})")
                continue
            try:
                steps = json.loads(self.ctx.artifacts.read(report["path"]))["steps"]
            except (ValueError, KeyError, OSError):
                steps = []
                problems.append("unreadable test_results.json")
                passed = False
            for step in steps[:50]:
                name = str(step.get("name", "?"))[:40]
                status = step.get("status") if step.get("status") in ("PASSED", "FAILED", "ERROR", "SKIPPED") else "ERROR"
                log = artifacts.get(f"steps__{name}.log")
                uow.cur.execute(
                    """
                    INSERT INTO test_runs (id, task_id, verification_id, execution_id, scope, kind, commit_sha, status, attempts,
                                           duration_ms, definitive, report_artifact_id, log_artifact_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)
                    """,
                    (uuid7(), task["id"], verification_id, execution_id, scope, name, row["commit_sha"], status,
                     int(step.get("attempts") or 1), int(step.get("duration_ms") or 0), report["id"],
                     log["id"] if log else None),
                )
                if status != "PASSED":
                    passed = False
                    problems.append(f"{name} {status.lower()}")
                elif int(step.get("attempts") or 1) > 1:
                    record_event(uow.cur, "TEST_FLAKY", actor="control-plane", project_id=project["id"], task_id=task["id"],
                                 summary=f"{name} passed only after a retry", data={"verification_id": str(verification_id)},
                                 pending=uow.events)
            if execution["state"] != "SUCCEEDED" and not problems:
                passed = False
                problems.append(f"{execution['role'].lower()} run {execution['state']}")
        state = "PASSED" if passed else "FAILED"
        uow.cur.execute("UPDATE verifications SET state = %s, error = %s, finished_at = now() WHERE id = %s",
                        (state, "; ".join(problems)[:500] or None, verification_id))
        self._on_finished(uow, row, state, note="; ".join(problems) or "all verification steps passed")

    def _artifacts(self, uow: UnitOfWork, execution: Row) -> dict[str, Row]:
        uow.cur.execute("SELECT id, path FROM artifacts WHERE id = ANY(%s)", (list(execution["result_artifact_ids"]),))
        return {_artifact_name(r["path"]): r for r in uow.cur.fetchall()}

    def _on_finished(self, uow: UnitOfWork, row: Row, state: str, *, note: str) -> None:
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (row["task_id"],))
        task = uow.cur.fetchone()
        assert task is not None
        passed = state == "PASSED"
        record_event(uow.cur, "TEST_PASSED" if passed else "TEST_FAILED", actor="control-plane", project_id=task["project_id"],
                     task_id=task["id"], summary=f"{row['purpose'].lower().replace('_', '-')} verification {state.lower()}: {note}"[:300],
                     data={"verification_id": str(row["id"]), "commit": row["commit_sha"]}, pending=uow.events)
        if row["environment"] or (row["plan"] or {}).get("browser"):
            key = task["key"]
            uow.after_commit.append(lambda: self._stop_services(key))
        if row["purpose"] == "INTEGRATION":
            uow.cur.execute("UPDATE git_changes SET retest_status = %s WHERE task_id = %s AND integration_sha = %s",
                            ("PASSED" if passed else "FAILED", task["id"], row["commit_sha"]))
        elif task["state"] == S.VERIFYING:
            uow.cur.execute("UPDATE git_changes SET post_merge_status = %s WHERE task_id = %s",
                            ("PASSED" if passed else "FAILED", task["id"]))
            self.git._finish_verification(uow, task, passed=passed,
                                          note="post-merge verification passed" if passed else f"post-merge verification failed: {note}")

    def _stop_services(self, task_key: str) -> None:
        try:
            self.ctx.agents.stop_test_services(task_key)  # type: ignore[union-attr]
        except AgentManagerError as exc:
            log.warning("test services of %s not removed yet: %s", task_key, exc)

    def results(self, uow: UnitOfWork, task_key: str) -> list[dict[str, Any]]:
        task = self.tasks.get(uow, task_key)
        uow.cur.execute("SELECT * FROM verifications WHERE task_id = %s ORDER BY created_at DESC LIMIT 20", (task["id"],))
        out = []
        for v in uow.cur.fetchall():
            uow.cur.execute("SELECT scope, kind, status, attempts, duration_ms, log_artifact_id FROM test_runs "
                            "WHERE verification_id = %s ORDER BY created_at", (v["id"],))
            out.append({"id": str(v["id"]), "purpose": v["purpose"], "commit": v["commit_sha"], "state": v["state"],
                        "error": v["error"], "plan": {k: v["plan"].get(k) for k in ("risk", "steps", "browser", "gaps",
                                                                                      "needs_approval", "requirements")},
                        "environment": v["environment"],
                        "runs": [{**r, "log_artifact_id": str(r["log_artifact_id"]) if r["log_artifact_id"] else None}
                                 for r in uow.cur.fetchall()]})
        return out


# ------------------------------------------------------------------------ reviews


class Reviews:
    def __init__(self, ctx: Context, tasks: Tasks, executions: Executions, git: GitChanges) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.executions = executions
        self.git = git
        executions.on_agent_result.append(self._record)

    def developer_providers(self, uow: UnitOfWork, task: Row) -> list[str]:
        uow.cur.execute("SELECT DISTINCT provider FROM executions WHERE task_id = %s AND role = 'DEVELOPER' AND agent_run",
                        (task["id"],))
        return sorted(r["provider"] for r in uow.cur.fetchall())

    def request(self, uow: UnitOfWork, task_key: str, *, provider: str, principal: Principal) -> Row:
        task, project, _ = self.git._context(uow, task_key, lock=True)
        changes = self.git._changes(uow, task)
        if changes is None or not changes["integration_sha"]:
            raise Conflict(f"integrate {task_key} before reviewing it")
        developers = self.developer_providers(uow, task)
        if provider in developers:
            raise BadRequest(f"cross-review needs a provider other than the developers ({', '.join(developers)})")
        uow.cur.execute("SELECT count(*) AS n FROM workspaces WHERE task_id = %s AND kind = 'VERIFICATION'", (task["id"],))
        suffix = f"review{uow.cur.fetchone()['n'] + 1}"  # type: ignore[index]
        ws = self.git.workspace(uow, task_key, principal=CONTROL_PLANE, suffix=suffix, kind="VERIFICATION",
                                base_ref=changes["integration_ref"])
        request_text = self._request_text(uow, task)
        prompt = "\n".join([
            f"Review the change of task {task_key} for merge into {changes['target_branch']}.",
            f"The change is `git diff {changes['base_sha']}...HEAD` in /workspace (commit {changes['integration_sha'][:12]}).",
            "", "## What the task asked for", "", request_text.strip()[:6000], "",
            "Check correctness, security, tests, and whether the change does what was asked. Run the tests if useful.",
            "Report every problem as a finding with a severity: CRITICAL and HIGH block the merge; MEDIUM and LOW are advice.",
            "Report requirements the change does not meet in unmet_requirements.",
        ])
        return self.executions.request(uow, principal=principal, task_key=task_key, req=ExecutionRequest(
            role="REVIEWER", provider=provider, prompt=prompt, workspace=ws["path"], result_schema="review-result",
            capabilities={"workspace": "READ", "git": "READ", "tests": "EXECUTE"},
            purpose={"review": True, "commit": changes["integration_sha"], "developers": developers}))

    def _request_text(self, uow: UnitOfWork, task: Row) -> str:
        uow.cur.execute("SELECT path FROM artifacts WHERE id = %s", (task["original_request_artifact_id"],))
        row = uow.cur.fetchone()
        try:
            return self.ctx.artifacts.read(row["path"]).decode("utf-8", "replace") if row else task["title"]
        except OSError:
            return task["title"]

    def _record(self, uow: UnitOfWork, execution: Row, result: Any) -> None:
        purpose = (execution["spec"] or {}).get("purpose") or {}
        if not purpose.get("review") or not result.ok or not result.structured:
            return
        review = result.structured
        outcome = {"approved": "APPROVED", "changes_requested": "CHANGES_REQUESTED"}.get(review["verdict"], "BLOCKED")
        review_id = uuid7()
        uow.cur.execute(
            """
            INSERT INTO reviews (id, task_id, execution_id, commit_sha, reviewer_provider, developer_providers, outcome,
                                 requirements_met, unmet_requirements, summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (review_id, execution["task_id"], execution["id"], purpose["commit"], execution["provider"],
             list(purpose.get("developers") or []), outcome, bool(review["requirements_met"]),
             jsonb(review["unmet_requirements"][:50]), review["summary"][:4000]),
        )
        for finding in review["findings"]:
            uow.cur.execute(
                "INSERT INTO review_findings (id, review_id, severity, category, path, line, description) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uuid7(), review_id, finding["severity"], str(finding["category"])[:60], finding["path"], finding["line"],
                 finding["description"]),
            )
        counts = {s: sum(1 for f in review["findings"] if f["severity"] == s) for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
        record_event(uow.cur, "REVIEW_PASSED" if outcome == "APPROVED" else "REVIEW_FAILED", actor=f"{execution['provider']}-reviewer",
                     project_id=execution["project_id"], task_id=execution["task_id"],
                     summary=f"{outcome.lower().replace('_', ' ')} by {execution['provider']}: "
                             + ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n) if any(counts.values())
                             else f"{outcome.lower().replace('_', ' ')} by {execution['provider']}: no findings",
                     data={"review_id": str(review_id), "commit": purpose["commit"]}, pending=uow.events)


# ------------------------------------------------------------------ Quality Gate


class QualityGate:
    def __init__(self, ctx: Context, tasks: Tasks, approvals: Approvals, git: GitChanges, reviews: Reviews) -> None:
        self.ctx = ctx
        self.tasks = tasks
        self.approvals = approvals
        self.git = git
        self.reviews = reviews
        approvals.register_handler(ApprovalAction.HIGH_RISK_OPERATION, self._on_exception_decision)
        git.quality_gate = self

    def evaluate(self, uow: UnitOfWork, task_key: str, *, actor: str = "control-plane") -> Row:
        task, project, config = self.git._context(uow, task_key, lock=True)
        changes = self.git._changes(uow, task, lock=True)
        if changes is None or not changes["integration_sha"]:
            raise Conflict(f"integrate {task_key} before evaluating the Quality Gate")
        commit = changes["integration_sha"]
        uow.cur.execute("SELECT * FROM verifications WHERE task_id = %s AND purpose = 'INTEGRATION' AND commit_sha = %s "
                        "ORDER BY created_at DESC LIMIT 1", (task["id"], commit))
        verification = uow.cur.fetchone()
        plan = (verification or {}).get("plan") or {}
        runs: dict[str, str] = {}
        browser_passed: bool | None = None
        if verification:
            uow.cur.execute("SELECT scope, kind, status FROM test_runs WHERE verification_id = %s", (verification["id"],))
            for r in uow.cur.fetchall():
                if r["scope"] == "BROWSER":
                    browser_passed = (browser_passed is not False) and r["status"] == "PASSED"
                else:
                    runs[r["kind"]] = r["status"]
        uow.cur.execute("SELECT * FROM reviews WHERE task_id = %s AND commit_sha = %s ORDER BY created_at DESC LIMIT 1",
                        (task["id"], commit))
        review = uow.cur.fetchone()
        findings: list[Row] = []
        if review:
            uow.cur.execute("SELECT * FROM review_findings WHERE review_id = %s AND status = 'OPEN'", (review["id"],))
            findings = uow.cur.fetchall()
        blocking = BLOCKING_ALWAYS | set((config.get("quality_gate") or {}).get("block_on_findings", []))
        current_target = self.ctx.git.refs(project["relative_path"], [changes["target_branch"]])["refs"][changes["target_branch"]]
        developers = self.reviews.developer_providers(uow, task)
        violations = self._violations(uow, task)
        changed = plan.get("changed_files") or []

        results: list[dict[str, Any]] = []

        def add(name: str, status: str, detail: str, evidence: Any = None) -> None:
            results.append({"name": name, "status": status, "detail": detail, **({"evidence": evidence} if evidence else {})})

        if verification is None:
            add("verification", "FAIL", "the integrated commit has not been verified")
        elif verification["state"] in ("PREPARING", "RUNNING"):
            add("verification", "PENDING", "verification is still running", str(verification["id"]))
        for requirement in plan.get("requirements", []):
            name = requirement["name"]
            key = "test" if name == "tests" else name
            if key in ("test", "build", "lint", "typecheck", "security"):
                status = runs.get(key)
                if not requirement["has_command"]:
                    add(name, "UNAVAILABLE", f"required ({requirement['source']}) but the project has no {key} command")
                elif status is None:
                    add(name, "FAIL" if verification and verification["state"] not in ("PREPARING", "RUNNING") else "PENDING",
                        f"no {key} result for this commit")
                else:
                    add(name, "PASS" if status == "PASSED" else "FAIL", f"{key} {status.lower()}", str(verification["id"]))
            elif name == "browser":
                add(name, "PASS" if browser_passed else "FAIL" if browser_passed is False else "PENDING",
                    "browser checks passed" if browser_passed else "browser checks failed or missing")
            elif name == "cross_review":
                if review is None:
                    add(name, "FAIL", "no cross-review of this commit")
                elif review["reviewer_provider"] in developers:
                    add(name, "FAIL", f"reviewed by {review['reviewer_provider']}, which also developed the change")
                else:
                    add(name, "PASS" if review["outcome"] != "BLOCKED" else "FAIL",
                        f"{review['outcome'].lower()} by {review['reviewer_provider']}", str(review["id"]))
            elif name == "requirements":
                add(name, "PASS" if review and review["requirements_met"] else "FAIL",
                    "requirements met" if review and review["requirements_met"]
                    else f"unmet: {'; '.join(review['unmet_requirements'][:5])}" if review else "not assessed")
            elif name == "no_blocking_findings":
                open_blocking = [f for f in findings if f["severity"] in blocking]
                add(name, "FAIL" if open_blocking else "PASS" if review else "FAIL",
                    f"{len(open_blocking)} open finding(s) of severity {', '.join(sorted(blocking))}" if open_blocking
                    else "no blocking findings" if review else "no review")
            elif name == "no_conflicts":
                if changes["integration_conflicts"]:
                    add(name, "FAIL", "the last integration had conflicts")
                elif changes["integration_target_sha"] != current_target:
                    add(name, "FAIL", f"{changes['target_branch']} moved since integration; reintegrate")
                elif changes["reconcile_required"]:
                    add(name, "FAIL", "human changes need reconciliation")
                else:
                    add(name, "PASS", f"integrated onto the current {changes['target_branch']}")
            elif name == "no_policy_violations":
                add(name, "FAIL" if violations else "PASS",
                    f"{len(violations)} policy violation(s): {'; '.join(violations[:3])}" if violations else "none")
            elif name == "ci_checks":
                if not changes["pr_number"]:
                    add(name, "FAIL", "no pull request yet")
                else:
                    checks = self.ctx.git.pr_checks(project["relative_path"], changes["pr_number"])
                    summary = checks["summary"]
                    add(name, {"PASS": "PASS", "NONE": "UNAVAILABLE", "PENDING": "PENDING"}.get(summary, "FAIL"),
                        f"CI {summary.lower()}")
            elif name == "docs_updated":
                docs = [p for p in changed if is_doc_file(p)]
                add(name, "PASS" if docs else "FAIL", f"{len(docs)} documentation file(s) changed" if docs
                    else "documentation is required and was not updated")

        gaps = list(plan.get("gaps", []))
        exception_reasons = list(plan.get("needs_approval", []))
        if violations:
            exception_reasons.append("policy violations were observed (section 58)")
            for item in results:
                if item["name"] == "no_policy_violations":
                    item["status"] = "APPROVAL_REQUIRED"
        exception = None
        if exception_reasons:
            exception = self._exception(uow, task, project, config, commit, exception_reasons)
            add("exception_approval", "PASS" if exception == "APPROVED" else "APPROVAL_REQUIRED",
                "; ".join(exception_reasons) + (" (approved)" if exception == "APPROVED" else " (waiting for approval)"))
            if exception == "APPROVED":
                for item in results:
                    if item["status"] == "APPROVAL_REQUIRED":
                        item["status"] = "PASS"
        blocking_status = [r for r in results if r["status"] in ("FAIL", "PENDING", "APPROVAL_REQUIRED")]
        outcome = "PASS" if not blocking_status else "FAIL"
        risk = (plan.get("risk") or {}).get("risk", "MEDIUM")
        alt = alternatives(runs, browser_passed)
        residual = f"risk {risk}" + (f"; test gaps: {', '.join(g['kind'] for g in gaps)}; alternative evidence: "
                                     f"{', '.join(alt) or 'none'}" if gaps else "; no test gaps")
        evaluation_id = uuid7()
        uow.cur.execute(
            """
            INSERT INTO quality_gate_evaluations (id, task_id, commit_sha, config_hash, policy_version, verification_id, review_id,
                                                  risk, requirements, test_gaps, residual_risk, outcome)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (evaluation_id, task["id"], commit, config["_hash"], self.ctx.policy_version,
             verification["id"] if verification else None, review["id"] if review else None, risk, jsonb(results),
             jsonb([{**g, "alternatives": alt} for g in gaps]), residual, outcome),
        )
        evaluation = uow.cur.fetchone()
        assert evaluation is not None
        uow.cur.execute("UPDATE git_changes SET quality_gate_id = %s, updated_at = now() WHERE task_id = %s",
                        (evaluation_id, task["id"]))
        failing = [f"{r['name']}: {r['detail']}" for r in blocking_status]
        record_event(uow.cur, "QUALITY_GATE_EVALUATED", actor=actor, project_id=project["id"], task_id=task["id"],
                     summary=f"{outcome} for {commit[:12]} (risk {risk})" + (f"; {'; '.join(failing[:3])}" if failing else ""),
                     data={"evaluation_id": str(evaluation_id), "outcome": outcome}, pending=uow.events)
        self._apply(uow, task, outcome, results, exception)
        return evaluation

    def _apply(self, uow: UnitOfWork, task: Row, outcome: str, results: list[dict[str, Any]], exception: str | None) -> None:
        """Move a task that is in QUALITY_GATE according to the evaluation (the only path to READY_FOR_MERGE)."""
        if task["state"] != S.QUALITY_GATE:
            return
        statuses = {r["status"] for r in results}
        if outcome == "PASS":
            self.tasks.transition(uow, task, S.READY_FOR_MERGE, trigger=Trigger.SYSTEM, actor="quality-gate",
                                  reason="all required checks passed")
        elif exception == "PENDING" and statuses <= {"PASS", "UNAVAILABLE", "APPROVAL_REQUIRED"}:
            self.tasks.transition(uow, task, S.APPROVAL_REQUIRED, trigger=Trigger.SYSTEM, actor="quality-gate",
                                  reason="the Quality Gate needs an explicit approval")
        elif "PENDING" in statuses and "FAIL" not in statuses:
            return  # wait for running verification or CI; re-evaluated by the scheduler
        else:
            self.tasks.transition(uow, task, S.FIX_REQUIRED, trigger=Trigger.SYSTEM, actor="quality-gate",
                                  reason="; ".join(f"{r['name']}: {r['detail']}" for r in results if r["status"] == "FAIL")[:300])

    def _violations(self, uow: UnitOfWork, task: Row) -> list[str]:
        uow.cur.execute("SELECT summary FROM events WHERE task_id = %s AND type = 'COMMAND_HIGH_RISK' ORDER BY seq", (task["id"],))
        found = [r["summary"] for r in uow.cur.fetchall()]
        uow.cur.execute("SELECT summary FROM policy_decisions WHERE task_id = %s AND decision = 'DENY'", (task["id"],))
        return found + [r["summary"] for r in uow.cur.fetchall()]

    def _exception(self, uow: UnitOfWork, task: Row, project: Row, config: dict[str, Any], commit: str,
                   reasons: list[str]) -> str:
        """An explicit approval for a critical change, a sensitive change with test gaps, or policy
        violations (sections 56-58), bound to the commit and the reasons."""
        subject = {"kind": GATE_EXCEPTION, "commit": commit, "reasons": sorted(reasons)}
        uow.cur.execute(
            "SELECT state FROM approvals WHERE task_id = %s AND action = 'HIGH_RISK_OPERATION' AND subject = %s "
            "ORDER BY requested_at DESC LIMIT 1", (task["id"], jsonb(subject)))
        existing = uow.cur.fetchone()
        if existing and existing["state"] in ("APPROVED", "CONSUMED"):
            return "APPROVED"
        if existing and existing["state"] == "PENDING":
            return "PENDING"
        if existing and existing["state"] == "REJECTED":
            return "REJECTED"
        self.approvals.request(uow, action=ApprovalAction.HIGH_RISK_OPERATION, project_id=project["id"], task_id=task["id"],
                               subject=subject, config_hash=config["_hash"], risk=Risk.CRITICAL,
                               summary=f"{task['key']}: Quality Gate exception needed: {'; '.join(reasons)}",
                               requested_by="quality-gate", from_task_state=task["state"])
        return "PENDING"

    def _on_exception_decision(self, uow: UnitOfWork, approval: Row, approved: bool, principal: Principal) -> None:
        if (approval["subject"] or {}).get("kind") != GATE_EXCEPTION or approval["task_id"] is None:
            return
        uow.cur.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (approval["task_id"],))
        task = uow.cur.fetchone()
        if task is None or task["state"] != S.APPROVAL_REQUIRED:
            return
        if approved:
            self.tasks.transition(uow, task, S(task["resume_state"]), trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason="Quality Gate exception approved")
            key = task["key"]
            uow.after_commit.append(lambda: self._reevaluate(key))
        else:
            self.tasks.transition(uow, task, S.FIX_REQUIRED, trigger=Trigger.APPROVAL, actor=principal.value,
                                  reason="Quality Gate exception rejected")

    def _reevaluate(self, task_key: str) -> None:
        with self.ctx.unit_of_work() as uow:
            self.evaluate(uow, task_key)

    def latest_passing(self, uow: UnitOfWork, task: Row, commit: str) -> str | None:
        uow.cur.execute("SELECT id FROM quality_gate_evaluations WHERE task_id = %s AND commit_sha = %s AND outcome = 'PASS' "
                        "ORDER BY evaluated_at DESC LIMIT 1", (task["id"], commit))
        row = uow.cur.fetchone()
        return str(row["id"]) if row else None

    def sync(self) -> int:
        """Re-evaluate tasks waiting in QUALITY_GATE whose verification or CI was pending."""
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                """
                SELECT t.key FROM tasks t JOIN git_changes g ON g.task_id = t.id
                LEFT JOIN quality_gate_evaluations q ON q.id = g.quality_gate_id
                WHERE t.state = 'QUALITY_GATE' AND g.integration_sha IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM verifications v WHERE v.task_id = t.id AND v.state IN ('PREPARING', 'RUNNING'))
                  AND (q.id IS NULL OR q.commit_sha <> g.integration_sha OR q.evaluated_at < now() - interval '2 minutes'
                       OR q.evaluated_at < (SELECT max(v.finished_at) FROM verifications v WHERE v.task_id = t.id))
                LIMIT 20
                """
            )
            keys = [r["key"] for r in uow.cur.fetchall()]
        for key in keys:
            try:
                with self.ctx.unit_of_work() as uow:
                    self.evaluate(uow, key)
            except Exception:  # noqa: BLE001 - keep evaluating the others
                log.exception("Quality Gate evaluation of %s failed", key)
        return len(keys)

