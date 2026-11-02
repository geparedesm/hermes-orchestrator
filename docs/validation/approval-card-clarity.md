# Quality Gate exception card validation

## Problem

Decision #138 (T-13, WhatsApp payment reminders) said only "This change needs a closer look before it is added: a
sensitive change has too few tests." Every test had passed. The actual reasons were: payment files changed and the
project has no type check (section 56), two commands matched the privilege rule CMD-H09 only because the Spanish word
"su" appeared in message text (section 58), and two open MEDIUM review findings still failed the gate. Approving the
exception would not have added the change, and the task was already running again. None of this was visible.

## Change

- `approval_details._high_risk` (quality gate exceptions) now returns `passed`, `reasons` (one plain explanation per
  exception reason, with the sensitive areas, the missing checks and the flagged commands), `blockers` (open findings
  that fail the gate whatever the decision), `minor_findings`, `note` (the task is no longer waiting for this decision),
  a rule-based recommendation and precise `if_yes`/`if_no`.
- `ho_core.policy.commands.classify` keeps the text a rule matched; `COMMAND_HIGH_RISK` events store it as `match`
  (redacted, 160 characters), because the stored command is cut to 200 characters and may not contain the match.
- Dashboard: `GateException` renders the design `Approval card #138 · v1 · After` in `ui/hermes-dashboard.pen`
  (frames `v1 · Before`, `v1 · After`, `v1 · Changes`) in both views; the recommendation is no longer hidden in Simple
  view for this card. Chat wording for section 56 changed to "a sensitive change missing some required checks".
- The same `.pen` holds `UI design review · v1 · Before/After/Changes`, the design for the `changes.png` rule
  (separate branch `codex/pen-design-changes-frame`).

## Automated checks

- Unit suite: **459 passed**. Integration suite: **327 passed** (new: `test_a_gate_exception_explains_each_reason_what_passed_and_what_still_blocks`,
  `test_a_high_risk_classification_keeps_the_text_it_matched`).
- Ruff and `node --check` passed.
- Browser Runner (networking disabled, every request intercepted, React 18.3.1 UMD bootstrap) rendered the Decisions view
  with the #138 data: all sections present, outcomes shown once in Advanced view, no overflow at 390 px, no page errors.

Nothing was deployed, no approval was decided and no task state was changed.
