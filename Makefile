# Hermes Orchestrator developer and operator commands.
# Copy/paste friendly: every target prints what it runs.

SHELL := /bin/bash
PYTHON ?= python3.12
VENV := .venv
IDENTITY ?= default
COMPOSE := docker compose
TEST_DB := postgresql://ho_test_admin:ho_test_admin@127.0.0.1:55432/postgres
TEST_REDIS := redis://127.0.0.1:56379/15

.PHONY: help venv secrets images up down ps logs cli auth-claude auth-codex auth-github test test-unit test-integration test-docker test-env test-env-down smoke smoke-phase3 smoke-phase4 smoke-phase5 lint validate-schemas

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

venv: ## Create the local Python 3.12 environment for development and tests
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -c requirements.lock -e packages/ho_core -e services/control-plane -e services/git-service -e services/agent-manager -e services/egress-proxy
	$(VENV)/bin/pip install pytest ruff

secrets: ## Generate service passwords and tokens in ./secrets (idempotent)
	./scripts/init-secrets.sh

images: ## Build execution images and pin them in config/images.lock.yaml (HO_TOOLCHAINS="generic node python")
	./scripts/build-images.sh

up: secrets images ## Build and start the platform (needs .env)
	$(COMPOSE) up -d --build --wait

down: ## Stop the platform (keeps data volumes)
	$(COMPOSE) down

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Follow service logs
	$(COMPOSE) logs -f --tail=100

cli: ## Open the operator CLI help (docker compose exec control-plane ho ...)
	$(COMPOSE) exec control-plane ho --help

auth-claude: ## Log in Claude Code with your subscription (IDENTITY=default)
	./scripts/auth-login.sh claude $(IDENTITY)

auth-codex: ## Log in Codex with your ChatGPT account (IDENTITY=default)
	./scripts/auth-login.sh codex $(IDENTITY)

auth-github: ## Log in the GitHub CLI for Git Service only (pushes, pull requests, approved merges)
	$(COMPOSE) run --rm --no-deps -e HOME=/tmp git-service gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key --insecure-storage
	$(COMPOSE) run --rm --no-deps -e HOME=/tmp git-service gh auth status

test: test-unit test-integration ## Run unit and integration tests

test-unit: ## Run unit tests
	$(VENV)/bin/pytest tests/unit -q

test-env: ## Start throwaway PostgreSQL and Redis for integration tests
	$(COMPOSE) -f compose.test.yaml up -d --wait

test-env-down: ## Remove the integration test containers
	$(COMPOSE) -f compose.test.yaml down -v

test-integration: test-env ## Run integration tests against real PostgreSQL and Redis
	HO_TEST_DATABASE_URL=$(TEST_DB) HO_TEST_REDIS_URL=$(TEST_REDIS) $(VENV)/bin/pytest tests/integration -q -p no:warnings

test-docker: images ## Agent Manager tests against the real Docker daemon (needs Internet for egress tests)
	HO_TEST_DOCKER=1 $(VENV)/bin/pytest tests/docker -q -p no:warnings

smoke: secrets images ## End-to-end Phase 2 smoke test on a throwaway Compose stack
	./scripts/smoke-phase2.sh

smoke-phase3: secrets images ## End-to-end Phase 3 smoke test (real workers)
	./scripts/smoke-phase3.sh

smoke-phase4: secrets images ## End-to-end Phase 4 smoke test (agent executions, auth, secrets)
	./scripts/smoke-phase4.sh

smoke-phase5: secrets images ## End-to-end Phase 5 smoke test (workspaces, human changes, integration, approved merge)
	./scripts/smoke-phase5.sh

lint: ## Static checks
	$(VENV)/bin/ruff check packages services tests migrations scripts

validate-schemas: ## Validate JSON Schemas, examples, and machine profiles
	$(VENV)/bin/python scripts/validate_schemas.py
