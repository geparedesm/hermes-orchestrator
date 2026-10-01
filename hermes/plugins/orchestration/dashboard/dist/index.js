(function () {
  "use strict";
  // hermes-orchestrator Dashboard tab: tasks and approvals (Phase 9; full views in Phase 10).
  // Data comes from /api/plugins/orchestration/, which proxies the orchestrator Task API.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const h = React.createElement;

  function api(path, options) {
    // The host SDK handles Dashboard authentication in both loopback and gated modes.
    return SDK.fetchJSON("/api/plugins/orchestration" + path, options);
  }

  function post(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }

  function Badge(props) {
    return h("span", { className: "orch-badge orch-" + String(props.value).toLowerCase() }, props.value);
  }

  function OrchestrationPage() {
    const [data, setData] = React.useState(null);
    const [error, setError] = React.useState(null);
    const [busy, setBusy] = React.useState(false);

    const load = React.useCallback(function () {
      api("/overview").then(function (d) { setData(d); setError(null); }).catch(function (e) { setError(String(e.message || e)); });
    }, []);

    React.useEffect(function () {
      load();
      const timer = setInterval(load, 10000);
      return function () { clearInterval(timer); };
    }, [load]);

    function act(promise) {
      setBusy(true);
      promise.then(load).catch(function (e) { setError(String(e.message || e)); }).finally(function () { setBusy(false); });
    }

    if (error && !data) return h("div", { className: "orch-page" }, h("p", { className: "orch-error" }, error));
    if (!data) return h("div", { className: "orch-page" }, "Loading…");

    const active = data.tasks.filter(function (t) { return ["DONE", "CANCELLED", "FAILED"].indexOf(t.state) < 0; });
    return h("div", { className: "orch-page" },
      error ? h("p", { className: "orch-error" }, error) : null,
      h("h2", null, "Pending approvals (" + data.approvals.length + ")"),
      data.approvals.length === 0 ? h("p", null, "Nothing waits for a decision.") :
        h("table", { className: "orch-table" }, h("tbody", null, data.approvals.map(function (a) {
          return h("tr", { key: a.id },
            h("td", null, h(Badge, { value: a.action })),
            h("td", null, a.summary),
            h("td", null,
              h("button", { disabled: busy, onClick: function () { act(post("/approvals/" + a.id, { decision: "APPROVE" })); } }, "Approve"),
              " ",
              h("button", { disabled: busy, onClick: function () { act(post("/approvals/" + a.id, { decision: "REJECT" })); } }, "Reject")));
        }))),
      h("h2", null, "Active tasks (" + active.length + ")"),
      h("table", { className: "orch-table" }, h("tbody", null, active.map(function (t) {
        return h("tr", { key: t.key },
          h("td", null, t.key), h("td", null, h(Badge, { value: t.state })), h("td", null, t.project), h("td", null, t.title),
          h("td", null, t.state === "PAUSED" ?
            h("button", { disabled: busy, onClick: function () { act(post("/tasks/" + t.key + "/resume")); } }, "Resume") :
            h("button", { disabled: busy, onClick: function () { act(post("/tasks/" + t.key + "/pause")); } }, "Pause")));
      }))),
      h("p", { className: "orch-muted" }, data.projects.length + " project(s) registered."));
  }

  window.__HERMES_PLUGINS__.register("orchestration", OrchestrationPage);
})();
