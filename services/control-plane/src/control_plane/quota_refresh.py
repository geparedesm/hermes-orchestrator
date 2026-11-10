"""PostgreSQL-fenced quota refresh. Every network operation runs outside a database transaction."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import threading

from ho_core.ids import uuid7
from ho_core.quota import snapshot
from ho_core.routing import PROVIDERS

from .agentmgr import AgentManagerError
from .agents import record_windows, routing_quota
from .context import Context, UnitOfWork
from .events import record_event

log = logging.getLogger(__name__)
CLAIM_SECONDS = 120  # proxy startup + CLI timeout + collection; survives an interrupted scheduler pass
DEMAND_STATES = ("BACKLOG", "READY", "PLANNING", "QUEUED", "RUNNING", "TESTING", "REVIEW", "FIX_REQUIRED", "QUALITY_GATE")


def enabled(ctx: Context) -> bool:
    return bool(ctx.agents and (ctx.platform.get("scheduler", {}).get("quota_refresh") or {}).get("enabled", False))


def before_assignment(ctx: Context, uow: UnitOfWork, *, providers: list[str], max_age_seconds: int) -> str | None:
    """Queue only until a bounded, due attempt finishes. A failed read's backoff permits scored fallback."""
    if not enabled(ctx) or len(providers) < 2:
        return None
    now = datetime.now(timezone.utc)
    quotas = routing_quota(uow, ctx.provider_identity, now=now, max_age_seconds=max_age_seconds)
    waiting = False
    for provider in providers:
        if quotas[provider].status == "RECENT":
            continue
        uow.cur.execute(
            "INSERT INTO provider_quota_refresh (provider, identity) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (provider, ctx.provider_identity))
        # A successful old probe must not suppress a stricter task's refresh. Failure cooldown is respected.
        uow.cur.execute(
            "UPDATE provider_quota_refresh SET status = 'PENDING', next_attempt_at = now() "
            "WHERE provider = %s AND identity = %s AND status <> 'RUNNING' "
            "AND (next_attempt_at <= now() OR (status = 'OK' AND completed_at < now() - %s * interval '1 second'))",
            (provider, ctx.provider_identity, max_age_seconds))
        uow.cur.execute("SELECT status FROM provider_quota_refresh WHERE provider = %s AND identity = %s",
                        (provider, ctx.provider_identity))
        waiting |= uow.cur.fetchone()["status"] in ("PENDING", "RUNNING")
    if waiting:
        uow.wake_scheduler = True
        return "refreshing provider quotas before assigning a new developer"
    return None


class QuotaRefresh:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    def background_tick(self) -> None:
        """Scheduler hook. A probe can take up to a minute, so it never runs on the scheduling loop itself.
        At most one background pass per process; the PostgreSQL claims fence other processes."""
        if not enabled(self.ctx):
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._safe_tick, name="quota-refresh-pass", daemon=True)
            self._worker.start()

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception:  # noqa: BLE001 - a failed pass is retried on a later scheduler pass
            log.exception("quota refresh pass failed")

    def tick(self, *, requested: bool = False) -> dict[str, int]:
        if not enabled(self.ctx):
            return {"refreshed": 0}
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT 1 FROM tasks WHERE state = ANY(%s) UNION ALL "
                            "SELECT 1 FROM executions WHERE agent_run AND state IN ('REQUESTED', 'STARTING', 'RUNNING') LIMIT 1",
                            (list(DEMAND_STATES),))
            demand = uow.cur.fetchone() is not None
            uow.cur.execute("SELECT 1 FROM provider_quota_refresh WHERE identity = %s AND status IN ('PENDING', 'RUNNING') LIMIT 1",
                            (self.ctx.provider_identity,))
            requested = requested or uow.cur.fetchone() is not None
        if not demand and not requested:
            return {"refreshed": 0}
        # The two independent provider reads run concurrently, each with its own database claim.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="quota-refresh") as pool:
            results = list(pool.map(self.refresh, PROVIDERS))
        return {"refreshed": sum(results)}

    def refresh(self, provider: str) -> int:
        settings = self.ctx.platform["scheduler"]["quota_refresh"]
        interval = int(settings.get("interval_seconds", 300))
        identity, claim = self.ctx.provider_identity, uuid7()
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute("SELECT status FROM credential_refs WHERE provider = %s AND identity = %s", (provider, identity))
            credential = uow.cur.fetchone()
            if credential and credential["status"] not in ("READY", "UNKNOWN"):
                return 0
            quotas = routing_quota(uow, identity, now=datetime.now(timezone.utc), max_age_seconds=interval)
            uow.cur.execute("SELECT * FROM provider_quota_refresh WHERE provider = %s AND identity = %s", (provider, identity))
            previous = uow.cur.fetchone()
            if quotas[provider].status == "RECENT" and (not previous or previous["status"] not in ("PENDING", "RUNNING")):
                return 0
            uow.cur.execute(
                "INSERT INTO provider_quota_refresh (provider, identity) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (provider, identity))
            uow.cur.execute(
                "UPDATE provider_quota_refresh SET status = 'RUNNING', claim_id = %s, "
                "lease_until = now() + %s * interval '1 second', last_attempt_at = now() "
                "WHERE provider = %s AND identity = %s AND next_attempt_at <= now() "
                "AND (lease_until IS NULL OR lease_until <= now()) RETURNING failures, last_attempt_at",
                (claim, CLAIM_SECONDS, provider, identity))
            claimed = uow.cur.fetchone()
            if claimed is None:
                return 0
        # No PostgreSQL connection or row lock is held while Docker/CLI/provider work happens.
        try:
            result = snapshot(self.ctx.agents.quota(provider, identity))
        except AgentManagerError:
            result = {"status": "UNAVAILABLE", "windows": []}
        except Exception:  # noqa: BLE001 - release the claim even for malformed upstream responses
            log.warning("quota probe failed for %s/%s", provider, identity)
            result = {"status": "UNAVAILABLE", "windows": []}
        status = result["status"]
        failures = 0 if status == "OK" else int(claimed["failures"]) + 1
        delay = interval if status == "OK" else min(int(settings.get("max_backoff_seconds", 1800)), interval * 2 ** min(failures - 1, 10))
        with self.ctx.unit_of_work() as uow:
            uow.cur.execute(
                "UPDATE provider_quota_refresh SET status = %s, claim_id = NULL, lease_until = NULL, completed_at = now(), "
                "next_attempt_at = now() + %s * interval '1 second', failures = %s "
                "WHERE provider = %s AND identity = %s AND claim_id = %s AND lease_until > now() RETURNING provider",
                (status, delay, failures, provider, identity, claim))
            if uow.cur.fetchone() is None:
                return 0  # expired/replaced claim, including an interactive credential renewal
            if status == "OK":
                # A provider cannot make an old cached response look freshly observed: cap at probe start.
                for w in result["windows"]:
                    w["observed_at"] = min(w["observed_at"], claimed["last_attempt_at"].timestamp())
                record_windows(uow, provider, identity, result["windows"])
            if previous is None or previous["status"] != status:
                record_event(uow.cur, "PROVIDER_QUOTA_REFRESH", actor="control-plane",
                             summary=f"{provider}/{identity} quota refresh: {status.lower()}",
                             data={"provider": provider, "identity": identity, "status": status}, pending=uow.events)
            uow.wake_scheduler = True
        return 1
