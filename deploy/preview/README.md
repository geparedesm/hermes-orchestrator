# Shared human acceptance environment

The current deployment serves `https://test.gp-developer.com` through its own Cloudflare tunnel.
It authenticates the existing Hermes `operator`, then provisions a separate session in the isolated Odoo copy.
The preview shows a version marker and a link back to Hermes. Source Odoo is not connected to preview networks.

## Components

- `compose.yaml`: isolated PostgreSQL, Odoo, authenticated gateway and Cloudflare connector.
- `anonymize_odoo.py`: installed-module policy, replacement of contacts/CVs/documents, credential reset and neutralization.
- `seed_odoo.py`: synthetic rental contract and payment scenarios, without replacing existing test edits.
- `controller.py`: operator-side image preparation from pinned Git commits and activation of a shared candidate.
- `hermes-preview-controller.service`: persistent user service; the existing user manager has lingering enabled.
- `operator_api.py`: invokes the configured internal operator identity without exporting its token to the host.
- `runtime.env`, `current.json`: local runtime state, excluded from Git. Secrets live under `secrets/preview/`.

The controller supports the current Odoo parent and child-module repositories. Other application families require
an explicit deployment adapter; they must never be approved merely because no preview is configured.

## Lifecycle

1. Tasks pass their existing independent reviews, tests and Quality Gate.
2. The controller selects at most one ready task per repository, batching compatible modules together.
3. It exports exact commits, builds the candidate, records its image digest and source/module commits, and verifies
   that the task heads did not change during the build.
4. It installs/upgrades modules in the private preview database and executes any reviewed
   `.hermes/preview-seed.py` hooks in the Odoo shell. Hooks must be idempotent and use synthetic data only.
5. After application health succeeds, the candidate becomes READY and the normal outbox delivers its test URL.
6. A navigation by the authenticated operator records TESTING. No merge occurs on navigation.
7. One explicit approval authorizes all tasks listed in that shared version. Every member approval is validated
   and consumed in one database transaction. Git merges across repositories are separate operations: failure in one
   repository does not claim that a multi-repository release was atomically applied.
8. The version stays fixed until explicit retirement, a request for corrections, or completion of all approved tasks.
   Each merge still requires the existing signed exact-commit authorization and post-merge verification.

A shared candidate cannot contain two tasks for the same repository. Those tasks queue, avoiding target-head changes
between two approvals within the same repository. If one member changes, the group must be prepared and tested again.

## Operator commands

```bash
systemctl --user status hermes-preview-controller.service
python3 deploy/preview/controller.py --once
docker compose --env-file deploy/preview/runtime.env -f deploy/preview/compose.yaml ps
docker compose --env-file deploy/preview/runtime.env -f deploy/preview/compose.yaml logs --tail 50
```

The persistent controller already owns `controller.lock`; stop the service before running a manual controller cycle.
Do not run a manual cycle concurrently with the daemon. Failed preparations back off instead of repeatedly using agents.

## Refreshing source data

Refreshing is a deliberate maintenance operation that replaces test edits. Stop the controller and retire any active
candidate before doing it. Stop the preview application, copy source PostgreSQL directly to the isolated database,
then run Odoo neutralization and `anonymize_odoo.py` before allowing the application to serve again. Do not copy the
source filestore: original uploaded documents are removed and replaced with generated synthetic documents.

`odoo_entrypoint.py` refuses to serve a database without the anonymization marker. Its fixed connection always targets
`db/preview`, never the production database. Changes to installed modules outside the reviewed policy require extending
the anonymization inventory before a refreshed copy can be published. Candidate upgrades do not refresh source data.

## Recovery

The pre-migration control-plane dump, recorded original image reference and deployed-image digest are in private
`backups/feature-delivery/`. The old untagged image is no longer resolvable locally; rebuild its source revision if
that image is needed for recovery. The running release also has the `feature-delivery-20261019` tag.
The migration is additive; older code can run with its extra tables. Preserve source Odoo and its database.
For a failed candidate, keep it unavailable and inspect the controller log. Never attest it READY to work around a
failed build or module upgrade. The final approval and Git Service still independently reject stale commits.
