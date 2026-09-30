# Hermes Orchestrator

A local-first multi-agent development platform built around the official [Hermes Agent](https://github.com/NousResearch/hermes-agent). Claude Code and Codex CLI work in isolated containers; nothing reaches `main` without explicit human approval.

- Specification: [MASTER_SPEC.md](MASTER_SPEC.md)
- Roadmap and status: [PHASES.md](PHASES.md)
- Design: [DISCOVERY.md](DISCOVERY.md), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md), [DATA_MODEL.md](DATA_MODEL.md), [NETWORK_MODEL.md](NETWORK_MODEL.md)

## Current status

Phases 2–3 are implemented: project registration and read-only onboarding, configuration proposals with hard-policy enforcement, approvals, tasks with deterministic states, a scheduler queue, an operator CLI, and the **Agent Manager**, which runs commands in isolated, resource-limited worker containers with per-execution egress control. **Claude Code and Codex do not run yet** (Phase 4), and Hermes integration arrives in Phase 9. Evidence: [docs/validation/phase-2.md](docs/validation/phase-2.md), [docs/validation/phase-3.md](docs/validation/phase-3.md).

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

## Development

```bash
make venv              # Python 3.12 environment with all packages in editable mode
make test              # unit tests + integration tests (starts throwaway PostgreSQL/Redis)
make test-docker       # Agent Manager against the real Docker daemon (needs Internet)
make smoke             # Phase 2 end-to-end test on a throwaway Compose stack
make smoke-phase3      # Phase 3 end-to-end test with real workers
make lint validate-schemas
make test-env-down     # remove the integration test containers
```

## Repository layout

See [ARCHITECTURE.md §12](ARCHITECTURE.md#12-repository-layout). The main pieces today are `packages/ho_core` (shared contracts and policy), `services/control-plane`, `services/git-service`, `services/agent-manager`, `services/egress-proxy`, `workers/agent-base`, `migrations/`, `schemas/`, and `config/`.
