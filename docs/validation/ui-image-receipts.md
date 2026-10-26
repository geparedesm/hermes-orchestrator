# Dashboard UI image delivery receipts

T-10 UI design v3 (approval 97) remained pending because Dashboard image downloads did not record
image delivery. The approval handler correctly rejected the decision transaction when its required
receipts were absent, even though the person could see both images.

The Dashboard now acknowledges each pending design image after fetching its bytes. A same-origin
POST proxy forwards the receipt to the existing authenticated control-plane endpoint. Invalid names,
cross-origin mutations, failed downloads, and stale proposals cannot satisfy the delivery prerequisite.
Design approval remains a separate human decision; no approval or review attestation was fabricated.

Validation: 28 focused Dashboard tests passed, including execution of the browser download function
in Node for successful, failed, stale, and already approved proposals. Ruff, JavaScript syntax and
Git whitespace checks passed. The plugin files were deployed with a backup of their previous contents
in /tmp/hermes-ui-receipts-backup, followed by a Hermes restart. All six platform services were healthy,
the Dashboard auth endpoint returned 200, and T-10 retained its pending approval 97 for the person's decision.
