# Hermes Orchestrator

A local-first multi-agent development platform built around the official [Hermes Agent](https://github.com/NousResearch/hermes-agent). You ask for a change through Hermes (chat, Dashboard, or CLI); Claude Code and Codex plan, implement, and cross-review it in isolated containers; it is integrated, tested, and checked by a Quality Gate; and nothing reaches your `main` branch until you approve the exact merge.

- Specification: [MASTER_SPEC.md](MASTER_SPEC.md) · Roadmap and evidence: [PHASES.md](PHASES.md), [docs/validation/](docs/validation/)
- Design: [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md), [DATA_MODEL.md](DATA_MODEL.md), [NETWORK_MODEL.md](NETWORK_MODEL.md)
- Operations: [docs/operations.md](docs/operations.md) (backups, updates, retention), [docs/recovery.md](docs/recovery.md), [docs/hermes.md](docs/hermes.md), [docs/security-tests.md](docs/security-tests.md)

In the commands below, `ho` means `docker compose exec control-plane ho` (add `alias ho='docker compose exec control-plane ho'` to your shell).

## 1. Prerequisites

| | macOS (Apple Silicon) | Linux (x86-64 or ARM64) |
| --- | --- | --- |
| Docker | Docker Desktop with Compose v2 | Docker Engine 24+ with the Compose v2 plugin |
| Memory for Docker | 16 GB or more (Docker Desktop → Settings → Resources) | Sized to `config/linux.yaml` |
| Tools | `git`, `make`, `openssl`, `curl`, `python3` | same |
| Accounts | A Claude subscription (Claude Code) and a ChatGPT plan with Codex; optionally GitHub; optionally a Telegram (or other) chat channel for Hermes | same |

Development of the platform itself also needs Python 3.12 (`make venv`).

## 2. Installation

### macOS (Apple Silicon)

```bash
git clone https://github.com/geparedesm/hermes-orchestrator.git
cd hermes-orchestrator
mkdir -p ~/HermesProjects
cp .env.example .env
sed -i '' "s|/Users/CHANGE_ME/HermesProjects|$HOME/HermesProjects|" .env
make up                                   # secrets, pinned images, migrations, all services
docker compose exec control-plane ho health
```

### Linux

```bash
git clone https://github.com/geparedesm/hermes-orchestrator.git
cd hermes-orchestrator
sudo mkdir -p /srv/HermesProjects && sudo chown "$USER" /srv/HermesProjects
cp .env.example .env
sed -i "s|^HO_MACHINE_PROFILE=.*|HO_MACHINE_PROFILE=linux|; s|/Users/CHANGE_ME/HermesProjects|/srv/HermesProjects|" .env
sed -i "s|^HO_DOCKER_GID=.*|HO_DOCKER_GID=$(getent group docker | cut -d: -f3)|" .env
make up
docker compose exec control-plane ho health
```

Before `make up` on Linux, size `max_agent_workers`, `resource_profiles`, and `runners` in `config/linux.yaml` to the machine (the macOS values live in `config/mac-m2-pro.yaml`; nothing Mac-specific is assumed on Linux). `make up` creates random service passwords and tokens in `./secrets/` (never committed) and builds and pins the execution images for the machine's architecture (ARM64 or x86-64).

To let the orchestrator lead tasks automatically (plan, delegate, cross-review, integrate, test, gate), set `HO_ORCHESTRATION=true` in `.env` and run `make up` again. Without it, the operator drives agents and Git steps by hand (section 10).

## 3. Directory structure

```text
hermes-orchestrator/
├── compose.yaml                 # the stack: postgres, redis, control-plane, git-service, agent-manager, hermes
├── config/                      # defaults.yaml, mac-m2-pro.yaml, linux.yaml, images.lock.yaml (generated)
├── packages/ho_core/            # shared contracts: state machine, policy engine, adapters, routing, schemas
├── services/                    # control-plane (API, scheduler, orchestrator, recovery), git-service, agent-manager, egress-proxy
├── hermes/                      # the Hermes plugin `orchestration` and hermes-init
├── workers/                     # agent-base, toolchains, Claude and Codex images, browser-runner, pinned versions
├── migrations/  schemas/        # PostgreSQL migrations; JSON Schemas (project, manifest, capability, results)
├── scripts/                     # smoke tests, backup/restore, update/rollback, check, auth helpers
├── docs/                        # design, validation evidence, operations, recovery, Hermes, security tests
├── secrets/                     # generated service secrets (not in Git)
└── ~/HermesProjects/<project>/  # your repositories (outside this tree); workspaces in <project>/.hermes/worktrees/
```

## 4. Initial Hermes setup

Hermes runs unmodified from its official image with the `orchestration` plugin enabled.

```bash
open http://127.0.0.1:9119                 # macOS; on Linux: xdg-open http://127.0.0.1:9119
cat secrets/ho_hermes_dashboard_password   # log in as "operator"
docker compose exec hermes hermes model    # choose the model provider for Hermes's own agent
```

To chat with it and receive notifications, connect a channel (Telegram example; other Hermes channels work the same way), then `make up`:

```bash
cat >> .env <<'EOF'
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_ALLOWED_USERS=<your Telegram user id>
HO_APPROVERS=telegram:<your Telegram user id>
HO_HERMES_DELIVER=telegram
HO_HERMES_DELIVER_CHAT_ID=<chat id for notifications>
HO_HERMES_WEBHOOK_URL=http://hermes:8644/webhooks/orchestration
EOF
make up
```

Details and security notes: [docs/hermes.md](docs/hermes.md).

## 5. Claude login

```bash
make auth-claude                          # claude setup-token: authorize in the browser, paste the printed token
ho auth status
```

## 6. Codex login

```bash
make auth-codex                           # codex login --device-auth: enter the code at the ChatGPT URL
ho auth status
```

Logins live in dedicated Docker volumes (`cred-claude-default`, `cred-codex-default`), never in Git, PostgreSQL, logs, or backups. When one expires, affected tasks wait in `AUTH_REQUIRED` (and a notification says so); run the same command again and they resume.

## 7. Optional GitHub login

```bash
make auth-github                          # gh auth login, stored only for Git Service
```

With it, approved merges of GitHub projects go through a pull request (push, PR, CI checks, merge with the approved head).

## 8. Registering the first project

Projects must be Git repositories inside the projects root. Registering never modifies a repository and unregistering never deletes it.

```bash
ho project register "$HOME/HermesProjects/my-app"      # Linux: /srv/HermesProjects/my-app
```

## 9. Onboarding

```bash
ho project scan my-app                    # read-only: toolchains, commands, CI, risks -> proposed .hermes/project.yaml
ho approval list                          # review the proposal
ho approval approve <approval-id>         # the project becomes PROJECT_READY
```

A `.hermes/project.yaml` in the repository is validated against [schemas/project.schema.json](schemas/project.schema.json); a local `.hermes.local.yaml` can tighten but never weaken it, and no project setting can relax the platform's hard policies.

## 10. Creating the first task

From chat (`/orch create my-app Add OAuth authentication`, or just ask Hermes), from the Dashboard, or from the CLI:

```bash
ho task create my-app "Add OAuth authentication" --priority HIGH
ho task list
```

With `HO_ORCHESTRATION=true` the orchestrator takes it from there. Without it, drive the steps yourself:

```bash
ho git workspace T-1
ho agent run T-1 "Add OAuth authentication with tests" --provider codex --workspace .hermes/worktrees/t-1-w1 --workspace-access WRITE
ho git integrate T-1                     # merge onto the current main without touching your checkout, then test
ho review run T-1 --provider claude      # cross-review by the provider that did not develop it
ho gate evaluate T-1                     # Quality Gate -> READY_FOR_MERGE
```

## 11. Monitoring

- Dashboard → **Orchestration**: required actions, queue, running work, provider usage, a board, and each task's plan, reviews, tests, budget, timeline, and manifests.
- Chat: `/orch tasks`, `/orch status T-1`.
- CLI:

```bash
ho task inspect T-1        # plan, subtasks, lease, recent orchestrator actions, budget
ho task events T-1         # audit timeline
ho workers                 # capacity, running workers
ho execution list --task T-1
ho recovery status         # platform health, open intents, pending notifications
curl -s -H "Authorization: Bearer $(cat secrets/ho_operator_token)" http://127.0.0.1:8080/metrics   # if you expose the port
```

## 12. Approving a merge

When the Quality Gate passes, the task is `READY_FOR_MERGE` and a MERGE approval is requested for the exact integrated commit. Approve it from chat (`/orch approve <id>`, an identity in `HO_APPROVERS`), the Dashboard, or the CLI:

```bash
ho approval list
ho approval approve <approval-id>
```

Git Service merges only if `main` and the change are exactly as approved, never over uncommitted work in your checkout; post-merge tests run, and only then is the task `DONE` (`TASK_COMPLETED` is notified).

## 13. Recovering after a failure

The platform recovers by itself on start: running work is reconciled with the containers that exist, dead workers are not resurrected, and orchestration resumes from PostgreSQL ([docs/recovery.md](docs/recovery.md)).

```bash
docker compose up -d --wait               # after a reboot or a crash
make check                                # health check of the whole stack
ho recovery run                           # reconcile now
ho recovery status
```

## 14. Updating components

Updates are approved like any other sensitive action, and roll back by themselves when the health check fails ([docs/operations.md](docs/operations.md#updates)):

```bash
git pull                                  # the new version of the platform
scripts/update.sh 1.1.0                   # requests the UPDATE approval
ho approval approve <approval-id>
scripts/update.sh 1.1.0 <approval-id>     # snapshot -> update -> health check -> rollback on failure
```

## 15. Backing up state

```bash
make backup                               # PostgreSQL, Hermes state, config, artifacts -> backups/<timestamp>
make restore BACKUP=backups/<timestamp>
```

Credentials, Redis, workers, and test environments are never backed up. Schedule a daily backup with launchd (macOS) or cron/systemd (Linux): [docs/operations.md](docs/operations.md#backups).

## 16. Troubleshooting

| Symptom | What to do |
| --- | --- |
| A task waits in `AUTH_REQUIRED` | `make auth-claude` or `make auth-codex`, then `ho auth status` |
| A task is `PAUSED_BUDGET` | `ho task budget T-1` to see it; `ho task budget T-1 --add agent_launches=10` and approve |
| A task is `BLOCKED` | `ho task inspect T-1` and `ho task events T-1`; then `ho task retry T-1` or `ho task cancel T-1` |
| `ho recovery status` shows `DEGRADED` | `docker compose ps`; restart the failing service (`docker compose up -d --wait`); work waits instead of failing |
| Executions are refused for memory | Give Docker more memory or lower `resource_profiles` in your machine profile |
| No notifications in chat | `HO_HERMES_DELIVER` and `HO_HERMES_WEBHOOK_URL` set? `ho recovery status` shows pending notifications; `docker compose logs hermes` |
| The Dashboard tab is missing | `docker compose exec hermes hermes plugins list` must show `orchestration` as enabled; `docker compose up -d hermes-init hermes` |
| Package installs are slow or a cache is broken | `ho cache list`; `ho cache clear my-app --ecosystem npm` |
| Something else | `make check`, `docker compose logs <service>`, and [docs/operations.md](docs/operations.md) |

## Development

```bash
make venv                  # Python 3.12 environment
make test                  # unit + integration tests (throwaway PostgreSQL and Redis)
make test-docker           # Agent Manager against the real Docker daemon
make test-security         # the security suite (docs/security-tests.md)
make lint validate-schemas
scripts/smoke-phaseN.sh    # end-to-end tests on throwaway stacks, N = 2..11
```
