# Validation: child projects (Odoo modules from odoo-base)

Design: [docs/design/child-projects.md](../design/child-projects.md). Date: 2026-10-12.

## What was built

- **Control plane** (`children.py`, migration `0014`):
  - `POST /v1/projects/{slug}/children` pins the parent's HEAD, the template tree, the destination, and
    `owner/repo` into a `CREATE_CHILD` approval. Only approvers may ask.
  - Once approved, `child_creations` advances one recorded step at a time (publish, then GitHub, then register and
    scan). The scheduler resumes transient failures, and a person resolves `NEEDS_RECOVERY` with retry or abandon.
- **Git Service** (`children.py`):
  - `plan` checks the destination: no symlink in any component, ignored by the parent, no tracked entries.
  - `publish` renders the template from Git objects into a directory created exclusively, with files opened using
    `O_EXCL|O_NOFOLLOW`. The first commit carries `Hermes-Operation: <id>`.
  - `remote` runs `gh repo create --private --description "hermes-orchestrator child <id>"`. It adopts a repository
    only with that evidence, and pushes `main` only onto an absent branch or the same commit.
  - The authorization is signed (`sign_child`/`verify_child`) and checked on every call.
- **Dashboard**:
  - Projects groups modules under their parent, with **New module** (name, title, description).
  - Creations waiting for approval or stopped for a person show Retry and Abandon.
  - The `CREATE_CHILD` decision is explained in plain words (also in chat, es/en).
- **Odoo toolchain** (`workers/toolchains/odoo`):
  - Odoo 19.0-20260926, pinned by date and SHA-256, plus `pypdf`.
  - `ho-odoo-test <module>` links `/workspace` as the module into a temporary addons path. It installs the module
    into a throwaway database on the test PostgreSQL service and runs its tests.
  - It fails when the module is not installed, when no test ran, or when any test failed.
- **odoo-base** (commit `fd0a7ea`, local):
  - `children` configuration;
  - the module template `.hermes/templates/odoo-module`: Odoo 19 skeleton, `AGENTS.md` rules (English;
    `[ACTION] Description` commits), `CLAUDE.md` importing them, the test PostgreSQL service, and the install test.

## Tests

| Suite | Result |
| --- | --- |
| Unit (`make test-unit`) | 320 passed |
| Integration (`make test-integration`) | 199 passed |
| Git Service children (`tests/unit/test_git_children.py`) | 11 passed: a component swapped for a link after the checks, rendering, binary files, exclusive creation, symlinked path, non-ignored path, template links, altered token, private repository and push, lost answer adopted with evidence, foreign repository refused |
| Control plane children (`tests/integration/test_children.py`) | 8 passed: an expired request renewed or abandoned, created only after approval, rejection, parent changed (invalidated), foreign folder (`NEEDS_RECOVERY`, then retry), non-approver refused, parent without `children`, unsafe titles |
| Docker, odoo toolchain (`tests/docker/test_odoo_toolchain.py`) | 4 passed against real PostgreSQL 16: module installed and tested; failing test fails; non-installable module fails; workspace without a module refused |
| The odoo-base template rendered as `job_alerts` | installs in Odoo 19; `0 failed, 0 error(s) of 1 tests` |

Odoo CLI flags were checked with `odoo-bin server --help` in the built image. The `gh repo create` and
`gh repo view --json` fields were checked with `gh` 2.101.0.

## Reviews

- **Codex adversarial review of the design** asked for four changes, all adopted before building:
  - an operation recorded before any effect, with proof of ownership;
  - a confined, exclusive destination;
  - an approval bound to the parent SHA, the parameters, and the repository;
  - an addons layout in which Odoo finds the module.
- **Codex review of the code:** see the PR.

## Not yet verified

- An end-to-end creation on the live stack, which needs `make images` with the odoo toolchain and a real GitHub
  repository creation, approved by the operator.
- Children may depend only on standard Odoo modules (v1). `job_finder` is not linked as a child yet.
