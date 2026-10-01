"""Task budgets (MASTER_SPEC sections 30 and 70; docs/design/phase-7.md, change 3).

Every launch reserves what it may consume, atomically under the budget row lock, so concurrent
launches cannot overshoot a limit: one agent launch, a retry when it is one, and an estimated
provider-usage charge for agent executions. When the execution finishes the estimate is replaced
by the usage the CLI reported; an execution that was lost keeps the full reservation. Review
cycles and subtasks are charged when they happen. Thresholds: warning, optimize, and EXHAUSTED.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .context import UnitOfWork
from .db import Row, jsonb
from .errors import Conflict
from .events import record_event

# Provider units reserved per agent execution until its real usage is known (tokens).
DEFAULT_USAGE_RESERVATION = 200_000
COUNTERS = ("runtime_minutes", "agent_launches", "retries", "review_cycles", "subtasks", "provider_usage_units")


class BudgetExhausted(Conflict):
    pass


def _lock(uow: UnitOfWork, task: Row) -> Row:
    uow.cur.execute("SELECT * FROM budgets WHERE task_id = %s FOR UPDATE", (task["id"],))
    budget = uow.cur.fetchone()
    assert budget is not None
    return budget


def runtime_minutes(task: Row) -> float:
    started = task.get("started_at")
    return 0.0 if not started else (datetime.now(timezone.utc) - started).total_seconds() / 60


def _state(budget: Row, consumed: dict[str, Any], reserved: dict[str, Any], task: Row) -> tuple[str, str | None, float]:
    """Budget state and the counter closest to its limit."""
    limits = budget["limits"]
    usage = {**consumed, "runtime_minutes": runtime_minutes(task)}
    worst, worst_counter = 0.0, None
    for counter in COUNTERS:
        limit = limits.get(counter)
        if not limit:
            continue
        used = float(usage.get(counter, 0)) + float(reserved.get(counter, 0))
        percent = 100 * used / float(limit)
        if percent > worst:
            worst, worst_counter = percent, counter
    thresholds = budget["thresholds"]
    state = ("EXHAUSTED" if worst >= 100 else "OPTIMIZE" if worst >= thresholds["optimize_percent"]
             else "WARNING" if worst >= thresholds["warning_percent"] else "OK")
    return state, worst_counter, worst


def _save(uow: UnitOfWork, task: Row, budget: Row, consumed: dict[str, Any], reserved: dict[str, Any]) -> str:
    state, counter, percent = _state(budget, consumed, reserved, task)
    uow.cur.execute("UPDATE budgets SET consumed = %s, reserved = %s, state = %s, updated_at = now() WHERE task_id = %s",
                    (jsonb(consumed), jsonb(reserved), state, task["id"]))
    if state != budget["state"] and state != "OK":
        record_event(uow.cur, "BUDGET_THRESHOLD", actor="control-plane", project_id=task["project_id"], task_id=task["id"],
                     summary=f"{task['key']} budget {state.lower()}: {counter} at {percent:.0f}%",
                     data={"state": state, "counter": counter, "percent": round(percent)}, pending=uow.events)
    return state


def reserve(uow: UnitOfWork, task: Row, *, agent: bool, retry: bool = False, timeout_minutes: int,
            usage_estimate: int = DEFAULT_USAGE_RESERVATION) -> tuple[dict[str, int], int]:
    """Reserve one launch. Returns (reservation, timeout capped by the remaining runtime budget);
    raises BudgetExhausted when any limit would be exceeded."""
    budget = _lock(uow, task)
    limits, consumed, reserved = budget["limits"], dict(budget["consumed"]), dict(budget.get("reserved") or {})
    reservation = {"agent_launches": 1, **({"retries": 1} if retry else {}),
                   **({"provider_usage_units": usage_estimate} if agent else {})}
    for counter, amount in reservation.items():
        limit = limits.get(counter)
        if counter == "agent_launches":
            used = int(consumed.get(counter, 0)) + amount  # launches are charged immediately
        else:
            used = int(consumed.get(counter, 0)) + int(reserved.get(counter, 0)) + amount
        if limit is not None and used > int(limit):
            raise BudgetExhausted(f"{task['key']} would exceed its {counter} budget ({limit})")
    remaining = timeout_minutes
    if limits.get("runtime_minutes"):
        left = float(limits["runtime_minutes"]) - runtime_minutes(task)
        if left <= 0:
            raise BudgetExhausted(f"{task['key']} used its {limits['runtime_minutes']} minutes of runtime")
        remaining = max(1, min(timeout_minutes, int(left)))
    consumed["agent_launches"] = int(consumed.get("agent_launches", 0)) + 1
    for counter, amount in reservation.items():
        if counter != "agent_launches":
            reserved[counter] = int(reserved.get(counter, 0)) + amount
    _save(uow, task, budget, consumed, reserved)
    return {k: v for k, v in reservation.items() if k != "agent_launches"}, remaining


def settle(uow: UnitOfWork, task: Row, reservation: dict[str, int] | None, *, usage_units: int | None, lost: bool) -> None:
    """Replace a reservation by what was really used. Unknown usage of a lost execution is charged in full."""
    if not reservation:
        return
    budget = _lock(uow, task)
    consumed, reserved = dict(budget["consumed"]), dict(budget.get("reserved") or {})
    for counter, amount in reservation.items():
        reserved[counter] = max(0, int(reserved.get(counter, 0)) - amount)
        if counter == "provider_usage_units":
            actual = amount if lost or usage_units is None else usage_units
        else:
            actual = amount
        consumed[counter] = int(consumed.get(counter, 0)) + int(actual)
    _save(uow, task, budget, consumed, reserved)


def release(uow: UnitOfWork, task: Row, reservation: dict[str, int] | None) -> None:
    """Drop a reservation for an execution that never started (refused before launch)."""
    if not reservation:
        return
    budget = _lock(uow, task)
    reserved = dict(budget.get("reserved") or {})
    for counter, amount in reservation.items():
        reserved[counter] = max(0, int(reserved.get(counter, 0)) - amount)
    _save(uow, task, budget, dict(budget["consumed"]), reserved)


def charge(uow: UnitOfWork, task: Row, counter: str, amount: int = 1) -> None:
    """Charge review cycles or subtasks; raises BudgetExhausted beyond the limit."""
    budget = _lock(uow, task)
    consumed = dict(budget["consumed"])
    used = int(consumed.get(counter, 0)) + amount
    limit = budget["limits"].get(counter)
    if limit is not None and used > int(limit):
        raise BudgetExhausted(f"{task['key']} would exceed its {counter} budget ({limit})")
    consumed[counter] = used
    _save(uow, task, budget, consumed, dict(budget.get("reserved") or {}))


def usage_units(units: dict[str, Any]) -> int:
    """Provider usage in tokens as reported by the CLI (cached reads excluded)."""
    return int(sum(int(units.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")))


def state(uow: UnitOfWork, task: Row) -> dict[str, Any]:
    uow.cur.execute("SELECT * FROM budgets WHERE task_id = %s", (task["id"],))
    budget = uow.cur.fetchone()
    if budget is None:
        return {}
    return {"state": budget["state"], "limits": budget["limits"], "consumed": budget["consumed"],
            "reserved": budget.get("reserved") or {}, "runtime_minutes": round(runtime_minutes(task), 1)}
