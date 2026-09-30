# Phase 4 Validation: Claude/Codex Workers

**Date:** 2026-09-30  
**Machine:** MacBook Pro (Apple Silicon), Docker Desktop 29.4.0, `linux/arm64`, Docker VM with 12 CPUs and 8 GB RAM  
**Scope:** PHASES.md Phase 4 and its completion criteria: *both providers execute isolated tasks through the same contract; credentials remain outside Git, PostgreSQL, logs, manifests, and normal backups.*

## What was built

| Deliverable | Location |
| --- | --- |
| `AgentAdapter` contract, `ClaudeAdapter`, `CodexAdapter`: execution plans, event allowlists, normalized results, usage, failure classes, health | `packages/ho_core/src/ho_core/adapters/` |
| Structured result every agent execution returns | `schemas/agent-result.schema.json` |
| Composable images: `agent-base` → toolchain layers (`node`, `python`, `php`, `java`, `flutter`) → provider layers (`claude`, `codex`) | `workers/`, `scripts/build-images.sh`, `workers/versions.env` |
| In-container runners and login scripts | `workers/providers/*/ho-agent-run`, `ho-auth-login` |
| Credential Broker: login bootstrap, credential status, `AUTH_REQUIRED` and resume | `scripts/auth-login.sh` (`make auth-claude`, `make auth-codex`), `services/control-plane/.../credentials.py` |
| Secrets Broker v1: file store, grant by reference, in-memory delivery, redaction | `services/agent-manager/.../secrets.py`, `docker_ops.py` |
| Agent executions in the control plane: prompts through adapters, resume, usage records, task environment cleanup | `services/control-plane/.../executions.py`, migration `0003` |
| Operator CLI: `ho agent run`, `ho execution resume`, `ho auth status`, `ho auth ready` | `services/control-plane/.../cli.py` |

## Official interfaces used

Every flag, variable, path, and host comes from the providers' current documentation or the pinned CLI's `--help`, checked on 2026-09-30:

| Interface | Source |
| --- | --- |
| Claude Code subscription token (`claude setup-token`, `CLAUDE_CODE_OAUTH_TOKEN`), `CLAUDE_CONFIG_DIR` | [Authentication](https://code.claude.com/docs/en/authentication) |
| `claude -p`, `stream-json`, `--json-schema`, `--resume`, `--permission-mode dontAsk`, `--permission-prompts none` | [Run Claude Code programmatically](https://code.claude.com/docs/en/headless), [CLI reference](https://code.claude.com/docs/en/cli-reference) |
| Keeping repository hooks, settings, and `.mcp.json` out of `-p` runs (`--setting-sources user`, `disableAllHooks`) | [Permissions: what runs before you trust a folder](https://code.claude.com/docs/en/permissions) |
| Claude Code hosts | [Network configuration](https://code.claude.com/docs/en/network-config) |
| Codex login (`codex login --device-auth`), `auth.json` under `CODEX_HOME`, `cli_auth_credentials_store`, `forced_login_method` | [Codex authentication](https://learn.chatgpt.com/docs/auth), [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) |
| `codex exec --json`, `--output-schema`, `-o`, `exec resume`, `--ignore-user-config`, `--ignore-rules` | [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), `codex exec --help` (0.159.2) |
| Untrusted projects skip project `.codex/` config, hooks, and rules | [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) (`projects.<path>.trust_level`) |
| `danger-full-access` when the container is the security boundary; Codex hosts | [Agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security) |

## Evidence

| Check | Command | Result |
| --- | --- | --- |
| Unit tests (adds adapters with captured CLI output, planning, secret store) | `make test-unit` | 142 passed |
| Integration tests (adds 11 agent, credential, and secret tests) | `make test-integration` | 48 passed |
| Agent Manager and real CLIs against the real Docker daemon | `make test-docker` | 38 of 39 passed; see *Network note* |
| Phase 4 end-to-end on the Compose stack | `make smoke-phase4` | 29 of 29 checks passed |
| Phase 3 end-to-end (regression) | `make smoke-phase3` | 20 of 21 passed; see *Network note* |
| Phase 2 end-to-end (regression) | `make smoke` | 21 of 21 checks passed |
| Lint, schemas, profiles | `make lint validate-schemas` | clean |
| Images (`linux/arm64`) | `make images HO_TOOLCHAINS="generic node python php java flutter node,python"` | 29 images pinned; every toolchain runs in a hardened container (PHP 8.2, Composer 2.5, OpenJDK 17, Maven 3.8, Flutter 3.47.5 with Dart 3.13, Node 22, Python 3.11) |
| Images (`linux/amd64`, emulated) | `docker buildx build --platform linux/amd64 ...` | Codex layer builds and runs (`x86_64`, 0.159.2). The Claude Code native binary aborts under Docker Desktop's QEMU emulation (Rosetta is disabled on this machine); validate it on an amd64 Linux host |

**Network note.** Two checks fetch `https://example.com` through the egress proxy. On this network the router's DNS server (10.0.0.138) currently answers `NXDOMAIN` for `example.com` (a public resolver, 1.1.1.1, resolves it), so the proxy cannot resolve the name and those two checks fail. Every other egress check passes, including real traffic to OpenAI and Anthropic. Both checks passed in Phase 3 and need no code change; rerun them on a network that resolves `example.com`.

### Real runs with the operator's subscriptions

After `make auth-codex` and `make auth-claude` (operator's accounts), one DEVELOPER execution per provider ran on `hello-api`, a small Python library in `~/HermesProjects`, with `PROVIDER_ONLY` egress and the project's `python` toolchain image. Both executions had been started before the logins, failed as `AUTH`, and waited in `AUTH_REQUIRED`; each login resumed its task and reran the assignment automatically.

| | Codex 0.159.2 (`codex-python`) | Claude Code 2.1.280 (`claude-python`) |
| --- | --- | --- |
| Assignment | Add `farewell()` with a unit test, run the tests, commit | Add `shout()` with a unit test, run the tests, commit |
| Execution | `SUCCEEDED`, structured result `completed`, tests passed | `SUCCEEDED`, structured result `completed`, tests passed |
| Commit in the workspace clone | `f20de8a Add farewell function and unittest` | `e98938a Add shout function with unit test` |
| Usage recorded | 1 turn, 76,253 input (59,648 cached) and 632 output tokens, 54 s | 9 turns, 1,343 output tokens, 16 s (CLI cost estimate 0.11 USD; subscription, not billed per request) |
| Egress | Allowed: `chatgpt.com`. Denied at startup: `sdmntpr*.oaiusercontent.com` (5 connections; not in the official host list, not needed for the task) | Allowed: `api.anthropic.com` only |
| Stored artifacts | `result.json`, `events.jsonl` (commands with their class, file changes, usage counts), `egress.jsonl`; no reasoning, messages, or raw stream | same |

This confirms the implement → test → commit cycle inside one execution for both providers. Neither agent listed its commit SHA in the result's `commits` field although both committed; from Phase 5 the Git Service reads commits from the workspace instead of relying on the agent's report.

### Real CLIs with invalid logins

The pinned CLIs ran inside hardened workers (UID 10001, read-only root, no capabilities) with `PROVIDER_ONLY` egress and deliberately invalid logins (`tests/docker/test_phase4.py`, `scripts/smoke-phase4.sh`):

- **Codex 0.159.2** reached `chatgpt.com` and `auth.openai.com` through the proxy, was rejected with HTTP 401, and the adapter classified the failure as `AUTH`; the session ID was captured.
- **Claude Code 2.1.280** reached `api.anthropic.com` through the proxy, was rejected with HTTP 401 (`authentication_failed`), and was classified as `AUTH`; the session ID was captured for resume.
- No other host was contacted and nothing was denied, so the recorded provider domains are sufficient for these flows. The invalid token did not appear in collected logs.

A successful model run needs a real subscription login, which only the operator can do (see *Operator review*).

### Credentials stay out of Git, PostgreSQL, logs, manifests, and backups

- Login material lives only in `cred-<provider>-<identity>` volumes (never backed up, ARCHITECTURE §11). PostgreSQL holds only `credential_refs` (provider, identity, status, last verification, last error).
- Claude's token volume is mounted read-only; each CLI's configuration directory is a fresh in-memory directory per execution, so a worker cannot leave settings, hooks, or instructions for later executions.
- A provider image runs only for an execution holding that provider's credential; each volume is mounted only at its own provider's path (tests: `test_image_must_match_the_granted_provider`, `test_provider_images_run_their_cli_with_the_right_credential_mount`).
- Commands that touch `/run/ho-credentials` are flagged (`COMMAND_HIGH_RISK`, advisory).
- The smoke tests use throwaway identities (`HO_PROVIDER_IDENTITY`), so they never touch the operator's login volumes. The Phase 3 smoke test previously created and deleted `cred-codex-default`; it now uses its own identity.

### Secrets

- Granted by reference only (`project/environment/NAME`), for secrets declared in the project configuration, granted environments, and roles allowed to hold secrets; production secrets need an approval (integration test).
- Delivered into an in-memory mount as mode 0600 files owned by the worker, or as environment variables when the project declares `delivery: env`; absent from the container's configuration and from the execution spec in PostgreSQL.
- Replaced with `[REDACTED:<NAME>]` in logs and output files; a missing secret fails the launch.

### Adapters and results

- Both providers use the same contract: prompt in `/run/ho-input/prompt.md`, raw output in `/output/ho/`, a normalized `result.json` validated against `agent-result.schema.json`, allowlisted `events.jsonl`, and a `usage_records` row.
- Reasoning items, assistant text, and tool outputs are dropped; the raw stream is never stored (tests use fixtures containing reasoning text and assert it is absent from every artifact).
- Failure classes: `AUTH` (401/403, missing login), `QUOTA` (429, usage limits), `TRANSIENT` (5xx, overload, network), `TASK` (max turns, invalid structured result), `UNKNOWN`.

### AUTH_REQUIRED and resume

- A missing login (Agent Manager 424) or a 401 from the provider fails the execution as `AUTH`, marks the identity `AUTH_REQUIRED`, moves the task to `AUTH_REQUIRED`, and emits an attention notification.
- `ho auth ready` (run automatically at the end of `make auth-<provider>`) marks the identity `READY`, resumes the waiting tasks, and continues each interrupted execution once: resuming its provider session when one was captured, otherwise running the assignment again. Shown end to end with the real Codex CLI in `smoke-phase4`.
- Sessions for resume live in a per-task session volume and are removed when the task ends.

## Decisions made in this phase

| Decision | Reason |
| --- | --- |
| Claude Code authenticates with a `claude setup-token` subscription token, mounted read-only | Documented for scripts and non-interactive use; it does not refresh, so executions can share it without write access, and no writable provider home is shared between executions. |
| Codex uses `auth.json` from `codex login --device-auth`, copied into a per-execution `CODEX_HOME`, with a locked compare-and-write of refreshed tokens | Codex refreshes tokens during use; a writable shared `CODEX_HOME` would also persist configuration and instructions across executions. |
| Codex runs with `sandbox_mode="danger-full-access"`, `approval_policy="never"` | Its bubblewrap sandbox cannot work in a container without capabilities; the documented alternative is to make the container the boundary, which ours already is. |
| Repository-defined CLI configuration is skipped (OI-02) | `--setting-sources user` and hooks disabled for Claude Code; untrusted workspace for Codex. Instruction files (`CLAUDE.md`, `AGENTS.md`) are still read. |
| One structured result schema for both providers | Same contract; the adapters normalize it and the control plane decides success. |
| Provider CLIs installed from the official npm packages at pinned versions | Official distribution with exact version pinning; Claude Code's package ships a native binary. |
| Secrets delivered by `exec` into a tmpfs after start, with the command waiting | Docker cannot copy files into a tmpfs before start, and a volume would persist the value on disk. |
| Configurable provider identity (`HO_PROVIDER_IDENTITY`) | Lets tests and additional stacks use separate logins without touching the operator's. |
| Toolchain images compose by chaining layers; image names follow the sorted profile set | One image per needed combination, no single giant image (MASTER_SPEC section 18). |

## Known limitations

- The first Claude login saved a truncated token: terminals wrap the long `setup-token` output, and the script read one line. Fixed: it now joins every pasted line, checks the format, and saves the token only after the verification request succeeds.
- Codex contacts `*.oaiusercontent.com` at startup; the host is not in its official network documentation, the task succeeded without it, and it stays denied under `PROVIDER_ONLY`.
- The success fixtures (`tests/fixtures/providers/*-success.jsonl`) were written in the documented formats; replace them with captured runs after the first real login.
- Whether a Codex token refresh invalidates the copy another running execution holds (OI-03) could not be measured without a real login. The write-back never overwrites a newer stored login; if refreshes invalidate older copies, use one identity per concurrent Codex worker.
- Codex's device-code login must be enabled for the ChatGPT account; the browser callback flow is not offered because it needs a port on the host.
- `usage_records` holds the provider's reported units; Docker CPU and memory statistics and budget charging by provider usage come later (Phases 8 and 11).
- Provider health is derived from pinned images, credential volumes, and the last outcome; there is no periodic live probe (it would consume subscription usage).
- Images are pinned by local image ID per machine; the approval-based promotion workflow of section 19 is Phase 11.
- Flutter's SDK cache is read-only at run time; a wrapper recreates `bin/cache` in `/tmp` with links (see `workers/toolchains/flutter`). Resolving Dart and Flutter packages offline needs the dependency caches of section 71.
- The Claude Code image for `linux/amd64` was not run: its native binary aborts under QEMU emulation on Apple Silicon. The Codex image ran on `linux/amd64`.
- Worker home directories are 256 MiB in memory; large dependency installs need the dependency caches of section 71 (later phase).
- Linux has not been run yet.

## Operator review

Done on 2026-09-30: stack rebuilt with `make up`, both providers logged in with `make auth-claude` and `make auth-codex`, and one real agent execution per provider succeeded (see *Real runs with the operator's subscriptions*).
