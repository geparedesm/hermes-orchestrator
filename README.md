# Hermes Orchestrator

A local-first multi-agent development platform built around the official [Hermes Agent](https://github.com/NousResearch/hermes-agent). Claude Code and Codex CLI work in isolated containers; nothing reaches `main` without explicit human approval.

- Specification: [MASTER_SPEC.md](MASTER_SPEC.md)
- Roadmap and status: [PHASES.md](PHASES.md)
- Design: [DISCOVERY.md](DISCOVERY.md), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md), [DATA_MODEL.md](DATA_MODEL.md), [NETWORK_MODEL.md](NETWORK_MODEL.md)

## Current status

Phases 2–5 are implemented: project registration and read-only onboarding, configuration proposals with hard-policy enforcement, approvals, tasks with deterministic states, a scheduler queue, an operator CLI, the **Agent Manager**, which runs isolated, resource-limited worker containers with per-execution egress control, and **Claude Code and Codex workers** that sign in with your subscriptions, return structured results, and pause in `AUTH_REQUIRED` when a login expires, and **Git isolation**: each task works in isolated clones, human changes are detected and never overwritten, and nothing merges into a protected branch without an explicit human approval. The operator starts agent executions by hand until the orchestrator arrives (Phase 7); Hermes integration arrives in Phase 9. Evidence: [docs/validation/phase-2.md](docs/validation/phase-2.md), [docs/validation/phase-3.md](docs/validation/phase-3.md), [docs/validation/phase-4.md](docs/validation/phase-4.md), [docs/validation/phase-5.md](docs/validation/phase-5.md).

This README grows with each phase. The full operations guide required by the specification is completed in Phase 11.

## Prerequisites

| | macOS (Apple Silicon) | Linux |
| --- | --- | --- |
| Docker | Docker Desktop with Compose v2 | Docker Engine with the Compose v2 plugin |
| Memory for Docker | 16 GB+ recommended (Settings → Resources). With 8 GB, Agent Manager refuses executions that do not fit | Size to your workload |
| Tools | `git`, `openssl`, `make`; `python3.12` for development | same |

## Setup

### macOS (Apple Silicon)

```bash
git clone https://github.com/geparedesm/hermes-orchestrator.git
cd hermes-orchestrator
mkdir -p ~/HermesProjects
cp .env.example .env
sed -i '' "s|/Users/CHANGE_ME/HermesProjects|$HOME/HermesProjects|" .env
make up
docker compose exec control-plane ho health
```

### Linux

```bash
git clone https://github.com/geparedesm/hermes-orchestrator.git
cd hermes-orchestrator
sudo mkdir -p /srv/HermesProjects && sudo chown "$USER" /srv/HermesProjects
cp .env.example .env
sed -i "s|^HO_MACHINE_PROFILE=.*|HO_MACHINE_PROFILE=linux|; s|/Users/CHANGE_ME/HermesProjects|/srv/HermesProjects|" .env
make up
docker compose exec control-plane ho health
```

Review `config/linux.yaml` and set worker limits for your hardware first, and set `HO_DOCKER_GID` in `.env` to your Docker socket's group (`getent group docker | cut -d: -f3`). `make up` creates random service passwords and tokens in `./secrets/` (never committed) and builds and pins the execution images.

## First project

Projects must live inside the projects root. Registering never modifies the repository, and unregistering never deletes it.

```bash
# Register a Git repository inside the projects root
docker compose exec control-plane ho project register "$HOME/HermesProjects/my-app"

# Read-only scan: detects toolchains, commands, CI, risks; proposes .hermes/project.yaml
docker compose exec control-plane ho project scan my-app

# Review the proposal, then approve it (the approval ID is in the scan output)
docker compose exec control-plane ho approval list
docker compose exec control-plane ho approval approve <approval-id>

# Create a task; it waits in the queue until workers exist (Phase 3+)
docker compose exec control-plane ho task create my-app "Add OAuth authentication" --priority HIGH
docker compose exec control-plane ho task list
docker compose exec control-plane ho task queue
```

## Isolated executions (Phase 3)

Until the orchestrator exists, the operator can run commands in workers directly. Workspaces must already exist under the project's `.hermes/worktrees/` (Git Service creates them from Phase 5).

```bash
mkdir -p ~/HermesProjects/my-app/.hermes/worktrees/w1

# A test runner: no network, no provider credential
docker compose exec control-plane ho execution run T-1 'id; ls /workspace' --workspace .hermes/worktrees/w1 --workspace-access READ

# Egress through the execution's proxy (agent roles need a provider credential volume, set up in Phase 4)
docker compose exec control-plane ho execution list --task T-1
docker compose exec control-plane ho execution show <execution-id>   # state, grant, artifacts
docker compose exec control-plane ho workers                          # capacity and managed containers
```

If the repository has its own `.hermes/project.yaml`, the scan uses it (validated against [schemas/project.schema.json](schemas/project.schema.json)). An untracked `.hermes.local.yaml` can tighten settings but never weaken them.

## Claude Code and Codex (Phase 4)

Log in once per provider. Each command opens the provider's official login flow in a throwaway container and stores the result in a dedicated Docker volume (`cred-claude-default`, `cred-codex-default`), never in Git, PostgreSQL, logs, or backups:

```bash
make auth-claude    # claude setup-token: authorize in the browser, then paste the printed token
make auth-codex     # codex login --device-auth: enter the code shown at the ChatGPT URL
docker compose exec control-plane ho auth status
```

Codex's device-code login must be allowed for your ChatGPT account. When a login expires, affected tasks wait in `AUTH_REQUIRED`; run the same `make auth-*` command again and they resume.

Run an agent on a task in its own workspace:

```bash
docker compose exec control-plane ho git workspace T-1                          # isolated clone, e.g. .hermes/worktrees/t-1-w1
docker compose exec control-plane ho agent run T-1 "Add a health check endpoint with tests" \
  --provider codex --workspace .hermes/worktrees/t-1-w1 --workspace-access WRITE
docker compose exec control-plane ho execution show <execution-id>          # state, result, usage, artifacts
docker compose exec control-plane ho execution resume <execution-id> "Also update the README"
```

The image follows the project's `toolchain.profiles` (for example `codex-node`); build extra profiles with `make images HO_TOOLCHAINS="generic node python php java flutter"`.

Project secrets are declared by name in `.hermes/project.yaml` and stored as files under `./project-secrets/<project>/<environment>/<NAME>` (mode 0600). Request them with `--secret NAME`; they are delivered into the worker's memory and redacted from everything collected.

## Git: integration and approved merges (Phase 5)

Agents commit only in their workspace clones; Git Service is the only writer of your repository.

```bash
ho git collect T-1          # read the workspace commits
ho git divergence T-1       # what you changed on main since the task started: NONE, LOW, MEDIUM, HIGH, CRITICAL
ho git integrate T-1        # merge the work onto the current main (without touching your checkout) and retest it
ho git resolve T-1 --provider claude   # if integration conflicts: an agent resolves it in a fresh clone
ho git merge-request T-1    # when the task is READY_FOR_MERGE: asks for your MERGE approval
ho approval approve <id>    # the merge happens only now, and only if main and the change are exactly as approved
```

(`ho` is `docker compose exec control-plane ho`.) The merge never overwrites uncommitted work in your checkout, and post-merge tests run before the task is `DONE`. For GitHub repositories, log in once with `make auth-github` (only Git Service holds the token); then `ho git push T-1`, `ho git pr T-1`, and `ho git checks T-1`, and the approved merge goes through the pull request.

## Development

```bash
make venv              # Python 3.12 environment with all packages in editable mode
make test              # unit tests + integration tests (starts throwaway PostgreSQL/Redis)
make test-docker       # Agent Manager against the real Docker daemon (needs Internet)
make smoke             # Phase 2 end-to-end test on a throwaway Compose stack
make smoke-phase3      # Phase 3 end-to-end test with real workers
make smoke-phase4      # Phase 4 end-to-end test: adapters, real CLIs (invalid logins), AUTH_REQUIRED, secrets
make smoke-phase5      # Phase 5 end-to-end test: workspaces, human changes, integration, approved merge
make lint validate-schemas
make test-env-down     # remove the integration test containers
```

## Repository layout

See [ARCHITECTURE.md §12](ARCHITECTURE.md#12-repository-layout). The main pieces today are `packages/ho_core` (shared contracts and policy), `services/control-plane`, `services/git-service`, `services/agent-manager`, `services/egress-proxy`, `workers/` (agent-base, toolchains, providers), `migrations/`, `schemas/`, and `config/`.
