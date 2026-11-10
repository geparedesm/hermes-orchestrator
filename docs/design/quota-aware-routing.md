# Developer selection by remaining quota

New developer assignments use deterministic control-plane routing. A model's provider suggestion and
`agents.preferred` remain soft preferences; they cannot outweigh a reliable quota comparison. Explicit operator
executions through the CLI/API retain their chosen provider. Planning, UI design, reviews, existing attempts' fixes,
transient retries, and alternate-developer recovery retain their existing selection rules.

## Configuration and rollout

Platform `project_defaults.agents.routing` defaults to:

```yaml
agents:
  routing:
    policy: MOST_REMAINING_QUOTA
    mode: ENFORCE
    max_age_seconds: 900
```

Projects may override these fields in `.hermes/project.yaml`. Configuration is pinned to the task as before:
existing task configurations without `routing` keep `SCORED` selection. Updating platform defaults does not rewrite
existing project/task configurations; use the normal project configuration scan/approval workflow before creating
tasks with the new settings. No database migration is needed.

`mode: OBSERVE` records both the quota candidate and the actual scored selection without changing assignments.
`policy: SCORED` disables quota selection. Rollback uses that setting through the normal configuration workflow;
already running executions are never moved. The implementation defaults to enforcement for newly configured work;
operators can observe first before enabling it for their projects.

## Quota calculation and unavailable information

The source is the existing `provider_usage` table, scoped to `Context.provider_identity`. Only numeric operational
observations collected by the provider adapters are used. The router makes no external calls, starts no measurement
executions, and never reads credential contents or raw provider output.

For each provider, both session and week observations must exist, be no older than `max_age_seconds`, not be dated
in the future, and have reset times still in the future. Availability is:

```text
min(100 - session.used_percent, 100 - week.used_percent)
```

This is a relative percentage of plan allowance, not a currency/token balance or a prediction of work capacity.
An expired reset does not become a measured zero consumption. Missing, stale, invalid, or reset observations produce
unknown availability. If either eligible provider has unknown availability, both are compared using the existing
scored router. No partial-window comparison or invented allowance is allowed.

The existing adapters currently normalize session/week windows only. Measurements arrive at execution end (Claude's
observation conservatively uses the execution start). Long runs and usage from other clients can therefore make
observations incomplete or stale. Model-specific quota pools are not represented: validate observation coverage for
the selected models in real executions before relying on enforcement; use OBSERVE or SCORED when the reported global
windows do not describe those models' effective limits. This change does not invent a provider quota endpoint.

## Selection and execution boundaries

1. Apply provider allowlists, pause state, known authentication unavailability, and existing quota-exhaustion holds.
   Existing bootstrap behavior for absent/UNKNOWN credential references is preserved; execution dispatch still
   performs its credential checks.
2. With two eligible providers and complete recent observations, choose the greater availability. Ties use the
   lower active execution count, then existing scores and their deterministic default-provider tiebreaker.
3. With one eligible provider, choose it. When all providers are held for quota/pause, queue the assignment using
   the existing hold mechanism, without spending an execution launch or rejecting the orchestrator action.
4. Mark only new assignments eligible for rerouting while queued. Reconsider when the scope/priority blocker clears,
   immediately before requesting an execution. If the provider changes, remove old session/model/image fields and
   resolve the chosen provider's model normally. Keep the prepared workspace, lease fencing, and attempt number.
5. Once an execution exists, its provider is fixed. No cross-provider continuation session is reused. Fixes and
   failure retries stay with the existing recovery rules.

Cross-review remains mandatory by the other provider. A reviewer without quota waits for its reset; it cannot be
replaced by its own developer. When both providers implement parts of a task, the existing integration rule still
requires reviews from both providers.

`AGENT_ROUTED` includes actual and candidate providers, policy/mode, scores/factors, exclusion reasons, identity,
window values/timestamps/ages/statuses, remaining percentages, and fallback reason. No new UI or secret data is added.

## Validation

Pure tests cover both winners, restrictive weekly quotas, tie breaking, missing/stale/reset/future/invalid data,
freshness boundaries, observation mode, rollback, and exclusions. Integration tests use real PostgreSQL/Redis and
the fake Agent Manager to cover identity isolation, configuration pinning, pause/auth filtering, cross-review/fix
continuity, queued provider/model changes without duplicate launches/workspaces, and waiting with both quotas exhausted.
