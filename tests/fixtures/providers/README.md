# Provider output fixtures

Raw stdout of the pinned CLIs (versions in `workers/versions.env`), used by the
adapter unit tests.

| File | Origin |
| --- | --- |
| `claude-auth-failure.jsonl` | Captured from `claude -p --output-format stream-json --verbose` 2.1.280 with an invalid `CLAUDE_CODE_OAUTH_TOKEN` (tool and command lists shortened). |
| `codex-auth-failure.jsonl` | Captured from `codex exec --json` 0.159.2 with no login. |
| `claude-success.jsonl` | Written by hand in the documented stream-json format (`system/init`, `assistant`, `user`, `result` with `structured_output`). Replace with a captured run after the first real login. |
| `codex-success.jsonl` | Written by hand in the documented `codex exec --json` format (`thread.started`, `item.*`, `turn.completed`). Replace with a captured run after the first real login. |
