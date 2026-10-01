# Phase 10 design: Dashboard and UX

Scope: PHASES.md Phase 10; MASTER_SPEC §73 (observability), §75 (Dashboard strategy). Principle: add only the orchestration views Hermes lacks, inside Hermes's Dashboard (the plugin tab from Phase 9); reuse Hermes for configuration, credentials, sessions, logs, skills, cron, and administration.

## Reuse decisions

- **Hermes administration, logs, sessions, skills, cron**: reused as is; nothing duplicated.
- **Hermes Kanban board**: not reused for platform tasks. Showing them there would need a second writer of task state (AD-02, D01) or a projection (OI-08); the orchestration tab renders its own read-only board from the Task API. OI-08 stays open for after v1.
- **Hermes Dashboard login and SDK**: reused (the tab's API is behind Hermes's auth; the frontend uses `__HERMES_PLUGIN_SDK__`).

## Views (one tab, client-side routing)

| View | Content | Source |
| --- | --- | --- |
| Overview | Required actions (pending approvals, blocked, waiting for login or budget), queue with effective priority and wait, platform health, workers in use, provider usage (tokens, executions, failures, retries) for 24 h / 7 d, recent test results | `GET /v1/dashboard/summary` |
| Board | Kanban columns by task state (read-only) | `GET /v1/tasks?active=true` |
| Task | State and controls (pause, resume, cancel, retry); plan DAG with subtask states and dependencies; approvals; budget with consumed/reserved/limits; Quality Gate requirements; reviews with findings; test runs; executions with provider, role, duration, usage; Git state; audit timeline; checkpoints; manifests (view or generate) | `GET /v1/dashboard/tasks/{key}` |
| Projects | Status, default branch, task counts | `GET /v1/projects` + summary |
| Workers | Running executions with role, provider, task, CPU and memory | `GET /v1/workers` (+ Agent Manager `GET /v1/stats`) |
| Approvals | Pending approvals with decide buttons | `GET /v1/approvals` |

## Backend additions

- `control_plane/dashboard.py`: read models computed from PostgreSQL (no new tables): summary and task detail.
- Agent Manager `GET /v1/stats`: per running managed container, CPU percent and memory from the Docker stats API (one non-streaming sample).
- `GET /metrics` (operator token): Prometheus text exposition of tasks by state, queue length, executions by state and provider, provider tokens, notifications pending, component health — the hook for future Prometheus/OpenTelemetry without adding either to v1.
- `GET /v1/tasks/{key}/manifests` and `.../manifests/{id}`: stored manifests.

## Tests

Integration: summary and task detail contents for an orchestrated task (DAG, reviews, gate, tests, budget, timeline), manifests listing, `/metrics` format and auth. Unit: Prometheus formatting. Frontend: syntax check (`node --check`). Smoke (`smoke-phase10.sh`): the real Hermes Dashboard rendered in headless Chromium (the Browser Runner image) — log in, open the tab, see the overview and a task's detail with its timeline, decide an approval from the page — plus the plugin API routes and `/metrics`.
