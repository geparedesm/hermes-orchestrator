# MASTER SPECIFICATION
## Hermes Agent + Claude Code + Codex Multi-Agent Docker Orchestration Platform

> This file is the source of truth for the project.
> Implementation agents must read this specification before making architectural or implementation decisions.

> This specification contains 95 numbered sections. Its implementation methodology defines 12 phases (0–11) in section 88. See [PHASES.md](PHASES.md) for the roadmap and the [complete section-to-phase coverage matrix](PHASES.md#specification-coverage-matrix).

## Role

Act as a Principal Software Architect, DevOps Engineer, Security Engineer, and Senior AI Agent Systems Engineer.

Design and implement a production-grade, local-first multi-agent software development platform built around the **official Hermes Agent by Nous Research**.

The system must run on:
- Apple Silicon macOS, specifically MacBook Pro M2 Pro.
- Linux servers using Docker.

The architecture must remain portable between both environments.

---

# 1. Primary Objective

Build a Docker Compose based platform where:

- Official Hermes Agent is the primary user interface, communication gateway, notification layer, skills/plugins layer, and existing Hermes functionality provider.
- A persistent Claude Code based Orchestrator acts as the main Tech Lead / Planner / Orchestrator.
- Claude Code and Codex CLI are available as isolated development workers.
- Development workers are dynamically created as ephemeral Docker containers.
- A dedicated Agent Manager is the ONLY component allowed to control Docker worker lifecycle.
- Agents collaborate through structured tasks and artifacts, never uncontrolled direct worker-to-worker communication.
- Work is automatically planned, implemented, tested, reviewed, integrated, and prepared for merge.
- NOTHING is merged into `main` or `master` without explicit human approval.

Normal lifecycle:

```text
User
→ Official Hermes Agent
→ Claude Orchestrator
→ Task Analysis
→ Requirements
→ DAG
→ Scheduler
→ Agent Manager
→ Workers
→ Implementation
→ Tests
→ Cross Review
→ Integration
→ Quality Gate
→ READY_FOR_MERGE
→ Human Approval
→ Merge
→ Post-Merge Verification
→ DONE
```

---

# 2. Hermes-First Principle

DO NOT rebuild functionality already correctly provided by official Hermes Agent.

Before implementation, inspect current official Hermes documentation and implementation for:

- Docker deployment
- Gateway
- Dashboard
- Kanban / boards / tasks
- Skills
- Plugins
- Hooks
- CLI extension mechanisms
- Webhooks
- MCP
- Claude Code / Codex integrations
- Worktrees
- Reviews
- Dependencies
- Concurrency
- Worker lanes

Reuse official Hermes functionality whenever appropriate.

Do NOT fork Hermes unless unavoidable.

Prefer:

```text
official Hermes
+ configuration
+ official extension mechanisms
+ external control-plane services
```

For every major proposed custom component determine:

```text
Does Hermes already provide this?

YES      → reuse/integrate
PARTIAL  → extend
NO       → implement externally
```

Document the decision.

---

# 3. Versioning and Updates

Use a tested pinned Hermes release or immutable image digest.

Never depend on uncontrolled `latest` tags in production configuration.

Before pinning, verify the currently recommended official Hermes Docker image and stable release.

Support:

- linux/arm64
- linux/amd64

MacBook M2 Pro is ARM64.

Controlled update flow:

```text
detect update
→ notify user
→ require approval
→ backup/checkpoint
→ build/pull candidate
→ health check
→ smoke tests
→ promote
→ rollback on failure
```

Apply equivalent controlled updates to Claude Code and Codex CLI.

---

# 4. High-Level Architecture

Prefer:

```text
Official Hermes Agent
├── Gateway
├── Dashboard
├── Channels
├── Skills
├── Plugins
├── Hooks
├── Kanban / Boards
└── Hermes state
        │
        ▼
Hermes Orchestration Integration
        │
        ▼
Extended Control Plane
├── Claude Orchestrator
├── Agent Manager
├── Policy Engine
├── Approval Service
├── Scheduler
├── Git Service
├── Project Registry
├── Credential Broker
├── Secrets Broker
├── Recovery Controller
├── Knowledge Service
├── Artifact / Event Layer
├── PostgreSQL
└── Redis
        │
        ▼
Dynamic Workers
├── Claude Code Workers
├── Codex Workers
├── Test Runners
├── Browser / Playwright Runners
└── Ephemeral Project Services
```

Logical components do not automatically require separate microservices.

Combine tightly related components where this reduces complexity without weakening security or isolation.

---

# 5. Claude Orchestrator

Claude Code is the preferred persistent orchestration authority.

Responsibilities:

- understand user requests
- inspect project context
- produce requirements
- identify ambiguity
- plan implementation
- generate DAGs
- decompose work
- assign subtasks
- request workers
- monitor progress
- analyze failures
- coordinate retries
- request tests
- coordinate cross-review
- integrate branches
- assess risk
- maintain relevant project knowledge
- prepare READY_FOR_MERGE

Claude MUST NOT receive direct Docker daemon access.

Claude requests workers through Agent Manager.

Claude may act as Developer when appropriate.

If Claude writes code, Codex MUST review it.

---

# 6. Orchestrator Failover and Leader Lease

Claude is the preferred leader.

Use persistent leader lease / heartbeat semantics.

Exactly one logical orchestrator may lead a task.

Failure:

```text
Claude heartbeat lost
→ lease expires
→ Recovery Controller reconciles
→ restart Claude
→ resume checkpoint
```

If Claude remains unavailable due to auth, quota, outage or persistent failure:

```text
Codex Orchestrator Adapter
→ acquire leadership
→ load persistent state
→ continue task
```

Failover/failback only at safe checkpoints.

Never allow dual leaders.

---

# 7. Agent Adapter Architecture

Create an extensible `AgentAdapter` abstraction with conceptual operations such as:

```text
execute_task()
resume_task()
cancel_task()
health_check()
collect_result()
collect_usage()
```

Implement:

- ClaudeAdapter
- CodexAdapter

Allow future adapters without redesigning the orchestration core.

Preserve provider-specific capabilities.

---

# 8. Scored Agent Router

Routing considers:

- task type
- complexity
- context
- availability
- limits/quota
- budget
- historical operational metrics
- test pass rate
- retries
- duration
- specialization

Default preference:

```text
architecture / planning / complex debugging / research → Claude
backend / frontend / refactoring / tests / routine implementation → Codex
```

Claude may override within Policy Engine constraints.

Do not invent personality-based scoring.

---

# 9. Cross Review

Mandatory:

```text
Codex writes → Claude reviews
Claude writes → Codex reviews
```

An agent cannot approve its own work.

Review failure:

```text
structured feedback
→ original developer
→ fix
→ tests
→ cross-review
```

Default configurable review-cycle limit: 2.

After the configured limit use alternate agent or BLOCKED.

---

# 10. Worker Lifecycle

Workers are normally ephemeral.

One worker should survive its entire logical cycle:

```text
implement → test → fix → retest → commit
```

Destroy after:

- successful completion
- cancellation
- unrecoverable failure
- deliberate replacement

Persistent task state must survive worker destruction.

---

# 11. Agent Manager

Agent Manager is the ONLY component allowed to control Docker worker lifecycle.

Claude may request:

- create worker
- replace worker
- stop worker
- create test environment

Agent Manager evaluates:

- Policy Engine
- Budget
- Resource limits
- Capability Grants
- Credentials
- Concurrency
- Project Security Boundary

Workers MUST NOT receive `/var/run/docker.sock`.

Agent Manager may have tightly controlled Docker access.

Its API must be private, authenticated and auditable.

---

# 12. Capability Grants

Implement temporary least-privilege grants bound to task and worker.

Example:

```yaml
worker: codex-284-02
task: T-284
project: project-A

capabilities:
  workspace: WRITE
  git: LOCAL_COMMIT
  network: DOCS
  secrets: TEST_DB
  docker: NONE
  production: NONE
```

Claude may REQUEST capabilities.

Claude cannot self-grant.

Policy Engine + Agent Manager decide.

Reviewer permissions should normally be narrower:

```text
repository READ
diff READ
artifacts READ
tests EXECUTE
code WRITE denied
```

unless explicitly assigned fixes.

Grants are temporary, auditable and revocable.

---

# 13. Project Security Boundary

Every registered project is an independent security domain.

Separate by project:

- workspaces
- worktrees
- networks
- caches
- secrets
- knowledge
- artifacts

A worker assigned to Project A MUST NOT automatically access Project B.

Deny unrelated host locations such as:

- ~/Documents
- ~/Downloads
- ~/Desktop
- ~/.ssh
- unrelated repositories

Claude Orchestrator gets READ only to explicitly registered projects.

When Claude acts as Developer, WRITE only to assigned task worktree.

---

# 14. Project Registry

Require explicit project registration.

Conceptual command:

```bash
hermes project add ~/HermesProjects/my-app
```

Also support equivalent Dashboard/Hermes actions.

Store:

- project_id
- name
- host_path
- git_remote
- default_branch
- status
- config_path
- knowledge_path
- registered_at

Registration does not imply access to the rest of the host.

Removing a project from registry never physically deletes its repository.

Physical deletion is separately protected.

---

# 15. Project Onboarding

Flow:

```text
register
→ read-only scan
→ detect environment
→ Claude proposal
→ user approval
→ PROJECT_READY
```

Inspect:

- languages
- frameworks
- build commands
- test commands
- lint/typecheck
- repository structure
- Dockerfiles
- Compose
- CI
- dependencies
- README
- CLAUDE.md
- AGENTS.md
- ADRs
- architecture documentation
- risks

Generate proposals for:

```text
.hermes/project.yaml
.hermes/PROJECT.md
.hermes/architecture.md
```

Do NOT modify application code during initial onboarding.

---

# 16. Project Configuration

Use:

```text
.hermes/project.yaml
```

for version-controlled project configuration.

Private machine-specific configuration may use:

```text
.hermes.local.yaml
.env
secure credential stores
```

Precedence:

```text
system defaults
→ .hermes/project.yaml
→ machine-local config
→ explicit task override
→ Policy Engine
```

Nothing can weaken mandatory Policy Engine rules.

---

# 17. Configuration Drift

Detect when repository changes make project configuration stale.

Examples:

- framework changed
- test command changed
- Compose changed
- CI changed
- dependencies changed
- architecture changed

Generate proposed changes.

Trivial non-sensitive changes may auto-update only if project policy explicitly authorizes it.

Changes involving security, network, secrets, production, toolchains, Quality Gates or infrastructure require approval.

---

# 18. Toolchain Profiles

Do NOT make one giant worker image.

Create composable/versioned profiles such as:

- generic
- node
- python
- flutter
- php
- java

Support combinations such as:

- Codex + Node
- Claude + Python
- Test Runner + Flutter

`.hermes/project.yaml` is authoritative.

Autodetection may propose profiles.

---

# 19. Worker Images

Use prebuilt versioned images:

- agent-base
- claude-worker
- codex-worker
- test-runner
- browser-runner

New CLI version:

```text
detect
→ notify
→ approval
→ build candidate image
→ smoke test
→ promote
```

Rollback if unhealthy.

Mac and Linux may pin independent versions.

---

# 20. Provider Authentication

Claude Code and Codex CLI should primarily use officially supported subscription/interactive authentication flows rather than requiring API keys.

Create secure Credential Broker.

Conceptual bootstrap:

```bash
hermes auth login claude
hermes auth login codex
```

Do NOT guess undocumented credential paths.

Verify officially supported Claude Code and Codex CLI authentication/session persistence mechanisms.

Provider sessions must:

- survive ephemeral workers
- remain separated
- never be committed
- never be stored in PostgreSQL
- never appear in logs
- never appear in Task Manifests
- never be included in normal backups

On macOS prefer Keychain-compatible secure storage when technically appropriate.

On Linux provide secure configurable backend.

Expired session:

```text
AUTH_REQUIRED
→ Hermes notification
→ re-authentication
→ resume checkpoint
```

---

# 21. Secrets Broker

Project secrets use a separate Secrets Broker.

Scope secrets by:

- project
- task
- environment
- capability

Use least privilege.

Redact secrets from logs, prompts where unnecessary, Task Manifest, PostgreSQL, artifacts, Git and error reports.

Production secrets denied by default.

---

# 22. Environment Access Policy

LOCAL/TEST:
automatic access within policy.

STAGING:
read-only by default; writing requires approval.

PRODUCTION:
deny by default; explicit approval; least privilege; time-limited and audited.

Distinguish:

- PROD_READ
- PROD_WRITE

Destructive production operations remain hard-policy protected.

---

# 23. Policy Engine

Two layers:

## Immutable hard security policies

Examples:

- Docker isolation
- secret boundaries
- protected branches
- mandatory approvals
- destructive-operation protection
- production protections
- capability boundaries

## Configurable project policies

Stored in `.hermes/project.yaml`.

Examples:

- allowed commands
- allowed domains
- tests
- autonomy
- budgets
- toolchains
- network rules

When policies conflict, the more restrictive rule wins.

Project config cannot weaken hard policies.

---

# 24. Command Policy

Classify commands.

SAFE:
routine read/test/lint/status commands, normally automatic.

CONTROLLED:
package installs, downloads, environment changes, etc., evaluated against project policy.

HIGH RISK:
destructive DB operations, mass deletion, force push, repo deletion, production deployment, production infrastructure changes, secrets, permissions/security changes.

HIGH RISK operations require Policy Engine approval flow.

---

# 25. Human Approval

Implement centralized Approval Service accessible through Hermes, Dashboard and CLI where appropriate.

Approval is action-specific, never blanket authorization.

Record:

- task
- action
- risk
- requested_at
- approved_by
- approved_at
- relevant state/config hash
- audit information

Meaningful state changes may invalidate prior approval.

Approval events include:

- APPROVAL_REQUIRED
- READY_FOR_MERGE
- PAUSED_BUDGET
- HIGH_RISK_OPERATION
- UPDATE_AVAILABLE

---

# 26. Autonomy Profiles

Support:

- SUPERVISED
- BALANCED
- AUTONOMOUS

Default: BALANCED.

BALANCED permits routine coding/testing/fixing/review/integration automatically but pauses for meaningful risk, scope or budget expansion.

No profile can remove hard approval boundaries such as:

- production deployment
- destructive DB operations
- production secrets
- security/permissions changes
- destructive Git
- major architecture migration
- READY_FOR_MERGE

---

# 27. Task State Machine

Support at least:

```text
BACKLOG
READY
PLANNING
QUEUED
RUNNING
TESTING
REVIEW
FIX_REQUIRED
QUALITY_GATE
APPROVAL_REQUIRED
PAUSED
PAUSED_BUDGET
BLOCKED
FAILED
READY_FOR_MERGE
MERGING
DONE
CANCELLED
```

Adapt to Hermes-native states when appropriate instead of unnecessarily duplicating them.

---

# 28. Completion Semantics

READY_FOR_MERGE is not DONE.

Required flow:

```text
RUNNING
→ TESTING
→ REVIEW
→ QUALITY_GATE
→ READY_FOR_MERGE
→ human approval
→ MERGING
→ post-merge verification
→ DONE
```

If rejected:

```text
READY_FOR_MERGE
→ FIX_REQUIRED
→ implementation
→ tests
→ review
→ Quality Gate
```

TASK_COMPLETED means completion after approved merge and post-merge verification.

It does not imply production deployment.

---

# 29. Task Decomposition

Claude automatically decomposes sufficiently complex work into a DAG.

Independent subtasks may run concurrently.

Respect:

- dependencies
- resources
- budgets
- risk
- file/change conflicts

---

# 30. Task Expansion Budget

Claude may dynamically create additional subtasks only within:

- original scope
- risk policy
- agent-launch budget
- runtime budget
- concurrency
- subtask limit
- provider/token budget

Support profiles:

- SMALL
- NORMAL
- LARGE
- UNLIMITED

UNLIMITED requires explicit user authorization.

Significant expansion triggers APPROVAL_REQUIRED.

---

# 31. Priority Scheduler

Support:

- CRITICAL
- HIGH
- NORMAL
- LOW

Explicit user priority has highest authority.

Implement:

- Priority-Aware Scheduler
- Safe Preemption
- Anti-Starvation

CRITICAL may preempt lower-priority work only at safe checkpoints.

Never kill mid-atomic operation merely to free capacity.

Prevent low-priority starvation.

---

# 32. Conflict-Aware Scheduler

Allow multiple tasks on the same project when estimated change scopes are compatible.

Estimate affected:

- files
- modules
- APIs
- DB areas
- infrastructure
- dependencies

Potential conflicts may serialize tasks, add dependencies or safely checkpoint/pause work.

Priority influences continuation.

---

# 33. Concurrency

Mac M2 Pro:

```text
max simultaneous agent workers = 3
```

Suggested starting mix:

```text
1 Claude + 2 Codex
```

Routing remains dynamic.

Linux concurrency/resource values are configurable according to hardware.

---

# 34. Resource Profiles

Claude requests semantic profiles:

- LIGHT
- NORMAL
- HEAVY

Agent Manager maps them to machine-specific CPU/RAM limits.

Example Mac defaults:

```text
LIGHT  = 1 CPU / 2 GB
NORMAL = 2 CPU / 4 GB
HEAVY  = 4 CPU / 8 GB
```

These are configurable defaults.

Claude cannot arbitrarily allocate host resources.

---

# 35. Host Workspace

Project source code lives on host filesystem.

Suggested root:

```text
~/HermesProjects/
```

Do not bury user source code inside disposable Docker volumes.

---

# 36. Git Worktree Isolation

User's normal checkout remains available for manual development.

Each task/subtask receives a dedicated host worktree and branch.

Example:

```text
~/HermesProjects/my-app/.hermes/worktrees/task-284-01
```

Agents never directly write into the user's main checkout.

---

# 37. Human Change Protection

Record base commit for every task.

If user changes project while agents work:

```text
LOW      different files                 → continue
MEDIUM   same file, different areas      → continue + reconciliation
HIGH     same lines/function/API         → checkpoint + rebase/replan
CRITICAL contradictory architecture     → APPROVAL_REQUIRED
```

Human changes must never be silently overwritten.

---

# 38. Smart Reconciliation

At integration:

```text
detect main divergence
→ controlled rebase/merge
→ agent-assisted simple conflict resolution
→ rerun tests
```

Hard conflicts become BLOCKED or APPROVAL_REQUIRED.

---

# 39. Git Service

Support local Git repositories and GitHub repositories.

Workers never receive GitHub credentials.

Controlled Git Service performs:

- push
- remote branch operations
- PR creation/update
- CI status retrieval
- approved merge

Prohibit:

- force push to protected branch
- protected branch deletion
- automatic merge to main/master

---

# 40. GitHub Authentication

For v1 support persistent:

```bash
gh auth login
```

Credential available only to Git Service.

Design for future GitHub App support.

---

# 41. Universal Merge Rule

NON-NEGOTIABLE:

```text
NO CODE ENTERS MAIN OR MASTER WITHOUT HUMAN APPROVAL.
```

Applies to GitHub and local-only repositories.

System may prepare everything automatically but final merge requires explicit approval.

---

# 42. Task Deduplication and Relationship Engine

Classify:

- DUPLICATE
- RELATED
- DEPENDENCY
- CONFLICTING
- INDEPENDENT

Compare project, scope, requirements, active tasks, dependencies and base commit.

Never silently discard a user request.

Related tasks may reuse relevant discoveries/artifacts, not permissions/workers.

Support DAG relationships across tasks.

---

# 43. Live Task Revision

Requirements are versioned.

When user changes requirements:

```text
checkpoint
→ impact analysis
→ classify subtasks
```

Classifications:

- KEEP
- REPLAN
- CANCEL
- NEW

Small reversible changes may replan automatically.

Major architecture/scope/budget changes require approval.

Preserve requirement history in Task Manifest.

---

# 44. Ambiguity and Assumption Policy

LOW reversible ambiguity:
Claude may assume and document.

MEDIUM:
reasonable assumption + record.

HIGH/irreversible:
APPROVAL_REQUIRED.

Store:

- assumption
- reason
- evidence
- impact
- reversibility

Corrected assumptions may update Project Memory.

---

# 45. Project Memory

Use curated hierarchical memory.

Categories:

- DISCOVERY
- CONVENTION
- DECISION
- KNOWN_ISSUE
- ARCHITECTURE
- LESSON_LEARNED

Trust states:

- CONFIRMED
- OBSERVED
- HYPOTHESIS
- STALE
- REJECTED

Retrieve only relevant memory for each task.

Do not dump entire project history into every agent prompt.

Detect staleness as code evolves.

---

# 46. Knowledge Storage

Hybrid storage.

PostgreSQL for operational knowledge, discoveries, hypotheses, provenance, confidence/status and task relationships.

Repository for stable confirmed knowledge:

```text
.hermes/
├── PROJECT.md
├── architecture.md
├── conventions.md
└── decisions/
    ├── ADR-001.md
    └── ...
```

Agents may propose knowledge.

Do not automatically convert agent hypotheses into permanent rules.

---

# 47. Task Context Artifacts

Persist structured context outside workers.

Examples:

```text
original_request.md
requirements.md
plan.md
architecture_decisions.md
subtasks.json
changed_files.json
test_results.json
developer_summary.md
review_feedback.md
result.json
```

Do not depend on full conversation replay.

---

# 48. Task Manifest

Create sanitized reproducibility manifest containing:

- task id
- original request
- requirements versions
- DAG/subtasks
- agent assignments
- agent versions
- worker image versions
- toolchain versions
- project config hash
- base Git commit
- generated commits
- commands
- tests
- reviews
- approvals
- important technical sources
- retries/fallbacks
- budget/usage
- final result

Never include secrets.

Potential CLI:

```bash
hermes task inspect <id>
hermes task replay <id>
```

Replay creates a NEW execution using reproducible context but current policies.

---

# 49. Decision and Audit Trail

Do NOT store private chain-of-thought.

Store structured operational records:

- decision
- short reason summary
- evidence
- alternatives/result
- commands
- changed files
- tests
- reviews
- approvals
- retries
- failovers
- policy decisions
- relevant technical sources

Never store secrets, tokens, passwords or private model reasoning.

---

# 50. Testing

Developer runs tests relevant to its changes.

After integration:

```text
dedicated Test Runner
→ full configured suite
```

Claude Orchestrator should not execute heavyweight full suites itself.

Persist `test_results.json`.

---

# 51. Ephemeral Test Environments

Agent Manager may create isolated services required by tests, such as PostgreSQL, Redis, application containers and mock services.

Each task receives private network.

Destroy after testing unless retention policy explicitly requires otherwise.

---

# 52. Existing Project Docker Compose

Keep Hermes/control-plane Compose separate from project Compose.

Do not permanently rewrite a project's Compose solely for testing.

Use isolated Docker project names and temporary generated overrides under something like:

```text
.hermes/generated/
```

Clean temporary services afterward.

---

# 53. Test Runner Network

Default:

```text
NO INTERNET
```

External access requires explicit project/test configuration and should be restricted/allowlisted.

Production services blocked by default.

---

# 54. Development Worker Network and Research Policy

Development workers may have normal outbound Internet by default, with configurable restricted mode per project/task.

Research Policy may allow official docs, package registries, public GitHub and technical references.

Sensitive/unknown destinations require stricter Policy Engine handling.

Record provenance/version for important external technical decisions.

---

# 55. Browser Worker

Provide isolated ephemeral Playwright + Chromium worker.

Use for:

- UI/E2E validation
- screenshots
- traces
- console logs
- network logs

Never inherit personal browser cookies, sessions or passwords.

Testing credentials come through Secrets Broker.

---

# 56. Test Gap Policy

If project lacks sufficient tests:

1. detect the gap
2. create tests when reasonable
3. otherwise produce alternative verification

Alternatives may include:

- build
- lint
- typecheck
- smoke tests
- API validation
- browser validation
- static analysis

Quality Gate must record unavailable tests and resulting risk.

Sensitive changes with inadequate tests may require approval.

---

# 57. Risk-Adaptive Verification

Classify change risk.

Suggested policy:

```text
LOW
→ tests + cross-review

MEDIUM
→ tests + review + lint/typecheck/static analysis

HIGH
→ above + security/dependency verification

CRITICAL
→ enhanced verification + explicit approval
```

Sensitive areas include:

- authentication
- authorization
- payments
- DB migrations
- Docker
- infrastructure
- CI/CD
- secrets
- permissions
- dependencies
- network configuration

Verification tooling configurable per Toolchain Profile and `.hermes/project.yaml`.

---

# 58. Quality Gate

READY_FOR_MERGE only when required project conditions pass.

Possible conditions:

- relevant tests
- full integration tests
- cross-review
- build
- lint
- typecheck
- security checks
- browser tests
- CI checks
- no unresolved HIGH/CRITICAL findings
- no unresolved conflicts
- no Policy Engine violations
- required docs updated
- requirements satisfied

Projects may customize requirements.

Policy Engine remains authoritative.

---

# 59. Agent-to-Agent Communication

No uncontrolled worker-to-worker communication.

Flow:

```text
Worker
→ structured result/artifact/event
→ Claude Orchestrator
→ DAG/context update
→ another worker
```

This preserves auditability and isolation.

---

# 60. PostgreSQL

Use dedicated PostgreSQL for extended orchestration state, separate from official Hermes storage unless official integration requires otherwise.

Store:

- tasks
- DAGs
- subtasks
- assignments
- checkpoints
- worker records
- retries
- approvals
- reviews
- test results
- PR state
- resource usage
- knowledge metadata
- manifests
- decisions

PostgreSQL is source of truth for extended control plane.

---

# 61. Redis

Use Redis for:

- queue
- locks
- heartbeats
- worker availability
- transient events
- scheduler coordination

Redis must not be the only source of critical task state.

Redis state must be reconstructable from PostgreSQL.

---

# 62. Network Topology

Use segmented trust networks.

Persistent control network contains only required trusted components.

Each task may receive private ephemeral network.

Workers must not directly access:

- control PostgreSQL
- control Redis
- Docker daemon
- GitHub credentials
- unrelated workers
- unrelated projects

---

# 63. Long-Running Execution

Execution survives:

- browser closing
- Hermes UI disconnect
- user terminal closing

Persistent task state drives execution.

Safe waiting states include:

- APPROVAL_REQUIRED
- PAUSED_BUDGET
- BLOCKED
- READY_FOR_MERGE
- AUTH_REQUIRED

---

# 64. Hermes Outage

Hermes is primary interface/channel, not sole execution engine.

If Hermes is unavailable, already-authorized work may continue.

Operations requiring new approval must stop.

Persist events and pending notifications.

When Hermes returns, reconcile and deliver pending notifications.

---

# 65. Recovery After Machine Reboot

Boot flow:

```text
Compose starts
→ PostgreSQL healthy
→ Redis healthy
→ control plane
→ Recovery Controller loads state
→ inspect Git/worktrees/checkpoints
→ reconstruct logical execution
→ create fresh workers where necessary
→ continue safely
```

Do not try to resurrect dead ephemeral containers.

Recreate workers from persistent state.

Recheck unverified operations.

---

# 66. Self-Healing

Layered recovery:

```text
health failure
→ restart service
→ health check
→ reconstruct state
→ reconcile workers/tasks
→ continue from checkpoint
```

Use backoff and retry limits.

If recovery fails, mark DEGRADED/BLOCKED and notify via Hermes.

---

# 67. Failure Strategy

Suggested:

```text
transient error → retry same agent
quota/unavailable → alternate provider
code/test issue → original developer fix attempt
repeated failure → alternate developer
both providers fail → BLOCKED + Hermes notification
```

Counts configurable.

---

# 68. Graceful Pause

Pause:

```text
stop new actions
→ allow atomic operation to finish
→ checkpoint
→ preserve Git diff/context
→ PAUSED
```

---

# 69. Graceful Cancel

Cancel:

```text
request checkpoint
→ grace period
→ stop
```

Escalate if necessary:

```text
SIGTERM
→ timeout
→ SIGKILL
```

Preserve unfinished worktree temporarily according to retention.

Controls:

- Pause
- Resume
- Cancel
- Retry
- Details

---

# 70. Cost and Usage Control

Support budgets for:

- runtime
- agent launches
- retries
- review cycles
- provider usage
- subtask expansion

Profiles:

- SMALL
- NORMAL
- LARGE
- UNLIMITED

UNLIMITED only with explicit user authorization.

Suggested configurable behavior:

```text
70% → warning
85% → optimize/attempt efficient completion
100% → PAUSED_BUDGET
```

User may Continue, Increase Budget or Cancel.

---

# 71. Dependency Cache

Controlled segmented dependency caching for ecosystems such as:

- npm
- pip
- Gradle
- Cargo
- Pub

Segment by machine/ecosystem/project.

Reuse downloads but do not blindly share:

- node_modules
- virtualenvs
- uncontrolled build artifacts

Include size limits, LRU cleanup, invalidation and metrics.

---

# 72. Adaptive Progress Reporting

Maintain detailed internal events.

Dashboard may show realtime progress.

Hermes should aggregate routine updates and immediately surface attention events:

- APPROVAL_REQUIRED
- AUTH_REQUIRED
- BLOCKED
- PAUSED_BUDGET
- definitive TEST_FAILED
- READY_FOR_MERGE
- recovery failure
- TASK_COMPLETED

Natural-language status queries must use persistent task state rather than reconstructing from chat.

---

# 73. Observability

Use lightweight native observability in v1.

Structured events may include:

- TASK_STARTED
- AGENT_ASSIGNED
- WORKER_CREATED
- FILE_CHANGED
- COMMAND_STARTED
- COMMAND_FINISHED
- TEST_STARTED
- TEST_PASSED
- TEST_FAILED
- REVIEW_STARTED
- REVIEW_FAILED
- COMMIT_CREATED
- PR_CREATED
- APPROVAL_REQUIRED
- BLOCKED
- READY_FOR_MERGE
- TASK_COMPLETED

Dashboard should expose tasks, workers, queue, blocked work, CPU/RAM, provider usage, retries, failures, test results and duration.

Do not require Prometheus/Grafana in v1.

Prepare architecture for future OpenTelemetry/Prometheus integration.

---

# 74. Hermes Notifications

Use official Hermes Gateway/channel mechanisms whenever possible.

Do not introduce Evolution API, n8n or another notification stack as mandatory v1 infrastructure.

Future adapters are acceptable.

---

# 75. Dashboard Strategy

Reuse official Hermes Dashboard wherever possible.

Do not rebuild Hermes configuration, credentials, sessions/logs, skills, cron or generic administration already provided.

Add only missing orchestration views where necessary:

- Projects
- Tasks
- DAG
- Kanban
- Workers
- Approvals
- Budgets
- Quality Gates
- Reviews
- Tests
- Task Manifest
- Audit Timeline

If Hermes already provides suitable board/Kanban functionality, integrate rather than duplicate.

---

# 76. Unified Task Entry

Hermes/chat, Dashboard and CLI use the same Task API/state/permissions.

Conceptual CLI:

```bash
hermes task create
hermes task status
hermes task inspect
hermes task pause
hermes task resume
hermes task cancel
```

Natural-language task creation should identify project.

If ambiguous, apply Ambiguity Policy.

---

# 77. Task Relationships

Support dependencies:

- within task DAGs
- between separate tasks

This allows larger project plans without one giant execution.

---

# 78. Dynamic Subtask Creation

Claude may create new subtasks automatically only when within original scope, risk policy and budget.

Significant scope/architecture/budget expansion requires APPROVAL_REQUIRED.

---

# 79. Backups

Layered automatic backups.

Critical daily backup:

- PostgreSQL
- Hermes state where appropriate
- configuration

Projects rely primarily on Git/GitHub plus optional backup.

Credentials are NOT included in normal backups.

Do not back up:

- Redis transient state
- workers
- test containers

Before important update:

```text
snapshot
→ update
→ health check
→ smoke test
→ rollback/restore on failure
```

---

# 80. Retention

Destroy workers and ephemeral test environments immediately after completion.

Remove worktrees after successful merge.

Logs/artifacts use configurable retention, e.g. 30 days.

FAILED/BLOCKED tasks may retain worktrees/logs/artifacts/context longer.

Git commits normally remain.

PostgreSQL task history follows configured policy.

Support storage limits and cleanup per machine/project.

---

# 81. Mac and Linux Configuration

Provide machine-specific profiles, e.g.:

```text
config/
├── defaults.yaml
├── mac-m2-pro.yaml
└── linux.yaml
```

Do not hardcode Mac resource assumptions into Linux.

---

# 82. Security Requirements

Workers:

- non-root where practical
- resource limited
- assigned workspace only
- required credentials only
- required network access only
- no Docker socket
- no unrelated projects
- no direct GitHub credentials
- cannot bypass Policy Engine

Control APIs use authentication/service identity.

Audit sensitive actions.

---

# 83. Docker Compose Deliverable

Produce clean Compose architecture.

Persistent services may include, where justified:

- hermes-agent
- claude-orchestrator
- control-plane / orchestration-api
- agent-manager
- postgres
- redis

Policy Engine, Approval Service, Scheduler, Project Registry, Git Service, Credential Broker and similar logical components may live inside a well-designed control-plane service rather than each becoming its own container.

Do not create microservices merely for aesthetics.

Dynamic workers should not all be permanently running Compose services if Agent Manager can create them safely from versioned images.

---

# 84. Expected Repository Structure

Use a clean structure similar to:

```text
hermes-orchestrator/
├── compose.yaml
├── compose.override.example.yaml
├── .env.example
├── README.md
├── Makefile
├── MASTER_SPEC.md
├── config/
│   ├── defaults.yaml
│   ├── mac-m2-pro.yaml
│   └── linux.yaml
├── services/
│   ├── control-plane/
│   ├── claude-orchestrator/
│   └── agent-manager/
├── workers/
│   ├── agent-base/
│   ├── claude/
│   ├── codex/
│   ├── test-runner/
│   └── browser-runner/
├── hermes/
│   ├── plugins/
│   ├── skills/
│   ├── hooks/
│   └── config/
├── toolchains/
│   ├── generic/
│   ├── node/
│   ├── python/
│   ├── flutter/
│   ├── php/
│   └── java/
├── schemas/
│   ├── project.schema.json
│   ├── task.schema.json
│   ├── manifest.schema.json
│   └── capability.schema.json
├── migrations/
├── scripts/
├── tests/
└── docs/
    ├── architecture.md
    ├── security.md
    ├── recovery.md
    ├── policies.md
    └── operations.md
```

Adjust if a better structure emerges and explain deviations.

---

# 85. `.hermes/project.yaml`

Create documented schema and example supporting concepts such as:

```yaml
project:
  name: example

autonomy: BALANCED

toolchain:
  profile: node

agents:
  preferred:
    planning: claude
    implementation: codex

resources:
  default_profile: NORMAL

network:
  development: standard
  testing: isolated

quality_gate:
  tests: true
  lint: true
  typecheck: true

budget:
  profile: NORMAL

git:
  protected_branches:
    - main

browser_tests:
  enabled: true
```

This example is illustrative. Design and validate a complete schema.

---

# 86. No Private Reasoning Storage

Never require storage or exposure of model chain-of-thought.

Use concise structured decision summaries and evidence instead.

---

# 87. Do Not Invent Interfaces

Before integrating with Hermes, Claude Code, Codex or GitHub CLI, verify current official documentation.

Do not fabricate:

- API endpoints
- environment variables
- CLI flags
- credential paths
- unsupported extension mechanisms

If an interface is unavailable, create a clean adapter abstraction and document the limitation.

---

# 88. Implementation Methodology

DO NOT attempt the whole platform in one blind coding pass.

## Phase 0: Discovery

Verify:

- current Hermes architecture
- Docker deployment
- extension mechanisms
- Kanban/worker lanes
- Claude Code authentication
- Codex authentication
- GitHub CLI behavior

Produce `DISCOVERY.md`.

Document:

- what Hermes already provides
- what should be reused
- what requires extension
- what must be custom

## Phase 1: Architecture

Produce:

- ARCHITECTURE.md
- SECURITY_MODEL.md
- DATA_MODEL.md
- NETWORK_MODEL.md

Include diagrams.

## Phase 2: Minimal Control Plane

Implement:

- PostgreSQL
- Redis
- Task API
- Project Registry
- basic Scheduler
- Policy Engine
- Approval Service

## Phase 3: Agent Manager

Implement:

- dynamic worker lifecycle
- resource limits
- networks
- workspace mounts
- capability grants

## Phase 4: Claude/Codex Workers

Implement:

- AgentAdapter
- ClaudeAdapter
- CodexAdapter
- authentication bootstrap
- structured results

## Phase 5: Git Isolation

Implement:

- worktrees
- branches
- base commit tracking
- Git Service
- Human Change Protection

## Phase 6: Testing

Implement:

- Test Runner
- ephemeral services
- Playwright Runner
- Quality Gate

## Phase 7: Multi-Agent Orchestration

Implement:

- DAG
- routing
- cross-review
- integration
- retries
- fallback
- Task Expansion

## Phase 8: Recovery

Implement:

- checkpoints
- reconciliation
- leader lease
- provider failover
- reboot recovery

## Phase 9: Hermes Integration

Integrate task creation, status, notifications, approvals and READY_FOR_MERGE using official Hermes extension mechanisms.

## Phase 10: Dashboard / UX

Extend only missing orchestration UI.

## Phase 11: Hardening

Perform:

- security testing
- failure injection
- reboot testing
- provider outage simulation
- budget testing
- Git conflict testing
- policy bypass testing

---

# 89. Test the Platform Itself

Automate scenarios such as:

- worker crashes
- Claude crashes
- Codex unavailable
- Redis restart
- PostgreSQL reconnect
- Hermes offline
- machine restart
- task cancellation
- pause/resume
- budget exhaustion
- authentication expiration
- Git conflict
- user modifies same file
- review failure
- test failure
- browser test failure
- scope expansion
- duplicate task
- high-risk command
- worker attempts another project's files
- worker attempts Docker socket
- worker attempts production secret

Security tests must prove forbidden operations fail.

---

# 90. Acceptance Scenario

Example user request through Hermes:

```text
Add OAuth authentication to project X.
```

Expected conceptual execution:

```text
Hermes
→ Task API

Claude
→ inspect registered project
→ load relevant Project Memory
→ requirements
→ assumptions
→ risk
→ DAG

Scheduler
→ independent subtasks

Agent Manager
→ isolated workers

Codex/Claude
→ implementation
→ relevant tests
→ commits

Cross-review
→ feedback/fixes

Git Service
→ integration branch

Test Runner
→ full suite

Browser Runner
→ E2E validation

Risk-Adaptive Verification
→ security checks

Quality Gate
→ PASS

Hermes
→ READY_FOR_MERGE
```

Nothing merges yet.

After explicit user approval:

```text
Approval Service
→ validate action-specific approval

Git Service
→ merge

Post-merge verification
→ PASS

Task
→ DONE

Hermes
→ TASK_COMPLETED
```

---

# 91. Absolute Rules

NON-NEGOTIABLE:

1. Do not fork Hermes unnecessarily.
2. Reuse official Hermes functionality.
3. Do not invent Hermes APIs.
4. Do not expose Docker socket to workers.
5. Do not give workers unrestricted host access.
6. Do not share unrelated project workspaces.
7. Do not expose GitHub credentials to workers.
8. Do not store provider credentials in Git/PostgreSQL/logs.
9. Do not store secrets in Task Manifests.
10. Do not expose/store private model chain-of-thought.
11. Do not automatically merge into main/master.
12. Do not silently overwrite human changes.
13. Do not let project config bypass hard policies.
14. Do not give production access by default.
15. Do not allow Claude to self-grant capabilities.
16. Do not allow workers to create arbitrary containers.
17. Do not rely on Redis as sole persistent state.
18. Do not make Hermes availability necessary for already-authorized work.
19. Do not silently discard duplicate user requests.
20. Do not allow two orchestrators to lead the same task simultaneously.

---

# 92. Final Deliverables

Produce a working repository, not merely architecture documentation.

At minimum deliver:

- Docker Compose configuration
- Dockerfiles
- control-plane implementation
- Claude Orchestrator
- Agent Manager
- Policy Engine
- Approval Service
- Scheduler
- Project Registry
- Git Service
- Credential/Secrets interfaces
- PostgreSQL migrations
- Redis integration
- AgentAdapter
- ClaudeAdapter
- CodexAdapter
- worker images
- Test Runner
- Browser Runner
- toolchain profiles
- Hermes integration
- project configuration schema
- Task Manifest schema
- Capability Grant schema
- CLI
- health checks
- backup scripts
- update scripts
- rollback scripts
- recovery logic
- tests
- security tests
- README
- architecture documentation
- operations documentation

---

# 93. README Requirements

Include separate setup instructions for macOS Apple Silicon and Linux.

Explain:

1. prerequisites
2. installation
3. directory structure
4. initial Hermes setup
5. Claude login
6. Codex login
7. optional GitHub login
8. registering first project
9. onboarding
10. creating first task
11. monitoring workers
12. approving merge
13. recovering after failure
14. updating components
15. backing up state
16. troubleshooting

Commands must be copy/paste ready.

---

# 94. Implementation Quality

Use:

- typed interfaces where practical
- migrations
- health checks
- structured logging
- explicit error handling
- idempotent operations
- deterministic state transitions
- transactional DB operations where appropriate
- secure defaults
- configuration validation
- clean abstractions
- unit tests
- integration tests

Avoid speculative complexity without concrete value.

---

# 95. First Action

DO NOT begin by generating random Docker Compose YAML.

Your first action must be:

1. Inspect the latest official Hermes Agent documentation and repository.
2. Identify which requested capabilities Hermes already implements.
3. Verify current Hermes extension mechanisms.
4. Verify Claude Code authentication behavior.
5. Verify Codex CLI authentication behavior.
6. Produce `DISCOVERY.md`.
7. Produce a component responsibility matrix.
8. Produce the proposed architecture.
9. Identify conflicts between this specification and current Hermes behavior.
10. STOP after Phase 0 and wait for explicit user approval before implementation.

For each major custom component answer:

```text
Does Hermes already provide this?

YES     → reuse/integrate
PARTIAL → extend
NO      → implement externally
```

The objective is not to build the largest possible system.

The objective is to build the smallest secure, reliable and extensible system that satisfies this specification while maximizing reuse of official Hermes Agent functionality.
