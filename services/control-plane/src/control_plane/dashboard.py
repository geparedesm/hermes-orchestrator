"""Read models for the Dashboard tab and lightweight metrics (MASTER_SPEC sections 73 and 75; docs/design/phase-10.md).

Everything is computed from PostgreSQL on request; there are no dashboard tables. `metrics_text` renders the
same facts in the Prometheus text exposition format, the hook for a future Prometheus/OpenTelemetry setup
(neither is required in v1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .context import UnitOfWork
from .db import Row

WAITING = ("APPROVAL_REQUIRED", "AUTH_REQUIRED", "PAUSED_BUDGET", "BLOCKED", "FIX_REQUIRED", "PAUSED")
FINISHED = ("DONE", "CANCELLED", "FAILED")
_TOKENS = ("COALESCE((u.units->>'input_tokens')::bigint, 0) + COALESCE((u.units->>'output_tokens')::bigint, 0) + "
           "COALESCE((u.units->>'cache_creation_input_tokens')::bigint, 0)")


def _minutes(start: datetime | None, end: datetime | None = None) -> float | None:
    if start is None:
        return None
    return round(((end or datetime.now(timezone.utc)) - start).total_seconds() / 60, 1)


def summary(uow: UnitOfWork, queue: list[dict[str, Any]], health: dict[str, Any]) -> dict[str, Any]:
    cur = uow.cur
    cur.execute("SELECT state, count(*) AS n FROM tasks GROUP BY state")
    states = {r["state"]: int(r["n"]) for r in cur.fetchall()}

    cur.execute("SELECT a.id, a.action, a.summary, a.risk, a.requested_at, a.expires_at, t.key AS task FROM approvals a "
                "LEFT JOIN tasks t ON t.id = a.task_id WHERE a.state = 'PENDING' ORDER BY a.requested_at")
    approvals = cur.fetchall()
    cur.execute("SELECT t.key, t.title, t.state, t.state_reason, t.priority, t.updated_at, p.slug AS project FROM tasks t "
                "JOIN projects p ON p.id = t.project_id WHERE t.state = ANY(%s) ORDER BY t.updated_at", (list(WAITING),))
    waiting = cur.fetchall()

    cur.execute("SELECT e.id, e.role, e.provider, e.state, e.started_at, t.key AS task FROM executions e "
                "JOIN tasks t ON t.id = e.task_id WHERE e.state IN ('REQUESTED', 'STARTING', 'RUNNING', 'STOPPING') "
                "ORDER BY e.created_at")
    running = [{**r, "minutes": _minutes(r["started_at"])} for r in cur.fetchall()]

    usage: dict[str, Any] = {}
    for window in ("24 hours", "7 days"):
        cur.execute(
            f"SELECT e.provider, count(*) AS executions, count(*) FILTER (WHERE e.state = 'SUCCEEDED') AS succeeded, "
            f"count(*) FILTER (WHERE e.state IN ('FAILED', 'LOST')) AS failed, "
            f"COALESCE(sum({_TOKENS}), 0) AS tokens, "
            f"COALESCE(avg(extract(epoch FROM e.ended_at - e.started_at) / 60), 0) AS mean_minutes "
            f"FROM executions e LEFT JOIN usage_records u ON u.execution_id = e.id "
            f"WHERE e.agent_run AND e.created_at > now() - interval '{window}' GROUP BY e.provider")
        usage[window.replace(" ", "_")] = {r["provider"]: {"executions": int(r["executions"]), "succeeded": int(r["succeeded"]),
                                                           "failed": int(r["failed"]), "tokens": int(r["tokens"]),
                                                           "mean_minutes": round(float(r["mean_minutes"]), 1)}
                                           for r in cur.fetchall()}
    cur.execute("SELECT count(*) FILTER (WHERE type = 'RETRY_SCHEDULED') AS retries, "
                "count(*) FILTER (WHERE type IN ('PROVIDER_FALLBACK', 'ALTERNATE_DEVELOPER', 'FAILOVER_COMPLETED')) AS fallbacks "
                "FROM events WHERE occurred_at > now() - interval '7 days'")
    resilience = cur.fetchone()

    cur.execute("SELECT v.id, v.purpose, v.state, v.commit_sha, v.created_at, v.finished_at, t.key AS task FROM verifications v "
                "JOIN tasks t ON t.id = v.task_id ORDER BY v.created_at DESC LIMIT 10")
    tests = [{**r, "minutes": _minutes(r["created_at"], r["finished_at"])} for r in cur.fetchall()]
    cur.execute("SELECT avg(extract(epoch FROM completed_at - started_at) / 60) AS mean, count(*) AS n FROM tasks "
                "WHERE state = 'DONE' AND completed_at > now() - interval '30 days' AND started_at IS NOT NULL")
    durations = cur.fetchone()
    cur.execute("SELECT count(*) AS n FROM notifications WHERE state = 'PENDING'")
    pending_notifications = int(cur.fetchone()["n"])  # type: ignore[index]
    return {
        "generated_at": datetime.now(timezone.utc),
        "tasks_by_state": states,
        "required_actions": {"approvals": approvals, "waiting_tasks": waiting},
        "queue": queue,
        "running": running,
        "provider_usage": usage,
        "resilience_7_days": {"retries": int(resilience["retries"]), "fallbacks": int(resilience["fallbacks"])},  # type: ignore[index]
        "recent_tests": tests,
        "done_tasks_30_days": {"count": int(durations["n"]),  # type: ignore[index]
                               "mean_minutes": round(float(durations["mean"]), 1) if durations["mean"] else None},  # type: ignore[index]
        "health": health,
        "pending_notifications": pending_notifications,
    }


def task_detail(uow: UnitOfWork, task: Row) -> dict[str, Any]:
    cur, tid = uow.cur, task["id"]
    cur.execute("SELECT s.id, s.key, s.local_key, s.kind, s.title, s.state, s.state_reason, s.risk, s.developer_provider, "
                "s.review_cycles, s.attempts FROM subtasks s WHERE s.task_id = %s AND s.plan_version = %s ORDER BY s.key",
                (tid, task.get("current_plan_version") or 0))
    subtasks = cur.fetchall()
    keys = {s["id"]: s["key"] for s in subtasks}
    cur.execute("SELECT subtask_id, depends_on_subtask_id FROM subtask_dependencies WHERE subtask_id = ANY(%s)", (list(keys),))
    edges = [{"from": keys[r["depends_on_subtask_id"]], "to": keys[r["subtask_id"]]} for r in cur.fetchall()
             if r["depends_on_subtask_id"] in keys]

    cur.execute("SELECT id, action, state, summary, risk, requested_at, decided_by, decided_at FROM approvals WHERE task_id = %s "
                "ORDER BY requested_at DESC", (tid,))
    approvals = cur.fetchall()
    cur.execute("SELECT profile, state, limits, consumed, reserved FROM budgets WHERE task_id = %s", (tid,))
    budget = cur.fetchone()

    cur.execute("SELECT outcome, commit_sha, risk, requirements, test_gaps, residual_risk, evaluated_at "
                "FROM quality_gate_evaluations WHERE task_id = %s ORDER BY evaluated_at DESC LIMIT 1", (tid,))
    gate = cur.fetchone()
    cur.execute("SELECT r.id, r.reviewer_provider, r.developer_providers, r.outcome, r.requirements_met, r.summary, "
                "r.commit_sha, r.created_at, s.key AS subtask FROM reviews r LEFT JOIN subtasks s ON s.id = r.subtask_id "
                "WHERE r.task_id = %s ORDER BY r.created_at DESC", (tid,))
    reviews = cur.fetchall()
    for review in reviews:
        cur.execute("SELECT severity, category, path, line, description, status FROM review_findings WHERE review_id = %s "
                    "ORDER BY severity", (review["id"],))
        review["findings"] = cur.fetchall()
    cur.execute("SELECT v.id, v.purpose, v.state, v.commit_sha, v.created_at, v.finished_at FROM verifications v "
                "WHERE v.task_id = %s ORDER BY v.created_at DESC", (tid,))
    tests = cur.fetchall()
    for verification in tests:
        cur.execute("SELECT scope, kind, status, attempts, duration_ms, definitive FROM test_runs WHERE verification_id = %s "
                    "ORDER BY created_at", (verification["id"],))
        verification["runs"] = cur.fetchall()
        verification["minutes"] = _minutes(verification["created_at"], verification["finished_at"])

    cur.execute(f"SELECT e.id, e.role, e.provider, e.state, e.failure_class, e.failure_reason, e.started_at, e.ended_at, "
                f"s.key AS subtask, {_TOKENS} AS tokens FROM executions e LEFT JOIN usage_records u ON u.execution_id = e.id "
                f"LEFT JOIN subtasks s ON s.id = e.subtask_id WHERE e.task_id = %s ORDER BY e.created_at", (tid,))
    executions = [{**r, "minutes": _minutes(r["started_at"], r["ended_at"]) if r["started_at"] else None}
                  for r in cur.fetchall()]
    cur.execute("SELECT base_sha, integration_sha, retest_status, pr_url, merge_commit_sha, post_merge_status "
                "FROM git_changes WHERE task_id = %s", (tid,))
    git = cur.fetchone()
    cur.execute("SELECT seq, occurred_at, type, actor, summary, audit FROM events WHERE task_id = %s ORDER BY seq DESC LIMIT 300",
                (tid,))
    timeline = cur.fetchall()
    cur.execute("SELECT seq, reason, state, epoch, created_at FROM task_checkpoints WHERE task_id = %s ORDER BY seq DESC LIMIT 20",
                (tid,))
    checkpoints = cur.fetchall()
    cur.execute("SELECT id, kind, sha256, generated_at FROM manifests WHERE task_id = %s ORDER BY generated_at DESC", (tid,))
    manifests = cur.fetchall()
    return {"dag": {"plan_version": task.get("current_plan_version"),
                    "subtasks": [{k: v for k, v in s.items() if k != "id"} for s in subtasks], "edges": edges},
            "approvals": approvals, "budget": budget, "quality_gate": gate, "reviews": reviews, "tests": tests,
            "executions": executions, "git": git, "timeline": timeline, "checkpoints": checkpoints, "manifests": manifests}


# ---------------------------------------------------------------- metrics


def _labels(**labels: Any) -> str:
    inner = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                     for k, v in labels.items())
    return "{" + inner + "}" if inner else ""


def metrics_text(uow: UnitOfWork, queue_length: int) -> str:
    """Prometheus text exposition format, version 0.0.4."""
    cur = uow.cur
    out: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[dict[str, Any], float]]) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{_labels(**labels)} {value}" for labels, value in samples)

    cur.execute("SELECT state, count(*) AS n FROM tasks GROUP BY state ORDER BY state")
    metric("ho_tasks", "gauge", "Tasks by state.", [({"state": r["state"]}, int(r["n"])) for r in cur.fetchall()])
    metric("ho_queue_length", "gauge", "READY tasks waiting for dispatch.", [({}, queue_length)])
    cur.execute("SELECT state, COALESCE(provider, 'none') AS provider, count(*) AS n FROM executions GROUP BY 1, 2 ORDER BY 1, 2")
    metric("ho_executions_total", "counter", "Executions by state and provider.",
           [({"state": r["state"], "provider": r["provider"]}, int(r["n"])) for r in cur.fetchall()])
    cur.execute(f"SELECT u.provider, COALESCE(sum({_TOKENS}), 0) AS tokens FROM usage_records u GROUP BY 1 ORDER BY 1")
    metric("ho_provider_tokens_total", "counter", "Provider tokens reported by the CLIs (input + output + cache creation).",
           [({"provider": r["provider"]}, int(r["tokens"])) for r in cur.fetchall()])
    cur.execute("SELECT state, count(*) AS n FROM approvals GROUP BY state ORDER BY state")
    metric("ho_approvals", "gauge", "Approvals by state.", [({"state": r["state"]}, int(r["n"])) for r in cur.fetchall()])
    cur.execute("SELECT state, count(*) AS n FROM notifications GROUP BY state ORDER BY state")
    metric("ho_notifications", "gauge", "Outbox notifications by state.",
           [({"state": r["state"]}, int(r["n"])) for r in cur.fetchall()])
    cur.execute("SELECT component, state FROM component_health ORDER BY component")
    metric("ho_component_healthy", "gauge", "1 when a dependency is healthy, 0 when DEGRADED.",
           [({"component": r["component"]}, 1 if r["state"] == "HEALTHY" else 0) for r in cur.fetchall()])
    return "\n".join(out) + "\n"
