# Implementation Phases

This roadmap contains all twelve implementation phases defined in section 88 of [MASTER_SPEC.md](MASTER_SPEC.md), with supporting requirements from the rest of the specification. `MASTER_SPEC.md` remains the source of truth if any detail conflicts with this roadmap.

All code, filenames, comments, configuration documentation, and project documentation must be written in English.

**Current status:** Phase 0 discovery is complete and was approved by Gabriel Paredes on 2026-09-30. Phase 1 architecture was approved by Gabriel Paredes on 2026-09-30 (PR #2). Phase 2 (minimal control plane) was approved by Gabriel Paredes on 2026-09-30 (PR #3; evidence in [docs/validation/phase-2.md](docs/validation/phase-2.md)). Phase 3 (Agent Manager) was approved by Gabriel Paredes on 2026-09-30 (PR #4; evidence in [docs/validation/phase-3.md](docs/validation/phase-3.md)). Phase 4 (Claude/Codex workers) is implemented and validated, pending review ([docs/validation/phase-4.md](docs/validation/phase-4.md)). Phases 5–11 are **Not started**. See [DISCOVERY.md](DISCOVERY.md) for findings, evidence, limitations, and the proposed architecture. Checkboxes track completed work, not planned work; production readiness still requires later runtime validation.

## Execution Rules

- Follow the phase order below; validate each phase before building on it.
- Reuse official Hermes functionality and verify official interfaces before integration.
- Stop after Phase 0 and wait for explicit user approval before implementation, as required by section 95.
- Apply the security boundaries and absolute rules in sections 82 and 91 throughout every phase.
- Never merge into `main` or `master` without action-specific human approval.
- Store structured decisions and evidence, never private model chain-of-thought.

## Phase 0: Discovery

**Objective:** Verify current official capabilities before choosing custom components.

- [x] Inspect the latest official Hermes Agent documentation and repository.
- [x] Verify Hermes architecture, Docker deployment, supported releases/images, and ARM64/AMD64 support.
- [x] Inspect Gateway, Dashboard, Kanban/boards, worker lanes, dependencies, concurrency, worktrees, and reviews.
- [x] Verify supported skills, plugins, hooks, CLI extensions, webhooks, MCP, and agent integrations.
- [x] Verify Claude Code and Codex CLI authentication and session persistence using official documentation.
- [x] Verify GitHub CLI authentication and relevant Git/PR behavior.
- [x] Produce a component responsibility matrix: **YES → reuse/integrate**, **PARTIAL → extend**, **NO → implement externally**.
- [x] Identify unsupported interfaces, limitations, and conflicts with the specification.
- [x] Document the proposed architecture and the evidence supporting each major custom component.
- [x] Produce `DISCOVERY.md`.

**Completion criteria:** Discovery includes verified sources, the responsibility matrix, the proposed architecture, and documented limitations. Stop here until explicit user approval is received before implementation.

**Status:** Complete. Approved by Gabriel Paredes on 2026-09-30.

## Phase 1: Architecture

**Objective:** Define the smallest secure, portable architecture that satisfies the specification.

- [x] Produce `ARCHITECTURE.md` with component responsibilities and diagrams.
- [x] Produce `SECURITY_MODEL.md` with trust boundaries, service identities, capabilities, approvals, and credential/secret handling.
- [x] Produce `DATA_MODEL.md` with persistent entities, task transitions, checkpoints, audit records, and schemas.
- [x] Produce `NETWORK_MODEL.md` with control-plane, worker, test, and project network boundaries.
- [x] Define PostgreSQL as the extended control-plane source of truth and Redis as reconstructable transient coordination state.
- [x] Define adapter contracts and Hermes integration boundaries using Discovery findings.
- [x] Define the repository layout, Compose service boundaries, and machine-specific configuration profiles.
- [x] Design project configuration, Task Manifest, and Capability Grant schemas.
- [x] Document any deviations from the suggested repository structure and avoid unnecessary microservices.

**Completion criteria:** Architecture documents and diagrams consistently describe storage, execution, security, networking, and integration responsibilities.

**Status:** Complete. Approved by Gabriel Paredes on 2026-09-30 (PR #2). Deliverables: [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md), [DATA_MODEL.md](DATA_MODEL.md), [NETWORK_MODEL.md](NETWORK_MODEL.md), and [schemas/](schemas/) (validated with `scripts/validate_schemas.py`). Open items OI-01 to OI-08 in ARCHITECTURE §15 are assigned to later phases.

## Phase 2: Minimal Control Plane

**Objective:** Establish persistent task management, project registration, scheduling, and policy enforcement.

- [x] Implement PostgreSQL migrations and Redis integration.
- [x] Implement the Task API and deterministic task state transitions.
- [x] Implement Project Registry operations and explicit project security boundaries.
- [x] Implement read-only onboarding, environment detection, configuration proposals, and approval before `PROJECT_READY`.
- [x] Implement validated project configuration and precedence rules without weakening hard policies.
- [x] Implement the basic Scheduler and task queue.
- [x] Implement the Policy Engine, command risk classification, environment access rules, and autonomy profiles.
- [x] Implement the Approval Service with action-specific records and state/configuration hashes.
- [x] Establish authenticated control APIs, structured logging, health checks, and idempotent operations.
- [x] Add initial Compose configuration and macOS/Linux configuration profiles.

**Completion criteria:** Registered projects and tasks persist across service restarts; scheduling and protected actions respect policy and approval checks.

**Status:** Complete. Approved by Gabriel Paredes on 2026-09-30 (PR #3). 76 unit tests, 23 integration tests, and a 21-check end-to-end smoke test pass on macOS (Apple Silicon); evidence and known limitations are in [docs/validation/phase-2.md](docs/validation/phase-2.md). Linux has been validated only through configuration schemas so far. No workers exist yet, so READY tasks wait in the queue until Phase 3.

## Phase 3: Agent Manager

**Objective:** Safely create and manage isolated ephemeral workers.

- [x] Implement dynamic worker creation, replacement, stopping, and cleanup.
- [x] Make Agent Manager the only component controlling Docker worker lifecycle.
- [x] Enforce temporary, auditable, revocable capabilities scoped to project, task, and worker.
- [x] Implement assigned-workspace mounts and project-specific network isolation.
- [x] Enforce machine-specific CPU/RAM profiles and concurrency limits, including the Mac default of three simultaneous agent workers.
- [x] Evaluate policy, budget, credentials, resources, and capabilities before launching workers.
- [x] Keep workers alive through their logical implement/test/fix/retest/commit cycle. *(Completed in Phase 4: one DEVELOPER agent execution runs the whole cycle and commits locally, confirmed with real Claude Code and Codex runs.)*
- [x] Prevent worker access to Docker sockets, unrelated host paths/projects, control databases, and GitHub credentials.

**Completion criteria:** Workers can be managed through the private authenticated API, and forbidden mounts, networks, and capabilities are denied.

**Status:** Complete. Approved by Gabriel Paredes on 2026-09-30 (PR #4). 114 unit, 37 integration, and 25 real-Docker tests pass, plus a 21-check end-to-end smoke test on the Compose stack (macOS, Apple Silicon). Workers run one command per execution until the Phase 4 adapters add the full implement/test/fix cycle. Evidence, design changes (one egress proxy per execution), and limitations: [docs/validation/phase-3.md](docs/validation/phase-3.md).

## Phase 4: Claude/Codex Workers

**Objective:** Provide interchangeable agent execution with secure authentication and structured results.

- [x] Implement `AgentAdapter`, `ClaudeAdapter`, and `CodexAdapter`.
- [x] Support task execution, resume, cancellation, health checks, result collection, and usage collection.
- [x] Build pinned/versioned agent-base, Claude worker, and Codex worker images for the supported architectures. *(`linux/arm64` built and tested; on `linux/amd64` the Codex image was built and run under emulation, and the Claude Code image still needs a run on an amd64 host.)*
- [x] Implement composable generic, Node, Python, Flutter, PHP, and Java toolchain profiles.
- [x] Implement authentication bootstrap using officially supported subscription/interactive flows. *(Validated with the operator's Claude and ChatGPT accounts.)*
- [x] Implement Credential Broker and separate Secrets Broker interfaces with least-privilege access and redaction.
- [x] Preserve provider sessions securely across ephemeral worker destruction.
- [x] Handle expired sessions through `AUTH_REQUIRED`, notification events, re-authentication, and checkpoint resume.
- [x] Return structured artifacts, results, and operational usage data without uncontrolled worker-to-worker communication.

**Completion criteria:** Both providers execute isolated tasks through the same contract; credentials remain outside Git, PostgreSQL, logs, manifests, and normal backups.

**Status:** Implemented, pending review. 142 unit, 48 integration, and 39 real-Docker tests, plus a 29-check end-to-end smoke test on the Compose stack (macOS, Apple Silicon). With the operator's subscription logins, one real Claude Code 2.1.280 and one real Codex 0.159.2 execution each implemented a change, ran the tests, and committed in an isolated workspace, reaching their providers only through the egress proxy. Evidence, decisions, and limitations: [docs/validation/phase-4.md](docs/validation/phase-4.md).

## Phase 5: Git Isolation

**Objective:** Preserve human work while preparing reviewed, approval-controlled changes.

- [ ] Create dedicated host worktrees and branches for tasks/subtasks.
- [ ] Track each task's base commit and preserve the user's normal checkout.
- [ ] Implement Human Change Protection and divergence/conflict classification.
- [ ] Implement controlled reconciliation, with retesting after rebase/merge and escalation for hard conflicts.
- [ ] Implement Git Service for local repositories and GitHub repositories.
- [ ] Restrict GitHub credentials to Git Service; support persistent `gh auth login` for v1.
- [ ] Support controlled push, remote branches, PR creation/update, and CI status retrieval.
- [ ] Enforce action-specific approval before merging into protected branches, including local-only repositories.
- [ ] Prevent force pushes and deletion of protected branches.

**Completion criteria:** Work stays isolated, human changes are not silently overwritten, and protected merges cannot proceed without valid human approval.

## Phase 6: Testing

**Objective:** Produce trustworthy test evidence and enforce project Quality Gates.

- [ ] Implement Test Runner and Browser/Playwright Runner images and execution.
- [ ] Create isolated ephemeral test services and safely integrate existing project Compose configurations.
- [ ] Enforce test-network restrictions and separate development research access from test access.
- [ ] Implement the Test Gap Policy and risk-adaptive verification.
- [ ] Collect relevant test, integration, browser, build, lint, typecheck, security, and CI evidence as required by project policy.
- [ ] Implement Quality Gate evaluation, including requirements, documentation, conflicts, review evidence, and policy violations.
- [ ] Persist test results and artifacts; clean up ephemeral environments after completion.

**Completion criteria:** Required failures block `READY_FOR_MERGE`; successful checks produce auditable evidence in isolated environments.

## Phase 7: Multi-Agent Orchestration

**Objective:** Coordinate planning, implementation, review, and integration within policy and budget.

- [ ] Implement the persistent Claude Orchestrator and structured requirements, assumptions, and risk assessment.
- [ ] Implement DAG decomposition, task dependencies, and relationships across separate tasks.
- [ ] Implement scored agent routing using operational metrics and provider availability.
- [ ] Implement priority-aware and conflict-aware scheduling, safe preemption, and anti-starvation.
- [ ] Implement mandatory cross-review: Codex reviews Claude's code, and Claude reviews Codex's code.
- [ ] Implement structured review feedback, fixes, retesting, and configurable review-cycle limits.
- [ ] Coordinate integration branches and Quality Gate evaluation.
- [ ] Implement retries, alternate-provider fallback, and bounded dynamic task expansion.
- [ ] Implement runtime, launch, retry, review, provider-usage, and subtask budgets with `PAUSED_BUDGET` handling.
- [ ] Implement task deduplication, live requirement revisions, and the ambiguity/assumption policy.
- [ ] Implement curated Project Memory, knowledge storage, context artifacts, Task Manifests, and decision/audit records.
- [ ] Maintain `READY_FOR_MERGE` separately from `DONE`; require approved merge and successful post-merge verification for completion.

**Completion criteria:** A task can progress from planning through implementation, cross-review, integration, and Quality Gate to `READY_FOR_MERGE`, with all scope, budget, and approval boundaries enforced.

## Phase 8: Recovery

**Objective:** Resume authorized execution safely after interruptions and failures.

- [ ] Implement durable checkpoints and task/worker/Git reconciliation.
- [ ] Implement leader leases and heartbeats so only one orchestrator leads a task.
- [ ] Implement Claude restart/resume and Codex Orchestrator Adapter failover when Claude remains unavailable.
- [ ] Restrict failover and failback to safe checkpoints.
- [ ] Implement reboot recovery from PostgreSQL, worktrees, and checkpoints using fresh ephemeral workers.
- [ ] Reconstruct Redis coordination state from persistent records.
- [ ] Implement self-healing with bounded retries, backoff, and `DEGRADED`/`BLOCKED` reporting.
- [ ] Implement graceful pause, resume, cancellation, and unfinished-work retention.
- [ ] Continue already-authorized work through Hermes outages while stopping actions requiring new approval.
- [ ] Persist pending notifications and reconcile delivery after Hermes returns.

**Completion criteria:** Recovery preserves work and approval boundaries, avoids duplicate leaders/actions, and survives UI disconnection, provider failures, and machine restart.

## Phase 9: Hermes Integration

**Objective:** Make official Hermes the primary interface using verified extension mechanisms.

- [ ] Integrate task creation, status, inspection, pause, resume, cancellation, and retry.
- [ ] Connect Hermes/chat, Dashboard, and CLI to the same Task API, state, and permissions.
- [ ] Integrate project registration, onboarding, and authentication bootstrap where supported.
- [ ] Integrate approvals and `READY_FOR_MERGE` actions.
- [ ] Use official Gateway/channel mechanisms for notifications.
- [ ] Aggregate routine updates and promptly surface approval, authentication, blocking, budget, definitive test failure, recovery failure, merge readiness, and completion events.
- [ ] Answer status queries from persistent task state.
- [ ] Deliver `TASK_COMPLETED` only after approved merge and successful post-merge verification.

**Completion criteria:** A user can manage the task lifecycle through Hermes, including explicit merge approval, with consistent permissions and persistent status.

## Phase 10: Dashboard / UX

**Objective:** Add only the orchestration views missing from official Hermes.

- [ ] Reuse existing Dashboard administration and suitable Kanban/board functionality.
- [ ] Add missing project, task, DAG, Kanban, and worker views.
- [ ] Add missing approval, budget, Quality Gate, review, and test views.
- [ ] Expose Task Manifests, audit timelines, and task controls.
- [ ] Show queue state, blocked work, resource usage, provider usage, retries, failures, test results, and duration.
- [ ] Keep v1 observability lightweight while allowing future OpenTelemetry/Prometheus integration.

**Completion criteria:** Users can inspect progress, evidence, and required actions without duplicating capabilities already supplied by Hermes.

## Phase 11: Hardening

**Objective:** Validate security, failure recovery, portability, and operational readiness.

- [ ] Automate the platform failure and security scenarios in section 89.
- [ ] Test worker/Claude crashes, Codex outages, Redis restarts, PostgreSQL reconnection, Hermes outages, and machine reboot.
- [ ] Test cancellation, pause/resume, budget exhaustion, authentication expiration, and scope expansion.
- [ ] Test Git conflicts, simultaneous human edits, duplicate requests, review failures, and test/browser failures.
- [ ] Prove that high-risk actions and attempts to bypass project, Docker, secret, capability, and production boundaries fail.
- [ ] Test leader exclusivity, approval invalidation, protected merges, and post-merge verification.
- [ ] Validate ARM64/AMD64 images and separate macOS Apple Silicon/Linux setup and resource profiles.
- [ ] Implement and test approval-controlled updates, pinned component versions, health/smoke checks, and rollback.
- [ ] Implement and test daily critical-state backups and restore procedures, excluding credentials and transient containers/Redis state.
- [ ] Implement bounded dependency caches, artifact/log/history retention, and safe worktree/environment cleanup.
- [ ] Validate configuration-drift detection and required approvals for sensitive changes.
- [ ] Complete README, architecture, security, recovery, policy, and operations documentation.
- [ ] Run the section 90 acceptance scenario, including the stop at `READY_FOR_MERGE`, explicit approval, merge, post-merge verification, and `TASK_COMPLETED`.

**Completion criteria:** Required security and failure tests pass, the acceptance scenario succeeds, and the repository provides all final deliverables below.

## Final Delivery Checklist

Verify this checklist against sections 92–94 of `MASTER_SPEC.md` before declaring the platform complete.

- [ ] Docker Compose configuration, Dockerfiles, versioned worker images, and toolchain profiles.
- [ ] Control plane, Claude Orchestrator, Agent Manager, Policy Engine, Approval Service, Scheduler, Project Registry, and Git Service.
- [ ] Credential/Secrets interfaces, PostgreSQL migrations, Redis integration, and recovery logic.
- [ ] `AgentAdapter`, `ClaudeAdapter`, and `CodexAdapter`.
- [ ] Test Runner, Browser Runner, automated tests, and security tests.
- [ ] Hermes integration and CLI.
- [ ] Project configuration, Task Manifest, and Capability Grant schemas.
- [ ] Health checks, backup scripts, update scripts, and rollback scripts.
- [ ] README with separate macOS Apple Silicon and Linux instructions and copy/paste-ready commands.
- [ ] README coverage of prerequisites, installation, directory structure, Hermes setup, provider login, optional GitHub login, project registration/onboarding, first task, monitoring, merge approval, recovery, updates, backups, and troubleshooting.
- [ ] Architecture and operations documentation aligned with the implemented system.
- [ ] Typed interfaces where practical, migrations, explicit errors, structured logs, idempotency, deterministic transitions, appropriate transactions, secure defaults, configuration validation, and unit/integration tests.

## Specification Coverage Matrix

`MASTER_SPEC.md` contains **95 numbered specification sections**, not 95 implementation phases. Section 88 defines **12 phases, numbered 0–11**. This matrix maps every specification section to its delivery work.

**Primary phase** owns delivery of the requirement. **Supporting phases** provide design, integration, or validation work; they do not postpone mandatory security rules. Cross-cutting rules apply whenever relevant, from the first affected phase onward. Phase ranges are inclusive.

This is a planning coverage map, not proof of implementation. Phase 0 research evidence is recorded in [DISCOVERY.md](DISCOVERY.md); implementation requirements remain unimplemented. A completed research phase does not complete every requirement mapped to it. The evidence column summarizes verification targets; the linked source section retains its full requirements.

| Section | Specification requirement | Primary phase | Supporting phases | Expected evidence / completion check |
| --- | --- | --- | --- | --- |
| 1 | [Primary Objective](MASTER_SPEC.md#1-primary-objective) | 7 | 0, 1, 9, 11 | End-to-end authorized task lifecycle and acceptance evidence. |
| 2 | [Hermes-First Principle](MASTER_SPEC.md#2-hermes-first-principle) | 0 | 1, 9, 10 | Verified reuse/extend/build responsibility matrix. |
| 3 | [Versioning and Updates](MASTER_SPEC.md#3-versioning-and-updates) | 11 | 0, 4 | Pinned versions and approved update, smoke-test, and rollback flow. |
| 4 | [High-Level Architecture](MASTER_SPEC.md#4-high-level-architecture) | 1 | 2, 3, 4, 9 | Architecture diagrams and implemented component boundaries. |
| 5 | [Claude Orchestrator](MASTER_SPEC.md#5-claude-orchestrator) | 7 | 4, 8 | Persistent Claude planning and orchestration through Agent Manager. |
| 6 | [Orchestrator Failover and Leader Lease](MASTER_SPEC.md#6-orchestrator-failover-and-leader-lease) | 8 | 1, 4 | Leader lease, safe failover/failback, and no dual leaders. |
| 7 | [Agent Adapter Architecture](MASTER_SPEC.md#7-agent-adapter-architecture) | 4 | 1, 8 | Extensible execution, resume, cancellation, health, result, and usage contracts. |
| 8 | [Scored Agent Router](MASTER_SPEC.md#8-scored-agent-router) | 7 | 4 | Routing based on task needs, availability, budget, and operational metrics. |
| 9 | [Cross Review](MASTER_SPEC.md#9-cross-review) | 7 | 6, 11 | Opposite-provider review, bounded fix cycles, and no self-approval. |
| 10 | [Worker Lifecycle](MASTER_SPEC.md#10-worker-lifecycle) | 3 | 4, 8, 11 | Worker retained through its logical cycle; state survives destruction. |
| 11 | [Agent Manager](MASTER_SPEC.md#11-agent-manager) | 3 | 1, 2, 11 | Exclusive Docker lifecycle authority with authenticated, audited API. |
| 12 | [Capability Grants](MASTER_SPEC.md#12-capability-grants) | 3 | 1, 2, 4, 11 | Scoped, temporary, revocable grants and narrower reviewer permissions. |
| 13 | [Project Security Boundary](MASTER_SPEC.md#13-project-security-boundary) | 3 | 1, 2, 5, 11 | Project-isolated files, networks, caches, secrets, knowledge, and artifacts. |
| 14 | [Project Registry](MASTER_SPEC.md#14-project-registry) | 2 | 9, 10 | Explicit registration; removal does not physically delete repositories. |
| 15 | [Project Onboarding](MASTER_SPEC.md#15-project-onboarding) | 2 | 7, 9 | Read-only scan, configuration proposals, approval, and PROJECT_READY. |
| 16 | [Project Configuration](MASTER_SPEC.md#16-project-configuration) | 2 | 1, 11 | Validated configuration precedence and machine-local separation. |
| 17 | [Configuration Drift](MASTER_SPEC.md#17-configuration-drift) | 11 | 2, 7 | Drift proposals and approval for sensitive configuration changes. |
| 18 | [Toolchain Profiles](MASTER_SPEC.md#18-toolchain-profiles) | 4 | 2, 6 | Versioned composable profiles selected by project configuration. |
| 19 | [Worker Images](MASTER_SPEC.md#19-worker-images) | 4 | 0, 6, 11 | Prebuilt pinned images with tested promotion and rollback. |
| 20 | [Provider Authentication](MASTER_SPEC.md#20-provider-authentication) | 4 | 0, 8, 9, 11 | Verified login, secure persistent sessions, and AUTH_REQUIRED recovery. |
| 21 | [Secrets Broker](MASTER_SPEC.md#21-secrets-broker) | 4 | 1, 2, 3, 11 | Scoped secret delivery, redaction, and production denial by default. |
| 22 | [Environment Access Policy](MASTER_SPEC.md#22-environment-access-policy) | 2 | 3, 4, 11 | Environment-specific access, separate PROD_READ/PROD_WRITE, and audit. |
| 23 | [Policy Engine](MASTER_SPEC.md#23-policy-engine) | 2 | 1, 3, 11 | Hard policies override configurable rules; stricter rule wins. |
| 24 | [Command Policy](MASTER_SPEC.md#24-command-policy) | 2 | 3, 4, 11 | SAFE/CONTROLLED/HIGH RISK classification and protected execution. |
| 25 | [Human Approval](MASTER_SPEC.md#25-human-approval) | 2 | 5, 9, 10, 11 | Action-specific approvals bound to relevant state and invalidated when stale. |
| 26 | [Autonomy Profiles](MASTER_SPEC.md#26-autonomy-profiles) | 2 | 7, 11 | SUPERVISED/BALANCED/AUTONOMOUS profiles preserve hard boundaries. |
| 27 | [Task State Machine](MASTER_SPEC.md#27-task-state-machine) | 2 | 0, 7, 8, 9 | Persistent deterministic transitions mapped to native Hermes states where suitable. |
| 28 | [Completion Semantics](MASTER_SPEC.md#28-completion-semantics) | 7 | 2, 5, 6, 9, 11 | DONE only after approved merge and successful post-merge verification. |
| 29 | [Task Decomposition](MASTER_SPEC.md#29-task-decomposition) | 7 | 2 | DAG decomposition with dependency, resource, budget, risk, and conflict constraints. |
| 30 | [Task Expansion Budget](MASTER_SPEC.md#30-task-expansion-budget) | 7 | 2, 9, 11 | Bounded expansion profiles; significant expansion and UNLIMITED require approval. |
| 31 | [Priority Scheduler](MASTER_SPEC.md#31-priority-scheduler) | 7 | 2, 8 | User-priority authority, safe preemption, and anti-starvation. |
| 32 | [Conflict-Aware Scheduler](MASTER_SPEC.md#32-conflict-aware-scheduler) | 7 | 2, 5 | Change-scope conflict estimation and safe serialization/dependencies. |
| 33 | [Concurrency](MASTER_SPEC.md#33-concurrency) | 3 | 2, 7, 11 | Mac maximum of three agent workers and configurable Linux limits. |
| 34 | [Resource Profiles](MASTER_SPEC.md#34-resource-profiles) | 3 | 1, 2, 11 | LIGHT/NORMAL/HEAVY mapped to machine-specific CPU/RAM limits. |
| 35 | [Host Workspace](MASTER_SPEC.md#35-host-workspace) | 5 | 1, 2, 3 | Source remains on the host rather than disposable Docker volumes. |
| 36 | [Git Worktree Isolation](MASTER_SPEC.md#36-git-worktree-isolation) | 5 | 3 | Dedicated task/subtask worktrees and branches preserve normal checkout. |
| 37 | [Human Change Protection](MASTER_SPEC.md#37-human-change-protection) | 5 | 7, 11 | Base tracking and risk-based response to simultaneous human changes. |
| 38 | [Smart Reconciliation](MASTER_SPEC.md#38-smart-reconciliation) | 5 | 6, 7, 11 | Controlled divergence reconciliation, retesting, and hard-conflict escalation. |
| 39 | [Git Service](MASTER_SPEC.md#39-git-service) | 5 | 2, 7, 11 | Controlled remote/PR/CI/merge operations without worker GitHub credentials. |
| 40 | [GitHub Authentication](MASTER_SPEC.md#40-github-authentication) | 5 | 0, 4 | Persistent GitHub CLI authentication restricted to Git Service. |
| 41 | [Universal Merge Rule](MASTER_SPEC.md#41-universal-merge-rule) | 5 | 2, 7, 9, 11 | Human approval enforced for local and GitHub main/master merges. |
| 42 | [Task Deduplication and Relationship Engine](MASTER_SPEC.md#42-task-deduplication-and-relationship-engine) | 7 | 2, 9, 11 | Relationship classification; no silently discarded duplicate requests. |
| 43 | [Live Task Revision](MASTER_SPEC.md#43-live-task-revision) | 7 | 2, 8 | Versioned requirements and KEEP/REPLAN/CANCEL/NEW impact analysis. |
| 44 | [Ambiguity and Assumption Policy](MASTER_SPEC.md#44-ambiguity-and-assumption-policy) | 7 | 2, 9 | Recorded assumptions with evidence and approval for irreversible ambiguity. |
| 45 | [Project Memory](MASTER_SPEC.md#45-project-memory) | 7 | 2, 11 | Curated relevant memory with categories, trust states, and staleness detection. |
| 46 | [Knowledge Storage](MASTER_SPEC.md#46-knowledge-storage) | 7 | 1, 2 | Operational knowledge in PostgreSQL; confirmed stable knowledge in repository. |
| 47 | [Task Context Artifacts](MASTER_SPEC.md#47-task-context-artifacts) | 7 | 4, 8 | Structured task context persisted outside workers without full chat replay. |
| 48 | [Task Manifest](MASTER_SPEC.md#48-task-manifest) | 7 | 1, 2, 9, 10 | Sanitized reproducibility manifest; replay creates new execution under current policy. |
| 49 | [Decision and Audit Trail](MASTER_SPEC.md#49-decision-and-audit-trail) | 2 | 4, 7, 8, 10, 11 | Structured operational audit evidence without secrets or private reasoning. |
| 50 | [Testing](MASTER_SPEC.md#50-testing) | 6 | 4, 7 | Relevant developer tests and dedicated post-integration suite with persisted results. |
| 51 | [Ephemeral Test Environments](MASTER_SPEC.md#51-ephemeral-test-environments) | 6 | 3, 11 | Private ephemeral test services with policy-controlled cleanup/retention. |
| 52 | [Existing Project Docker Compose](MASTER_SPEC.md#52-existing-project-docker-compose) | 6 | 3 | Separate platform/project Compose, generated overrides, and isolated project names. |
| 53 | [Test Runner Network](MASTER_SPEC.md#53-test-runner-network) | 6 | 1, 2, 3, 11 | Default no-internet testing with explicit restricted exceptions; production blocked. |
| 54 | [Development Worker Network and Research Policy](MASTER_SPEC.md#54-development-worker-network-and-research-policy) | 3 | 1, 2, 4, 7 | Configurable development egress and recorded technical-source provenance. |
| 55 | [Browser Worker](MASTER_SPEC.md#55-browser-worker) | 6 | 3, 4, 11 | Ephemeral Playwright/Chromium with isolated credentials and captured evidence. |
| 56 | [Test Gap Policy](MASTER_SPEC.md#56-test-gap-policy) | 6 | 2, 7 | Test-gap detection, added tests or alternative evidence, and recorded residual risk. |
| 57 | [Risk-Adaptive Verification](MASTER_SPEC.md#57-risk-adaptive-verification) | 6 | 2, 7, 11 | Verification depth matches change risk and sensitive areas. |
| 58 | [Quality Gate](MASTER_SPEC.md#58-quality-gate) | 6 | 2, 5, 7, 11 | Required evidence passes before READY_FOR_MERGE; hard policy remains authoritative. |
| 59 | [Agent-to-Agent Communication](MASTER_SPEC.md#59-agent-to-agent-communication) | 7 | 3, 4 | Worker results/artifacts/events flow through orchestrator, not uncontrolled direct exchange. |
| 60 | [PostgreSQL](MASTER_SPEC.md#60-postgresql) | 2 | 1, 7, 8, 11 | Migrated PostgreSQL holds durable extended orchestration state. |
| 61 | [Redis](MASTER_SPEC.md#61-redis) | 2 | 1, 8, 11 | Redis coordination is reconstructable and never sole critical-state storage. |
| 62 | [Network Topology](MASTER_SPEC.md#62-network-topology) | 3 | 1, 2, 6, 11 | Segmented control/task networks block unauthorized worker access. |
| 63 | [Long-Running Execution](MASTER_SPEC.md#63-long-running-execution) | 8 | 2, 7, 9 | Execution and safe waiting survive browser, terminal, and UI disconnection. |
| 64 | [Hermes Outage](MASTER_SPEC.md#64-hermes-outage) | 8 | 2, 9, 11 | Authorized work continues offline; new approvals wait; notifications reconcile. |
| 65 | [Recovery After Machine Reboot](MASTER_SPEC.md#65-recovery-after-machine-reboot) | 8 | 3, 5, 11 | Ordered boot, persistent-state reconciliation, and fresh worker creation. |
| 66 | [Self-Healing](MASTER_SPEC.md#66-self-healing) | 8 | 2, 3, 9, 11 | Bounded restart/reconcile loop with backoff and degraded/blocked notification. |
| 67 | [Failure Strategy](MASTER_SPEC.md#67-failure-strategy) | 7 | 4, 8, 11 | Configurable retry, original-developer fixes, alternate provider, and BLOCKED paths. |
| 68 | [Graceful Pause](MASTER_SPEC.md#68-graceful-pause) | 8 | 3, 7, 9 | Finish atomic action, checkpoint diff/context, and enter PAUSED. |
| 69 | [Graceful Cancel](MASTER_SPEC.md#69-graceful-cancel) | 8 | 3, 9, 10, 11 | Graceful cancellation with escalation and unfinished-work retention. |
| 70 | [Cost and Usage Control](MASTER_SPEC.md#70-cost-and-usage-control) | 7 | 2, 4, 9, 10, 11 | Usage accounting, budget thresholds, PAUSED_BUDGET, and explicit UNLIMITED approval. |
| 71 | [Dependency Cache](MASTER_SPEC.md#71-dependency-cache) | 11 | 3, 4 | Segmented bounded dependency caches with LRU cleanup, invalidation, and metrics. |
| 72 | [Adaptive Progress Reporting](MASTER_SPEC.md#72-adaptive-progress-reporting) | 9 | 2, 7, 10 | Aggregated routine updates, immediate attention events, persistent-state status answers. |
| 73 | [Observability](MASTER_SPEC.md#73-observability) | 2 | 7, 9, 10, 11 | Structured events and lightweight operational views with future telemetry seams. |
| 74 | [Hermes Notifications](MASTER_SPEC.md#74-hermes-notifications) | 9 | 0 | Official Hermes Gateway/channels; no mandatory alternate notification stack. |
| 75 | [Dashboard Strategy](MASTER_SPEC.md#75-dashboard-strategy) | 10 | 0, 9 | Only missing orchestration views are added; existing Hermes administration reused. |
| 76 | [Unified Task Entry](MASTER_SPEC.md#76-unified-task-entry) | 9 | 2, 10 | Chat, Dashboard, and CLI share Task API, permissions, and state. |
| 77 | [Task Relationships](MASTER_SPEC.md#77-task-relationships) | 7 | 2 | Dependencies supported within DAGs and between separate tasks. |
| 78 | [Dynamic Subtask Creation](MASTER_SPEC.md#78-dynamic-subtask-creation) | 7 | 2, 9, 11 | Dynamic subtasks remain within scope, risk, and budget or request approval. |
| 79 | [Backups](MASTER_SPEC.md#79-backups) | 11 | 2, 8 | Daily critical backups and tested pre-update restore; credentials/transient state excluded. |
| 80 | [Retention](MASTER_SPEC.md#80-retention) | 11 | 3, 5, 6, 8 | Configurable storage limits and retention for workers, worktrees, artifacts, and history. |
| 81 | [Mac and Linux Configuration](MASTER_SPEC.md#81-mac-and-linux-configuration) | 2 | 1, 3, 4, 11 | Separate defaults, Mac M2 Pro, and Linux profiles without hardcoded Linux assumptions. |
| 82 | [Security Requirements](MASTER_SPEC.md#82-security-requirements) | 3 | 1, 2, 4, 5, 6, 11 | Least-privilege workers, authenticated control APIs, and sensitive-action auditing. |
| 83 | [Docker Compose Deliverable](MASTER_SPEC.md#83-docker-compose-deliverable) | 2 | 1, 3, 4, 9, 11 | Clean Compose for justified persistent services; workers created dynamically. |
| 84 | [Expected Repository Structure](MASTER_SPEC.md#84-expected-repository-structure) | 1 | 2, 3, 4, 6, 9, 11 | Repository layout follows specification or documents justified deviations. |
| 85 | [`.hermes/project.yaml`](MASTER_SPEC.md#85-hermesprojectyaml) | 2 | 1, 4, 6, 7 | Complete validated project schema and documented example. |
| 86 | [No Private Reasoning Storage](MASTER_SPEC.md#86-no-private-reasoning-storage) | 1 | 2, 4, 7, 10, 11 | Only concise decisions and evidence are stored; applies throughout all phases. |
| 87 | [Do Not Invent Interfaces](MASTER_SPEC.md#87-do-not-invent-interfaces) | 0 | 1, 4, 5, 9 | Officially verified interfaces; unsupported functionality has documented adapter limits. |
| 88 | [Implementation Methodology](MASTER_SPEC.md#88-implementation-methodology) | 0 | 1–11 | Twelve ordered phases with scoped deliverables and completion criteria. |
| 89 | [Test the Platform Itself](MASTER_SPEC.md#89-test-the-platform-itself) | 11 | 2–10 | Automated fault/security scenarios prove forbidden operations fail. |
| 90 | [Acceptance Scenario](MASTER_SPEC.md#90-acceptance-scenario) | 11 | 5, 6, 7, 9 | OAuth task acceptance flow pauses for approval and completes after verified merge. |
| 91 | [Absolute Rules](MASTER_SPEC.md#91-absolute-rules) | 1 | 0, 2–11 | All twenty absolute rules constrain design, implementation, and validation throughout. |
| 92 | [Final Deliverables](MASTER_SPEC.md#92-final-deliverables) | 11 | 1–10 | Final delivery checklist verified against the working repository. |
| 93 | [README Requirements](MASTER_SPEC.md#93-readme-requirements) | 11 | 0, 2, 4, 5, 8, 9 | Complete separate macOS/Linux setup and operational README with usable commands. |
| 94 | [Implementation Quality](MASTER_SPEC.md#94-implementation-quality) | 2 | 1, 3–11 | Typed contracts, migrations, health, errors, idempotency, validation, and meaningful tests. |
| 95 | [First Action](MASTER_SPEC.md#95-first-action) | 0 | 1 | Discovery, responsibility matrix, architecture proposal, and explicit approval before implementation. |

### Coverage Review

- [ ] Reconcile all 95 rows with implementation artifacts and verification results before final delivery.
- [ ] Check every supporting phase dependency when marking a requirement complete.
- [ ] Keep this matrix synchronized when the source specification or phase assignments change.
- [ ] Apply mandatory policy and security constraints during implementation, not only during Phase 11 validation.
