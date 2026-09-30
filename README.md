# Hermes Orchestrator

A local-first multi-agent development platform built around the official [Hermes Agent](https://github.com/NousResearch/hermes-agent). Claude Code and Codex CLI work in isolated containers; nothing reaches `main` without explicit human approval.

- Specification: [MASTER_SPEC.md](MASTER_SPEC.md)
- Roadmap and status: [PHASES.md](PHASES.md)
- Design: [DISCOVERY.md](DISCOVERY.md), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md), [DATA_MODEL.md](DATA_MODEL.md), [NETWORK_MODEL.md](NETWORK_MODEL.md)

## Current status

Phase 2 (minimal control plane) is implemented: project registration and read-only onboarding, configuration proposals with hard-policy enforcement, approvals, tasks with deterministic states, a scheduler queue, and an operator CLI. **No agent executes work yet.** Workers arrive in Phases 3–4 and Hermes integration in Phase 9. Evidence: [docs/validation/phase-2.md](docs/validation/phase-2.md).

This README grows with each phase. The full operations guide required by the specification is completed in Phase 11.

## Prerequisites

| | macOS (Apple Silicon) | Linux |
| --- | --- | --- |
| Docker | Docker Desktop with Compose v2 | Docker Engine with the Compose v2 plugin |
| Memory for Docker | 8 GB minimum now; 16 GB+ recommended once workers exist (Settings → Resources) | Size to your workload |
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

Review `config/linux.yaml` and set worker limits for your hardware first. `make up` creates random service passwords and tokens in `./secrets/` (never committed).

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

If the repository has its own `.hermes/project.yaml`, the scan uses it (validated against [schemas/project.schema.json](schemas/project.schema.json)). An untracked `.hermes.local.yaml` can tighten settings but never weaken them.

## Development

```bash
make venv              # Python 3.12 environment with all packages in editable mode
make test              # unit tests + integration tests (starts throwaway PostgreSQL/Redis)
make smoke             # end-to-end test on a throwaway Compose stack
make lint validate-schemas
make test-env-down     # remove the integration test containers
```

## Repository layout

See [ARCHITECTURE.md §12](ARCHITECTURE.md#12-repository-layout). The main pieces today are `packages/ho_core` (shared contracts and policy), `services/control-plane`, `services/git-service`, `migrations/`, `schemas/`, and `config/`.
