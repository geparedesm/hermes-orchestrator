# Child projects from a template (Odoo modules from odoo-base)

Status: built (child projects, CREATE_CHILD approval, odoo toolchain). A parent project (odoo-base) creates child projects (custom Odoo modules) from a template it
versions. Each child is its own repository, GitHub repository, and platform project, like `job_finder` today.

## Data

- `project.yaml` of the parent gains `children`: `path` (e.g. `custom-addons`), `template` (a directory in the
  parent's repository, e.g. `.hermes/templates/odoo-module`), `name_pattern` (`^[a-z][a-z0-9_]{1,40}$`, an Odoo
  technical name), and `github` (`owner`, `repo_name` such as `odoo-{name}`, `private: true`).
- Migration `0014`:
  - `projects.parent_project_id` (nullable FK);
  - a `CREATE_CHILD` approval action;
  - a `child_creations` table with the pinned values, the step, the evidence, and the state
    (`PENDING`, `RUNNING`, `DONE`, `NEEDS_RECOVERY`, `FAILED`), unique per destination and per `owner/repo`.
- Template files may use `{{name}}`, `{{title}}`, `{{description}}`, and `{{odoo_series}}` (from the parent config).

## Flow (human principals only; never an LLM tool, AD-10)

1. **Request** `POST /v1/projects/{slug}/children` (name, title, description). The parent must be PROJECT_READY and
   declare `children`. The request pins everything the creation will use: parent HEAD SHA, the template tree's
   SHA, the parameters, the destination path, `owner/repo`, and the visibility. It creates a `CREATE_CHILD`
   approval that shows exactly that ("Create module job_alerts as private repo geparedesm/odoo-job_alerts from
   odoo-base@1a2b3c"). Nothing touches disk or GitHub before you approve it.
2. **Durable operation** first. On approval, `child_creations` records the operation (id, the pinned values, the
   step, and evidence) before any effect. It is unique per destination path and per `owner/repo`. Git Service
   receives a signed authorization bound to those values and re-verifies it on every call, including retries
   (as for merges, invariant 1).
3. **Local publish, exclusive and confined.** `children.path` must:
   - be relative and resolve inside the parent;
   - have no symlink in any path component (each component checked with `lstat` and opened with `O_NOFOLLOW`);
   - not be `.git` or `.hermes`;
   - be ignored by the parent's Git (`git check-ignore`) and contain no tracked entries.
   Git Service renders the template from the pinned SHA's Git objects (no symlinks, no `..`, at most 200 files and
   2 MiB). It creates the destination with an exclusive `mkdir` that fails if the directory exists, so a concurrent
   human directory wins. Files are written relative to that directory's descriptor with `O_CREAT|O_EXCL|O_NOFOLLOW`.
   Then it runs `git init -b main` and makes the first commit; that commit's SHA becomes the step's evidence.
4. **Remote, in separate steps with proof of ownership.**
   - Create: `gh repo create <owner>/<repo> --private --description "hermes-orchestrator child <operation id>"`,
     without pushing.
   - If the answer is lost and a retry finds the repository, it is adopted only when its description carries this
     operation's id, it is empty, or its `main` equals the recorded commit. Otherwise the operation stops in
     `NEEDS_RECOVERY` and tells you why; an unknown repository is never adopted and nothing is deleted.
   - Push: add `origin`, then push `main` only when the remote branch is absent or already equals the recorded
     commit. A brand-new repository has no protected branch history, so this first push is not a merge into a
     protected branch.
5. **Register and scan.** The control plane registers the child with `parent_project_id` and starts the usual
   scan, so you approve its configuration (PROJECT_READY). A notification says it is ready.

Each step records its result before the next one starts, and a retry continues from the last recorded step.
Ambiguous states (a directory or repository without this operation's evidence) need an explicit recovery; the
platform never guesses.

The Dashboard project card shows **New module** only for ready parents that declare `children`. The pending
creation appears in Needs you, and the child appears under its parent.

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
- `ho-odoo-test <module>` builds a temporary addons directory in which `<module>` points only at `/workspace`, so
  Odoo finds `<addons_path>/<module>` while the repository keeps `__manifest__.py` at its root. It sets
  `--addons-path` to Odoo's standard addons plus that directory, and creates a throwaway database on the test
  PostgreSQL service. Then it runs `odoo -i <module> --test-enable --stop-after-init`.
- It fails when:
  - the module was not discovered;
  - the module is not installed afterwards (checked in `ir_module_module`);
  - no test ran;
  - any test failed or errored.

## Tests

- Unit: name validation, template rendering limits and placeholders, and `children` schema validation.
- Integration (fake `gh`):
  - A child is created only after its `CREATE_CHILD` approval, then registered and scanned.
  - A changed parent HEAD or configuration invalidates the approval.
  - A lost answer after `gh repo create` is adopted only with this operation's evidence; a foreign repository leads
    to `NEEDS_RECOVERY`.
  - A crash before each step's result is recorded is retried safely.
  - Creation is refused for an existing or concurrently created directory, a symlinked component, a tracked or
    non-ignored path, or a push onto a different `main`.
- Docker, with the real `/workspace` mount:
  - the scaffold is installed and its test ran and passed;
  - a module that is not discovered and a failing test both fail the run.
- A smoke test covers the full flow with a throwaway identity.

## Risks

- Writing into the parent's working tree is confined to a new directory that is ignored by the parent's Git, created
  exclusively, and reached without following symlinks.
- Codex's adversarial review of this proposal asked for four changes, now part of the design:
  - an operation recorded before any effect, with proof of ownership when reconciling;
  - a confined, exclusive destination;
  - an approval that pins the parent SHA, the parameters, and the repository;
  - an addons layout in which Odoo can find the module.
- The odoo toolchain image is about 1 GB.
- v1 children may depend only on standard Odoo modules: sibling custom modules are not on the addons path yet.
- `job_finder` predates this: it can be linked as a child, and its rules and test environment added, as a later
  task.
