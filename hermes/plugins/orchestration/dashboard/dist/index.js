(function () {
  "use strict";
  // hermes-orchestrator Dashboard tab (docs/design/phase-10.md, docs/hermes.md). What needs a person comes first;
  // tasks move through five plain columns; a side panel shows one task without leaving the board. State changes
  // only through the platform. Data comes from /api/plugins/orchestration/, which proxies the orchestrator Task API
  // behind Hermes's login. Colors come from the Hermes theme variables, so the tab follows the active theme.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = React;

  // Mirrors ho_core.attachments; the server checks everything again.
  const ATTACH = { maxFiles: 10, maxFile: 10 * 1024 * 1024, maxTotal: 25 * 1024 * 1024,
    accept: ".txt,.md,.csv,.tsv,.json,.yaml,.yml,.xml,.py,.js,.ts,.sql,.log,.png,.jpg,.jpeg,.gif,.webp,.pdf" };
  const POLL_MS = 10000;

  // ------------------------------------------------------------ vocabulary

  const COLUMNS = [
    { id: "todo", title: "To do", hint: "Waiting for an agent", states: ["BACKLOG", "READY", "QUEUED"] },
    { id: "working", title: "Working", hint: "Agents are on it", states: ["PLANNING", "RUNNING", "FIX_REQUIRED"] },
    { id: "checking", title: "Checking", hint: "Tests, reviews, and merge", states: ["TESTING", "REVIEW", "QUALITY_GATE", "READY_FOR_MERGE", "MERGING", "VERIFYING"] },
    { id: "you", title: "Needs you", hint: "Waiting for a person", states: ["APPROVAL_REQUIRED", "AUTH_REQUIRED", "PAUSED_BUDGET", "PAUSED", "BLOCKED"] },
    { id: "done", title: "Done", hint: "Last 7 days", states: ["DONE", "CANCELLED", "FAILED"] },
  ];
  const STATUS = {
    BACKLOG: "Waiting to start", READY: "Ready to start", QUEUED: "In the queue for an agent",
    PLANNING: "Planning the work", RUNNING: "Agents are working", FIX_REQUIRED: "Fixing what the checks found",
    TESTING: "Running the tests", REVIEW: "Being reviewed by another agent", QUALITY_GATE: "Final quality check",
    READY_FOR_MERGE: "Ready to merge", MERGING: "Merging", VERIFYING: "Checking the merged result",
    APPROVAL_REQUIRED: "Waiting for your decision", AUTH_REQUIRED: "An agent needs to sign in again",
    PAUSED_BUDGET: "Out of budget", PAUSED: "Paused", BLOCKED: "Stuck: needs a look",
    DONE: "Done", CANCELLED: "Cancelled", FAILED: "Failed",
  };
  const WAITING = ["APPROVAL_REQUIRED", "AUTH_REQUIRED", "PAUSED_BUDGET", "PAUSED", "BLOCKED"];
  const FINISHED = ["DONE", "CANCELLED", "FAILED"];
  const STAGES = ["Plan", "Build", "Test", "Review", "Merge"];
  const STAGE_OF = { PLANNING: 0, RUNNING: 1, FIX_REQUIRED: 1, TESTING: 2, REVIEW: 3, QUALITY_GATE: 3,
    READY_FOR_MERGE: 4, MERGING: 4, VERIFYING: 4, DONE: 5 };
  const COUNTERS = { runtime_minutes: "Working time (min)", agent_launches: "Agent runs", retries: "Retries",
    review_cycles: "Review rounds", subtasks: "Subtasks", provider_usage_units: "Usage units" };
  const REQUIREMENTS = { tests: "Tests pass", verification: "Tests pass", review: "Independent review",
    cross_review: "Independent review", requirements: "Requirements met", no_blocking_findings: "No blocking findings",
    no_conflicts: "Up to date with the target branch", no_policy_violations: "No risky commands",
    ci_checks: "CI checks", docs_updated: "Documentation updated", exception_approval: "Your approval for exceptions" };
  const KEY_EVENTS = ["TASK_CREATED", "PLAN_VERSIONED", "SUBTASK_ACCEPTED", "INTEGRATION_COMPLETED", "INTEGRATION_CONFLICT",
    "TEST_PASSED", "TEST_FAILED", "REVIEW_PASSED", "REVIEW_FAILED", "QUALITY_GATE_EVALUATED", "READY_FOR_MERGE", "PR_CREATED",
    "MERGE_COMPLETED", "TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED", "BLOCKED", "PAUSED_BUDGET", "AUTH_REQUIRED",
    "APPROVAL_REQUIRED", "APPROVAL_DECIDED", "BUDGET_RAISED", "REQUIREMENTS_REVISED", "TASK_STATE_CHANGED"];
  const EVENT_TONE = { SUBTASK_ACCEPTED: "good", INTEGRATION_COMPLETED: "good", TEST_PASSED: "good", REVIEW_PASSED: "good",
    READY_FOR_MERGE: "good", MERGE_COMPLETED: "good", TASK_COMPLETED: "good", TEST_FAILED: "bad", TASK_FAILED: "bad",
    BLOCKED: "bad", INTEGRATION_CONFLICT: "warn", REVIEW_FAILED: "warn", PAUSED_BUDGET: "warn", AUTH_REQUIRED: "warn",
    APPROVAL_REQUIRED: "warn", TASK_CANCELLED: "muted" };

  // ------------------------------------------------------------ helpers

  function api(path, options) {
    // The host SDK handles Dashboard authentication in both loopback and gated modes.
    return SDK.fetchJSON("/api/plugins/orchestration" + path, options);
  }
  function post(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }
  function message(e) { return String((e && e.message) || e); }
  function cx() { return Array.prototype.filter.call(arguments, Boolean).join(" "); }
  function count(n) { return n === undefined || n === null ? "0" : Number(n).toLocaleString(); }
  function capital(text) { return text ? text.charAt(0).toUpperCase() + text.slice(1) : text; }
  function when(value) { return value ? new Date(value).toLocaleString() : "—"; }
  function ago(value) {
    if (!value) return "—";
    const s = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }
  function left(value) {
    if (!value) return null;
    const s = (new Date(value).getTime() - Date.now()) / 1000;
    if (s <= 0) return "expired";
    if (s < 3600) return "expires in " + Math.max(1, Math.floor(s / 60)) + " min";
    if (s < 2 * 86400) return "expires in " + Math.floor(s / 3600) + " h";
    return "expires in " + Math.floor(s / 86400) + " days";
  }
  function minutes(value) {
    if (value === null || value === undefined) return "—";
    return value < 1 ? "<1 min" : value < 90 ? Math.round(value) + " min" : (value / 60).toFixed(1) + " h";
  }
  function bytes(value) { return value ? (value / 1073741824 >= 1 ? (value / 1073741824).toFixed(1) + " GB" : (value / 1048576).toFixed(0) + " MB") : "—"; }
  function size(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
  function columnOf(state) { return COLUMNS.find(function (c) { return c.states.indexOf(state) >= 0; }) || COLUMNS[0]; }
  function tone(state) {
    if (state === "DONE") return "good";
    if (state === "BLOCKED" || state === "FAILED") return "bad";
    if (WAITING.indexOf(state) >= 0) return "warn";
    if (state === "CANCELLED") return "muted";
    return "info";
  }
  function matches(task, query) {
    if (!query) return true;
    const q = query.toLowerCase();
    return [task.key, task.title, task.project].some(function (v) { return v && String(v).toLowerCase().indexOf(q) >= 0; });
  }

  // Polls a path while the view is shown; an answer for a path no longer asked for is dropped.
  function usePoll(path) {
    const [state, setState] = useState({ path: null, data: null, error: null });
    const load = useCallback(function () {
      if (!path) return Promise.resolve();
      return api(path).then(function (d) { setState({ path: path, data: d, error: null }); })
        .catch(function (e) { setState(function (s) { return { path: path, data: s.path === path ? s.data : null, error: message(e) }; }); });
    }, [path]);
    useEffect(function () { load(); const t = setInterval(load, POLL_MS); return function () { clearInterval(t); }; }, [load]);
    const current = state.path === path;
    return { data: current ? state.data : null, error: current ? state.error : null, reload: load };
  }

  // ------------------------------------------------------------ small pieces

  const ICONS = {
    plus: "M12 5v14M5 12h14", search: "M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16zM21 21l-4.3-4.3", x: "M18 6 6 18M6 6l12 12",
    check: "M20 6 9 17l-5-5", alert: "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
    clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2", play: "M6 4l14 8-14 8z", pause: "M6 4h4v16H6zM14 4h4v16h-4z",
    retry: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5", file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
    link: "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7",
    folder: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z", branch: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9",
    cpu: "M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z",
    inbox: "M22 12h-6l-2 3h-4l-2-3H2M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z",
  };
  function Icon(props) {
    return h("svg", { className: cx("orch-icon", props.className), viewBox: "0 0 24 24", width: props.size || 16, height: props.size || 16,
      fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": "true" },
    h("path", { d: ICONS[props.name] }));
  }
  function Pill(props) {
    return h("span", { className: cx("orch-pill", "orch-tone-" + (props.tone || "muted"), props.className), title: props.title }, props.children);
  }
  function StatusPill(props) {
    return h(Pill, { tone: tone(props.state), title: props.state }, STATUS[props.state] || props.state);
  }
  function Button(props) {
    return h("button", { type: props.type || "button", className: cx("orch-btn", "orch-btn-" + (props.kind || "ghost"), props.small && "orch-btn-sm"),
      disabled: props.disabled, onClick: props.onClick, title: props.title, "aria-label": props.label },
    props.icon ? h(Icon, { name: props.icon, size: props.small ? 14 : 16 }) : null, props.children);
  }
  function Empty(props) {
    return h("div", { className: "orch-empty" }, h(Icon, { name: props.icon || "inbox", size: 28 }),
      h("div", { className: "orch-empty-title" }, props.title), props.text ? h("div", { className: "orch-muted" }, props.text) : null);
  }
  function Loading(props) {
    return props.error ? h("div", { className: "orch-alert" }, h(Icon, { name: "alert" }), props.error)
      : h("div", { className: "orch-loading" }, h("span", { className: "orch-spinner" }), "Loading…");
  }
  function Card(props) {
    return h("section", { className: cx("orch-card", props.className) },
      props.title ? h("header", { className: "orch-card-head" }, h("h3", null, props.title),
        props.count !== undefined ? h("span", { className: "orch-count" }, props.count) : null,
        props.action ? h("div", { className: "orch-card-action" }, props.action) : null) : null,
      props.children);
  }
  function TaskLink(props) {
    if (!props.task) return null;
    return h("button", { type: "button", className: "orch-tasklink", onClick: function (e) { e.stopPropagation(); props.open(props.task); } }, props.task);
  }
  function Table(props) {
    if (!props.rows || props.rows.length === 0) return h("p", { className: "orch-muted orch-pad" }, props.empty || "Nothing to show.");
    return h("div", { className: "orch-scroll" }, h("table", { className: "orch-table" },
      h("thead", null, h("tr", null, props.columns.map(function (c) { return h("th", { key: c[0] }, c[0]); }))),
      h("tbody", null, props.rows.map(function (row, i) {
        return h("tr", { key: row.id || row.key || row.seq || i }, props.columns.map(function (c) { return h("td", { key: c[0] }, c[1](row)); }));
      }))));
  }
  function Meter(props) {
    const pct = props.max ? Math.min(100, Math.round(100 * (props.value || 0) / props.max)) : 0;
    const level = props.max === null || props.max === undefined ? "info" : pct >= 100 ? "bad" : pct >= 80 ? "warn" : "good";
    return h("div", { className: "orch-meter", title: props.title },
      h("div", { className: "orch-meter-fill orch-fill-" + (props.tone || level), style: { width: (props.max ? pct : 0) + "%" } }));
  }

  // The five stages of a task; a waiting task shows where it stopped.
  function Stepper(props) {
    const state = props.state;
    const at = STAGE_OF[WAITING.indexOf(state) >= 0 ? props.resume || "" : state];
    const current = at === undefined ? (FINISHED.indexOf(state) >= 0 && state !== "DONE" ? -2 : -1) : at;
    const stopped = WAITING.indexOf(state) >= 0 || state === "FAILED";
    return h("ol", { className: cx("orch-stepper", props.compact && "orch-stepper-compact"), "aria-label": "Progress" },
      STAGES.map(function (name, i) {
        const status = current === -2 ? "off" : i < current ? "done" : i === current ? (stopped ? (state === "BLOCKED" || state === "FAILED" ? "bad" : "wait") : "now") : "todo";
        return h("li", { key: name, className: "orch-step orch-step-" + status, title: name },
          h("span", { className: "orch-step-dot" }, status === "done" ? h(Icon, { name: "check", size: 10 }) : null),
          props.compact ? null : h("span", { className: "orch-step-name" }, name));
      }));
  }
  function Progress(props) {
    const t = props.task;
    if (!t.subtasks) return null;
    return h("div", { className: "orch-progress" },
      h(Meter, { value: t.subtasks_done, max: t.subtasks, tone: t.subtasks_done === t.subtasks ? "good" : "info" }),
      h("span", { className: "orch-muted" }, t.subtasks_done + " of " + t.subtasks + " subtasks"));
  }

  // ------------------------------------------------------------ approvals

  // One decision, explained the way the chat explains it, with the two outcomes as the buttons.
  function ApprovalCard(props) {
    const a = props.approval;
    const plain = a.plain || { question: a.summary, yes: "approve", no: "reject", reasons: [] };
    const [busy, setBusy] = useState(null);
    const [error, setError] = useState(null);
    function decide(decision) {
      setBusy(decision);
      setError(null);
      post("/approvals/" + a.id, { decision: decision }).then(props.done)
        .catch(function (e) { setError(message(e)); }).finally(function () { setBusy(null); });
    }
    const expires = left(a.expires_at);
    return h("article", { className: "orch-approval" },
      h("div", { className: "orch-approval-head" },
        h("span", { className: "orch-approval-icon" }, h(Icon, { name: "alert", size: 16 })),
        h("strong", null, "Decision needed"),
        a.number !== undefined && a.number !== null ? h("span", { className: "orch-muted" }, "#" + a.number) : null,
        expires ? h("span", { className: "orch-approval-expiry" }, h(Icon, { name: "clock", size: 13 }), expires) : null),
      a.task ? h("div", { className: "orch-approval-task" }, h(TaskLink, { task: a.task, open: props.open }),
        a.task_title ? h("span", null, a.task_title) : null, a.project ? h(Pill, null, a.project) : null)
        : a.project ? h("div", { className: "orch-approval-task" }, h(Pill, null, a.project)) : null,
      h("p", { className: "orch-approval-question" }, plain.question),
      h("div", { className: "orch-approval-actions" },
        h(Button, { kind: "good", icon: "check", disabled: !!busy, onClick: function () { decide("APPROVE"); } }, busy === "APPROVE" ? "Saving…" : capital(plain.yes)),
        h(Button, { kind: "bad-outline", icon: "x", disabled: !!busy, onClick: function () { decide("REJECT"); } }, busy === "REJECT" ? "Saving…" : capital(plain.no)),
        a.task && props.open ? h(Button, { kind: "ghost", small: true, onClick: function () { props.open(a.task); } }, "Details") : null),
      error ? h("div", { className: "orch-alert" }, error) : null);
  }

  // An expired approval requested again: the platform checks it still applies and says why when it does not.
  function Renew(props) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);
    function renew() {
      setBusy(true);
      setError(null);
      post("/approvals/" + props.id + "/renew").then(props.done)
        .catch(function (e) { setError(message(e)); }).finally(function () { setBusy(false); });
    }
    return h("span", null, h(Button, { small: true, icon: "retry", disabled: busy, onClick: renew }, busy ? "Requesting…" : "Request again"),
      error ? h("div", { className: "orch-alert" }, error) : null);
  }

  // ------------------------------------------------------------ task actions

  function available(state) {
    return {
      pause: FINISHED.indexOf(state) < 0 && WAITING.indexOf(state) < 0,
      resume: state === "PAUSED" || state === "PAUSED_BUDGET",
      retry: state === "BLOCKED" || state === "FAILED" || state === "CANCELLED",
      cancel: FINISHED.indexOf(state) < 0,
    };
  }
  function Actions(props) {
    const t = props.task;
    const can = available(t.state);
    const [busy, setBusy] = useState(null);
    const [confirming, setConfirming] = useState(false);
    const [error, setError] = useState(null);
    function act(verb) {
      setBusy(verb);
      setError(null);
      post("/tasks/" + t.key + "/" + verb).then(function () { setConfirming(false); return props.done && props.done(); })
        .catch(function (e) { setError(message(e)); }).finally(function () { setBusy(null); });
    }
    return h("div", { className: "orch-actions" },
      can.resume ? h(Button, { kind: "primary", small: props.small, icon: "play", disabled: !!busy, onClick: function () { act("resume"); } }, busy === "resume" ? "Resuming…" : "Resume") : null,
      can.retry ? h(Button, { kind: "primary", small: props.small, icon: "retry", disabled: !!busy, onClick: function () { act("retry"); } }, busy === "retry" ? "Retrying…" : "Retry") : null,
      can.pause && !props.compact ? h(Button, { small: props.small, icon: "pause", disabled: !!busy, onClick: function () { act("pause"); } }, busy === "pause" ? "Pausing…" : "Pause") : null,
      can.cancel && !props.compact ? (confirming
        ? h("span", { className: "orch-confirm" }, "Cancel this task?",
          h(Button, { kind: "bad", small: true, disabled: !!busy, onClick: function () { act("cancel"); } }, busy === "cancel" ? "Cancelling…" : "Yes, cancel"),
          h(Button, { small: true, onClick: function () { setConfirming(false); } }, "Keep it"))
        : h(Button, { kind: "bad-outline", small: props.small, icon: "x", onClick: function () { setConfirming(true); } }, "Cancel")) : null,
      error ? h("div", { className: "orch-alert" }, error) : null);
  }

  // What a waiting task needs from a person, in one sentence.
  function nextStep(t) {
    switch (t.state) {
      case "BLOCKED": return "The agents could not continue. Read the reason, then retry it (or revise the request from chat).";
      case "PAUSED": return "Paused by a person. Resume it when you are ready.";
      case "PAUSED_BUDGET": return "It used up its budget. Raise it from chat (/orch budget " + t.key + " agent_launches=10), then resume.";
      case "AUTH_REQUIRED": return "A provider login expired. On your Mac run make auth-claude or make auth-codex.";
      case "FAILED": return "It failed. Retry it to start again from where it stopped.";
      default: return null;
    }
  }
  function WaitingCard(props) {
    const t = props.task;
    return h("article", { className: "orch-waiting orch-tone-border-" + tone(t.state) },
      h("div", { className: "orch-waiting-head" }, h(TaskLink, { task: t.key, open: props.open }), h(StatusPill, { state: t.state }),
        h("span", { className: "orch-muted orch-right" }, ago(t.updated_at))),
      h("div", { className: "orch-waiting-title" }, t.title),
      t.state_reason ? h("p", { className: "orch-reason" }, t.state_reason) : null,
      nextStep(t) ? h("p", { className: "orch-next" }, nextStep(t)) : null,
      h("div", { className: "orch-row" }, h(Actions, { task: t, compact: true, small: true, done: props.done }),
        h(Button, { small: true, onClick: function () { props.open(t.key); } }, "Open")));
  }

  // ------------------------------------------------------------ home

  function Stat(props) {
    return h("button", { type: "button", className: cx("orch-stat", props.onClick && "orch-clickable"), onClick: props.onClick },
      h("span", { className: "orch-stat-icon orch-tone-" + props.tone }, h(Icon, { name: props.icon, size: 18 })),
      h("span", { className: "orch-stat-body" }, h("span", { className: "orch-stat-n" }, props.value),
        h("span", { className: "orch-stat-label" }, props.label), props.sub ? h("span", { className: "orch-stat-sub" }, props.sub) : null));
  }
  function Activity(props) {
    const items = props.items || [];
    if (!items.length) return h(Empty, { title: "No activity yet", text: "What the agents do shows up here." });
    return h("ul", { className: "orch-feed" }, items.slice(0, props.limit || 12).map(function (e) {
      return h("li", { key: e.seq, className: "orch-feed-item" },
        h("span", { className: "orch-dot orch-tone-" + e.tone }),
        h("div", { className: "orch-feed-body" },
          h("div", null, e.task ? h(TaskLink, { task: e.task, open: props.open }) : null, " ", h("strong", null, e.label)),
          h("div", { className: "orch-muted orch-ellipsis" }, e.task_title || e.project || e.summary)),
        h("span", { className: "orch-muted orch-feed-time" }, ago(e.occurred_at)));
    }));
  }
  function Agents(props) {
    const usage = props.usage || {};
    const providers = Object.keys(usage);
    if (!providers.length) return h(Empty, { title: "No agent runs in 24 h", icon: "cpu" });
    return h("ul", { className: "orch-agents" }, providers.map(function (p) {
      const u = usage[p];
      const models = Object.keys(u.models || {}).filter(function (m) { return m !== "unknown"; });
      const rate = u.executions ? Math.round(100 * u.succeeded / u.executions) : 0;
      return h("li", { key: p, className: "orch-agent" },
        h("span", { className: "orch-avatar" }, capital((p || "runner").slice(0, 2))),
        h("div", { className: "orch-agent-body" }, h("strong", null, p || "runner"),
          h("div", { className: "orch-muted orch-ellipsis" }, models.join(", ") || "model not recorded")),
        h("div", { className: "orch-agent-stats" }, h("div", null, count(u.executions) + " runs"),
          h("div", { className: "orch-muted" }, rate + "% ok · " + count(u.tokens) + " tok")));
    }));
  }
  function TaskRow(props) {
    const t = props.task;
    return h("button", { type: "button", className: "orch-taskrow", onClick: function () { props.open(t.key); } },
      h("div", { className: "orch-taskrow-main" },
        h("div", { className: "orch-taskrow-top" }, h("span", { className: "orch-key" }, t.key), h("span", { className: "orch-ellipsis" }, t.title)),
        h("div", { className: "orch-muted orch-small" }, STATUS[t.state] || t.state, " · ", t.project, t.running ? " · " : null,
          t.running ? h("span", { className: "orch-live" }, "agents working") : null)),
      h("div", { className: "orch-taskrow-side" }, h(Stepper, { state: t.state, resume: t.resume_state, compact: true }), h(Progress, { task: t })));
  }

  function Home(props) {
    const summary = usePoll("/summary");
    const board = usePoll("/board");
    if (!summary.data || !board.data) return h(Loading, { error: summary.error || board.error });
    const s = summary.data;
    const tasks = board.data.tasks.filter(function (t) { return matches(t, props.query); });
    const approvals = s.required_actions.approvals.filter(function (a) { return !props.query || matches({ key: a.task, title: a.task_title, project: a.project }, props.query); });
    const waiting = tasks.filter(function (t) { return WAITING.indexOf(t.state) >= 0 && t.state !== "APPROVAL_REQUIRED"; });
    const active = tasks.filter(function (t) { return FINISHED.indexOf(t.state) < 0 && WAITING.indexOf(t.state) < 0; });
    const needs = approvals.length + waiting.length;
    const done = s.done_tasks_30_days || {};
    function reload() { summary.reload(); board.reload(); }
    return h("div", { className: "orch-home" },
      h("div", { className: "orch-stats" },
        h(Stat, { icon: "alert", tone: needs ? "warn" : "good", value: needs, label: "Need you", sub: needs ? "decisions and stuck tasks" : "all clear" }),
        h(Stat, { icon: "play", tone: "info", value: active.length, label: "In progress", onClick: function () { props.go("board"); } }),
        h(Stat, { icon: "cpu", tone: "info", value: s.running.length, label: "Agents running", onClick: function () { props.go("system"); } }),
        h(Stat, { icon: "check", tone: "good", value: count(done.count), label: "Done in 30 days", sub: done.mean_minutes ? "about " + minutes(done.mean_minutes) + " each" : null })),
      h("div", { className: "orch-grid" },
        h("div", { className: "orch-main" },
          h(Card, { title: "Needs you", count: needs, className: needs ? "orch-attention" : null },
            needs === 0 ? h(Empty, { icon: "check", title: "All clear", text: "Nothing is waiting for you right now." })
              : h("div", { className: "orch-stack" },
                approvals.map(function (a) { return h(ApprovalCard, { key: a.id, approval: a, done: reload, open: props.open }); }),
                waiting.map(function (t) { return h(WaitingCard, { key: t.key, task: t, open: props.open, done: reload }); }))),
          h(Card, { title: "In progress", count: active.length, action: h(Button, { small: true, onClick: function () { props.go("board"); } }, "Open board") },
            active.length === 0 ? h(Empty, { title: "No task in progress", text: "Create one with New task." })
              : h("div", { className: "orch-list" }, active.map(function (t) { return h(TaskRow, { key: t.key, task: t, open: props.open }); })))),
        h("aside", { className: "orch-side" },
          h(Card, { title: "Recent activity" }, h(Activity, { items: s.recent_activity, open: props.open })),
          h(Card, { title: "Agents (24 h)" }, h(Agents, { usage: (s.provider_usage || {})["24_hours"] })))));
  }

  // ------------------------------------------------------------ board

  function BoardCard(props) {
    const t = props.task;
    return h("button", { type: "button", className: cx("orch-tile", "orch-tone-border-" + tone(t.state)), onClick: function () { props.open(t.key); } },
      h("div", { className: "orch-tile-top" }, h("span", { className: "orch-key" }, t.key),
        t.priority === "HIGH" || t.priority === "CRITICAL" ? h(Pill, { tone: t.priority === "CRITICAL" ? "bad" : "warn" }, t.priority.toLowerCase()) : null,
        t.pending_approvals ? h(Pill, { tone: "warn" }, "decision") : null,
        t.running ? h("span", { className: "orch-live orch-right", title: "agents working" }, "●") : null),
      h("div", { className: "orch-tile-title" }, t.title),
      h("div", { className: "orch-tile-status" }, STATUS[t.state] || t.state),
      h(Stepper, { state: t.state, resume: t.resume_state, compact: true }),
      h(Progress, { task: t }),
      h("div", { className: "orch-tile-foot" }, h(Pill, null, t.project), h("span", { className: "orch-muted orch-right" }, ago(t.updated_at))));
  }
  function Board(props) {
    const board = usePoll("/board");
    const [project, setProject] = useState("");
    if (!board.data) return h(Loading, { error: board.error });
    const all = board.data.tasks;
    const projects = Array.from(new Set(all.map(function (t) { return t.project; }))).sort();
    const tasks = all.filter(function (t) { return matches(t, props.query) && (!project || t.project === project); });
    return h("div", null,
      h("div", { className: "orch-toolbar" },
        h("div", { className: "orch-chips" }, ["", ...projects].map(function (p) {
          return h("button", { key: p || "all", type: "button", className: cx("orch-chip", project === p && "orch-chip-on"), onClick: function () { setProject(p); } }, p || "All projects");
        }))),
      h("div", { className: "orch-board" }, COLUMNS.map(function (c) {
        const items = tasks.filter(function (t) { return c.states.indexOf(t.state) >= 0; });
        return h("div", { key: c.id, className: cx("orch-column", c.id === "you" && items.length && "orch-column-attention") },
          h("div", { className: "orch-column-head" }, h("strong", null, c.title), h("span", { className: "orch-count" }, items.length),
            h("div", { className: "orch-muted orch-small" }, c.hint)),
          items.length ? items.map(function (t) { return h(BoardCard, { key: t.key, task: t, open: props.open }); })
            : h("div", { className: "orch-column-empty" }, "Nothing here"));
      })));
  }

  // ------------------------------------------------------------ approvals view

  function Approvals(props) {
    const [tab, setTab] = useState("PENDING");
    const summary = usePoll(tab === "PENDING" ? "/summary" : null);
    const expired = usePoll(tab === "EXPIRED" ? "/approvals?state=EXPIRED" : null);
    const tabs = h("div", { className: "orch-chips" }, [["PENDING", "Waiting for you"], ["EXPIRED", "Expired"]].map(function (x) {
      return h("button", { key: x[0], type: "button", className: cx("orch-chip", tab === x[0] && "orch-chip-on"), onClick: function () { setTab(x[0]); } }, x[1]);
    }));
    let body;
    if (tab === "PENDING") {
      body = !summary.data ? h(Loading, { error: summary.error })
        : summary.data.required_actions.approvals.length === 0 ? h(Empty, { icon: "check", title: "No decisions waiting", text: "When an agent needs you, it appears here and in your chat." })
          : h("div", { className: "orch-approval-grid" }, summary.data.required_actions.approvals.map(function (a) {
            return h(ApprovalCard, { key: a.id, approval: a, done: summary.reload, open: props.open });
          }));
    } else {
      body = !expired.data ? h(Loading, { error: expired.error }) : h(Card, null, h(Table, { rows: expired.data.approvals, empty: "No expired approvals.", columns: [
        ["Decision", function (a) { return a.summary; }],
        ["Task", function (a) { return a.task ? h(TaskLink, { task: a.task, open: props.open }) : "—"; }],
        ["Expired", function (a) { return ago(a.expires_at); }],
        ["", function (a) { return h(Renew, { id: a.id, done: expired.reload }); }]] }));
    }
    return h("div", { className: "orch-stack" }, tabs, body);
  }

  // ------------------------------------------------------------ projects view

  const PROJECT_STATUS = { PROJECT_READY: ["Ready", "good"], SCANNING: ["Scanning", "info"], AWAITING_APPROVAL: ["Needs approval", "warn"],
    SUSPENDED: ["Suspended", "bad"], UNREGISTERED: ["Removed", "muted"] };
  function Projects(props) {
    const projects = usePoll("/projects");
    const board = usePoll("/board");
    if (!projects.data) return h(Loading, { error: projects.error });
    const list = (projects.data.projects || []).filter(function (p) { return p.status !== "UNREGISTERED"; });
    if (!list.length) return h(Empty, { icon: "folder", title: "No projects yet", text: "Register one from chat or the CLI: ho project register <path>." });
    const tasks = (board.data && board.data.tasks) || [];
    return h("div", { className: "orch-project-grid" }, list.map(function (p) {
      const mine = tasks.filter(function (t) { return t.project === p.slug; });
      const active = mine.filter(function (t) { return FINISHED.indexOf(t.state) < 0; }).length;
      const needs = mine.filter(function (t) { return WAITING.indexOf(t.state) >= 0; }).length;
      const st = PROJECT_STATUS[p.status] || [p.status, "muted"];
      return h("article", { key: p.slug, className: "orch-project" },
        h("div", { className: "orch-project-head" }, h("span", { className: "orch-project-icon" }, h(Icon, { name: "folder", size: 18 })),
          h("div", null, h("strong", null, p.slug), h("div", { className: "orch-muted orch-small orch-ellipsis" }, p.host_path || p.name)),
          h(Pill, { tone: st[1], className: "orch-right" }, st[0])),
        h("div", { className: "orch-project-facts" },
          h("span", null, h(Icon, { name: "branch", size: 13 }), " ", p.default_branch || "—"),
          h("span", null, active + " active"), needs ? h("span", { className: "orch-text-warn" }, needs + " need you") : null,
          h("span", { className: "orch-muted" }, "since " + new Date(p.registered_at).toLocaleDateString())),
        p.git_remote ? h("div", { className: "orch-muted orch-small orch-ellipsis" }, p.git_remote) : null,
        h("div", { className: "orch-row" },
          p.status === "PROJECT_READY" ? h(Button, { kind: "primary", small: true, icon: "plus", onClick: function () { props.create(p.slug); } }, "New task") : null,
          h(Button, { small: true, onClick: function () { props.filter(p.slug); } }, "Show tasks")));
    }));
  }

  // ------------------------------------------------------------ system view

  function System(props) {
    const workers = usePoll("/workers");
    const summary = usePoll("/summary");
    const [windowKey, setWindowKey] = useState("24_hours");
    if (!workers.data || !summary.data) return h(Loading, { error: workers.error || summary.error });
    const w = workers.data;
    const s = summary.data;
    const health = s.health || {};
    const components = health.components || {};
    const usage = (s.provider_usage || {})[windowKey] || {};
    return h("div", { className: "orch-grid" },
      h("div", { className: "orch-main" },
        h(Card, { title: "Workers" },
          h("div", { className: "orch-capacity" },
            h("div", null, h("div", { className: "orch-small orch-muted" }, "Agent workers"),
              h("div", { className: "orch-big" }, w.capacity.agent_workers + " / " + w.capacity.max_agent_workers),
              h(Meter, { value: w.capacity.agent_workers, max: w.capacity.max_agent_workers })),
            h("div", null, h("div", { className: "orch-small orch-muted" }, "Memory reserved"),
              h("div", { className: "orch-big" }, bytes(w.capacity.memory_limit_bytes_in_use)),
              h("div", { className: "orch-small orch-muted" }, bytes(w.capacity.memory_available_for_executions) + " available")),
            h("div", null, h("div", { className: "orch-small orch-muted" }, "All containers"), h("div", { className: "orch-big" }, count(w.capacity.workers)))),
          h(Table, { rows: w.running, empty: "No worker is running.", columns: [
            ["Task", function (e) { return h(TaskLink, { task: e.task, open: props.open }); }], ["Role", function (e) { return e.role.toLowerCase(); }],
            ["Agent", function (e) { return e.provider || "runner"; }],
            ["CPU", function (e) { return e.cpu_percent === undefined ? "—" : e.cpu_percent + " %"; }],
            ["Memory", function (e) { return bytes(e.memory_bytes) + (e.memory_limit_bytes ? " / " + bytes(e.memory_limit_bytes) : ""); }],
            ["Started", function (e) { return ago(e.started_at); }]] })),
        h(Card, { title: "Agent usage", action: h("div", { className: "orch-chips" }, [["24_hours", "24 h"], ["7_days", "7 days"]].map(function (x) {
          return h("button", { key: x[0], type: "button", className: cx("orch-chip", windowKey === x[0] && "orch-chip-on"), onClick: function () { setWindowKey(x[0]); } }, x[1]);
        })) }, h(Table, { rows: Object.keys(usage).map(function (p) { return Object.assign({ id: p, provider: p }, usage[p]); }), empty: "No agent runs.", columns: [
          ["Agent", function (u) { return u.provider || "runner"; }],
          ["Models", function (u) { return Object.keys(u.models || {}).map(function (m) { return m + " (" + u.models[m].executions + ")"; }).join(", ") || "—"; }],
          ["Runs", function (u) { return count(u.executions); }], ["Succeeded", function (u) { return count(u.succeeded); }],
          ["Failed", function (u) { return count(u.failed); }], ["Tokens", function (u) { return count(u.tokens); }],
          ["Mean time", function (u) { return minutes(u.mean_minutes); }]] })),
        h(Card, { title: "Queue" }, h(Table, { rows: s.queue, empty: "The queue is empty.", columns: [
          ["Task", function (q) { return h(TaskLink, { task: q.key, open: props.open }); }], ["Project", function (q) { return q.project; }],
          ["Priority", function (q) { return q.priority; }], ["Waiting", function (q) { return minutes(q.waiting_minutes); }]] })),
        h(Card, { title: "Recent test runs" }, h(Table, { rows: s.recent_tests, empty: "No test runs yet.", columns: [
          ["Task", function (v) { return h(TaskLink, { task: v.task, open: props.open }); }], ["Purpose", function (v) { return v.purpose.toLowerCase().replace(/_/g, " "); }],
          ["Result", function (v) { return h(Pill, { tone: v.state === "PASSED" ? "good" : v.state === "FAILED" ? "bad" : "info" }, v.state.toLowerCase()); }],
          ["Duration", function (v) { return minutes(v.minutes); }], ["When", function (v) { return ago(v.created_at); }]] }))),
      h("aside", { className: "orch-side" },
        h(Card, { title: "Health" },
          h("div", { className: "orch-health" }, h(Pill, { tone: health.state === "DEGRADED" ? "bad" : "good" }, health.state === "DEGRADED" ? "Degraded" : "Healthy")),
          h("ul", { className: "orch-checks" }, Object.keys(components).sort().map(function (name) {
            const c = components[name];
            return h("li", { key: name }, h("span", { className: "orch-dot orch-tone-" + (c.state === "HEALTHY" ? "good" : "bad") }), name.replace(/_/g, " "),
              c.last_error && c.state !== "HEALTHY" ? h("div", { className: "orch-muted orch-small" }, c.last_error) : null);
          }))),
        h(Card, { title: "Last 7 days" }, h("ul", { className: "orch-facts" },
          h("li", null, h("span", null, "Retries"), h("strong", null, count((s.resilience_7_days || {}).retries))),
          h("li", null, h("span", null, "Provider fallbacks"), h("strong", null, count((s.resilience_7_days || {}).fallbacks))),
          h("li", null, h("span", null, "Notifications waiting for Hermes"), h("strong", null, count(s.pending_notifications)))))));
  }

  // ------------------------------------------------------------ task panel

  function Plan(props) {
    const dag = props.dag;
    if (!dag.subtasks.length) return h(Empty, { title: "No plan yet", text: "The orchestrator writes the plan when the task starts." });
    const depth = {};
    function level(key, seen) {
      if (depth[key] !== undefined) return depth[key];
      const parents = dag.edges.filter(function (e) { return e.to === key && seen.indexOf(e.from) < 0; });
      depth[key] = parents.length ? 1 + Math.max.apply(null, parents.map(function (e) { return level(e.from, seen.concat([key])); })) : 0;
      return depth[key];
    }
    dag.subtasks.forEach(function (s) { level(s.key, []); });
    const ordered = dag.subtasks.slice().sort(function (a, b) { return depth[a.key] - depth[b.key] || a.key.localeCompare(b.key, undefined, { numeric: true }); });
    const doneN = dag.subtasks.filter(function (s) { return s.state === "ACCEPTED" || s.state === "INTEGRATED"; }).length;
    const live = dag.subtasks.filter(function (s) { return s.state !== "CANCELLED"; }).length;
    return h("div", { className: "orch-stack" },
      h("div", { className: "orch-progress" }, h(Meter, { value: doneN, max: live, tone: doneN === live ? "good" : "info" }),
        h("span", { className: "orch-muted" }, doneN + " of " + live + " subtasks accepted · plan v" + (dag.plan_version || 1))),
      h("ol", { className: "orch-plan" }, ordered.map(function (s) {
        const deps = dag.edges.filter(function (e) { return e.to === s.key; }).map(function (e) { return e.from.split("-").pop(); });
        const st = s.state === "ACCEPTED" || s.state === "INTEGRATED" ? "good" : s.state === "CANCELLED" ? "muted" : s.state === "FAILED" || s.state === "BLOCKED" ? "bad" : "info";
        return h("li", { key: s.key, className: "orch-plan-item" },
          h("span", { className: "orch-dot orch-tone-" + st }),
          h("div", { className: "orch-plan-body" },
            h("div", null, h("strong", null, s.local_key), " ", s.title),
            h("div", { className: "orch-muted orch-small" }, s.state.toLowerCase().replace(/_/g, " "),
              s.developer_provider ? " · " + s.developer_provider : "", s.review_cycles ? " · " + s.review_cycles + " review round(s)" : "",
              deps.length ? " · after " + deps.join(", ") : ""),
            s.state_reason && st !== "good" ? h("div", { className: "orch-muted orch-small" }, s.state_reason) : null));
      })));
  }
  function Quality(props) {
    const d = props.data;
    const gate = d.quality_gate;
    const mark = { PASS: ["check", "good"], FAIL: ["x", "bad"], APPROVAL_REQUIRED: ["alert", "warn"], PENDING: ["clock", "info"], UNAVAILABLE: ["clock", "muted"] };
    return h("div", { className: "orch-stack" },
      h(Card, { title: "Quality gate" }, gate ? h("div", null,
        h("div", { className: "orch-row" }, h(Pill, { tone: gate.outcome === "PASS" ? "good" : "bad" }, gate.outcome === "PASS" ? "Passed" : "Not passed"),
          h("span", { className: "orch-muted orch-small" }, "risk " + String(gate.risk || "").toLowerCase() + " · " + ago(gate.evaluated_at))),
        h("ul", { className: "orch-checklist" }, gate.requirements.map(function (r) {
          const m = mark[r.status] || ["clock", "muted"];
          return h("li", { key: r.name }, h("span", { className: "orch-check orch-tone-" + m[1] }, h(Icon, { name: m[0], size: 12 })),
            h("div", null, h("div", null, REQUIREMENTS[r.name] || capital(r.name.replace(/_/g, " "))), r.detail ? h("div", { className: "orch-muted orch-small" }, r.detail) : null));
        })),
        gate.residual_risk ? h("p", { className: "orch-muted orch-small" }, "Residual risk: " + gate.residual_risk) : null)
        : h("p", { className: "orch-muted" }, "Not evaluated yet: it runs once the work is integrated, tested, and reviewed.")),
      h(Card, { title: "Reviews", count: d.reviews.length }, d.reviews.length === 0 ? h("p", { className: "orch-muted" }, "No reviews yet.") :
        h("div", { className: "orch-stack" }, d.reviews.map(function (r) {
          const ok = r.outcome === "APPROVED" || r.outcome === "PASSED";
          return h("div", { key: r.id, className: "orch-review" },
            h("div", { className: "orch-row" }, h(Pill, { tone: ok ? "good" : "warn" }, r.outcome.toLowerCase().replace(/_/g, " ")),
              h("span", null, r.reviewer_provider + " reviewed " + (r.subtask || "the integrated change")), h("span", { className: "orch-muted orch-right orch-small" }, ago(r.created_at))),
            r.summary ? h("p", { className: "orch-small" }, r.summary) : null,
            r.findings.length ? h("ul", { className: "orch-findings" }, r.findings.map(function (f, i) {
              return h("li", { key: i }, h(Pill, { tone: f.severity === "LOW" || f.severity === "INFO" ? "muted" : f.severity === "MEDIUM" ? "warn" : "bad" }, f.severity.toLowerCase()), " ",
                f.path ? h("code", null, f.path + (f.line ? ":" + f.line : "")) : null, " ", f.description);
            })) : null);
        }))),
      h(Card, { title: "Test runs", count: d.tests.length }, h(Table, { rows: d.tests, empty: "No test runs yet.", columns: [
        ["Purpose", function (v) { return v.purpose.toLowerCase().replace(/_/g, " "); }],
        ["Result", function (v) { return h(Pill, { tone: v.state === "PASSED" ? "good" : v.state === "FAILED" ? "bad" : "info" }, v.state.toLowerCase()); }],
        ["Steps", function (v) { return v.runs.map(function (r) { return r.kind.toLowerCase() + " " + r.status.toLowerCase(); }).join(", ") || "—"; }],
        ["Duration", function (v) { return minutes(v.minutes); }], ["When", function (v) { return ago(v.created_at); }]] })));
  }
  function Timeline(props) {
    const [all, setAll] = useState(false);
    const events = props.events.filter(function (e) { return all || KEY_EVENTS.indexOf(e.type) >= 0; });
    return h("div", null,
      h("div", { className: "orch-toolbar" }, h("label", { className: "orch-toggle" }, h("input", { type: "checkbox", checked: all, onChange: function (e) { setAll(e.target.checked); } }),
        "Show every event (" + props.events.length + ")")),
      events.length === 0 ? h(Empty, { title: "Nothing yet" }) : h("ol", { className: "orch-timeline" }, events.map(function (e) {
        return h("li", { key: e.seq, className: "orch-timeline-item" }, h("span", { className: "orch-dot orch-tone-" + (EVENT_TONE[e.type] || "muted") }),
          h("div", null, h("div", null, e.summary), h("div", { className: "orch-muted orch-small" }, e.type.toLowerCase().replace(/_/g, " ") + " · " + e.actor + " · " + when(e.occurred_at))));
      })));
  }
  function Advanced(props) {
    const d = props.data;
    const t = d.task;
    const [manifest, setManifest] = useState(null);
    const [error, setError] = useState(null);
    function show(promise) { promise.then(setManifest).catch(function (e) { setError(message(e)); }); }
    return h("div", { className: "orch-stack" },
      error ? h("div", { className: "orch-alert" }, error) : null,
      h(Card, { title: "Agent runs", count: d.executions.length }, h(Table, { rows: d.executions, empty: "No agent runs.", columns: [
        ["Role", function (e) { return e.role.toLowerCase(); }], ["Agent", function (e) { return e.provider || "runner"; }],
        ["Model", function (e) { return e.model || "—"; }], ["Subtask", function (e) { return e.subtask || "—"; }],
        ["Result", function (e) { return h(Pill, { tone: e.state === "SUCCEEDED" ? "good" : e.state === "FAILED" || e.state === "LOST" ? "bad" : "info", title: e.failure_reason || "" }, e.state.toLowerCase()); }],
        ["Tokens", function (e) { return e.tokens ? count(e.tokens) : "—"; }], ["Time", function (e) { return minutes(e.minutes); }]] })),
      h(Card, { title: "Decisions", count: d.approvals.length }, h(Table, { rows: d.approvals, empty: "No decisions.", columns: [
        ["Decision", function (a) { return (a.plain && a.plain.question) || a.summary; }],
        ["Result", function (a) { return h(Pill, { tone: a.state === "APPROVED" || a.state === "CONSUMED" ? "good" : a.state === "REJECTED" ? "bad" : a.state === "PENDING" ? "warn" : "muted" }, a.state.toLowerCase()); }],
        ["By", function (a) { return a.decided_by || "—"; }],
        ["", function (a) { return a.state === "EXPIRED" && (!a.decided_by || a.action === "MERGE") ? h(Renew, { id: a.id, done: props.reload }) : null; }]] })),
      d.git ? h(Card, { title: "Git" }, h("ul", { className: "orch-facts" },
        h("li", null, h("span", null, "Base"), h("code", null, (d.git.base_sha || "—").slice(0, 12))),
        h("li", null, h("span", null, "Integrated"), h("code", null, (d.git.integration_sha || "—").slice(0, 12))),
        h("li", null, h("span", null, "Retest"), h("span", null, (d.git.retest_status || "—").toLowerCase())),
        d.git.merge_commit_sha ? h("li", null, h("span", null, "Merged"), h("code", null, d.git.merge_commit_sha.slice(0, 12))) : null,
        d.git.post_merge_status ? h("li", null, h("span", null, "After merge"), h("span", null, d.git.post_merge_status.toLowerCase())) : null)) : null,
      h(Card, { title: "Manifests", action: h(Button, { small: true, onClick: function () { show(post("/tasks/" + t.key + "/manifest")); } }, "Generate now") },
        h(Table, { rows: d.manifests, empty: "No manifest yet.", columns: [["Kind", function (m) { return m.kind.toLowerCase().replace(/_/g, " "); }],
          ["Generated", function (m) { return when(m.generated_at); }], ["SHA-256", function (m) { return h("code", null, m.sha256.slice(0, 16) + "…"); }],
          ["", function (m) { return h(Button, { small: true, onClick: function () { show(api("/tasks/" + t.key + "/manifests/" + m.id)); } }, "View"); }]] }),
        manifest ? h("pre", { className: "orch-pre" }, JSON.stringify(manifest, null, 2)) : null),
      h(Card, { title: "Checkpoints" }, h(Table, { rows: d.checkpoints, empty: "No checkpoints.", columns: [
        ["#", function (c) { return c.seq; }], ["Reason", function (c) { return c.reason; }], ["State", function (c) { return c.state; }],
        ["When", function (c) { return when(c.created_at); }]] })));
  }
  function download(task, a) {
    // Through the Dashboard's own authentication; saved as a file, never opened in this page.
    return SDK.authedFetch("/api/plugins/orchestration/tasks/" + task + "/attachments/" + a.id).then(function (r) {
      if (!r.ok) throw new Error("download failed (" + r.status + ")");
      return r.blob();
    }).then(function (blob) {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = a.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
    });
  }
  function Summary(props) {
    const d = props.data;
    const t = d.task;
    const budget = d.budget;
    const pending = d.approvals.filter(function (a) { return a.state === "PENDING"; });
    const [error, setError] = useState(null);
    return h("div", { className: "orch-stack" },
      pending.map(function (a) { return h(ApprovalCard, { key: a.id, approval: Object.assign({ task_title: t.title }, a), done: props.reload }); }),
      WAITING.indexOf(t.state) >= 0 && nextStep(t) ? h("div", { className: "orch-callout orch-tone-border-" + tone(t.state) }, nextStep(t)) : null,
      h(Card, { title: "About" }, h("ul", { className: "orch-facts" },
        h("li", null, h("span", null, "Project"), h("strong", null, t.project)),
        h("li", null, h("span", null, "Priority"), h("span", null, String(t.priority).toLowerCase())),
        t.risk ? h("li", null, h("span", null, "Risk"), h("span", null, String(t.risk).toLowerCase())) : null,
        h("li", null, h("span", null, "Created"), h("span", null, when(t.created_at))),
        h("li", null, h("span", null, "Last change"), h("span", null, ago(t.updated_at))),
        d.git && d.git.pr_url ? h("li", null, h("span", null, "Pull request"), h("a", { href: d.git.pr_url, target: "_blank", rel: "noopener noreferrer", className: "orch-link" }, h(Icon, { name: "link", size: 13 }), " open on GitHub")) : null)),
      budget ? h(Card, { title: "Budget", action: h(Pill, { tone: budget.state === "EXHAUSTED" ? "bad" : budget.state === "WARNING" ? "warn" : "good" }, budget.profile.toLowerCase() + " · " + budget.state.toLowerCase()) },
        h("ul", { className: "orch-budget" }, Object.keys(budget.limits).map(function (k) {
          const used = budget.consumed[k] || 0;
          const limit = budget.limits[k];
          return h("li", { key: k }, h("div", { className: "orch-row" }, h("span", null, COUNTERS[k] || k),
            h("span", { className: "orch-muted orch-right orch-small" }, count(Math.round(used)) + " / " + (limit === null ? "no limit" : count(limit)))),
          h(Meter, { value: used, max: limit }));
        }))) : null,
      (d.attachments || []).length ? h(Card, { title: "Attachments", count: d.attachments.length },
        h("ul", { className: "orch-files" }, d.attachments.map(function (a) {
          return h("li", { key: a.id }, h(Icon, { name: "file", size: 14 }), h("span", { className: "orch-ellipsis" }, a.name), h("span", { className: "orch-muted orch-small" }, size(a.size_bytes)),
            h(Button, { small: true, onClick: function () { download(t.key, a).catch(function (e) { setError(message(e)); }); } }, "Download"));
        })), error ? h("div", { className: "orch-alert" }, error) : null) : null);
  }

  const PANEL_TABS = [["summary", "Summary"], ["plan", "Plan"], ["quality", "Quality"], ["activity", "Activity"], ["advanced", "Advanced"]];
  function TaskPanel(props) {
    const view = usePoll("/tasks/" + props.task);
    const [tab, setTab] = useState("summary");
    useEffect(function () { setTab("summary"); }, [props.task]);
    useEffect(function () {
      function onKey(e) { if (e.key === "Escape") props.close(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [props.close]);
    const d = view.data;
    const t = d && d.task;
    return h("div", { className: "orch-overlay", onClick: props.close },
      h("aside", { className: "orch-panel", role: "dialog", "aria-label": props.task, onClick: function (e) { e.stopPropagation(); } },
        h("header", { className: "orch-panel-head" },
          h("div", { className: "orch-panel-title" },
            h("div", { className: "orch-row" }, h("span", { className: "orch-key" }, props.task), t ? h(StatusPill, { state: t.state }) : null, t ? h(Pill, null, t.project) : null),
            h("h2", null, t ? t.title : "Loading…")),
          h(Button, { kind: "ghost", icon: "x", label: "Close", onClick: props.close })),
        !d ? h(Loading, { error: view.error }) : h("div", { className: "orch-panel-body" },
          h(Stepper, { state: t.state, resume: t.resume_state }),
          t.state_reason ? h("p", { className: "orch-reason" }, t.state_reason) : null,
          h(Actions, { task: t, done: view.reload }),
          h("nav", { className: "orch-tabs" }, PANEL_TABS.map(function (x) {
            const n = x[0] === "plan" ? d.dag.subtasks.length : x[0] === "summary" ? d.approvals.filter(function (a) { return a.state === "PENDING"; }).length : null;
            return h("button", { key: x[0], type: "button", className: cx("orch-tab", tab === x[0] && "orch-tab-on"), onClick: function () { setTab(x[0]); } },
              x[1], n ? h("span", { className: "orch-count" }, n) : null);
          })),
          tab === "summary" ? h(Summary, { data: d, reload: view.reload })
            : tab === "plan" ? h(Plan, { dag: d.dag })
              : tab === "quality" ? h(Quality, { data: d })
                : tab === "activity" ? h(Timeline, { events: d.timeline })
                  : h(Advanced, { data: d, reload: view.reload }))));
  }

  // ------------------------------------------------------------ new task

  function readBase64(file) {
    return new Promise(function (resolve, reject) {
      const reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result).split(",", 2)[1] || ""); };
      reader.onerror = function () { reject(new Error("could not read " + file.name)); };
      reader.readAsDataURL(file);
    });
  }
  function attachmentProblem(files) {
    const allowed = ATTACH.accept.split(",");
    if (files.length > ATTACH.maxFiles) return "At most " + ATTACH.maxFiles + " files.";
    for (const f of files) {
      if (allowed.indexOf("." + (f.name.split(".").pop() || "").toLowerCase()) < 0) return f.name + ": this type of file is not allowed.";
      if (f.size === 0) return f.name + " is empty.";
      if (f.size > ATTACH.maxFile) return f.name + " is larger than 10 MB.";
    }
    if (files.reduce(function (n, f) { return n + f.size; }, 0) > ATTACH.maxTotal) return "Attachments are limited to 25 MB in total.";
    return null;
  }
  function newKey() {
    const random = new Uint8Array(12);
    window.crypto.getRandomValues(random);
    return "dash-" + Array.from(random, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
  }

  // A new task: the control plane validates it and records the Dashboard operator as its creator. One
  // idempotency key per form, so a double click or a retried request creates one task.
  function NewTask(props) {
    const [projects, setProjects] = useState(null);
    const [form, setForm] = useState({ project: props.project || "", request: "", title: "", priority: "NORMAL", budget: "", depends_on: "" });
    const [key] = useState(newKey);
    const [files, setFiles] = useState([]);
    const [more, setMore] = useState(false);
    const [dragging, setDragging] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);
    const picker = useRef(null);
    useEffect(function () {
      api("/projects").then(function (d) {
        const ready = (d.projects || []).filter(function (p) { return p.status === "PROJECT_READY"; });
        setProjects(ready);
        if (ready.length === 1) setForm(function (f) { return Object.assign({}, f, { project: f.project || ready[0].slug }); });
      }).catch(function (e) { setError(message(e)); });
    }, []);
    useEffect(function () {
      function onKey(e) { if (e.key === "Escape" && !busy) props.cancel(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [busy]);
    function field(name) {
      return function (e) { const value = e.target.value; setForm(function (f) { const n = Object.assign({}, f); n[name] = value; return n; }); };
    }
    function add(chosen) {
      const all = files.concat(chosen.filter(function (f) { return !files.some(function (g) { return g.name === f.name; }); }));
      const problem = attachmentProblem(all);
      setError(problem);
      if (!problem) setFiles(all);
    }
    function submit(e) {
      e.preventDefault();
      const problem = attachmentProblem(files);
      if (problem) { setError(problem); return; }
      setBusy(true);
      setError(null);
      Promise.all(files.map(function (f) { return readBase64(f).then(function (b64) { return { name: f.name, content_base64: b64 }; }); }))
        .then(function (attachments) {
          return post("/tasks", { project: form.project, request: form.request, title: form.title || null, priority: form.priority,
            budget: form.budget || null, idempotency_key: key, attachments: attachments,
            depends_on: form.depends_on.split(/[\s,]+/).filter(function (k) { return k; }) });
        })
        .then(function (t) { props.created(t.key); })
        .catch(function (e) { setError(message(e)); setBusy(false); });
    }
    let body;
    if (projects === null) body = h(Loading, { error: error });
    else if (!projects.length) body = h(Empty, { icon: "folder", title: "No project is ready", text: "Register a project and approve its configuration first." });
    else body = h("form", { className: "orch-form", onSubmit: submit },
      h("label", null, h("span", null, "Project"), h("select", { value: form.project, onChange: field("project"), required: true },
        h("option", { value: "" }, "Choose a project…"),
        projects.map(function (p) { return h("option", { key: p.slug, value: p.slug }, p.slug); }))),
      h("label", null, h("span", null, "What should be done?"), h("textarea", { value: form.request, onChange: field("request"), required: true, rows: 7, autoFocus: true,
        maxLength: 20000, placeholder: "Describe the result you want: what to change, where, and how to tell it works." })),
      h("div", { className: cx("orch-drop", dragging && "orch-drop-on"),
        onDragOver: function (e) { e.preventDefault(); setDragging(true); }, onDragLeave: function () { setDragging(false); },
        onDrop: function (e) { e.preventDefault(); setDragging(false); add(Array.from(e.dataTransfer.files || [])); },
        onClick: function () { picker.current && picker.current.click(); } },
        h(Icon, { name: "file", size: 18 }), h("span", null, "Drop files here or ", h("u", null, "browse")),
        h("span", { className: "orch-muted orch-small" }, "Optional · up to 10 files, 10 MB each"),
        h("input", { ref: picker, type: "file", multiple: true, accept: ATTACH.accept, hidden: true,
          onChange: function (e) { add(Array.from(e.target.files || [])); e.target.value = ""; } })),
      files.length ? h("ul", { className: "orch-files" }, files.map(function (f) {
        return h("li", { key: f.name }, h(Icon, { name: "file", size: 14 }), h("span", { className: "orch-ellipsis" }, f.name), h("span", { className: "orch-muted orch-small" }, size(f.size)),
          h(Button, { small: true, icon: "x", label: "Remove " + f.name, onClick: function () { setFiles(files.filter(function (g) { return g !== f; })); setError(null); } }));
      })) : null,
      h("p", { className: "orch-muted orch-small" }, "The agents read attachments, so they are sent to the AI providers (Anthropic, OpenAI). Do not attach passwords, keys, or personal data."),
      h("button", { type: "button", className: "orch-disclosure", onClick: function () { setMore(!more); } }, (more ? "▾ " : "▸ ") + "More options"),
      more ? h("div", { className: "orch-form-row" },
        h("label", null, h("span", null, "Title"), h("input", { value: form.title, onChange: field("title"), maxLength: 200, placeholder: "Written for you if empty" })),
        h("label", null, h("span", null, "Priority"), h("select", { value: form.priority, onChange: field("priority") },
          [["LOW", "Low"], ["NORMAL", "Normal"], ["HIGH", "High"], ["CRITICAL", "Critical"]].map(function (p) { return h("option", { key: p[0], value: p[0] }, p[1]); }))),
        h("label", null, h("span", null, "Budget"), h("select", { value: form.budget, onChange: field("budget") },
          h("option", { value: "" }, "Project default"),
          [["SMALL", "Small"], ["NORMAL", "Normal"], ["LARGE", "Large"]].map(function (b) { return h("option", { key: b[0], value: b[0] }, b[1]); }))),
        h("label", null, h("span", null, "Starts after"), h("input", { value: form.depends_on, onChange: field("depends_on"), placeholder: "T-12, T-13" }))) : null,
      error ? h("div", { className: "orch-alert" }, error) : null,
      h("div", { className: "orch-form-actions" },
        h(Button, { onClick: props.cancel, disabled: busy }, "Cancel"),
        h(Button, { kind: "primary", type: "submit", icon: "plus", disabled: busy || !form.project || !form.request.trim() }, busy ? "Creating…" : "Create task")));
    return h("div", { className: "orch-overlay orch-overlay-center", onClick: function () { if (!busy) props.cancel(); } },
      h("div", { className: "orch-modal", role: "dialog", "aria-label": "New task", onClick: function (e) { e.stopPropagation(); } },
        h("header", { className: "orch-panel-head" }, h("div", { className: "orch-panel-title" }, h("h2", null, "New task"),
          h("div", { className: "orch-muted orch-small" }, "Agents plan it, build it, test it, and ask you before merging.")),
        h(Button, { kind: "ghost", icon: "x", label: "Close", onClick: props.cancel })),
        body));
  }

  // ------------------------------------------------------------ page

  const VIEWS = [["home", "Home"], ["board", "Board"], ["approvals", "Approvals"], ["projects", "Projects"], ["system", "System"]];
  function greeting() {
    const hour = new Date().getHours();
    return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  }
  function OrchestrationPage() {
    const [view, setView] = useState("home");
    const [task, setTask] = useState(null);
    const [creating, setCreating] = useState(null); // null, or the project to preselect ("" for none)
    const [query, setQuery] = useState("");
    const counts = usePoll("/summary");
    const search = useRef(null);
    const open = useCallback(function (key) { setCreating(null); setTask(key); }, []);
    const close = useCallback(function () { setTask(null); }, []);
    useEffect(function () {
      function onKey(e) {
        const typing = /INPUT|TEXTAREA|SELECT/.test((e.target && e.target.tagName) || "");
        if (e.key === "/" && !typing && search.current) { e.preventDefault(); search.current.focus(); }
      }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, []);
    const pending = counts.data ? counts.data.required_actions.approvals.length : 0;
    const waiting = counts.data ? counts.data.required_actions.waiting_tasks.filter(function (t) { return t.state !== "APPROVAL_REQUIRED" && t.state !== "FIX_REQUIRED"; }).length : 0;
    const go = function (v) { setView(v); };
    const props = { open: open, query: query, go: go };
    let body;
    if (view === "home") body = h(Home, props);
    else if (view === "board") body = h(Board, props);
    else if (view === "approvals") body = h(Approvals, props);
    else if (view === "projects") body = h(Projects, Object.assign({}, props, { create: function (p) { setCreating(p); },
      filter: function (p) { setQuery(p); setView("board"); } }));
    else body = h(System, props);
    return h("div", { className: "orch-page" },
      h("header", { className: "orch-top" },
        h("div", { className: "orch-hello" }, h("h1", null, greeting()),
          h("p", { className: "orch-muted" }, pending + waiting ? (pending + waiting) + (pending + waiting === 1 ? " thing needs" : " things need") + " you." : "Here is what your agents are doing.")),
        h("div", { className: "orch-top-tools" },
          h("label", { className: "orch-search" }, h(Icon, { name: "search", size: 15 }),
            h("input", { ref: search, value: query, onChange: function (e) { setQuery(e.target.value); }, placeholder: "Search tasks and projects", "aria-label": "Search" }),
            query ? h("button", { type: "button", className: "orch-search-clear", "aria-label": "Clear search", onClick: function () { setQuery(""); } }, h(Icon, { name: "x", size: 13 }))
              : h("kbd", null, "/")),
          h(Button, { kind: "primary", icon: "plus", onClick: function () { setCreating(""); } }, "New task"))),
      h("nav", { className: "orch-nav" }, VIEWS.map(function (v) {
        const badge = v[0] === "approvals" ? pending : v[0] === "home" ? waiting : 0;
        return h("button", { key: v[0], type: "button", className: cx("orch-nav-item", view === v[0] && "orch-nav-on"), onClick: function () { setView(v[0]); } },
          v[1], badge ? h("span", { className: "orch-badge" }, badge) : null);
      })),
      body,
      task ? h(TaskPanel, { task: task, close: close }) : null,
      creating !== null ? h(NewTask, { project: creating, created: function (key) { setCreating(null); setView("board"); setTask(key); }, cancel: function () { setCreating(null); } }) : null);
  }

  window.__HERMES_PLUGINS__.register("orchestration", OrchestrationPage);
})();
