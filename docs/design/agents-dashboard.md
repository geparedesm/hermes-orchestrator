# Agents in the Dashboard: usage, connect, and manage

Status: in two parts. PR A builds the frontend, the usage per agent, and pause and resume. PR B builds connect and
disconnect, revised after the Codex adversarial review (see the end). UI design: `ui/hermes-dashboard.pen` (frames "Orchestration — Home (final)" and "Orchestration — System").

## Usage per agent (session and week, with the reset time)

Where the numbers come from, as seen in the pinned CLIs:
- **Claude Code 2.1.280** emits `{"type":"rate_limit_event","rate_limit_info":{status, resetsAt, rateLimitType, utilization}}`
  in `stream-json`. `rateLimitType` is `five_hour` or `seven_day*`, and `resetsAt` is in epoch seconds (CLI schema).
- **Codex 0.159.2**: `exec --json` has no rate limits; it emits only thread, turn, and item events. Codex keeps
  `TokenCountEvent.rate_limits` (`primary` and `secondary`, each with `used_percent`, `window_minutes`, `resets_at`) in
  its session log under `$CODEX_HOME/sessions`, which is in-memory per execution. When the run ends, `ho-agent-run`
  copies only the last `rate_limits` object, with its numeric fields, to `/output/ho/rate-limits.json`. Files under
  `ho/` are raw provider output and are never stored as artifacts; only the adapter's bounded numbers are kept. If it is absent,
  nothing is shown. To be confirmed on the first real run before relying on it.

How the numbers are kept and shown:
- **Adapters** turn these into an allowlisted `RATE_LIMIT` result field: `{window: session|week, used_percent,
  resets_at}`. No other provider data is kept (invariant 4).
- **Storage:** migration `provider_usage (provider, identity, window, used_percent, resets_at, observed_at)`, upserted
  when an execution finishes.
- **Display:** the latest value, with its age ("as of 12 min ago"). After `resets_at` passes, it shows "reset".

## Connect and manage (Dashboard, human only)

The operator's Dashboard calls new plugin routes, which call the control plane, which calls the Agent Manager. These
are never Hermes LLM tools (AD-10).

**Connect Codex** (device code):
- The Agent Manager starts the documented login (`codex login --device-auth`). It uses the bootstrap container of
  `scripts/auth-login.sh`: the same hardening, only `cred-codex-<identity>` mounted, and the provider endpoints as the
  only egress.
- It reads the verification URL and the one-time code from the output and returns them as a login session (`WAITING`).
- The Dashboard shows the URL and code. When the container exits successfully, the credential is marked `READY`
  (`Credentials.mark_ready`).

**Connect Claude** (setup-token):
- The person runs `claude setup-token` themselves, as documented today, and pastes the token into a password field.
- The token travels in memory from the plugin to the control plane to the Agent Manager. It is never logged, stored in
  PostgreSQL, or echoed back.
- The Agent Manager runs the existing verification (one-turn request) in the bootstrap container and writes the token
  to the volume (mode 0600).

**Pause and resume:** a new `provider_settings (provider, identity, enabled)` table. While an agent is paused:
- routing gives it no new work, and no execution of it is created or dispatched (the launches queue);
- running work finishes; an unstarted execution keeps its worker slot and waits (rare: a pause right after it was created);
- its queued launches never hold back other work by priority;
- tasks it leads wait until it is resumed. Pausing never moves a lead: that would invalidate queued launches.
and running work finishes.

**Disconnect:** after a confirmation, the Agent Manager removes `cred-<provider>-<identity>` and the credential becomes
`DISCONNECTED`. It is refused while executions of that provider are active. `cred-*-default` is only ever removed by
this explicit person action.

**Only supported providers** can be connected (the adapter registry). A new kind of provider needs an adapter (code).

## Frontend (from the Pencil design, Midnight theme tokens)

- **Shared header:** title, subtitle, New task, tabs with a pending count, and the compact usage line (dot, name, bar,
  %, reset), present on every tab.
- **Home, Board, Approvals, Projects, System** as designed.
- **System:** agent cards with session and week, Connect an agent (dialog), Activity 24 h / 7 days (success rate,
  tokens, mean time), Capacity (workers, memory), services, and queue.

## Tests

- Adapters: Claude `rate_limit_event` parsing; the Codex `rate-limits.json` file; absent or malformed input ignored.
- Integration: usage upserted when an execution finishes; the read model with age and reset; pause excludes the
  provider from routing; disconnect is refused while executions are active.
- Agent Manager: the device-code session parses URL and code from fixed output; the setup-token path never writes the
  token outside the volume (fake Docker).

## Risks

1. **Token handling** in the Claude paste path. Keep it in memory only, redact it, never put it in events, and use
   size and format checks.
2. **A login container left running.** Give it a timeout, and the Agent Manager reaps it.
3. **Codex rate limits absent** from the session log. The Dashboard shows "—"; nothing is invented.
4. **Migration numbering** with parallel work (Gemini). Renumber on merge.

## Design review (Codex adversarial, needs-attention)

All three findings are accepted:
1. **Secret-safe errors at every hop** (PR B). The credential routes in the plugin, the control plane, and the Agent
   Manager get their own validation handlers that return fixed error codes, never the input or the provider's output.
   `AgentManagerClient` maps those codes without the response text. Tests send a sentinel token through an invalid
   format, an oversized input, a failed verification, and upstream errors, and check that it appears nowhere.
2. **A serialized credential lifecycle** (PR B).
   - `credential_refs` gets a state (`CONNECTING`, `READY`, `AUTH_REQUIRED`, `DISCONNECTING`, `DISCONNECTED`) and a
     login `generation`.
   - Launches are refused unless the state is `READY`, checked under the same Agent Manager lock that validates the
     credential volume (the existence check moves inside the lock).
   - Disconnect sets `DISCONNECTING`, cancels and removes pending login containers, refuses while executions are
     active, removes the volume under that lock, then sets `DISCONNECTED`.
   - A login completion is accepted only for the current generation and from `CONNECTING`. Stale callbacks are
     ignored.
   - Tests cover concurrent connect, disconnect, and launch, and a restart in the middle.
3. **Codex output path:** `ho/rate-limits.json`, as built (`OUT=/output/ho`). A test checks that a malformed file, or
   one with extra fields, never becomes an artifact.
