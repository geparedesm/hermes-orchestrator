"""Task Manifests (MASTER_SPEC section 49; schemas/manifest.schema.json).

Built only from durable records, never from model output, and validated against the schema before
they are stored: READY_FOR_MERGE when the Quality Gate passes, FINAL when a task ends, ON_DEMAND
for `ho task inspect --manifest` on tasks that reached one of those outcomes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from ho_core import schemas

from . import budgets
from .context import Context, UnitOfWork
from .db import Row
from .errors import Conflict
from .events import record_event
from ho_core.ids import uuid7

FINAL_STATES = ("READY_FOR_MERGE", "DONE", "CANCELLED", "FAILED", "BLOCKED")
_SUBTASK_STATE = {"IN_PROGRESS": "RUNNING", "IN_REVIEW": "REVIEW"}
_EXECUTION_STATE = {"REQUESTED": "RUNNING", "STARTING": "RUNNING", "STOPPING": "RUNNING"}
_FALLBACKS = {"RETRY_SCHEDULED": "RETRY", "PROVIDER_FALLBACK": "ALTERNATE_PROVIDER", "ALTERNATE_DEVELOPER": "ALTERNATE_DEVELOPER",
              "FAILOVER_COMPLETED": "ORCHESTRATOR_FAILOVER", "ORCHESTRATOR_FAILBACK": "ORCHESTRATOR_FAILBACK"}


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _principal(value: str) -> dict[str, str]:
    channel, _, subject = value.partition(":")
    return {"channel": channel if subject else "system", "subject": (subject or value)[:200]}


def _artifact(uow: UnitOfWork, artifact_id: Any) -> dict[str, str] | None:
    uow.cur.execute("SELECT path, sha256 FROM artifacts WHERE id = %s", (artifact_id,))
    row = uow.cur.fetchone()
    return {"path": row["path"], "sha256": row["sha256"]} if row else None


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if v is not None}


def build(uow: UnitOfWork, ctx: Context, task: Row, kind: str) -> dict[str, Any]:
    tid = task["id"]
    uow.cur.execute("SELECT * FROM projects WHERE id = %s", (task["project_id"],))
    project_row = uow.cur.fetchone()
    assert project_row is not None
    project = project_row["slug"]
    original = _artifact(uow, task["original_request_artifact_id"])

    uow.cur.execute("SELECT * FROM requirement_versions WHERE task_id = %s ORDER BY version", (tid,))
    requirements = [_compact({"version": r["version"], "artifact": _artifact(uow, r["artifact_id"]), "source": r["source"],
                              "change_reason": (r["change_reason"] or "")[:1000] or None, "created_at": _ts(r["created_at"]),
                              "approval": str(r["approval_id"]) if r["approval_id"] else None})
                    for r in uow.cur.fetchall()]
    if not requirements and original:  # tasks worked without an orchestrator: the request is the requirement
        requirements = [{"version": 1, "artifact": original, "source": "USER", "created_at": _ts(task["created_at"])}]

    uow.cur.execute("SELECT * FROM assumptions WHERE task_id = %s ORDER BY created_at", (tid,))
    assumptions = [{"level": a["level"], "assumption": a["assumption"][:1000], "reason": (a["reason"] or "")[:1000],
                    "reversibility": "REVERSIBLE" if a["reversible"] else "IRREVERSIBLE",
                    "status": {"APPROVED": "CONFIRMED", "REJECTED": "CORRECTED"}.get(a["status"], "ACTIVE")}
                   for a in uow.cur.fetchall()]

    uow.cur.execute("SELECT * FROM subtasks WHERE task_id = %s AND plan_version = %s ORDER BY key",
                    (tid, task["current_plan_version"] or 0))
    subtasks = uow.cur.fetchall()
    keys = {s["id"]: s["key"] for s in subtasks}
    dag_items = []
    for s in subtasks:
        uow.cur.execute("SELECT depends_on_subtask_id AS d FROM subtask_dependencies WHERE subtask_id = %s", (s["id"],))
        deps = [keys[r["d"]] for r in uow.cur.fetchall() if r["d"] in keys]
        dag_items.append(_compact({"key": s["key"], "kind": s["kind"], "title": s["title"][:200],
                                   "state": _SUBTASK_STATE.get(s["state"], s["state"]), "risk": s["risk"],
                                   "developer_provider": s["developer_provider"], "depends_on": deps}))
    uow.cur.execute("SELECT t.key, r.kind FROM task_relationships r JOIN tasks t ON t.id = r.to_task_id WHERE r.from_task_id = %s",
                    (tid,))
    relationships = [{"task": r["key"], "kind": r["kind"]} for r in uow.cur.fetchall()]

    uow.cur.execute("SELECT e.*, s.key AS subtask_key FROM executions e LEFT JOIN subtasks s ON s.id = e.subtask_id "
                    "WHERE e.task_id = %s AND e.role IN ('ORCHESTRATOR', 'DEVELOPER', 'REVIEWER', 'TESTER', 'BROWSER') "
                    "ORDER BY e.created_at", (tid,))
    assignments = [_compact({"execution": str(e["id"]), "subtask": e["subtask_key"], "role": e["role"],
                             "provider": e["provider"] if e["provider"] in ("claude", "codex") else "none",
                             "grant": f"G-{e['id']}", "state": _EXECUTION_STATE.get(e["state"], e["state"]),
                             "started_at": _ts(e["started_at"] or e["created_at"]), "ended_at": _ts(e["ended_at"])})
                   for e in uow.cur.fetchall()]

    uow.cur.execute("SELECT * FROM git_changes WHERE task_id = %s", (tid,))
    changes = uow.cur.fetchone() or {}
    commits = [{"sha": changes["integration_sha"], "subject": f"integration of {task['key']}"}] if changes.get("integration_sha") else []

    uow.cur.execute("SELECT * FROM verifications WHERE task_id = %s AND state IN ('PASSED', 'FAILED', 'ERROR') ORDER BY created_at",
                    (tid,))
    tests = [{"scope": "POST_MERGE" if v["purpose"] == "POST_MERGE" else "FULL_SUITE", "commit": v["commit_sha"],
              "status": v["state"]} for v in uow.cur.fetchall()]

    uow.cur.execute("SELECT r.*, s.key AS subtask_key FROM reviews r LEFT JOIN subtasks s ON s.id = r.subtask_id "
                    "WHERE r.task_id = %s ORDER BY r.created_at", (tid,))
    reviews, cycles = [], {}  # type: ignore[var-annotated]
    for r in uow.cur.fetchall():
        subject = r["subtask_key"] or task["key"]
        cycles[subject] = cycles.get(subject, 0) + 1
        uow.cur.execute("SELECT severity, count(*) AS n FROM review_findings WHERE review_id = %s AND status = 'OPEN' "
                        "GROUP BY severity", (r["id"],))
        developers = [p for p in (r["developer_providers"] or []) if p != r["reviewer_provider"]]
        reviews.append({"subject": subject, "developer_provider": developers[0] if developers else
                        ("codex" if r["reviewer_provider"] == "claude" else "claude"),
                        "reviewer_provider": r["reviewer_provider"], "cycle": cycles[subject], "outcome": r["outcome"],
                        "open_findings": {f["severity"]: int(f["n"]) for f in uow.cur.fetchall()}})

    uow.cur.execute("SELECT * FROM approvals WHERE task_id = %s ORDER BY requested_at", (tid,))
    approvals = [_compact({"id": str(a["id"]), "action": a["action"], "state": a["state"], "state_hash": a["state_hash"],
                           "requested_at": _ts(a["requested_at"]),
                           "decided_by": _principal(a["decided_by"]) if a["decided_by"] else None, "decided_at": _ts(a["decided_at"])})
                 for a in uow.cur.fetchall()]

    uow.cur.execute("SELECT type, summary, data, occurred_at FROM events WHERE task_id = %s AND type = ANY(%s) ORDER BY seq",
                    (tid, list(_FALLBACKS)))
    fallbacks = [_compact({"kind": _FALLBACKS[e["type"]], "at": _ts(e["occurred_at"]), "reason": e["summary"][:500],
                           "from_provider": (e["data"] or {}).get("from"), "to_provider": (e["data"] or {}).get("to")})
                 for e in uow.cur.fetchall()]

    uow.cur.execute("SELECT * FROM budgets WHERE task_id = %s", (tid,))
    budget_row = uow.cur.fetchone()
    budget = None
    if budget_row:
        consumed = {**budget_row["consumed"], "runtime_minutes": int(budgets.runtime_minutes(task))}
        budget = {"profile": budget_row["profile"],
                  "limits": {c: budget_row["limits"].get(c) for c in budgets.COUNTERS},
                  "consumed": {c: int(consumed.get(c) or 0) for c in budgets.COUNTERS}}

    uow.cur.execute("SELECT * FROM quality_gate_evaluations WHERE task_id = %s ORDER BY evaluated_at DESC LIMIT 1", (tid,))
    gate = uow.cur.fetchone()

    state = task["state"] if task["state"] in FINAL_STATES else None
    if state is None:
        raise Conflict(f"{task['key']} is {task['state']}; manifests describe READY_FOR_MERGE or finished tasks")
    manifest = _compact({
        "schema_version": 1, "kind": kind, "generated_at": datetime.now().astimezone().isoformat(),
        "task": {"key": task["key"], "project": project, "title": task["title"][:200], "priority": task["priority"],
                 "requested_by": _principal(task["requested_by"]), "created_at": _ts(task["created_at"])},
        "original_request": original, "requirements": requirements, "assumptions": assumptions,
        "dag": {"plan_version": task["current_plan_version"] or 1, "subtasks": dag_items, "task_relationships": relationships},
        "agent_assignments": assignments, "agent_versions": {}, "worker_images": [], "toolchains": [],
        "project_config_hash": _config_hash(uow, task), "policy_version": ctx.policy_version[:64],
        "base_commit": changes.get("base_sha") or task["base_commit"] or _target_head(ctx, task, project_row), "generated_commits": commits, "commands": [], "tests": tests,
        "quality_gate": _compact({"outcome": gate["outcome"], "commit": gate["commit_sha"], "evaluated_at": _ts(gate["evaluated_at"]),
                                  "test_gaps": [str(g)[:500] for g in gate["test_gaps"]][:50],
                                  "residual_risk": (gate["residual_risk"] or "")[:1000]}) if gate else None,
        "reviews": reviews, "approvals": approvals, "technical_sources": [], "retries_and_fallbacks": fallbacks,
        "budget": budget,
        "final_result": _compact({"state": state, "integration_branch": (changes.get("integration_ref") or "")[:200] or None,
                                  "head_commit": changes.get("integration_sha"), "merge_commit": changes.get("merge_commit_sha"),
                                  "post_merge_verification": changes.get("post_merge_status") if changes.get("post_merge_status")
                                  in ("PASSED", "FAILED") else "NOT_RUN"}),
    })
    return manifest


def _target_head(ctx: Context, task: Row, project: Row) -> str | None:
    """A task that never got a workspace has no pinned base: record the target branch head now."""
    branch = task["target_branch"] or project["default_branch"] or "main"
    try:
        return ctx.git.refs(project["relative_path"], [branch])["refs"].get(branch)
    except Exception:  # noqa: BLE001 - the schema check reports the missing base
        return None


def _config_hash(uow: UnitOfWork, task: Row) -> str | None:
    uow.cur.execute("SELECT effective_hash FROM project_configs WHERE id = %s", (task["config_id"],))
    row = uow.cur.fetchone()
    return row["effective_hash"] if row else None


def store(uow: UnitOfWork, ctx: Context, task: Row, kind: str) -> Row:
    manifest = build(uow, ctx, task, kind)
    schemas.validate("manifest", manifest)
    content = json.dumps(manifest, indent=2, sort_keys=True).encode()
    artifact = ctx.artifacts.write(uow.cur, project_id=task["project_id"], task_id=task["id"], kind="manifest",
                                   name=f"manifest-{kind.lower()}-{datetime.now():%Y%m%d%H%M%S%f}.json", content=content,
                                   media_type="application/json")
    uow.cur.execute("INSERT INTO manifests (id, task_id, kind, artifact_id, sha256) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                    (uuid7(), task["id"], kind, artifact.id, hashlib.sha256(content).hexdigest()))
    row = uow.cur.fetchone()
    record_event(uow.cur, "MANIFEST_GENERATED", actor="control-plane", project_id=task["project_id"], task_id=task["id"],
                 summary=f"{kind.lower().replace('_', '-')} manifest for {task['key']}", data={"artifact": str(artifact.id)},
                 pending=uow.events)
    assert row is not None
    return row
