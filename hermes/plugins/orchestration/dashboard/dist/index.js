(function () {
  "use strict";
  // hermes-orchestrator Dashboard tab (docs/design/phase-10.md). Only the orchestration views Hermes lacks:
  // overview with required actions, a read-only board, task detail (DAG, approvals, budget, Quality Gate,
  // reviews, tests, executions, audit timeline, checkpoints, manifests, controls), projects, workers, approvals.
  // Data comes from /api/plugins/orchestration/, which proxies the orchestrator Task API behind Hermes's login.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const h = React.createElement;
  const BOARD = ["BACKLOG", "READY", "PLANNING", "QUEUED", "RUNNING", "TESTING", "REVIEW", "FIX_REQUIRED", "QUALITY_GATE",
    "READY_FOR_MERGE", "MERGING", "VERIFYING", "APPROVAL_REQUIRED", "AUTH_REQUIRED", "PAUSED_BUDGET", "PAUSED", "BLOCKED"];
  const VIEWS = [["overview", "Overview"], ["board", "Board"], ["approvals", "Approvals"], ["projects", "Projects"], ["workers", "Workers"]];

  function api(path, options) {
    // The host SDK handles Dashboard authentication in both loopback and gated modes.
    return SDK.fetchJSON("/api/plugins/orchestration" + path, options);
  }
  function post(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }
  function when(value) { return value ? new Date(value).toLocaleString() : "—"; }
  function minutes(value) { return value === null || value === undefined ? "—" : value + " min"; }
  function bytes(value) { return value ? (value / 1048576).toFixed(0) + " MiB" : "—"; }
  function count(n) { return n === undefined || n === null ? "0" : Number(n).toLocaleString(); }

  function Badge(props) {
    return h("span", { className: "orch-badge orch-" + String(props.value || "").toLowerCase() }, props.value || "—");
  }
  function Section(props) {
    return h("section", { className: "orch-section" }, h("h3", null, props.title), props.children);
  }
  function Table(props) {
    if (!props.rows || props.rows.length === 0) return h("p", { className: "orch-muted" }, props.empty || "Nothing to show.");
    return h("div", { className: "orch-scroll" }, h("table", { className: "orch-table" },
      h("thead", null, h("tr", null, props.columns.map(function (c) { return h("th", { key: c[0] }, c[0]); }))),
      h("tbody", null, props.rows.map(function (row, i) {
        return h("tr", { key: row.id || row.key || row.seq || i }, props.columns.map(function (c) {
          return h("td", { key: c[0] }, c[1](row));
        }));
      }))));
  }
  function TaskLink(props) {
    return props.task ? h("a", { href: "#", className: "orch-link", onClick: function (e) { e.preventDefault(); props.open(props.task); } }, props.task) : "—";
  }

  // ------------------------------------------------------------ views

  function Overview(props) {
    const s = props.data;
    const actions = s.required_actions;
    const usage = s.provider_usage["24_hours"] || {};
    return h("div", null,
      h("div", { className: "orch-cards" },
        Object.keys(s.tasks_by_state).sort().map(function (state) {
          return h("div", { key: state, className: "orch-card" }, h("div", { className: "orch-card-n" }, count(s.tasks_by_state[state])), h(Badge, { value: state }));
        })),
      h(Section, { title: "Required actions" },
        h(Table, { rows: actions.approvals, empty: "No decisions waiting.", columns: [
          ["Approval", function (a) { return h(Badge, { value: a.action }); }],
          ["Task", function (a) { return h(TaskLink, { task: a.task, open: props.open }); }],
          ["Summary", function (a) { return a.summary; }],
          ["Expires", function (a) { return when(a.expires_at); }],
          ["", function (a) { return h(Decide, { id: a.id, done: props.reload }); }]] }),
        h(Table, { rows: actions.waiting_tasks, empty: "No task is waiting.", columns: [
          ["Task", function (t) { return h(TaskLink, { task: t.key, open: props.open }); }],
          ["State", function (t) { return h(Badge, { value: t.state }); }],
          ["Reason", function (t) { return t.state_reason || "—"; }],
          ["Since", function (t) { return when(t.updated_at); }]] })),
      h(Section, { title: "Queue" }, h(Table, { rows: s.queue, empty: "The queue is empty.", columns: [
        ["Task", function (q) { return h(TaskLink, { task: q.key, open: props.open }); }], ["Project", function (q) { return q.project; }],
        ["Priority", function (q) { return q.priority; }], ["Effective rank", function (q) { return q.effective_rank; }],
        ["Waiting", function (q) { return minutes(q.waiting_minutes); }]] })),
      h(Section, { title: "Running now" }, h(Table, { rows: s.running, empty: "No execution is running.", columns: [
        ["Task", function (e) { return h(TaskLink, { task: e.task, open: props.open }); }], ["Role", function (e) { return e.role; }],
        ["Provider", function (e) { return e.provider || "runner"; }], ["State", function (e) { return h(Badge, { value: e.state }); }],
        ["Duration", function (e) { return minutes(e.minutes); }]] })),
      h(Section, { title: "Provider usage (24 h)" }, h(Table, { rows: Object.keys(usage).map(function (p) { return Object.assign({ id: p, provider: p }, usage[p]); }),
        empty: "No agent executions in the last 24 hours.", columns: [
          ["Provider", function (u) { return u.provider; }], ["Executions", function (u) { return count(u.executions); }],
          ["Succeeded", function (u) { return count(u.succeeded); }], ["Failed", function (u) { return count(u.failed); }],
          ["Tokens", function (u) { return count(u.tokens); }], ["Mean duration", function (u) { return minutes(u.mean_minutes); }]] }),
        h("p", { className: "orch-muted" }, "Last 7 days: " + s.resilience_7_days.retries + " retries, " + s.resilience_7_days.fallbacks +
          " provider fallbacks. Done in 30 days: " + s.done_tasks_30_days.count + " task(s), mean " + minutes(s.done_tasks_30_days.mean_minutes) + ".")),
      h(Section, { title: "Recent tests" }, h(Table, { rows: s.recent_tests, empty: "No verification yet.", columns: [
        ["Task", function (v) { return h(TaskLink, { task: v.task, open: props.open }); }], ["Purpose", function (v) { return v.purpose; }],
        ["Result", function (v) { return h(Badge, { value: v.state }); }], ["Commit", function (v) { return (v.commit_sha || "").slice(0, 12); }],
        ["Duration", function (v) { return minutes(v.minutes); }]] })),
      h(Section, { title: "Platform" }, h("p", null, "Health: ", h(Badge, { value: (s.health && s.health.state) || "HEALTHY" }),
        " · notifications waiting for Hermes: " + s.pending_notifications)));
  }

  function Decide(props) {
    const [busy, setBusy] = React.useState(false);
    const [error, setError] = React.useState(null);
    function decide(decision) {
      setBusy(true);
      post("/approvals/" + props.id, { decision: decision }).then(props.done)
        .catch(function (e) { setError(String(e.message || e)); }).finally(function () { setBusy(false); });
    }
    return h("span", null,
      h("button", { className: "orch-button", disabled: busy, onClick: function () { decide("APPROVE"); } }, "Approve"), " ",
      h("button", { className: "orch-button", disabled: busy, onClick: function () { decide("REJECT"); } }, "Reject"),
      error ? h("div", { className: "orch-error" }, error) : null);
  }

  function Board(props) {
    const columns = BOARD.filter(function (state) { return props.tasks.some(function (t) { return t.state === state; }); });
    if (columns.length === 0) return h("p", { className: "orch-muted" }, "No active tasks.");
    return h("div", { className: "orch-board" }, columns.map(function (state) {
      return h("div", { key: state, className: "orch-column" }, h("div", { className: "orch-column-head" }, h(Badge, { value: state })),
        props.tasks.filter(function (t) { return t.state === state; }).map(function (t) {
          return h("div", { key: t.key, className: "orch-tile", onClick: function () { props.open(t.key); } },
            h("strong", null, t.key), " ", h("span", { className: "orch-muted" }, t.project), h("div", null, t.title));
        }));
    }));
  }

  function Dag(props) {
    const dag = props.dag;
    if (!dag.subtasks.length) return h("p", { className: "orch-muted" }, "No plan (the task was not decomposed).");
    const depth = {};
    function level(key, seen) {
      if (depth[key] !== undefined) return depth[key];
      const parents = dag.edges.filter(function (e) { return e.to === key && seen.indexOf(e.from) < 0; });
      depth[key] = parents.length ? 1 + Math.max.apply(null, parents.map(function (e) { return level(e.from, seen.concat([key])); })) : 0;
      return depth[key];
    }
    dag.subtasks.forEach(function (s) { level(s.key, []); });
    const levels = Math.max.apply(null, dag.subtasks.map(function (s) { return depth[s.key]; })) + 1;
    return h("div", { className: "orch-dag" }, Array.from({ length: levels }, function (_, i) {
      return h("div", { key: i, className: "orch-dag-level" }, dag.subtasks.filter(function (s) { return depth[s.key] === i; }).map(function (s) {
        const deps = dag.edges.filter(function (e) { return e.to === s.key; }).map(function (e) { return e.from; });
        return h("div", { key: s.key, className: "orch-node" }, h("strong", null, s.local_key), " ", h(Badge, { value: s.state }),
          h("div", { className: "orch-muted" }, s.key + " · " + s.kind + (s.developer_provider ? " · " + s.developer_provider : "") +
            (s.review_cycles ? " · " + s.review_cycles + " review cycle(s)" : "")),
          h("div", null, s.title), deps.length ? h("div", { className: "orch-muted" }, "after " + deps.join(", ")) : null,
          s.state_reason ? h("div", { className: "orch-muted" }, s.state_reason) : null);
      }));
    }));
  }

  function TaskView(props) {
    const [data, setData] = React.useState(null);
    const [error, setError] = React.useState(null);
    const [manifest, setManifest] = React.useState(null);
    const load = React.useCallback(function () {
      api("/tasks/" + props.task).then(setData).catch(function (e) { setError(String(e.message || e)); });
    }, [props.task]);
    React.useEffect(function () { load(); const t = setInterval(load, 10000); return function () { clearInterval(t); }; }, [load]);
    function act(promise) { promise.then(load).catch(function (e) { setError(String(e.message || e)); }); }
    if (!data) return h("p", null, error || "Loading " + props.task + "…");
    const t = data.task;
    const budget = data.budget;
    return h("div", null,
      h("p", null, h("a", { href: "#", className: "orch-link", onClick: function (e) { e.preventDefault(); props.back(); } }, "← back")),
      h("h2", null, t.key + " ", h(Badge, { value: t.state })), h("p", null, t.title + " · " + t.project + " · priority " + t.priority),
      t.state_reason ? h("p", { className: "orch-muted" }, t.state_reason) : null,
      error ? h("p", { className: "orch-error" }, error) : null,
      h("div", null, ["pause", "resume", "cancel", "retry"].map(function (verb) {
        return h("button", { key: verb, className: "orch-button", onClick: function () { act(post("/tasks/" + t.key + "/" + verb)); } }, verb);
      })),
      h(Section, { title: "Plan" }, h(Dag, { dag: data.dag })),
      h(Section, { title: "Approvals" }, h(Table, { rows: data.approvals, empty: "No approvals.", columns: [
        ["Action", function (a) { return h(Badge, { value: a.action }); }], ["State", function (a) { return h(Badge, { value: a.state }); }],
        ["Summary", function (a) { return a.summary; }], ["Decided by", function (a) { return a.decided_by || "—"; }],
        ["", function (a) { return a.state === "PENDING" ? h(Decide, { id: a.id, done: load }) : null; }]] })),
      budget ? h(Section, { title: "Budget (" + budget.profile + ", " + budget.state + ")" }, h(Table, {
        rows: Object.keys(budget.limits).map(function (k) { return { id: k, counter: k, limit: budget.limits[k], used: budget.consumed[k], reserved: (budget.reserved || {})[k] }; }),
        columns: [["Counter", function (b) { return b.counter; }], ["Used", function (b) { return count(b.used); }],
          ["Reserved", function (b) { return count(b.reserved); }], ["Limit", function (b) { return b.limit === null ? "unlimited" : count(b.limit); }]] })) : null,
      h(Section, { title: "Quality Gate" }, data.quality_gate ? h("div", null,
        h("p", null, h(Badge, { value: data.quality_gate.outcome }), " on " + (data.quality_gate.commit_sha || "").slice(0, 12) +
          " · risk " + data.quality_gate.risk + " · " + when(data.quality_gate.evaluated_at)),
        h(Table, { rows: data.quality_gate.requirements, columns: [["Requirement", function (r) { return r.name; }],
          ["Status", function (r) { return h(Badge, { value: r.status }); }], ["Detail", function (r) { return r.detail || "—"; }]] }),
        data.quality_gate.residual_risk ? h("p", { className: "orch-muted" }, "Residual risk: " + data.quality_gate.residual_risk) : null)
        : h("p", { className: "orch-muted" }, "Not evaluated yet.")),
      h(Section, { title: "Reviews" }, data.reviews.length === 0 ? h("p", { className: "orch-muted" }, "No reviews yet.") :
        data.reviews.map(function (r) {
          return h("div", { key: r.id, className: "orch-review" },
            h("p", null, h(Badge, { value: r.outcome }), " " + r.reviewer_provider + " reviewed " + (r.subtask || "the integration") +
              " (" + (r.commit_sha || "").slice(0, 12) + ") · " + when(r.created_at)), h("p", null, r.summary),
            h(Table, { rows: r.findings, empty: "No findings.", columns: [["Severity", function (f) { return h(Badge, { value: f.severity }); }],
              ["Where", function (f) { return (f.path || "") + (f.line ? ":" + f.line : ""); }], ["Finding", function (f) { return f.description; }],
              ["Status", function (f) { return f.status; }]] }));
        })),
      h(Section, { title: "Tests" }, h(Table, { rows: data.tests, empty: "No verification yet.", columns: [
        ["Purpose", function (v) { return v.purpose; }], ["Result", function (v) { return h(Badge, { value: v.state }); }],
        ["Runs", function (v) { return v.runs.map(function (r) { return r.kind + " " + r.status; }).join(", ") || "—"; }],
        ["Commit", function (v) { return (v.commit_sha || "").slice(0, 12); }], ["Duration", function (v) { return minutes(v.minutes); }]] })),
      h(Section, { title: "Executions" }, h(Table, { rows: data.executions, empty: "No executions.", columns: [
        ["Role", function (e) { return e.role; }], ["Provider", function (e) { return e.provider || "runner"; }],
        ["Subtask", function (e) { return e.subtask || "—"; }], ["State", function (e) { return h(Badge, { value: e.state }); }],
        ["Failure", function (e) { return e.failure_class ? e.failure_class + ": " + (e.failure_reason || "") : "—"; }],
        ["Tokens", function (e) { return e.tokens ? count(e.tokens) : "—"; }], ["Duration", function (e) { return minutes(e.minutes); }]] })),
      data.git ? h(Section, { title: "Git" }, h("p", null, "base " + (data.git.base_sha || "").slice(0, 12) + " · integration " +
        (data.git.integration_sha || "—").slice(0, 12) + " · retest " + (data.git.retest_status || "—") +
        (data.git.merge_commit_sha ? " · merged " + data.git.merge_commit_sha.slice(0, 12) + " (post-merge " + (data.git.post_merge_status || "—") + ")" : ""))) : null,
      h(Section, { title: "Manifests" },
        h("button", { className: "orch-button", onClick: function () { act(post("/tasks/" + t.key + "/manifest").then(setManifest)); } }, "Generate on-demand manifest"),
        h(Table, { rows: data.manifests, empty: "No manifest yet.", columns: [["Kind", function (m) { return m.kind; }],
          ["Generated", function (m) { return when(m.generated_at); }], ["SHA-256", function (m) { return m.sha256.slice(0, 16) + "…"; }],
          ["", function (m) { return h("button", { className: "orch-button", onClick: function () { api("/tasks/" + t.key + "/manifests/" + m.id).then(setManifest); } }, "View"); }]] }),
        manifest ? h("pre", { className: "orch-pre" }, JSON.stringify(manifest, null, 2)) : null),
      h(Section, { title: "Audit timeline" }, h(Table, { rows: data.timeline, columns: [
        ["When", function (e) { return when(e.occurred_at); }], ["Event", function (e) { return e.type; }],
        ["Actor", function (e) { return e.actor; }], ["Summary", function (e) { return e.summary; }]] })),
      h(Section, { title: "Checkpoints" }, h(Table, { rows: data.checkpoints, empty: "No checkpoints.", columns: [
        ["#", function (c) { return c.seq; }], ["Reason", function (c) { return c.reason; }], ["State", function (c) { return h(Badge, { value: c.state }); }],
        ["When", function (c) { return when(c.created_at); }]] })));
  }

  function Projects(props) {
    return h(Table, { rows: props.projects, empty: "No projects registered.", columns: [
      ["Project", function (p) { return p.slug; }], ["Status", function (p) { return h(Badge, { value: p.status }); }],
      ["Default branch", function (p) { return p.default_branch || "—"; }], ["Registered", function (p) { return when(p.registered_at); }]] });
  }

  function Workers(props) {
    const w = props.data;
    return h("div", null,
      h("p", null, "Agent workers " + w.capacity.agent_workers + " / " + w.capacity.max_agent_workers + " · all workers " + w.capacity.workers +
        " · memory reserved " + bytes(w.capacity.memory_limit_bytes_in_use) + " · available " + bytes(w.capacity.memory_available_for_executions)),
      h(Table, { rows: w.running, empty: "No worker is running.", columns: [
        ["Task", function (e) { return h(TaskLink, { task: e.task, open: props.open }); }], ["Role", function (e) { return e.role; }],
        ["Provider", function (e) { return e.provider || "runner"; }], ["State", function (e) { return h(Badge, { value: e.state }); }],
        ["CPU", function (e) { return e.cpu_percent === undefined ? "—" : e.cpu_percent + " %"; }],
        ["Memory", function (e) { return bytes(e.memory_bytes) + (e.memory_limit_bytes ? " / " + bytes(e.memory_limit_bytes) : ""); }],
        ["Since", function (e) { return when(e.started_at); }]] }));
  }

  // ------------------------------------------------------------ page

  function OrchestrationPage() {
    const [view, setView] = React.useState("overview");
    const [task, setTask] = React.useState(null);
    const [data, setData] = React.useState(null);
    const [error, setError] = React.useState(null);
    const sources = { overview: "/summary", board: "/board", approvals: "/approvals", projects: "/projects", workers: "/workers" };
    const load = React.useCallback(function () {
      if (task) return;
      api(sources[view]).then(function (d) { setData({ view: view, body: d }); setError(null); })
        .catch(function (e) { setError(String(e.message || e)); });
    }, [view, task]);
    React.useEffect(function () { setData(null); load(); const t = setInterval(load, 10000); return function () { clearInterval(t); }; }, [load]);
    function open(key) { setTask(key); }
    const nav = h("nav", { className: "orch-nav" }, VIEWS.map(function (v) {
      return h("button", { key: v[0], className: "orch-tab" + (v[0] === view && !task ? " orch-active" : ""),
        onClick: function () { setTask(null); setView(v[0]); } }, v[1]);
    }));
    let body;
    if (task) body = h(TaskView, { task: task, back: function () { setTask(null); } });
    else if (!data || data.view !== view) body = h("p", null, error || "Loading…");
    else if (view === "overview") body = h(Overview, { data: data.body, open: open, reload: load });
    else if (view === "board") body = h(Board, { tasks: data.body.tasks, open: open });
    else if (view === "approvals") body = h(Table, { rows: data.body.approvals, empty: "No decisions waiting.", columns: [
      ["Approval", function (a) { return h(Badge, { value: a.action }); }], ["Summary", function (a) { return a.summary; }],
      ["Requested by", function (a) { return a.requested_by; }], ["Expires", function (a) { return when(a.expires_at); }],
      ["", function (a) { return h(Decide, { id: a.id, done: load }); }]] });
    else if (view === "projects") body = h(Projects, { projects: data.body.projects });
    else body = h(Workers, { data: data.body, open: open });
    return h("div", { className: "orch-page" }, nav,
      error && data ? h("p", { className: "orch-error" }, error) : null, body);
  }

  window.__HERMES_PLUGINS__.register("orchestration", OrchestrationPage);
})();
