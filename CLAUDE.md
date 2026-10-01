# hermes-orchestrator — working context

Local-first multi-agent platform: Claude Code and Codex work in isolated Docker workers; PostgreSQL is the only execution authority; nothing enters `main`/`master` without an action-bound human approval. Spec: `MASTER_SPEC.md` (2,500 lines; read by section, never whole). Roadmap: `PHASES.md`.

## Status

Phases 0–6 approved and merged (PRs #2–#7). Evidence per phase: `docs/validation/phase-N.md`. Next: Phase 7 (multi-agent orchestration), then 8 recovery, 9 Hermes integration, 10 dashboard, 11 hardening.

## Architecture decisions (ARCHITECTURE.md §2)

- AD-01 One Hermes plugin `orchestration` + Dashboard tab; no fork of Hermes.
- AD-02 PostgreSQL is the only execution authority; no dual-write to Hermes Kanban.
- AD-03 Six services: hermes, control-plane, agent-manager, git-service, postgres, redis. No persistent Claude container.
- AD-04 Orchestrator = bounded steps in an ORCHESTRATOR execution returning a schema-validated action proposal; the control plane validates and applies it.
- AD-05 Workers cannot reach control plane/DB/Redis/Docker; Agent Manager collects their output.
- AD-06 Each workspace is an isolated clone under `<project>/.hermes/worktrees/`, never a linked worktree.
- AD-07 Git Service: only holder of GitHub credentials and only writer of project repositories.
- AD-08 Leases, queue, idempotency in PostgreSQL (row locks, epochs); Redis only for wake-ups/fan-out.
- AD-09 Python 3.12, FastAPI, psycopg 3, Alembic (raw SQL migrations), shared package `ho_core`.
- AD-10 Human-only actions (approve, budgets, UNLIMITED, production) are never LLM tools.
- AD-11 Provider logins in Docker volumes `cred-<provider>-<identity>`; per-execution in-memory CLI homes.
- AD-12 Notifications through a durable outbox → Hermes webhook (Phase 9).
- AD-13 Artifacts on a volume partitioned by project; PostgreSQL stores metadata + SHA-256.
- AD-14 High-risk capabilities are absent from workers; in-worker command classification is advisory.

## Invariants (never break)

1. No merge into a protected branch without a consumed MERGE approval bound to exact commits and a passing Quality Gate evaluation; Git Service re-verifies a signed authorization.
2. Workers: no Docker socket, no GitHub credentials, no other projects, no platform services, egress only through their per-execution proxy.
3. Credentials and secret values never reach Git, PostgreSQL, logs, artifacts, manifests, or backups; secrets are granted by reference.
4. No model reasoning or raw provider streams are stored; adapters keep allowlisted operational events only.
5. Human changes are never overwritten; integration and merges run without touching the user's working tree except a fast-forward Git refuses when unsafe.
6. Task states change only through `ho_core.statemachine` via `Tasks.transition`; `READY_FOR_MERGE` only from a passing Quality Gate; `DONE` only after post-merge verification.
7. Never invent CLI flags, APIs, or credential paths: verify in official docs or `--help` (pinned versions in `workers/versions.env`).

## Module map

| Path | Responsibility |
| --- | --- |
| `packages/ho_core/` | enums, `statemachine`, `config` (layered + tighten-only), `policy/` (engine, hard, commands), `adapters/` (Claude, Codex), `gitpolicy`, `verification` (risk, gaps, steps), `routing` (provider scores), `redact`, `schemas` |
| `services/control-plane/.../` | `app.py` (API), `cli.py` (`ho`), `tasks`, `projects`, `approvals`, `scheduler` (+hooks), `executions` (grants, dispatch, sync, finalize), `credentials`, `gitops` (workspaces, integration, merges), `verification` (verifications, reviews, Quality Gate), `orchestration` (leases, steps, actions, subtask cycle, launches), `budgets` (reservations), `manifests` |
| `services/agent-manager/.../` | `plan.py` (hard invariants), `docker_ops.py` (workers, proxies, inputs, secrets, sessions, test environments), `compose.py` (project Compose sanitizing), `secrets.py`, `images.py` |
| `services/git-service/.../` | `repo_ops.py` (workspaces, collect, divergence, integrate, merge), `github.py` (`gh`), `gitcmd.py` (hardened git) |
| `services/egress-proxy/` | CONNECT-only allowlisting proxy, one per agent execution |
| `workers/` | `agent-base` (+`ho-verify`, `ho-wait-secrets`), `toolchains/*`, `providers/{claude,codex}` (`ho-agent-run`, `ho-auth-login`), `browser-runner` |
| `migrations/versions/` | `0001`–`0006` (raw SQL) |
| `schemas/` | JSON Schemas: project, capability, platform, manifest, task, agent-result, review-result |
| `scripts/` | `build-images.sh`, `auth-login.sh`, `init-secrets.sh`, `smoke-phase{2..7}.sh` |

## Commands

```bash
make test-unit                      # fast, no services
make test-integration               # starts throwaway PostgreSQL/Redis (compose.test.yaml)
HO_TEST_DOCKER=1 .venv/bin/pytest tests/docker -q -p no:warnings   # real Docker (needs make images)
make lint validate-schemas
./scripts/smoke-phaseN.sh           # end-to-end on a throwaway Compose stack (N = 2..7)
make images && make up              # rebuild the operator's live stack
docker compose exec -T control-plane ho <group> <cmd>   # operator CLI
```

Integration tests use the real Git Service in process and `tests/integration/fake_agents.py` for Agent Manager. Test fixtures for provider output: `tests/fixtures/providers/`.

## Conventions

- Branch `codex/phase-N-<topic>`; commits `[feat|fix|docs] Message` + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR body ends with the Claude Code line; give the merge one-liner after opening a PR.
- Each phase: design → (Codex adversarial review for critical phases) → build → `/codex:review --base main` → `docs/validation/phase-N.md`, `PHASES.md` status, design-doc updates (ARCHITECTURE, SECURITY_MODEL, DATA_MODEL, NETWORK_MODEL).
- Operator project for real runs: `~/HermesProjects/hello-api`. Test harnesses may set task states directly in SQL only as a documented stand-in.
- Smoke tests use throwaway provider identities (`HO_PROVIDER_IDENTITY`); never touch `cred-*-default`.
- Code and docs in English; talk to the user in Spanish.
