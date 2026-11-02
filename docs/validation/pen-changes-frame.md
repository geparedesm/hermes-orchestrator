# pen.dev Changes frame validation

## Rule

Every pen.dev design version shows its changes at a glance: `vN · Before`, `vN · After` and `vN · Changes` frames
side by side; the Changes frame marks each changed element with a numbered `#E5007E` outline and a legend whose numbers
match the numbered summary. Recorded in `CLAUDE.md` (Hermes's own designs) and enforced for orchestrated projects.

## Change

- `ui_designs.DESIGN_INSTRUCTIONS`: the designer draws the frames and exports `changes.png`.
- Collection requires `design/changes.png` (a design without it fails and never requests an approval). It is in the
  manifest, so the approval's manifest hash covers it, but not in the implementation package: developers and reviewers
  receive only the clean `desktop.png`, `mobile.png`, `design.md` and `design.pen`.
- Approval needs `changes.png` delivered (chat) or fetched (Dashboard) too; the operator attestation covers it.
  Designs collected before the rule have no `changes.png` and keep working with two images.
- Chat delivery sends the changes image first. The Dashboard shows the numbered summary, the changes image wide, then
  the clean images labelled "what gets built" (design `UI design review · v1 · After` in `ui/hermes-dashboard.pen`,
  shipped with `codex/approval-card-clarity`).

## Automated checks

- Unit suite: **459 passed**. Integration suite: **329 passed** (new: designs without `changes.png` are refused,
  approval waits for the changes image, legacy designs still approve, developers never receive `ui-changes.png`;
  chat sends the changes first).
- Ruff and `node --check` passed.
- Browser Runner (networking disabled, every request intercepted) rendered a pending design with and without
  `changes.png`: order, numbered summary, Changes tab in the full-size viewer, receipts for all shown images, approval
  enabled after loading, no page errors.

Nothing was deployed and no design or approval was decided.
