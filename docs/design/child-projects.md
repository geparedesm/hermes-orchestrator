# Child projects from a template (Odoo modules from odoo-base)

Status: proposal. A parent project (odoo-base) creates child projects (custom Odoo modules) from a template it
versions. Each child is its own repository, GitHub repository, and platform project, like `job_finder` today.

## Data

- `project.yaml` of the parent gains `children`: `path` (e.g. `custom-addons`), `template` (a directory in the
  parent's repository, e.g. `.hermes/templates/odoo-module`), `name_pattern` (`^[a-z][a-z0-9_]{1,40}$`, an Odoo
  technical name), and `github` (`owner`, `repo_name` such as `odoo-{name}`, `private: true`).
- Migration `0014`: `projects.parent_project_id` (nullable FK) and a `child_creations` table (id, parent, name,
  step, error, idempotency key) so a creation interrupted between steps resumes instead of repeating.
- Template files may use `{{name}}`, `{{title}}`, `{{description}}`, and `{{odoo_series}}` (from the parent config).

## Flow (`POST /v1/projects/{slug}/children`, human principals only; never an LLM tool, AD-10)

1. Validate the name, that the parent declares `children`, and that `<parent>/<path>/<name>` does not exist (a
   human's directory is never overwritten, invariant 5).
2. Git Service renders the template **from the parent's committed HEAD** (Git objects, not the working tree, like
   read views): no symlinks, no `..`, at most 200 files and 2 MiB. It writes the child, runs `git init -b main`, and
   makes the first commit "[add] Scaffold <name> module".
3. Git Service (the only GitHub credential holder, AD-07) runs `gh repo create <owner>/<repo> --private
   --source <child> --remote origin --push`. It refuses when the repository already exists (it never adopts an
   unknown repository).
4. The control plane registers the child with `parent_project_id` and starts the usual scan. You approve its
   configuration (PROJECT_READY), and a notification says it is ready.

The Dashboard project card shows **New module** (name, title, description) only for parents that declare
`children`. The child appears under its parent.

## The template (in odoo-base, `.hermes/templates/odoo-module`)

- An Odoo 19 module skeleton: `__manifest__.py` (version `19.0.1.0.0`, LGPL-3), `models/`, `security/ir.model.access.csv`,
  `views/`, `data/`, `i18n/`, `tests/test_install.py` (`TransactionCase`, `post_install`), `README.md`, `.gitignore`.
- `AGENTS.md` and `CLAUDE.md` hold the development rules:
  - Odoo 19 conventions and naming;
  - an access rule for every model, and record rules when data is per user;
  - no raw SQL without parameters;
  - every feature comes with `TransactionCase` tests, and an `HttpCase` when it has views or controllers;
  - bump the manifest version on changes to data or models;
  - user-facing strings are translatable;
  - commits use the `[action] description` format.
- `.hermes/project.yaml`: toolchain `odoo`; `build` compiles the code; `test` is `ho-odoo-test {{name}}`;
  `test_environment` uses `.hermes/test-services.yaml`, a pinned `postgres:16` service; the Quality Gate requires
  tests, a full suite, and build.

## Real Odoo tests

- A new toolchain profile `workers/toolchains/odoo`: Odoo 19, pinned and with a verified checksum in
  `workers/versions.env`, installed into the image together with its Python dependencies.
- `ho-odoo-test <module>` creates a throwaway database on the test PostgreSQL service and runs `odoo -i <module>
  --test-enable --stop-after-init`. It fails on any test error or on an install error.

## Tests

- Unit: name validation, template rendering limits and placeholders, and `children` schema validation.
- Integration (fake `gh`): a child is created, registered, and scanned. A failure after `git init` or after
  `gh repo create` resumes from the recorded step. Creation is refused when the directory or the repository already
  exists.
- Docker: the odoo toolchain installs the scaffold and its test passes against the test PostgreSQL. A smoke test
  covers the full flow with a throwaway identity.

## Risks

- Writing into the parent's working tree: limited to a new directory under the declared ignored path.
- A partial failure leaves a local repository without a remote, or a remote without a project: `child_creations`
  records the step, and a retry continues from there.
- The odoo toolchain image is about 1 GB.
- v1 children may depend only on standard Odoo modules: sibling custom modules are not on the addons path yet.
- `job_finder` predates this: it can be linked as a child, and its rules and test environment added, as a later
  task.
