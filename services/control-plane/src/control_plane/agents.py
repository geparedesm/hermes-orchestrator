"""Agents in the Dashboard (docs/design/agents-dashboard.md): each supported provider with its login status, whether a
person paused it, what it runs now, and its plan's usage windows (session and week) with their reset times.

The usage comes from the provider's own CLI during an execution (adapters' `rate_limits`), so it is as recent as the
last run: the view says how old it is, and a window whose reset time has passed shows as reset.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ho_core.adapters import ADAPTERS

from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row
from .errors import Forbidden, NotFound
from .events import record_event
from .executions import ACTIVE


def record_usage(uow: UnitOfWork, execution: Row, rate_limits: list[dict[str, Any]]) -> None:
    """Keep the latest usage window a provider reported in this execution (never an older one over a newer one)."""
    for window in rate_limits:
        uow.cur.execute(
            "INSERT INTO provider_usage (provider, identity, window_kind, used_percent, resets_at, observed_at, execution_id) "
            "VALUES (%s, %s, %s, %s, to_timestamp(%s), now(), %s) ON CONFLICT (provider, identity, window_kind) DO UPDATE SET "
            "used_percent = EXCLUDED.used_percent, resets_at = EXCLUDED.resets_at, observed_at = EXCLUDED.observed_at, "
            "execution_id = EXCLUDED.execution_id WHERE provider_usage.observed_at <= EXCLUDED.observed_at",
            (execution["provider"], execution["provider_identity"] or "default", window["window"], window["used_percent"],
             window["resets_at"], execution["id"]))


def paused(uow: UnitOfWork, identity: str) -> set[str]:
    uow.cur.execute("SELECT provider FROM provider_settings WHERE identity = %s AND NOT enabled", (identity,))
    return {r["provider"] for r in uow.cur.fetchall()}


class Agents:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def view(self, uow: UnitOfWork) -> list[dict[str, Any]]:
        identity = self.ctx.provider_identity
        now = datetime.now(timezone.utc)
        uow.cur.execute("SELECT provider, status FROM credential_refs WHERE identity = %s", (identity,))
        login = {r["provider"]: r["status"] for r in uow.cur.fetchall()}
        off = paused(uow, identity)
        uow.cur.execute("SELECT provider, count(*) AS n FROM executions WHERE state = ANY(%s) AND provider IS NOT NULL "
                        "GROUP BY provider", (list(ACTIVE),))
        running = {r["provider"]: int(r["n"]) for r in uow.cur.fetchall()}
        uow.cur.execute("SELECT provider, window_kind, used_percent, resets_at, observed_at FROM provider_usage WHERE identity = %s",
                        (identity,))
        usage: dict[str, dict[str, Any]] = {}
        for r in uow.cur.fetchall():
            reset = r["resets_at"] <= now
            usage.setdefault(r["provider"], {})[r["window_kind"]] = {
                "used_percent": 0.0 if reset else float(r["used_percent"]), "resets_at": r["resets_at"],
                "observed_at": r["observed_at"], "reset": reset}
        uow.cur.execute("SELECT DISTINCT provider FROM executions WHERE failure_class = 'QUOTA' AND ended_at > now() - interval '30 minutes'")
        limited = {r["provider"] for r in uow.cur.fetchall()}
        out = []
        for provider in sorted(ADAPTERS):
            windows = usage.get(provider, {})
            status = ("paused" if provider in off else "login_needed" if login.get(provider) == "AUTH_REQUIRED"
                      else "limited" if provider in limited
                      else "near_limit" if any(w["used_percent"] >= 80 for w in windows.values()) else "available")
            out.append({"provider": provider, "identity": identity, "login": login.get(provider, "UNKNOWN"),
                        "enabled": provider not in off, "status": status, "running": running.get(provider, 0),
                        "session": windows.get("session"), "week": windows.get("week")})
        return out

    def set_enabled(self, uow: UnitOfWork, provider: str, enabled: bool, *, principal: Principal) -> dict[str, Any]:
        # Which agents may work is a person's decision (AD-10): only approvers may pause or resume one.
        if principal.value not in self.ctx.platform["platform"]["approvers"]:
            raise Forbidden(f"{principal.value} may not pause or resume agents")
        if provider not in ADAPTERS:
            raise NotFound(f"no agent {provider!r}")
        identity = self.ctx.provider_identity
        uow.cur.execute(
            "INSERT INTO provider_settings (provider, identity, enabled, updated_by) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (provider, identity) DO UPDATE SET enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by, "
            "updated_at = now()", (provider, identity, enabled, principal.value))
        record_event(uow.cur, "AGENT_RESUMED" if enabled else "AGENT_PAUSED", actor=principal.value,
                     summary=f"{provider} {'resumed' if enabled else 'paused'} by {principal.value}"
                             + ("" if enabled else ": new work goes to the other agents; running work finishes"),
                     data={"provider": provider, "identity": identity}, pending=uow.events)
        return next(a for a in self.view(uow) if a["provider"] == provider)
