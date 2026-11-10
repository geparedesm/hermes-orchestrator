# Independent provider quota refresh

The control plane periodically refreshes subscription allowances even when one provider is idle. Agent Manager
runs only the pinned CLIs' account/usage commands in short-lived, resource-limited containers. Probes mount one
identity's credential volume and no projects, task sessions, Docker socket, or platform services. Their sole network
route is a provider-only proxy. Raw CLI output stays in memory and only allowlisted quota observations leave the
container. No model turn is requested and probes do not consume execution launches or task budgets.

Codex uses the App Server initialize/initialized handshake followed by `account/rateLimits/read`, with no thread or
turn. Claude Code 2.1.280 accepts `/usage` in print mode and returns `usage_report` on a synthetic assistant message;
offline verification confirmed zero model turns and zero tokens. Its existing setup-token credential does not carry
profile access. The operator must use `claude auth login --claudeai` once to enable independent quota readings.
Existing setup-token execution support remains available. Saved Claude logins are copied into each execution's
tmpfs and refreshed credentials are persisted with locking and compare-and-swap, as for Codex.

Official interfaces:
- https://learn.chatgpt.com/docs/app-server
- https://code.claude.com/docs/en/agent-sdk/typescript#sdkassistantmessage
- https://code.claude.com/docs/en/authentication#credential-management

`scheduler.quota_refresh` defaults to enabled, a 300-second interval, and bounded failure backoff. The scheduler
refreshes both providers for its configured identity while tasks are runnable or agent executions are active.
The scheduler hook starts the pass on a background thread (at most one per process), so a probe never stalls scheduling. PostgreSQL holds attempt status, a fenced expiring claim, and next-attempt time. Network I/O occurs after the claim
transaction commits. Concurrent requests share the persisted attempt; a crashed process's claim expires. Failed
reads retain the last observation and its original timestamp. Authentication/profile failures are visible as
operational status and do not revoke an otherwise usable inference credential.

Only new quota-aware developer assignments in ENFORCE mode wait for a due refresh before launching. They reuse
the existing pending-launch queue. Once the bounded attempt completes, routing uses fresh comparable windows or
the existing scored fallback with an explicit reason. Failure backoff prevents a permanent queue stall. OBSERVE,
SCORED, pinned running assignments, fixes and cross-review keep their existing behavior.

Snapshots keep true observation times, never overwrite newer execution measurements, and never turn an elapsed
reset into measured zero usage. Global session/week meters alone feed the existing router; unknown/missing windows
and model-specific pools are not fabricated. This policy measures relative plan percentages, not money or tokens.

Validation includes protocol fixtures, rejected/partial/non-finite snapshots, credential isolation, refresh fencing,
backoff, restart recovery, demand/idle behavior, queued assignment routing, and zero-token CLI smoke checks using
throwaway provider identities. Live operator verification reads only account usage and does not launch a model.
