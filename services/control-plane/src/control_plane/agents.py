"""Agents in the Dashboard (docs/design/agents-dashboard.md): each supported provider with its login status, whether a
person paused it, what it runs now, and its plan's usage windows (session and week) with their reset times.

The usage comes from the provider's own CLI during an execution (adapters' `rate_limits`), so it is as recent as the
last run: the view says how old it is, and a window whose reset time has passed shows as reset.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from ho_core.adapters import ADAPTERS

from .auth import Principal
from .context import Context, UnitOfWork
from .db import Row
from .errors import BadRequest, Forbidden, NotFound
from .events import record_event
from .executions import ACTIVE

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}$")
log = logging.getLogger(__name__)

# Claude Code has no command that lists models; these are its documented `--model` aliases, which always resolve to the
# provider's latest model of each family (https://code.claude.com/docs/en/model-config).
CLAUDE_MODELS = [{"id": "fable", "label": "Fable (latest)"}, {"id": "opus", "label": "Opus (latest)"},
                 {"id": "sonnet", "label": "Sonnet (latest)"}, {"id": "haiku", "label": "Haiku (latest)"}]
# Other providers' catalogs come from their pinned CLIs (scripts/model-catalog.sh).
CATALOG_FILE = Path(os.environ.get("HO_CONFIG_DIR", "/app/config")) / "models.lock.json"


def model_catalog(path: Path | None = None) -> dict[str, list[dict[str, str]]]:
    """Models each provider offers, from the provider itself; a provider without a catalog has none."""
    catalog: dict[str, list[dict[str, str]]] = {"claude": list(CLAUDE_MODELS)}
    path = path or CATALOG_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return catalog
    except (OSError, ValueError) as exc:
        log.warning("model catalog %s unreadable: %s", path, exc)
        return catalog
    for provider in ADAPTERS:
        entries = data.get(provider) if isinstance(data, dict) else None
        if provider != "claude" and isinstance(entries, list):
            catalog[provider] = [{"id": str(e["id"]), "label": str(e.get("label") or e["id"])} for e in entries
                                 if isinstance(e, dict) and _MODEL.fullmatch(str(e.get("id", "")))]
    return catalog


def record_usage(uow: UnitOfWork, execution: Row, rate_limits: list[dict[str, Any]]) -> None:
    """Keep the latest usage window a provider reported (never an older observation over a newer one). When it was
    observed: the provider's own time when it gives one (Codex), else when the execution started (Claude's events
    carry none): never later than the truth, so "as of" never overstates freshness."""
    for window in rate_limits:
        uow.cur.execute(
            "INSERT INTO provider_usage (provider, identity, window_kind, used_percent, resets_at, observed_at, execution_id) "
            "VALUES (%s, %s, %s, %s, to_timestamp(%s), coalesce(to_timestamp(%s), %s, now()), %s) "
            "ON CONFLICT (provider, identity, window_kind) DO UPDATE SET "
            "used_percent = EXCLUDED.used_percent, resets_at = EXCLUDED.resets_at, observed_at = EXCLUDED.observed_at, "
            "execution_id = EXCLUDED.execution_id WHERE provider_usage.observed_at <= EXCLUDED.observed_at",
            (execution["provider"], execution["provider_identity"] or "default", window["window"], window["used_percent"],
             window["resets_at"], window.get("observed_at"), execution.get("started_at"), execution["id"]))


def default_model(uow: UnitOfWork, provider: str, role: str | None = None) -> str | None:
    """The model a person chose for this agent in the Dashboard (None: the CLI's own default)."""
    if role:
        uow.cur.execute("SELECT model FROM role_models WHERE provider = %s AND role = %s", (provider, role))
        selected = uow.cur.fetchone()
        if selected:
            return selected["model"]
    uow.cur.execute("SELECT model FROM provider_models WHERE provider = %s", (provider,))
    row = uow.cur.fetchone()
    return row["model"] if row else None


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
        uow.cur.execute("SELECT provider, model, updated_at FROM provider_models")
        models = {r["provider"]: {"model": r["model"], "updated_at": r["updated_at"]} for r in uow.cur.fetchall()}
        uow.cur.execute("SELECT provider, role, model FROM role_models")
        role_models: dict[str, dict[str, str]] = {}
        for r in uow.cur.fetchall():
            role_models.setdefault(r["provider"], {})[r["role"]] = r["model"]
        # Models observed in runs complete the provider's catalog (a model it no longer lists, or one set by hand).
        uow.cur.execute("SELECT DISTINCT provider, units->>'model' AS model FROM usage_records WHERE units ? 'model'")
        observed_models: dict[str, set[str]] = {}
        for row in uow.cur.fetchall():
            model = row["model"]
            if model and model != "unknown" and _MODEL.fullmatch(model):
                observed_models.setdefault(row["provider"], set()).add(model)
        catalog = model_catalog()
        out = []
        for provider in sorted(ADAPTERS):
            windows = usage.get(provider, {})
            status = ("paused" if provider in off else "login_needed" if login.get(provider, "UNKNOWN") != "READY"
                      else "limited" if provider in limited
                      else "near_limit" if any(w["used_percent"] >= 80 for w in windows.values()) else "available")
            configured = models.get(provider, {}).get("model")
            options = [{**m, "source": "catalog"} for m in catalog.get(provider, [])]
            known = {m["id"] for m in options}
            extra = set(observed_models.get(provider, set())) | ({configured} if configured else set())
            options += [{"id": m, "label": m, "source": "provider_usage" if m in observed_models.get(provider, set()) else "configured"}
                        for m in sorted(extra - known)]
            out.append({"provider": provider, "identity": identity, "login": login.get(provider, "UNKNOWN"),
                        "enabled": provider not in off, "status": status, "running": running.get(provider, 0),
                        "session": windows.get("session"), "week": windows.get("week"),
                        "role_models": role_models.get(provider, {}),
                        "model": models.get(provider, {}).get("model"),
                        "model_updated_at": models.get(provider, {}).get("updated_at"),
                        "model_options": options,
                        "model_catalog_status": "CATALOG" if catalog.get(provider) else "OBSERVED" if observed_models.get(provider) else "UNAVAILABLE"})
        return out

    def model_for(self, uow: UnitOfWork, provider: str) -> str | None:
        return default_model(uow, provider)

    def set_model(self, uow: UnitOfWork, provider: str, model: str | None, *, principal: Principal) -> dict[str, Any]:
        if principal.value not in self.ctx.platform["platform"]["approvers"]:
            raise Forbidden(f"{principal.value} may not change agent models")
        if provider not in ADAPTERS:
            raise NotFound(f"no agent {provider!r}")
        if model is not None and not _MODEL.fullmatch(model):
            raise BadRequest("invalid model identifier")
        if model:
            uow.cur.execute("INSERT INTO provider_models (provider, model, updated_by) VALUES (%s, %s, %s) "
                            "ON CONFLICT (provider) DO UPDATE SET model = EXCLUDED.model, updated_by = EXCLUDED.updated_by, updated_at = now()",
                            (provider, model, principal.value))
            record_event(uow.cur, "AGENT_MODEL_CHANGED", actor=principal.value,
                         summary=f"{provider} default model changed by {principal.value}",
                         data={"provider": provider, "model": model}, pending=uow.events)
        else:
            uow.cur.execute("DELETE FROM provider_models WHERE provider = %s", (provider,))
            record_event(uow.cur, "AGENT_MODEL_RESET", actor=principal.value,
                         summary=f"{provider} default model reset by {principal.value}",
                         data={"provider": provider}, pending=uow.events)
        return next(a for a in self.view(uow) if a["provider"] == provider)

    def set_role_model(self, uow: UnitOfWork, provider: str, role: str, model: str | None,
                       *, principal: Principal) -> dict[str, Any]:
        if principal.value not in self.ctx.platform["platform"]["approvers"]:
            raise Forbidden(f"{principal.value} may not change agent models")
        if provider not in ADAPTERS:
            raise NotFound(f"no agent {provider!r}")
        if role not in ("ORCHESTRATOR", "DEVELOPER", "REVIEWER"):
            raise BadRequest("role must be ORCHESTRATOR, DEVELOPER or REVIEWER")
        if model is not None and not _MODEL.fullmatch(model):
            raise BadRequest("invalid model identifier")
        if model:
            uow.cur.execute("INSERT INTO role_models (provider, role, model, updated_by) VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (provider, role) DO UPDATE SET model = EXCLUDED.model, "
                            "updated_by = EXCLUDED.updated_by, updated_at = now()",
                            (provider, role, model, principal.value))
        else:
            uow.cur.execute("DELETE FROM role_models WHERE provider = %s AND role = %s", (provider, role))
        record_event(uow.cur, "ROLE_MODEL_CHANGED", actor=principal.value,
                     summary=f"{provider} {role} model {'changed' if model else 'reset'}",
                     data={"provider": provider, "role": role, "model": model}, pending=uow.events)
        return next(a for a in self.view(uow) if a["provider"] == provider)

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
                             + ("" if enabled else ": new work goes to the other agents; running work finishes; "
                                "tasks it leads wait until it is resumed"),
                     data={"provider": provider, "identity": identity}, pending=uow.events)
        return next(a for a in self.view(uow) if a["provider"] == provider)
