// Runs inside an isolated pinned provider image. Never print raw CLI output or credentials.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const readline = require('node:readline');

const fail = (status) => ({ status, windows: [] });
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
function window(kind, percent, reset, observed) {
  if (!finite(percent) || percent < 0 || percent > 100 || !finite(reset) || reset <= observed ||
      reset > observed + 8 * 86400) return null;
  return { window: kind, used_percent: percent, resets_at: reset, observed_at: observed };
}
function codexSnapshot(result, observed) {
  const bucket = result?.rateLimitsByLimitId?.codex ?? result?.rateLimits;
  if (!bucket || (bucket.limitId != null && bucket.limitId !== 'codex')) return fail('INVALID_RESPONSE');
  const windows = [];
  for (const key of ['primary', 'secondary']) {
    const w = bucket[key];
    if (!w) continue;
    const kind = w.windowDurationMins === 300 ? 'session' : w.windowDurationMins === 10080 ? 'week' : null;
    if (!kind) continue;
    const normalized = window(kind, w.usedPercent, w.resetsAt, observed);
    if (!normalized || windows.some((v) => v.window === kind)) return fail('INVALID_RESPONSE');
    windows.push(normalized);
  }
  return windows.length ? { status: 'OK', windows } : fail('INVALID_RESPONSE');
}
function claudeSnapshot(events, observed) {
  const final = events.findLast((e) => e.type === 'result');
  if (!final || final.is_error || final.local_command !== 'usage' || final.num_turns !== 0 ||
      Object.values(final.modelUsage || {}).length || final.duration_api_ms !== 0) return fail('INVALID_RESPONSE');
  const report = events.findLast((e) => e.type === 'assistant' && e.usage_report)?.usage_report;
  if (!report?.rate_limits || !Array.isArray(report.rate_limits.limits)) return fail('PROFILE_REQUIRED');
  const windows = [];
  for (const w of report.rate_limits.limits) {
    const kind = w.kind === 'session' ? 'session' : w.kind === 'weekly_all' ? 'week' : null;
    // Scoped model/surface pools are not the global quota used by the current router.
    if (!kind || w.scope?.model || w.scope?.surface) continue;
    const reset = typeof w.resets_at === 'string' ? Date.parse(w.resets_at) / 1000 : null;
    const normalized = window(kind, w.percent, reset, observed);
    if (!normalized || windows.some((v) => v.window === kind)) return fail('INVALID_RESPONSE');
    windows.push(normalized);
  }
  return windows.length ? { status: 'OK', windows } : fail('INVALID_RESPONSE');
}

function readCodex(observed) {
  return new Promise((resolve) => {
    const child = spawn('codex', ['app-server', '--listen', 'stdio://'], { stdio: ['pipe', 'pipe', 'ignore'] });
    let done = false, bytes = 0;
    const finish = (result) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.kill('SIGTERM');
      resolve(result);
    };
    const timer = setTimeout(() => finish(fail('TIMEOUT')), 25000);
    child.on('error', () => finish(fail('UNAVAILABLE')));
    child.on('exit', () => finish(fail('UNAVAILABLE')));
    child.stdin.on('error', () => finish(fail('UNAVAILABLE')));
    const send = (v) => child.stdin.write(JSON.stringify(v) + '\n');
    const lines = readline.createInterface({ input: child.stdout });
    child.stdout.on('data', (data) => { bytes += data.length; if (bytes > 2 * 1024 * 1024) finish(fail('INVALID_RESPONSE')); });
    lines.on('line', (line) => {
      if (done) return;
      let event; try { event = JSON.parse(line); } catch { return finish(fail('INVALID_RESPONSE')); }
      if (event.id === 1) {
        if (event.error || !event.result) return finish(fail('UNAVAILABLE'));
        send({ method: 'initialized', params: {} });
        send({ method: 'account/rateLimits/read', id: 2 });
      } else if (event.id === 2) {
        if (event.error) return finish(fail(event.error.code === -32001 ? 'AUTH_REQUIRED' : 'UNAVAILABLE'));
        finish(codexSnapshot(event.result, observed));
      }
    });
    send({ method: 'initialize', id: 1, params: { clientInfo: { name: 'hermes_orchestrator', title: 'Hermes quota monitor', version: '1' } } });
  });
}
function readClaude(observed) {
  return new Promise((resolve) => {
    const child = spawn('claude', ['-p', '/usage', '--verbose', '--output-format', 'stream-json', '--safe-mode',
      '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config', '--tools', '', '--prompt-suggestions', 'false'],
    { stdio: ['ignore', 'pipe', 'ignore'] });
    let bytes = 0, done = false;
    const events = [];
    const finish = (result) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.kill('SIGTERM');
      resolve(result);
    };
    const timer = setTimeout(() => finish(fail('TIMEOUT')), 25000);
    child.on('error', () => finish(fail('UNAVAILABLE')));
    const lines = readline.createInterface({ input: child.stdout });
    child.stdout.on('data', (data) => { bytes += data.length; if (bytes > 2 * 1024 * 1024) finish(fail('INVALID_RESPONSE')); });
    lines.on('line', (line) => {
      if (done) return;
      try { events.push(JSON.parse(line)); } catch { finish(fail('INVALID_RESPONSE')); }
    });
    child.on('close', () => finish(claudeSnapshot(events, observed)));
  });
}

async function main(provider) {
  if (!['claude', 'codex'].includes(provider)) return fail('INVALID_RESPONSE');
  const stored = `/run/ho-credentials/${provider}/${provider === 'codex' ? 'auth.json' : '.credentials.json'}`;
  if (!fs.existsSync(stored)) return fail(provider === 'claude' && fs.existsSync('/run/ho-credentials/claude/oauth_token')
    ? 'PROFILE_REQUIRED' : 'AUTH_REQUIRED');
  const original = fs.readFileSync(stored);
  let credential; try { credential = JSON.parse(original); } catch { return fail('AUTH_REQUIRED'); }
  if (provider === 'claude' && !credential.claudeAiOauth?.scopes?.includes('user:profile')) return fail('PROFILE_REQUIRED');
  const dir = provider === 'codex' ? process.env.CODEX_HOME : process.env.CLAUDE_CONFIG_DIR;
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const working = path.join(dir, path.basename(stored));
  fs.writeFileSync(working, original, { mode: 0o600 });
  const observed = Date.now() / 1000;
  const result = await (provider === 'codex' ? readCodex(observed) : readClaude(observed));
  // Token refresh and interactive renewal must never overwrite one another. A changed identity also invalidates
  // the probe result. The lock is shared with the execution runners and the login bootstrap.
  const before = crypto.createHash('sha256').update(original).digest('hex');
  const save = `const fs=require('fs'),c=require('crypto');const [s,d,b]=process.argv.slice(1);
    if(c.createHash('sha256').update(fs.readFileSync(d)).digest('hex')!==b)process.exit(4);
    const v=fs.readFileSync(s);if(c.createHash('sha256').update(v).digest('hex')!==b){
      const t=d+'.quota-'+process.pid;fs.writeFileSync(t,v,{mode:0o600});fs.renameSync(t,d);}`;
  const saved = spawnSync('flock', ['-w', '5', path.join(path.dirname(stored), '.lock'), process.execPath, '-e', save, working, stored, before],
    { stdio: 'ignore', timeout: 7000 });
  return saved.status === 0 ? result : fail('BUSY');
}
module.exports = { codexSnapshot, claudeSnapshot, main };
if (process.env.HO_QUOTA_PROBE === '1') {
  main(process.argv[1]).then((result) => { process.stdout.write(JSON.stringify(result)); process.exit(0); })
    .catch(() => { process.stdout.write(JSON.stringify(fail('UNAVAILABLE'))); process.exit(0); });
}
