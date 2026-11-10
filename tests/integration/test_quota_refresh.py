"""Independent quota reads against real PostgreSQL: leases, fallback and credential isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time

import pytest

from control_plane.agents import record_windows
from control_plane.quota_refresh import QuotaRefresh, before_assignment
from fake_agents import FakeAgentManager  # type: ignore[import-not-found]
from test_agents import ready_task  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration


def result(session=25, week=40, *, age=1):
    stamp = datetime.now(timezone.utc).timestamp() - age
    return {"status": "OK", "windows": [
        {"window": kind, "used_percent": used, "observed_at": stamp, "resets_at": stamp + 3600}
        for kind, used in (("session", session), ("week", week))]}


def rows(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def agents(services):
    fake = FakeAgentManager()
    fake.quota_results = {p: result for p in ("claude", "codex")}
    services.ctx.agents = fake
    return fake


@pytest.fixture
def refresh(services, agents):
    services.ctx.platform["scheduler"]["quota_refresh"]["enabled"] = True
    return QuotaRefresh(services.ctx)


def test_idle_has_no_probes_and_explicit_operator_read_never_creates_executions(api, services, agents, refresh):
    assert refresh.tick() == {"refreshed": 0}
    assert not agents.quota_calls
    response = api.post("/v1/agents/quota-refresh")
    assert response.status_code == 200 and response.json()["refreshed"] == 2
    assert sorted(agents.quota_calls) == [("claude", "default"), ("codex", "default")]
    assert rows(services, "SELECT * FROM executions") == []
    assert len(rows(services, "SELECT * FROM provider_usage")) == 4
    assert refresh.tick(requested=True) == {"refreshed": 0}
    assert len(agents.quota_calls) == 2
    view = api.get("/v1/agents").json()["agents"]
    assert all(a["quota_refresh"]["status"] == "OK" for a in view)
    assert all(a["quota_refresh"]["completed_at"] for a in view)


def test_runnable_work_refreshes_both_providers_even_with_no_execution(api, services, projects_root, agents, refresh):
    task = ready_task(api, services, projects_root)
    assert api.get(f"/v1/tasks/{task}").json()["state"] == "READY"
    services.scheduler.run_once()
    deadline = time.monotonic() + 10  # the scheduler hook refreshes on a background thread
    while len(agents.quota_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(agents.quota_calls) == 2 and not agents.specs


def test_background_pass_never_blocks_the_scheduler_and_runs_once_per_process(services, agents, refresh):
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(10)
        return result()

    agents.quota_results["claude"] = slow
    rows(services, "INSERT INTO provider_quota_refresh (provider, identity) VALUES ('claude', 'default')")
    try:
        started = time.monotonic()
        refresh.background_tick()
        assert entered.wait(10)
        refresh.background_tick()  # a pass is still running: no second pass
        assert time.monotonic() - started < 5
    finally:
        release.set()
    refresh._worker.join(10)
    assert not refresh._worker.is_alive()
    assert agents.quota_calls.count(("claude", "default")) == 1


def test_profile_failure_backoff_falls_back_and_does_not_revoke_inference_credentials(services, agents, refresh):
    agents.quota_results["claude"] = {"status": "PROFILE_REQUIRED", "windows": [], "error": "private value"}
    assert refresh.tick(requested=True)["refreshed"] == 2
    [attempt] = rows(services, "SELECT * FROM provider_quota_refresh WHERE provider = 'claude'")
    assert attempt["status"] == "PROFILE_REQUIRED" and attempt["failures"] == 1
    assert 290 < (attempt["next_attempt_at"] - attempt["completed_at"]).total_seconds() <= 300
    with services.ctx.unit_of_work() as uow:
        assert before_assignment(services.ctx, uow, providers=["claude", "codex"], max_age_seconds=900) is None
    assert refresh.tick(requested=True)["refreshed"] == 0
    assert rows(services, "SELECT * FROM credential_refs") == []
    assert "private value" not in str(rows(services, "SELECT * FROM events"))


def test_missing_window_does_not_repeatedly_refresh_or_permanently_queue_work(services, agents, refresh):
    partial = result()
    partial["windows"] = partial["windows"][:1]
    agents.quota_results["claude"] = partial
    refresh.tick(requested=True)
    with services.ctx.unit_of_work() as uow:
        assert before_assignment(services.ctx, uow, providers=["claude", "codex"], max_age_seconds=900) is None
    assert refresh.tick(requested=True)["refreshed"] == 0
    assert len(agents.quota_calls) == 2


def test_before_assignment_requests_stale_data_and_preserves_other_identities(services, agents, refresh):
    with services.ctx.unit_of_work() as uow:
        record_windows(uow, "claude", "other", result(1, 1)["windows"])
        reason = before_assignment(services.ctx, uow, providers=["claude", "codex"], max_age_seconds=900)
    assert "refreshing provider quotas" in reason
    assert refresh.tick()["refreshed"] == 2
    assert rows(services, "SELECT used_percent FROM provider_usage WHERE identity = 'other'")[0]["used_percent"] == 1
    with services.ctx.unit_of_work() as uow:
        assert before_assignment(services.ctx, uow, providers=["claude", "codex"], max_age_seconds=900) is None


def test_fresh_execution_observations_skip_probes(services, agents, refresh):
    with services.ctx.unit_of_work() as uow:
        for p in ("claude", "codex"):
            record_windows(uow, p, "default", result()["windows"])
    assert refresh.tick(requested=True)["refreshed"] == 0
    assert not agents.quota_calls


def test_concurrent_refresh_has_one_claim_and_holds_no_database_lock_during_network(services, agents, refresh):
    entered, release = threading.Event(), threading.Event()

    def slow():
        # This independent transaction must see a committed claim while the first call is still waiting.
        [claim] = rows(services, "SELECT status FROM provider_quota_refresh WHERE provider = 'claude'")
        assert claim["status"] == "RUNNING"
        entered.set()
        assert release.wait(10)
        return result()

    agents.quota_results["claude"] = slow
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(refresh.refresh, "claude")
        try:
            assert entered.wait(10)
            assert QuotaRefresh(services.ctx).refresh("claude") == 0
        finally:
            release.set()
        assert first.result(timeout=10) == 1
    assert agents.quota_calls == [("claude", "default")]


def test_credential_renewal_fences_probe_and_clears_old_account_quota(api, services, agents, refresh):
    def renew_during_read():
        assert api.post("/v1/credentials/claude/default/ready").status_code == 200
        return result(90, 90)

    with services.ctx.unit_of_work() as uow:
        record_windows(uow, "claude", "default", result(age=1000)["windows"])
    agents.quota_results["claude"] = renew_during_read
    assert refresh.refresh("claude") == 0
    assert not rows(services, "SELECT * FROM provider_usage WHERE provider = 'claude'")
    [claim] = rows(services, "SELECT * FROM provider_quota_refresh WHERE provider = 'claude'")
    assert claim["status"] == "PENDING" and claim["claim_id"] is None


def test_older_probe_never_overwrites_a_newer_execution_observation(services, agents, refresh):
    def finish_during_read():
        with services.ctx.unit_of_work() as uow:
            record_windows(uow, "claude", "default", result(70, 80, age=0)["windows"])
        return result(10, 20, age=10)

    agents.quota_results["claude"] = finish_during_read
    assert refresh.refresh("claude") == 1
    assert {float(r["used_percent"]) for r in rows(services, "SELECT * FROM provider_usage WHERE provider = 'claude'")} == {70, 80}


def test_expired_claim_is_recovered_but_late_result_cannot_publish(services, agents, refresh):
    rows(services, "INSERT INTO provider_quota_refresh (provider, identity, status, lease_until) "
         "VALUES ('codex', 'default', 'RUNNING', now() - interval '1 second')")
    assert refresh.refresh("codex") == 1
    rows(services, "DELETE FROM provider_usage")
    rows(services, "UPDATE provider_quota_refresh SET next_attempt_at = now()")

    def expired():
        rows(services, "UPDATE provider_quota_refresh SET lease_until = now() - interval '1 second'")
        return result()

    agents.quota_results["codex"] = expired
    assert refresh.refresh("codex") == 0
    assert not rows(services, "SELECT * FROM provider_usage")


def test_invalid_upstream_response_is_filtered_and_failed_attempt_retains_old_timestamps(services, agents, refresh):
    old = result(30, 40, age=1000)
    with services.ctx.unit_of_work() as uow:
        record_windows(uow, "codex", "default", old["windows"])
    agents.quota_results["codex"] = {"status": "OK", "windows": [{"token": "private"}]}
    assert refresh.refresh("codex") == 1
    [attempt] = rows(services, "SELECT * FROM provider_quota_refresh")
    assert attempt["status"] == "INVALID_RESPONSE"
    assert all(abs(r["observed_at"].timestamp() - old["windows"][0]["observed_at"]) < .001
               for r in rows(services, "SELECT * FROM provider_usage"))


def test_disable_restores_immediate_fallback_and_known_auth_unavailability_skips_probe(services, agents, refresh):
    services.ctx.platform["scheduler"]["quota_refresh"]["enabled"] = False
    assert refresh.tick(requested=True)["refreshed"] == 0
    with services.ctx.unit_of_work() as uow:
        assert before_assignment(services.ctx, uow, providers=["claude", "codex"], max_age_seconds=900) is None
    assert not agents.quota_calls
