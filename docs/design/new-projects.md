# New projects from a platform template

Status: approved UI (`ui/hermes-dashboard.pen`, frame "Orchestration — Projects · New project"); building. Later,
more templates (API, web, mobile) add options to a Template selector in the same form.

## Behavior

- **The form.** Projects › New project asks for:
  - **Name**: `^[a-z][a-z0-9-]{1,40}$`; it is the folder `<projects root>/<name>` and the project slug.
  - **Description**: one plain paragraph.
  - **GitHub**: a private repository `<owner>/<name>`, checked by default and optional. The owner comes from the
    platform configuration (`new_projects.github_owner`).
- **CREATE_PROJECT approval.** The request pins everything the creation uses: the template name, the template tree
  SHA, the name, the parameters, whether to create the GitHub repository, the owner, the repository, the visibility,
  and the commit message. Nothing is written before a person approves it. Like CREATE_CHILD, only approvers may ask
  for it (AD-10), and it is never an LLM tool.
- **A durable operation, one recorded step at a time.** It uses the existing `child_creations` machinery,
  generalized:
  - **PUBLISHED.** Git Service renders the template into `<root>/<name>`. The directory is created with an exclusive
    `mkdir` relative to the opened root directory; files use `O_CREAT|O_EXCL|O_NOFOLLOW`. Then `git init -b main` and
    the first commit, with the operation id as a trailer (`Hermes-Operation`). A directory that exists without that
    evidence stops the operation in NEEDS_RECOVERY; nothing is adopted or deleted.
  - **PUSHED**, only when GitHub was chosen. The same `remote` logic as children: create the repository described
    with the operation id, adopt it only with that evidence, and push `main` only onto an absent branch.
  - **REGISTERED.** The project is registered (no parent) and scanned. The person approves its configuration
    (PROJECT_READY), as for any project.
- **Retry and abandon** as for children: a retry continues from the last recorded step; abandoning deletes nothing.

## The template (`templates/generic/` in this repository)

Copied read-only into the Git Service image (`/opt/ho/templates`). Git Service turns it into Git objects in a
private cache repository (`git hash-object -w` and `git write-tree`, with no commit), so a template is pinned by its
tree SHA and rendered by the same code as a parent's template: no links, no `..`, at most 200 files and 2 MiB.

Files:
- `README.md`: the name and the description;
- `AGENTS.md` and `CLAUDE.md` (`@AGENTS.md`): general rules for the agents. Code and documentation in English; every
  change with its tests; commits `[ACTION] Description`; no secrets in the repository; update the README. The first
  task picks the stack, creates the skeleton with passing tests, and proposes the real `.hermes/project.yaml`
  (build and test commands, Quality Gate) as a configuration change for the person to approve;
- `.hermes/project.yaml`: version 1, the project name, toolchain `generic`, no required tests yet;
- `.gitignore`.

Values: `{{name}}` and `{{description}}`. The description is validated as for children: no quotes, backslashes,
braces, `<`, `>`, `&`, or control characters.

## Data

Migration `0020`:
- `child_creations.parent_id` becomes nullable; a new `kind` column (`CHILD` or `PROJECT`); `github_repo` becomes
  nullable (no GitHub).
- A unique index on `destination` for projects (no parent).
- `CREATE_PROJECT` is added to the approval actions.

Signing: `sign_project` and `verify_project` use their own domain (`{"project": body}`) and keys. They can never stand
for a child creation or a merge.

## Interfaces

- Control plane: `POST /v1/projects/new` (approvers only), `POST /v1/projects/new/{operation_id}/{retry|abandon}`,
  `GET /v1/projects/new` (live creations).
- Git Service: `/v1/projects/plan` (pins the template tree and checks the destination), `/v1/projects/publish`, and
  `/v1/projects/remote`, each verifying the signed authorization.
- Plugin: matching routes. The Dashboard has the button and the form from the design; live creations appear above the
  projects table.

## Tests

- Git Service: publish into the root; refusal for an existing directory, a link at the destination, an invalid name,
  a changed template, or a forged or child token; a retry reuses its own directory; remote with a fake gh.
- Integration: request, approve, publish, (push), register, and scan; no GitHub when unchecked; non-approver
  refused; reject; NEEDS_RECOVERY when the folder exists; the approval details headline.

## Risks

1. **Writing at the projects root.** The name regex, `O_NOFOLLOW` component by component from the opened root, and
   exclusive creation.
2. **A template that drifts from the approval.** The tree SHA is pinned and checked again at publish.
3. **A GitHub repository that already exists.** It is adopted only with this operation's evidence.
