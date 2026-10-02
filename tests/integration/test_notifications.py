"""Phase 9 notifications through Hermes: attention events immediately with the action to take, routine events
in a digest, internal events not notified, HMAC V2 signatures Hermes accepts, order and backoff while Hermes
is down."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from control_plane.notifications import ROUTINE_DIGEST, render, sign

import test_git  # type: ignore[import-not-found]
from conftest import PLUGIN_TOKEN  # type: ignore[import-not-found]

pytestmark = pytest.mark.integration
agents = pytest.fixture(test_git.agents.__wrapped__)
repo = pytest.fixture(test_git.repo.__wrapped__)
task = pytest.fixture(test_git.task.__wrapped__)
SECRET = "test-webhook-secret"


def q(services, sql, *args):
    with services.ctx.db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


class Hermes:
    """Stand-in for the Hermes webhook route: verifies the signature with Hermes's formula."""

    def __init__(self) -> None:
        self.up = True
        self.received: list[dict] = []
        self.request_ids: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            return httpx.Response(503)
        timestamp = request.headers["X-Webhook-Timestamp"]
        expected = hmac.new(SECRET.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers["X-Webhook-Signature-V2"]):
            return httpx.Response(401)
        self.received.append(json.loads(request.content))
        self.request_ids.append(request.headers["X-Request-ID"])
        return httpx.Response(200, json={"status": "delivered", "target": "log"})


@pytest.fixture
def hermes(services):
    fake = Hermes()
    services.outbox.url = "http://hermes.test/webhooks/orchestration"
    services.outbox.secret = SECRET
    services.outbox.client = httpx.Client(transport=httpx.MockTransport(fake))
    return fake


def test_signature_matches_hermes_formula():
    headers = sign("k", b'{"a":1}', 1700000000)
    expected = hmac.new(b"k", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()
    assert headers == {"X-Webhook-Signature-V2": expected, "X-Webhook-Timestamp": "1700000000"}


def test_internal_events_are_not_notified(api, services, agents, task):
    api.post(f"/v1/tasks/{task}/executions", {"role": "DEVELOPER", "provider": "codex", "prompt": "x"})
    types = {r["type"] for r in q(services, "SELECT payload->>'type' AS type FROM notifications")}
    assert "GRANT_ISSUED" not in types and "AGENT_ASSIGNED" not in types and "TASK_STATE_CHANGED" not in types
    assert "TASK_CREATED" in types


def test_attention_events_are_delivered_immediately_with_the_action(api, services, agents, task, hermes):
    from control_plane.events import record_event

    [row] = q(services, "SELECT id, project_id FROM tasks WHERE key = %s", task)
    with services.ctx.unit_of_work() as uow:
        record_event(uow.cur, "APPROVAL_REQUIRED", actor="control-plane", project_id=row["project_id"], task_id=row["id"],
                     summary="MERGE: merge the change", data={"approval_id": "01a0f000-aaaa", "action": "MERGE"})
    sent = services.outbox.deliver()
    assert sent["sent"] >= 1 and sent["digests"] == 0  # routine waits for the digest
    [message] = [m for m in hermes.received if "01a0f000-aaaa" in m["text"]]
    assert message["task"] == task and message["project"] == "demo"
    assert message["text"].startswith(f"[{task}] MERGE: merge the change")
    assert "/orch approve 01a0f000-aaaa" in message["text"]


def test_routine_events_are_aggregated_into_a_digest(api, services, agents, task, hermes):
    services.outbox.deliver()
    before = len(hermes.received)
    q(services, "UPDATE notifications SET created_at = now() - %s WHERE priority = 'ROUTINE'", ROUTINE_DIGEST * 2)
    services.outbox.deliver()
    [digest] = hermes.received[before:]
    assert digest["event"] == "DIGEST" and digest["text"].startswith("Progress (")
    assert task in digest["text"]
    assert q(services, "SELECT count(*) AS n FROM notifications WHERE state = 'PENDING'")[0]["n"] == 0


def test_outage_keeps_order_and_backs_off(api, services, agents, task, hermes):
    hermes.up = False
    assert services.outbox.deliver()["failed"] == 1
    assert services.outbox.deliver() == {"sent": 0, "failed": 0, "digests": 0}  # the head backs off: no request
    hermes.up = True
    q(services, "UPDATE notifications SET next_attempt_at = now()")
    services.outbox.deliver()
    assert hermes.received and len(hermes.request_ids) == len(set(hermes.request_ids))
    # The retry carries a new attempt number: Hermes caches failed request IDs and would call a repeat a duplicate.
    assert any(rid.endswith(":2") for rid in hermes.request_ids)


def test_ready_for_merge_and_completion_messages():
    text = render({"type": "READY_FOR_MERGE", "summary": "T-3: all required checks passed", "data": {}}, "T-3")
    assert text.startswith("[T-3] ") and "/orch status T-3" in text
    assert render({"type": "TASK_COMPLETED", "summary": "T-3: merged and verified"}, "T-3") == "[T-3] T-3: merged and verified"


def test_digests_are_rate_limited_however_large_the_backlog(api, services, agents, task, hermes):
    from control_plane.events import record_event
    from control_plane.notifications import DIGEST_MAX

    [row] = q(services, "SELECT id, project_id FROM tasks WHERE key = %s", task)
    with services.ctx.unit_of_work() as uow:
        for n in range(DIGEST_MAX * 2):
            record_event(uow.cur, "SUBTASK_ACCEPTED", actor="x", project_id=row["project_id"], task_id=row["id"],
                         summary=f"subtask {n} accepted")
    q(services, "UPDATE notifications SET created_at = now() - %s WHERE priority = 'ROUTINE'", ROUTINE_DIGEST * 2)
    services.outbox.deliver()
    digests = [m for m in hermes.received if m["event"] == "DIGEST"]
    assert len(digests) == 1 and "more (/orch tasks)" in digests[0]["text"]
    with services.ctx.unit_of_work() as uow:  # more routine events right after: no second digest yet
        record_event(uow.cur, "SUBTASK_ACCEPTED", actor="x", project_id=row["project_id"], task_id=row["id"], summary="late")
    q(services, "UPDATE notifications SET created_at = now() - %s WHERE state = 'PENDING'", ROUTINE_DIGEST * 2)
    services.outbox.deliver()
    assert len([m for m in hermes.received if m["event"] == "DIGEST"]) == 1


def test_active_tasks_are_filtered_before_the_limit(api, services, agents, task):
    q(services, "UPDATE tasks SET state = 'DONE' WHERE key <> %s", task)
    for _ in range(3):
        key = api.post("/v1/tasks", {"project": "demo", "request": "noise"}).json()["key"]
        q(services, "UPDATE tasks SET state = 'CANCELLED' WHERE key = %s", key)
    keys = [t["key"] for t in api.get("/v1/tasks?limit=2&active=true").json()["tasks"]]
    assert keys == [task]


def test_a_backlog_is_suppressed_before_a_channel_is_connected(api, services, task, hermes):
    pending = q(services, "SELECT count(*) AS n FROM notifications WHERE state = 'PENDING'")[0]["n"]
    assert pending > 0
    assert api.post("/v1/notifications/suppress", {}, token=PLUGIN_TOKEN, principal="dashboard:operator").status_code == 403
    response = api.post("/v1/notifications/suppress", {})
    assert response.status_code == 200 and response.json()["suppressed"] == pending
    assert q(services, "SELECT count(*) AS n FROM notifications WHERE state = 'PENDING'")[0]["n"] == 0
    [audit] = q(services, "SELECT actor, data FROM events WHERE type = 'NOTIFICATIONS_SUPPRESSED'")
    assert audit["actor"] == "host-cli:operator" and audit["data"]["attention"] + audit["data"]["routine"] == pending
    # Nothing old is delivered; what happens afterwards is.
    assert api.post("/v1/notifications/test").status_code == 200
    services.outbox.deliver()
    assert [r["event"] for r in hermes.received] == ["NOTIFICATION_TEST"]
    assert "approvals and completed tasks will be reported here" in hermes.received[0]["text"]


def test_suppression_can_be_limited_to_older_notifications(api, services, task):
    cutoff = q(services, "SELECT max(created_at) AS t FROM notifications")[0]["t"]
    api.post("/v1/notifications/test")
    response = api.post("/v1/notifications/suppress", {"before": cutoff.isoformat()})
    assert response.status_code == 200
    rows = q(services, "SELECT n.state FROM notifications n JOIN events e ON e.seq = n.event_seq WHERE e.type = 'NOTIFICATION_TEST'")
    assert [r["state"] for r in rows] == ["PENDING"]


def test_notifications_withdrawn_while_a_delivery_pass_runs_are_not_sent(api, services, task, hermes, monkeypatch):
    original = services.outbox._keys

    def suppress_meanwhile(rows):  # the pass has read its batch; the operator withdraws it before it is posted
        api.post("/v1/notifications/suppress", {})
        return original(rows)

    monkeypatch.setattr(services.outbox, "_keys", suppress_meanwhile)
    q(services, "UPDATE notifications SET created_at = now() - interval '1 hour'")  # routine ones are due for a digest
    services.outbox.deliver()
    assert hermes.received == []
    assert q(services, "SELECT count(*) AS n FROM notifications WHERE state = 'SENT'")[0]["n"] == 0
